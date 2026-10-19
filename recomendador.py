"""Seleção explicável; métricas ausentes nunca viram vendas ou tendência fictícia."""
import datetime as dt
import itertools
import math
import urllib.parse


class InvalidData(ValueError):
    pass


def number(value, low=0, high=float('inf')):
    if isinstance(value, bool) or not isinstance(value,(int,float)):
        raise InvalidData('Use números JSON, sem texto ou booleanos.')
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        raise InvalidData('Métrica fora do intervalo.')
    return result


def stamp(value):
    result = dt.datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise InvalidData('Data precisa incluir fuso horário.')
    return result


def validate(data, moment):
    """Contrato de importação: janelas adjacentes, mesma métrica/fonte e deduplicação."""
    if data.get('example_only'):
        raise InvalidData('Modelo ilustrativo não pode ser usado como métrica real.')
    if data.get('version') != 1 or not isinstance(data.get('observations'), list):
        raise InvalidData('Arquivo de inteligência: version=1 e observations obrigatórios.')
    seen = set()
    for r in data['observations']:
        if r.get('kind') not in ('demand', 'listing', 'performance','offer','counter_signal'):
            raise InvalidData('Tipo de observação inválido.')
        url = urllib.parse.urlsplit(r.get('source_url', ''))
        if url.scheme != 'https' or not url.hostname or not r.get('source'):
            raise InvalidData('Cada observação precisa de fonte e URL HTTPS de evidência.')
        if not 0 <= (moment-stamp(r['observed_at'])).total_seconds() <= 90*86400:
            raise InvalidData('Observação futura ou mais antiga que 90 dias.')
        key = (r['kind'], r.get('product_id'), r.get('post_id'), r.get('source'), r.get('metric'))
        if r['kind']=='performance': key=('performance',r.get('channel'),r.get('post_id'))
        if key in seen:
            raise InvalidData('Observação duplicada: mantenha somente o retrato mais recente.')
        seen.add(key)
        if r['kind']=='offer':
            if not r.get('product_id'): raise InvalidData('Oferta sem identidade.')
            number(r['price_min']); number(r['price_max'],r['price_min'])
            number(r['commission_percent_max'],0,100);number(r['rating'],0,5)
            if type(r.get('affiliate_offer_active')) is not bool:
                raise InvalidData('Atividade da oferta inválida.')
        elif r['kind']=='counter_signal':
            samples=r.get('samples',[])
            if len(samples)!=3: raise InvalidData('Sinal de contador precisa de três medições.')
            times=[stamp(s['observed_at']) for s in samples]
            counts=[number(s['counter']) for s in samples]
            intervals=[(times[i+1]-times[i]).total_seconds()/86400 for i in (0,1)]
            if any(not .75<=d<=3 for d in intervals) or any(counts[i+1]<counts[i] for i in (0,1)):
                raise InvalidData('Intervalo irregular ou reset do contador: não inferir tendência.')
            if stamp(r['observed_at'])!=times[-1]: raise InvalidData('Data do sinal diverge da medição.')
            for i,key in enumerate(('previous_velocity','current_velocity')):
                actual=(counts[i+1]-counts[i])/intervals[i]
                if not math.isclose(number(r[key]),actual,rel_tol=1e-8,abs_tol=1e-8):
                    raise InvalidData('Velocidade não corresponde às medições.')
        elif r['kind'] == 'demand':
            if r.get('metric') not in ('sales', 'searches') or not r.get('product_id'):
                raise InvalidData('Demanda deve informar produto e métrica sales/searches.')
            number(r['current'], 0); number(r['previous'], 0)
            days = number(r['window_days'], 1, 28)
            end = stamp(r['window_end'])
            if not 0 <= (stamp(r['observed_at'])-end).total_seconds() <= 48*3600:
                raise InvalidData('Fim da janela de demanda não corresponde à coleta.')
            # Os números devem representar estas duas janelas contíguas de igual duração.
            if stamp(r['previous_end']) != end-dt.timedelta(days=days):
                raise InvalidData('Janelas de demanda devem ser contíguas e ter a mesma duração.')
        elif r['kind'] == 'listing':
            if not r.get('product_id') or not r.get('variant'):
                raise InvalidData('Oferta precisa identificar produto e variante.')
            for field in ('price', 'stock', 'reviews'):
                number(r[field])
            number(r['rating'], 0, 5); number(r['commission_percent'], 0, 100)
            if type(r.get('affiliate_eligible')) is not bool:
                raise InvalidData('Elegibilidade de afiliados precisa ser explícita.')
        else:
            if r.get('channel') not in ('instagram', 'facebook', 'tiktok', 'shopee'):
                raise InvalidData('Rede desconhecida.')
            if not r.get('post_id') or not r.get('category'):
                raise InvalidData('Métrica precisa de post e categoria confirmada.')
            published = stamp(r['published_at'])
            # Comparar desempenho com a mesma idade: primeiro retrato entre 24 e 48 horas.
            if not 24 <= (stamp(r['observed_at'])-published).total_seconds()/3600 <= 48:
                raise InvalidData('Desempenho deve ser medido entre 24 e 48 horas após publicar.')
            views = number(r['views'])
            if r.get('metric')=='engaged_views':
                number(r['engaged_views'], 0, views)
            elif r.get('metric')=='engagement_rate':
                number(r['engagement_percent'],0,100)
            else:
                raise InvalidData('Métrica de desempenho não suportada.')
    return data


