"""Shopee Affiliate Open API BR. Apenas consultas oficiais, sem sessão do navegador."""
import datetime as dt
import hashlib
import html
import json
import math
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT='https://open-api.affiliate.shopee.com.br/graphql'
SOURCE='https://affiliate.shopee.com.br/open_api/list?type=product_offer'
FIELDS='''itemId shopId productName priceMin priceMax sales commissionRate commission
 sellerCommissionRate shopeeCommissionRate ratingStar productLink offerLink imageUrl
 productCatIds periodStartTime periodEndTime shopName shopType'''


class ApiError(ValueError):
    pass


def encode_request(query, credentials, timestamp):
    app_id,secret=credentials.get('app_id',''),credentials.get('secret','')
    if not isinstance(app_id,str) or not app_id.isdigit() or not isinstance(secret,str) or not secret:
        raise ApiError('Credencial Shopee ausente ou inválida.')
    payload=json.dumps({'query':query},ensure_ascii=False,separators=(',',':')).encode('utf8')
    ts=str(timestamp)
    signature=hashlib.sha256(app_id.encode()+ts.encode()+payload+secret.encode()).hexdigest()
    return payload,{'Content-Type':'application/json',
        'Authorization':f'SHA256 Credential={app_id}, Timestamp={ts}, Signature={signature}'}


class Client:
    def __init__(self,credential_env='ACHADINHOS_SHOPEE_CREDENTIALS'):
        try: self.credentials=json.loads(os.environ.get(credential_env,''))
        except (ValueError,TypeError): raise ApiError('Carregue a credencial Shopee protegida pelo iniciar.ps1.') from None
        if not isinstance(self.credentials,dict): raise ApiError('Credencial Shopee inválida.')
        self.calls=0

    def query(self,query):
        # Não aceitar mutations através do coletor.
        if not query.lstrip().startswith(('{','query')) or 'mutation' in query.lower():
            raise ApiError('Coletor permite somente consultas.')
        if self.calls>=100: raise ApiError('Orçamento de cem chamadas por coleta excedido.')
        self.calls+=1
        payload,headers=encode_request(query,self.credentials,int(time.time()))
        request=urllib.request.Request(ENDPOINT,data=payload,headers=headers,method='POST')
        try:
            with urllib.request.urlopen(request,timeout=45) as response:
                raw=response.read(8*1024*1024+1)
            if len(raw)>8*1024*1024: raise ApiError('Resposta Shopee excedeu o limite de segurança.')
            result=json.loads(raw)
        except urllib.error.HTTPError as ex:
            raise ApiError(f'Shopee HTTP {ex.code}; coleta anterior preservada, sem repetição imediata.') from None
        except (OSError,ValueError):
            raise ApiError('Falha na consulta Shopee; resposta omitida para proteger a credencial.') from None
        if result.get('errors'):
            # Mensagens do servidor podem conter consultas; nunca registrar o texto bruto.
            codes=sorted({str(e.get('extensions',{}).get('code','indisponível')) for e in result['errors']})
            raise ApiError('Shopee recusou a consulta; códigos: '+', '.join(codes))
        if not isinstance(result.get('data'),dict): raise ApiError('Resposta Shopee sem dados.')
        return result['data']

    def offers(self,keyword=None,sort=2,page=1,limit=20,shop_id=None,item_id=None):
        if not 1<=limit<=50 or not 1<=page<=5 or sort not in (1,2,3,4,5):
            raise ApiError('Consulta fora dos limites de paginação configurados.')
        args=[f'page:{page}',f'limit:{limit}',f'sortType:{sort}']
        if keyword: args.append('keyword:'+json.dumps(keyword,ensure_ascii=False))
        for name,value in (('shopId',shop_id),('itemId',item_id)):
            if value is not None:
                if not str(value).isdigit(): raise ApiError('ID de anúncio inválido.')
                args.append(name+':'+str(value))
        query='{ productOfferV2('+','.join(args)+') { nodes { '+FIELDS+' } pageInfo { page limit hasNextPage } } }'
        data=self.query(query)['productOfferV2']
        if not isinstance(data,dict) or not isinstance(data.get('nodes'),list):
            raise ApiError('Catálogo Shopee retornou estrutura inesperada.')
        return data

    def feeds(self):
        return self.query('''{listItemFeeds(feedMode:FULL){feeds{
         datafeedId datafeedName referenceId description totalCount date feedMode}}}''')['listItemFeeds']['feeds']


def numeric(value,minimum=0,maximum=1e15):
    if isinstance(value,bool): raise ApiError('Número inválido no catálogo.')
    try: number=float(value)
    except (ValueError,TypeError): raise ApiError('Número ausente ou inválido no catálogo.') from None
    if not math.isfinite(number) or not minimum<=number<=maximum:
        raise ApiError('Número fora do intervalo no catálogo.')
    return number


def normalize(row,observed_at):
    shop,item=str(row['shopId']),str(row['itemId'])
    if not shop.isdigit() or not item.isdigit(): raise ApiError('Identidade de produto inválida.')
    url=f'https://shopee.com.br/product/{shop}/{item}'
    original=urllib.parse.urlsplit(row.get('productLink',''))
    if original.scheme!='https' or original.hostname!='shopee.com.br' or original.path!=f'/product/{shop}/{item}':
        raise ApiError('URL do produto não corresponde aos IDs retornados.')
    link=urllib.parse.urlsplit(row.get('offerLink',''))
    if link.scheme!='https' or link.hostname not in ('s.shopee.com.br','shopee.com.br','shope.ee'):
        raise ApiError('Link de afiliado retornado fora dos domínios oficiais.')
    price_min=numeric(row['priceMin']); price_max=numeric(row['priceMax'])
    if price_max<price_min: raise ApiError('Faixa de preços inconsistente.')
    rate=numeric(row['commissionRate'],0,1)
    sales=numeric(row['sales'],0,2147483647)
    if not sales.is_integer(): raise ApiError('Contador de vendas inválido.')
    at=dt.datetime.fromisoformat(observed_at).timestamp()
    start,end=numeric(row['periodStartTime']),numeric(row['periodEndTime'])
    eligible=start<=at<=end and rate>0
    return dict(key=shop+'/'+item,shop_id=shop,item_id=item,name=row['productName'],
        product_url=url,affiliate_link=row['offerLink'],image_url=row.get('imageUrl'),
        category_ids=row.get('productCatIds',[]),shop_name=row.get('shopName',''),shop_type=row.get('shopType',[]),
        price_min=price_min,price_max=price_max,currency='BRL',sales_counter=int(sales),
        commission_percent_max=round(rate*100,8),commission_amount_reported=numeric(row['commission']),
        rating=numeric(row['ratingStar'],0,5),reviews=None,stock=None,variant=None,
        affiliate_offer_active=eligible,period_start=start,period_end=end,observed_at=observed_at,
        source_url=SOURCE,
        warning='Estoque, variante e quantidade de avaliações não informados. Comissão máxima da oferta; ganho não garantido.')


def signals(history,catalog,moment):
    """Variação do contador informado, nunca chamada de vendas semanais comprovadas."""
    output=[]
    for p in catalog:
        samples=[]
        for date,items in sorted(history.get('days',{}).items()):
            if p['key'] in items: samples.append(items[p['key']])
        samples=samples[-3:]
        if len(samples)!=3: continue
        times=[dt.datetime.fromisoformat(s['observed_at']) for s in samples]
        intervals=[(times[i+1]-times[i]).total_seconds()/86400 for i in (0,1)]
        if any(not .75<=n<=3 for n in intervals): continue
        counts=[s['counter'] for s in samples]
        if any(counts[i+1]<counts[i] for i in (0,1)): continue  # reset/revisão do provedor
        output.append(dict(kind='counter_signal',key=p['key'],observed_at=samples[-1]['observed_at'],
            source='Shopee Affiliate Open API — variação do contador, período do provedor não especificado',
            source_url=p['product_url'],samples=samples,
            previous_velocity=(counts[1]-counts[0])/intervals[0],
            current_velocity=(counts[2]-counts[1])/intervals[1]))
    return output