def fresh(r, moment, hours):
    return 0 <= (moment-stamp(r['observed_at'])).total_seconds()/3600 <= hours


def score(product, channel, hour, day, data, moment):
    rows = data.get('observations', [])
    pid, category = product['id'], product.get('category', '').strip().casefold()
    reasons, components, sources = [], {}, []
    offers=[r for r in rows if r['kind']=='offer' and r['product_id']==pid and fresh(r,moment,48)]
    if any(not r['affiliate_offer_active'] for r in offers):
        return dict(product=pid,score=None,blocked='Oferta da API de afiliados não está ativa.')
    listing = [r for r in rows if r['kind'] == 'listing' and r['product_id'] == pid
               and fresh(r, moment, 48) and r.get('variant') == product.get('variant')]
    if listing:
        # Havendo divergência entre fontes atuais, não escolher a oferta mais conveniente.
        if any(r['stock'] <= 0 or not r['affiliate_eligible'] for r in listing):
            return dict(product=pid, score=None, blocked='Sem estoque ou sem comissão de afiliados confirmada.')
        r = max(listing, key=lambda x:x['observed_at'])
        confidence = min(1, r['reviews']/50)
        components['quality'] = ((r['rating']/5)*confidence + .5*(1-confidence))*20
        components['commission'] = min(r['price']*r['commission_percent']/100, 20)/20*10
        sources.append(r['source_url'])
    else:
        components.update(quality=10, commission=0)
        reasons.append('Oferta/estoque/comissão sem retrato atual: conferir antes de publicar.')
        if offers:
            offer=max(offers,key=lambda r:r['observed_at'])
            components['commission']=min(offer['price_min']*offer['commission_percent_max']/100,20)/20*10
            sources.append(offer['source_url'])
            reasons.append('Comissão máxima da API usada para comparação; estoque e variante ainda desconhecidos.')
    trends = [r for r in rows if r['kind'] == 'demand' and r['product_id'] == pid
              and fresh(r,moment,48)]
    components['trend'] = 15
    if trends:
        values=[]
        for r in trends:
            growth=math.log2((r['current']+10)/(r['previous']+10))
            confidence=min(1,(r['current']+r['previous'])/100)
            values.append(15+15*max(-1,min(1,growth))*confidence)
            sources.append(r['source_url'])
        components['trend']=sum(values)/len(values)
        reasons.append('Crescimento comparado em janelas iguais; suavizado para baixo volume.')
    else:
        reasons.append('Sem série recente de demanda; tendência desconhecida.')
        counter=[r for r in rows if r['kind']=='counter_signal' and r.get('product_id')==pid and fresh(r,moment,48)]
        if counter:
            signal=max(counter,key=lambda r:r['observed_at'])
            growth=math.log2((signal['current_velocity']+10)/(signal['previous_velocity']+10))
            confidence=min(1,(signal['current_velocity']+signal['previous_velocity'])/100)
            components['trend']=15+7.5*max(-1,min(1,growth))*confidence
            sources.append(signal['source_url'])
            reasons.append('Sinal auxiliar de variação do contador da API; não comprova vendas por período.')
    # Separar rede e fuso. Não transferir o horário vencedor de uma rede para outra.
    perf=[r for r in rows if r['kind']=='performance' and r['channel']==channel
          and fresh(r,moment,28*24)]
    # Nunca misturar espectadores únicos com uma taxa de interações da plataforma.
    metric='engagement_rate' if any(r['metric']=='engagement_rate' for r in perf) else 'engaged_views'
    perf=[r for r in perf if r['metric']==metric]
    def engagement(r):
        return r['views']*r['engagement_percent']/100 if metric=='engagement_rate' else r['engaged_views']
    views=sum(r['views'] for r in perf)
    engaged=sum(engagement(r) for r in perf)
    prior=(engaged+5)/(views+100)  # prior fraco de 5%, explícito, não uma medição.
    local=[]
    for r in perf:
        published=stamp(r['published_at']).astimezone(dt.timezone(dt.timedelta(hours=-3)))
        if (r['category'].strip().casefold()==category and category
                and published.hour==int(hour[:2]) and published.weekday()==day.weekday()):
            local.append(r)
    n=sum(r['views'] for r in local)
    e=sum(engagement(r) for r in local)
    posterior=(e+500*prior)/(n+500)
    lift=max(-1,min(1,math.log2(max(posterior,1e-8)/prior)))
    dates={stamp(r['published_at']).astimezone(dt.timezone(dt.timedelta(hours=-3))).date() for r in local}
    confidence=min(1,n/1000)*min(1,len(local)/3)*min(1,len(dates)/2)
    components['hour_fit']=20+20*lift*confidence
    reliable=len(local)>=3 and n>=1000 and len(dates)>=2
    reasons.append('Afinidade medida de categoria/rede/dia/hora.' if reliable else
                   'Afinidade exploratória: exige 3 posts, 1.000 visualizações e duas datas comparáveis.')
    return dict(product=pid,score=round(sum(components.values()),4),
        components=components, hour=hour,channel=channel,category=category,
        confidence='medida' if reliable else 'exploratoria',
        sample_posts=len(local),sample_views=n,sample_dates=len(dates),performance_metric=metric,
        reasons=reasons,sources=sorted(set(sources)),evidence={'listing':listing,'offer':offers,'demand':trends,
            'counter_signals':[r for r in rows if r['kind']=='counter_signal' and r.get('product_id')==pid and fresh(r,moment,48)],
            'performance':local})