def collect(root,cfg,known,moment,read,write):
    cache=root/'catalogo-shopee.json'
    if cache.exists():
        age=(moment-dt.datetime.fromisoformat(read(cache)['collected_at'])).total_seconds()/3600
        if 0<=age<cfg.get('interval_hours',6):
            print('Shopee: catálogo preservado; próxima consulta após o intervalo mínimo.'); return read(cache)
    client=Client(cfg.get('credential_env','ACHADINHOS_SHOPEE_CREDENTIALS'))
    observed=moment.isoformat(); all_offers={}; failures=[]
    queries=cfg.get('searches',[])
    if not queries or len(queries)>12: raise ApiError('Configure entre uma e doze buscas Shopee.')
    for search in queries:
        if not search.get('keyword') or search.get('category') not in ('Moda','Casa','Utilidades'):
            raise ApiError('Busca Shopee sem palavra-chave e categoria válidas.')
        for sort in (2,5):
            page=client.offers(keyword=search['keyword'],sort=sort,limit=cfg.get('page_size',20))
            for raw in page['nodes']:
                try: offer=normalize(raw,observed)
                except (ApiError,KeyError):
                    failures.append({'reason':'Registro inválido ou incompleto omitido.'}); continue
                old=all_offers.get(offer['key'])
                offer['search_matches']=list(old.get('search_matches',[])) if old else []
                match={'keyword':search['keyword'],'group':search['category'],'sort':sort}
                if match not in offer['search_matches']: offer['search_matches'].append(match)
                # Categoria sugerida pela busca não confirma a natureza do produto.
                offer['category']=''; offer['category_status']='needs_review'
                all_offers[offer['key']]=offer
    missing=[]
    # Atualização exata dos anúncios existentes: não depende de continuar no top da busca.
    for p in known:
        if not str(p.get('shop_id','')).isdigit() or not str(p.get('item_id','')).isdigit(): continue
        page=client.offers(shop_id=p['shop_id'],item_id=p['item_id'],limit=1)
        if not page['nodes']:
            missing.append({'product_id':p['id'],'reason':'Oferta não retornada na consulta exata; não comprova estoque zero.'});continue
        offer=normalize(page['nodes'][0],observed)
        if offer['key']!=str(p['shop_id'])+'/'+str(p['item_id']):
            raise ApiError('Shopee retornou outro produto na consulta exata.')
        offer.update(category=p.get('category',''),category_status='from_existing_catalog',product_id=p['id'])
        all_offers[offer['key']]=offer
    feeds=client.feeds() if cfg.get('check_feeds',True) else []
    catalog=list(all_offers.values())
    if not catalog: raise ApiError('Nenhuma oferta válida retornada; coleta anterior preservada.')
    history_file=root/'historico-shopee.json'
    history=read(history_file) if history_file.exists() else {'days':{}}
    history['days']={day:rows for day,rows in history['days'].items()
        if 0<=(moment.date()-dt.date.fromisoformat(day)).days<=28}
    today=history['days'].setdefault(str(moment.date()),{})
    for p in catalog: today[p['key']]={'observed_at':observed,'counter':p['sales_counter']}
    derived=signals(history,catalog,moment)
    result={'collected_at':observed,'source':SOURCE,'read_only':True,'calls':client.calls,
        'products':catalog,'missing_known_offers':missing,'omitted':failures,'feeds':feeds,
        'capabilities':{'products':True,'affiliate_links':True,'commissions':True,
            'sales_counter':True,'variant_stock':False,'trends':'requires_successive_observations'},
        'trend_signals':derived}
    write(history_file,history)
    # O catálogo é o marcador final de conclusão. Falha de rede antes deste ponto preserva todos os arquivos.
    write(cache,result)
    print(f'Shopee: {len(catalog)} ofertas consultadas, {len(feeds)} feeds disponíveis, {client.calls} chamadas oficiais.')
    return result


def render_candidates(root,result,known,write,editorial=None):
    existing={str(p.get('shop_id'))+'/'+str(p.get('item_id')) for p in known}
    signals_by_key={s['key']:s for s in result.get('trend_signals',[])}
    candidates=[]
    for offer in result['products']:
        if offer['key'] in existing or not offer['affiliate_offer_active']: continue
        # Priorização inicial, nunca chamada de tendência sem série temporal.
        priority=30*offer['rating']/5+30*min(1,math.log1p(offer['sales_counter'])/math.log1p(10000))
        priority+=10*min(1,offer['commission_amount_reported']/20)
        signal=signals_by_key.get(offer['key'])
        if signal:
            growth=math.log2((signal['current_velocity']+10)/(signal['previous_velocity']+10))
            priority+=5*max(-1,min(1,growth))
        candidates.append(dict(offer,priority=round(priority,3),trend_signal=signal,
            editorial=(editorial or {}).get(offer['key']),
            status='Aguardando revisão de categoria, variante e estoque; sem vídeo aprovado.'))
    candidates.sort(key=lambda p:(-p['priority'],p['key']))
    write(root/'candidatos-shopee.json',{'collected_at':result['collected_at'],
        'note':'Descoberta automática; não faz parte da fila de publicação. Contador sem período especificado; comissão máxima não garantida.',
        'products':candidates})
    esc=lambda x:html.escape(str(x),quote=True)
    cards=[]
    for p in candidates:
        topics=', '.join(sorted({r['keyword'] for r in p.get('search_matches',[])}))
        script=p.get('editorial')
        editorial_note=('<p>Roteiro de referência: <b>'+esc(script['selected_script_id'])+
            '</b> · '+esc(script['status'])+'</p>') if script else ''
        cards.append('<article><h2>'+esc(p['name'])+'</h2><p><b>R$ '+esc(f"{p['price_min']:.2f}")+
            ' a R$ '+esc(f"{p['price_max']:.2f}")+'</b> · Comissão máxima: '+esc(p['commission_percent_max'])+
            '%</p><p>Avaliação: '+esc(p['rating'])+' · Contador informado: '+esc(p['sales_counter'])+
            ' · Prioridade inicial: '+esc(p['priority'])+'</p><p>Encontrado nas buscas: '+esc(topics)+
            '</p><p><strong>Estoque e variante: não informados pela API.</strong><br>'+esc(p['status'])+
            '</p>'+editorial_note+'<p><a href="'+esc(p['product_url'])+'" rel="noreferrer">Ver anúncio</a> · '+
            '<a href="'+esc(p['affiliate_link'])+'" rel="noreferrer">Link de afiliada</a></p></article>')
    page='''<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
    <title>Catálogo automático — Achadinhos da Lu</title><style>body{font:16px/1.6 system-ui;background:#faf6fa;
    color:#29212b;max-width:1050px;margin:30px auto;padding:20px}article,header{background:white;border:1px solid #e1d5e2;
    border-radius:14px;padding:24px;margin:20px 0}h2{font-size:20px;line-height:1.3}a{color:#793565}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:18px}article{margin:0}</style>
    <h1>Achadinhos da Lu · catálogo automático</h1><header><p><b>'''+esc(len(candidates))+''' candidatos novos</b> ·
    Coleta: '''+esc(result['collected_at'])+'''</p><p>Dados consultados diretamente na API oficial.
    O contador de vendas não especifica período. A comissão é máxima e pode variar por contexto.
    Ainda não há histórico suficiente para comprovar tendência. Nenhum produto desta página está liberado para publicar.</p>
    <p>Novas medições alimentam o histórico. A API não confirmou estoque por variante.
    Preços e ofertas podem mudar após a coleta.</p></header><main class="grid">'''+''.join(cards)+'</main></html>'
    (root/'catalogo-shopee.html').write_text(page,encoding='utf8')
    print(len(candidates),'candidatos novos separados da fila; nenhum vídeo aprovado ou publicado.')