def select(catalog, channels, hours_by_channel, day, data, moment, limit=3):
    """Seleção gulosa explicável, com penalidade de repetição de categoria no dia."""
    ranked=[]
    for p in catalog:
        results=[score(p,c,h,day,data,moment) for c in channels for h in hours_by_channel[c]]
        available=[x['score'] for x in results if x['score'] is not None]
        if available:
            ranked.append((p, sum(available)/len(available),results))
    chosen=[]; categories={}
    while ranked and len(chosen)<limit:
        best=max(range(len(ranked)),key=lambda i:ranked[i][1]-8*categories.get(
            ranked[i][0].get('category','').casefold(),0))
        p,base,results=ranked.pop(best)
        category=p.get('category','').casefold()
        chosen.append(dict(product=p['id'],score=base-8*categories.get(category,0),details=results))
        categories[category]=categories.get(category,0)+1
    return chosen


def assign(catalog, channel, hours, day, data, moment):
    """Até 3! combinações; mesmos produtos, ordem própria por rede."""
    if not catalog:
        return []
    best=None
    for arrangement in itertools.permutations(range(len(hours)),len(catalog)):
        decisions=[score(p,channel,hours[index],day,data,moment)
                   for p,index in zip(catalog,arrangement)]
        if any(d['score'] is None for d in decisions):
            continue
        value=sum(d['score'] for d in decisions)
        if best is None or value>best[0]:
            best=(value,decisions)
    return best[1] if best else []
