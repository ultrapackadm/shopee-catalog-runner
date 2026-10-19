"""Fila local: 3 produtos/dia, revisão vinculada por hash e conectores oficiais.
Python 3.12+. Nenhum segredo é salvo neste arquivo. Veja LEIA-ME.md.
"""
from __future__ import annotations
import argparse
import contextlib
import csv
import datetime as dt
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import recomendador
import shopee_affiliate
import gerador_roteiros
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent
CHANNELS = ('instagram', 'facebook', 'tiktok', 'shopee')
CHECKS = {
    'anuncio_variante': 'Conferiu o anúncio e a variante exata (não apenas o título)?',
    'quantidade': 'Quantidade e itens inclusos correspondem à variante?',
    'aparencia': 'Cor, formato, estampa, tampas e detalhes correspondem à foto?',
    'video_inteiro': 'Assistiu ao vídeo INTEIRO, sem peças inventadas ou imagem estranha?',
    'fala': 'Ouviu a fala inteira em português e conferiu cada afirmação no anúncio?',
    'legenda': 'Legenda não promete material, medida, benefício ou preço sem evidência?',
    'direitos': 'Tem autorização para usar as imagens e o vídeo?',
    'divulgacoes': 'Publicidade, link de afiliada e uso de IA estão identificados?',
}

class Blocked(Exception):
    pass

def now():
    try:
        return dt.datetime.now(ZoneInfo('America/Sao_Paulo'))
    except ZoneInfoNotFoundError:
        return dt.datetime.now(dt.timezone(dt.timedelta(hours=-3)))

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def write(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding='utf8')
    tmp.replace(path)

def sha(path):
    with open(path, 'rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def path_for(value):
    return (ROOT / value).resolve()

def config():
    cfg = read(ROOT / 'config.json')
    times = cfg.get('times', [])
    if len(times) != 3 or len(set(times)) != 3 or times != sorted(times) or any(
            not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', x) for x in times):
        raise Blocked('Configure três horários distintos em ordem crescente.')
    if cfg.get('daily_products') != 3 or not 1 <= cfg.get('approval_max_hours', 0) <= 48:
        raise Blocked('Limite fixo: 3 produtos/dia; aprovação válida por até 48 horas.')
    return cfg

def products():
    items = read(ROOT / 'fila.json')
    ids = [p['id'] for p in items]
    if len(ids) != len(set(ids)) or any(not re.fullmatch(r'[A-Za-z0-9_-]+', x) for x in ids):
        raise Blocked('IDs devem ser únicos e conter apenas letras, números, _ ou -.')
    return items

def peak_hours(channel, day):
    """Usar picos medidos por rede/dia; sem dados suficientes, assumir provisório."""
    cfg = config()
    record = cfg.get('peak_schedule', {}).get(channel, {}).get(str(day.weekday()))
    if record:
        age = (now() - dt.datetime.fromisoformat(record['verified_at'])).total_seconds()/86400
        hours = record.get('hours', [])
        measurement_age = (now().date() - dt.date.fromisoformat(record['latest_measurement'])).days
        if (0 <= age <= 14 and 0 <= measurement_age <= 14 and record.get('source') and len(hours) == len(set(hours)) == 3
                and hours == sorted(hours) and all(re.fullmatch(r'(?:[01]\d|2[0-3]):00', h) for h in hours)):
            return hours, 'medido'
    return cfg['times'], 'provisorio'

def due_for(channel, slot):
    with connection() as db:
        assignment = db.execute('SELECT due,basis FROM recommendations WHERE day=? AND product=? AND channel=?',
            (slot['day'],slot['product'],channel)).fetchone()
    if assignment:
        due=dt.datetime.fromisoformat(assignment['due'])
        # Não manter alegação de pico medido quando a evidência de audiência expirou.
        current_hours, current_basis=peak_hours(channel,dt.date.fromisoformat(slot['day']))
        measured=assignment['basis']=='medido' and current_basis=='medido' and due.strftime('%H:%M') in current_hours
        return due, 'medido' if measured else 'provisorio'
    hours, basis = peak_hours(channel, dt.date.fromisoformat(slot['day']))
    return dt.datetime.fromisoformat(slot['day']+'T'+hours[slot['slot']]+':00-03:00'), basis

def import_peaks(file, source):
    """CSV: rede,data,hora,usuarios_ativos; datas e horas em America/Sao_Paulo."""
    groups = {}
    seen = set()
    with open(file, encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            ch, day, hour = row['rede'], dt.date.fromisoformat(row['data']), int(row['hora'])
            score = float(row['usuarios_ativos'])
            if ch not in CHANNELS or not 0 <= hour <= 23 or not 0 <= score < float('inf'):
                raise Blocked('Métrica de audiência inválida.')
            if not 0 <= (now().date()-day).days <= 28:
                raise Blocked('Use somente dados medidos nos últimos 28 dias.')
            key = (ch,day,hour)
            if key in seen:
                raise Blocked('CSV tem linha de audiência duplicada.')
            seen.add(key)
            bucket = groups.setdefault((ch,day.weekday()), {})
            bucket.setdefault(hour, []).append((day,score))
    cfg = config(); accepted = 0
    for (ch,weekday), hours in groups.items():
        # Cobertura completa para não chamar três horários arbitrários de top 3.
        if len(hours) != 24 or any(len({d for d,v in rows}) < 2 for rows in hours.values()):
            print(ch,weekday,'insuficiente: precisa de 24 horas em pelo menos 2 datas do mesmo dia da semana.'); continue
        common_dates=set.intersection(*({d for d,v in rows} for rows in hours.values()))
        if len(common_dates)<2:
            print(ch,weekday,'insuficiente: as 24 horas precisam cobrir as mesmas duas datas.'); continue
        means = {h:sum(v for d,v in rows if d in common_dates)/len(common_dates) for h,rows in hours.items()}
        ranked = sorted(means, key=lambda h:(-means[h],h))[:3]
        if any(means[h] <= 0 for h in ranked):
            print(ch,weekday,'insuficiente: menos de três horários com audiência positiva.'); continue
        cfg.setdefault('peak_schedule',{}).setdefault(ch,{})[str(weekday)] = {
            'hours':sorted(f'{h:02}:00' for h in ranked),'verified_at':now().isoformat(),
            'source':source,'metric':'media de usuarios_ativos por hora',
            'latest_measurement':max(d.isoformat() for d in common_dates),
            'ranking':{f'{h:02}:00':means[h] for h in ranked}}
        accepted += 1
    if not accepted:
        raise Blocked('Nenhum grupo de audiência teve dados suficientes; horários preservados.')
    write(ROOT/'config.json',cfg)
    print(accepted,'combinações de rede/dia com três picos medidos importadas.')

def product(pid):
    return next(p for p in products() if p['id'] == pid)

@contextlib.contextmanager
def connection():
    db = sqlite3.connect(ROOT / 'estado.sqlite3', timeout=2)
    db.row_factory = sqlite3.Row
    db.executescript('''
    CREATE TABLE IF NOT EXISTS slots (
      day TEXT, slot INTEGER, product TEXT UNIQUE, fingerprint TEXT UNIQUE,
      video_hash TEXT UNIQUE, listing_key TEXT UNIQUE,
      due TEXT, PRIMARY KEY(day, slot));
    CREATE TABLE IF NOT EXISTS posts (
      product TEXT, channel TEXT, status TEXT, remote_id TEXT, url TEXT,
      updated TEXT, PRIMARY KEY(product, channel));
    CREATE TABLE IF NOT EXISTS recommendations (
      day TEXT, product TEXT, channel TEXT, due TEXT, basis TEXT, evidence TEXT,
      PRIMARY KEY(day,product,channel), UNIQUE(day,channel,due));
    ''')
    try:
        with db:
            yield db
    finally:
        db.close()

@contextlib.contextmanager
def exclusive():
    # Lock de processo liberado pelo sistema operacional mesmo após interrupção.
    with open(ROOT / '.execucao.lock', 'a+b') as handle:
        handle.seek(0)
        if handle.read(1) == b'':
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Blocked('Outra execução está trabalhando na fila.') from None
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

def fingerprint(p):
    payload = {k: p.get(k) for k in (
        'id', 'product_url', 'shop_id', 'item_id', 'variant', 'quantity',
        'visible_details', 'speech', 'caption', 'affiliate_link', 'media_url', 'ai_generated')}
    for key in ('video', 'reference', 'listing_evidence'):
        target = path_for(p.get(key, '__ausente__'))
        if not target.is_file():
            raise Blocked(f'{p["id"]}: arquivo ausente: {key}')
        payload[key] = sha(target)
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

def validate_identity(p):
    if p.get('blocked_reason'):
        raise Blocked(p['blocked_reason'])
    if p.get('already_published'):
        raise Blocked('Produto já publicado: não reutilizar automaticamente.')
    required = ('shop_id', 'item_id', 'variant', 'quantity', 'visible_details',
                'speech', 'caption', 'affiliate_link')
    if any(not p.get(k) for k in required):
        raise Blocked('Faltam variante, quantidade, detalhes, fala ou legenda confirmados.')
    expected = f'https://shopee.com.br/product/{p["shop_id"]}/{p["item_id"]}'
    if p.get('product_url') != expected:
        raise Blocked('IDs do anúncio não correspondem à URL do produto.')
    if not isinstance(p['quantity'], int) or isinstance(p['quantity'], bool) or p['quantity'] < 1:
        raise Blocked('Quantidade precisa ser um inteiro positivo.')
    if 'publicidade' not in p['caption'].lower():
        raise Blocked('Legenda precisa identificar publicidade.')
    if p.get('ai_generated') and not re.search(r'\bIA\b|inteligência artificial', p['caption'], re.I):
        raise Blocked('Legenda precisa identificar uso de IA.')
    return fingerprint(p)

def ffmpeg():
    configured = config().get('ffmpeg')
    if configured and path_for(configured).is_file():
        return str(path_for(configured))
    exe = shutil.which('ffmpeg')
    if exe:
        return exe
    deps = ROOT.parent.parent / 'work' / 'video_dependencies'
    if deps.is_dir():
        sys.path.insert(0, str(deps))
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        raise Blocked('Configure FFmpeg em config.json para verificar os vídeos.') from None

def audit(p):
    fp = validate_identity(p)
    folder = ROOT / 'revisoes' / p['id'] / fp[:16]
    folder.mkdir(parents=True, exist_ok=True)
    video = str(path_for(p['video']))
    result = subprocess.run([ffmpeg(), '-v', 'error', '-xerror', '-i', video,
                             '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'],
                            capture_output=True, timeout=180)
    if result.returncode:
        raise Blocked('Falha ao decodificar vídeo/áudio completo. Corrija antes de revisar.')
    info = subprocess.run([ffmpeg(), '-hide_banner', '-i', video], capture_output=True)
    log = info.stderr.decode('utf8', errors='replace')
    dimensions = re.search(r'Video:.*? (\d{3,5})x(\d{3,5})', log)
    duration = re.search(r'Duration: (\d+):(\d+):([\d.]+)', log)
    if not dimensions or not duration:
        raise Blocked('Não foi possível identificar dimensão/duração.')
    w, h = map(int, dimensions.groups())
    seconds = sum(float(v) * factor for v, factor in zip(duration.groups(), (3600, 60, 1)))
    if w < 540 or abs(w / h - 9 / 16) > .02 or not 3 <= seconds <= 60:
        raise Blocked('Vídeo precisa ser vertical 9:16, largura >=540 e duração 3–60 s.')
    # Painel por segundo é auxílio visual; não substitui assistir ao vídeo completo.
    result = subprocess.run([ffmpeg(), '-y', '-v', 'error', '-i', video,
        '-vf', 'fps=1,scale=180:-1,tile=8x8', '-frames:v', '1', str(folder/'quadros.jpg')],
        capture_output=True, timeout=180)
    if result.returncode:
        raise Blocked('Falha na geração do painel de conferência.')
    report = {'fingerprint': fp, 'at': now().isoformat(), 'decode_complete': True,
              'width': w, 'height': h, 'seconds': seconds}
    write(folder/'auditoria.json', report)
    esc = lambda x: html.escape(str(x), quote=True)
    checklist = ''.join(f'<li>{esc(q)}</li>' for q in CHECKS.values())
    (folder/'revisao.html').write_text(f'''<!doctype html><html lang="pt-BR"><meta charset="utf-8">
    <title>Revisão {esc(p['id'])}</title><style>body{{font:18px system-ui;max-width:1100px;margin:30px auto;padding:20px}}
    img,video{{max-width:46%;max-height:650px;vertical-align:top}}li{{margin:14px 0}}pre{{white-space:pre-wrap}}</style>
    <h1>{esc(p['name'])}</h1><p>Bloqueado até conferência humana completa.</p>
    <p><a href="{esc(p['product_url'])}">Abrir anúncio exato</a> · Variante: {esc(p['variant'])} · Quantidade: {p['quantity']}</p>
    <img src="{esc(path_for(p['reference']).as_uri())}" alt="Foto de referência">
    <video controls src="{esc(path_for(p['video']).as_uri())}"></video>
    <p><a href="{esc(path_for(p['listing_evidence']).as_uri())}">Evidência do anúncio e variante</a></p>
    <h2>Detalhes confirmados</h2><pre>{esc(p['visible_details'])}</pre>
    <h2>Fala e legenda</h2><pre>{esc(p['speech'])}\n\n{esc(p['caption'])}</pre>
    <h2>Conferir todos os itens</h2><ol>{checklist}</ol>
    <h2>Amostras por segundo</h2><img style="max-width:100%" src="quadros.jpg">
    <p>O painel não detecta automaticamente divergências. Assista e ouça o arquivo inteiro.</p>
    <p>Após conferir: <code>python publicador.py aprovar {esc(p['id'])}</code></p>
    <small>Identificador da revisão: {fp}</small></html>''', encoding='utf8')
    return folder

def approve(p):
    folder = audit(p)
    reviewed_fingerprint = read(folder/'auditoria.json')['fingerprint']
    print('Abra e confira antes de responder:', folder/'revisao.html')
    reviewer = input('Nome de quem conferiu: ').strip()
    if not reviewer:
        raise Blocked('Informe quem realizou a revisão.')
    answers = {}
    for key, question in CHECKS.items():
        answers[key] = input(question + ' [sim/não] ').strip().lower() == 'sim'
    if not all(answers.values()):
        raise Blocked('Revisão reprovada/incompleta; publicação bloqueada.')
    if fingerprint(p) != reviewed_fingerprint:
        raise Blocked('Arquivos mudaram durante a conferência; audite novamente.')
    write(ROOT/'aprovacoes'/f'{p["id"]}.json', {
        'fingerprint': fingerprint(p), 'reviewer': reviewer, 'checks': answers,
        'listing_checked_at': now().isoformat(), 'approved_at': now().isoformat()})

def eligible(p, moment):
    fp = validate_identity(p)
    # Uma oferta ativa/contador de vendas não confirma o estoque da variante do vídeo.
    current=load_intelligence()['observations']
    if any(r['kind']=='offer' and r['product_id']==p['id'] and not r['affiliate_offer_active']
            and recomendador.fresh(r,moment,48) for r in current):
        raise Blocked('API de afiliados informou oferta inativa/sem comissão.')
    if config().get('require_current_offer'):
        rows=[r for r in current if r['kind']=='listing'
              and r['product_id']==p['id'] and r.get('variant')==p['variant']
              and recomendador.fresh(r,moment,48)]
        if not rows or any(r['stock']<=0 or not r['affiliate_eligible'] for r in rows):
            raise Blocked('Oferta atual da variante/estoque/elegibilidade não confirmada nas últimas 48h.')
    ap = ROOT/'aprovacoes'/f'{p["id"]}.json'
    if not ap.exists():
        raise Blocked('Aguardando revisão humana vinculada ao arquivo e anúncio.')
    approval = read(ap)
    if approval.get('fingerprint') != fp:
        raise Blocked('Arquivo, anúncio, variante, fala ou legenda mudou após aprovação.')
    if not all(approval.get('checks', {}).get(k) is True for k in CHECKS):
        raise Blocked('Revisão incompleta.')
    stamp = dt.datetime.fromisoformat(approval['listing_checked_at'])
    hours = (moment-stamp).total_seconds()/3600
    if not 0 <= hours <= config()['approval_max_hours']:
        raise Blocked('Conferência do anúncio vencida; reabra e confira a variante novamente.')
    return fp

def plan(day):
    cfg = config()
    start = dt.date.fromisoformat(cfg['start_date'])
    if day < start:
        raise Blocked(f'A fila começa em {start}.')
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        slots = list(db.execute('SELECT * FROM slots WHERE day=? ORDER BY slot', (str(day),)))
        used = {r[0] for r in db.execute('SELECT product FROM slots')}
        moment = now()
        catalog = products()
        historical = {sha(path_for(p['video'])) for p in catalog
                      if p.get('already_published') and path_for(p['video']).is_file()}
        historical_items = {(p.get('shop_id'),p.get('item_id')) for p in catalog if p.get('already_published')}
        intelligence=load_intelligence()
        hours={c:peak_hours(c,day)[0] for c in CHANNELS}
        order=recomendador.select(catalog,CHANNELS,hours,day,intelligence,moment,limit=len(catalog))
        by_id={p['id']:p for p in catalog}
        catalog=[by_id[r['product']] for r in order]
        for p in catalog:
            if len(slots) >= 3:
                break
            if p['id'] in used:
                continue
            try:
                fp = eligible(p, moment)
            except (Blocked, ValueError, KeyError) as ex:
                print(f'{p["id"]}: BLOQUEADO — {ex}'); continue
            if db.execute('SELECT 1 FROM slots WHERE fingerprint=?', (fp,)).fetchone():
                continue
            video_hash = sha(path_for(p['video']))
            listing_key = f'{p["shop_id"]}/{p["item_id"]}/{p["variant"]}'
            if video_hash in historical or (p['shop_id'],p['item_id']) in historical_items:
                print(p['id'],'BLOQUEADO — arquivo ou produto consta no histórico publicado.'); continue
            if db.execute('SELECT 1 FROM slots WHERE video_hash=? OR listing_key=?',
                          (video_hash,listing_key)).fetchone():
                print(p['id'],'BLOQUEADO — vídeo ou anúncio/variante já presente na agenda.'); continue
            occupied = {r['slot'] for r in slots}
            slot = next(i for i in range(3) if i not in occupied)
            due = str(day)+'T'+cfg['times'][slot]+':00-03:00'
            db.execute('INSERT INTO slots VALUES(?,?,?,?,?,?,?)',
                       (str(day),slot,p['id'],fp,video_hash,listing_key,due))
            # Não usar a ordem de chegada para extrapolar o limite diário.
            slots = list(db.execute('SELECT * FROM slots WHERE day=? ORDER BY slot', (str(day),)))
        # Congelar a decisão: novas métricas não movem uma publicação já reservada.
        for channel in CHANNELS:
            assigned={r['product'] for r in db.execute(
                'SELECT product FROM recommendations WHERE day=? AND channel=?',(str(day),channel))}
            occupied={dt.datetime.fromisoformat(r[0]).strftime('%H:%M') for r in db.execute(
                'SELECT due FROM recommendations WHERE day=? AND channel=?',(str(day),channel))}
            available=[h for h in hours[channel] if h not in occupied]
            pending=[by_id[r['product']] for r in slots if r['product'] not in assigned]
            decisions=recomendador.assign(pending,channel,available,day,intelligence,moment)
            for decision in decisions:
                due=str(day)+'T'+decision['hour']+':00-03:00'
                db.execute('INSERT INTO recommendations VALUES(?,?,?,?,?,?)',(
                    str(day),decision['product'],channel,due,peak_hours(channel,day)[1],
                    json.dumps(decision,ensure_ascii=False)))
        return [dict(r) for r in slots]


def load_intelligence():
    file=ROOT/'inteligencia.json'
    data=read(file) if file.exists() else {'version':1,'observations':[]}
    learned=ROOT/'aprendizado-buffer.json'
    if learned.exists():
        data['observations'] += [r for r in read(learned)['observations']
            if recomendador.fresh(r,now(),28*24)]
    market=ROOT/'catalogo-shopee.json'
    if market.exists():
        catalog=read(market)
        known={str(p.get('shop_id'))+'/'+str(p.get('item_id')):p for p in products()}
        for offer in catalog['products']:
            if offer['key'] not in known or not recomendador.fresh(offer,now(),48): continue
            p=known[offer['key']]
            data['observations'].append(dict(kind='offer',product_id=p['id'],
                observed_at=offer['observed_at'],source='Shopee Affiliate Open API',source_url=offer['product_url'],
                price_min=offer['price_min'],price_max=offer['price_max'],
                commission_percent_max=offer['commission_percent_max'],rating=offer['rating'],
                affiliate_offer_active=offer['affiliate_offer_active'],stock=None,variant=None))
        for signal in catalog.get('trend_signals',[]):
            if signal['key'] in known and recomendador.fresh(signal,now(),48):
                data['observations'].append(dict(signal,product_id=known[signal['key']]['id']))
    return recomendador.validate(data,now())


def collect_shopee():
    result=shopee_affiliate.collect(ROOT,config().get('shopee_api',{}),products(),now(),read,write)
    prepare_video_scripts(result)
    shopee_affiliate.render_candidates(ROOT,result,products(),write,editorial=editorial_index())
    return result


def script_bank():
    """Banco editorial separado da fila e da aprovação factual de anúncios."""
    cfg=config().get('editorial',{})
    file=path_for(cfg.get('bank_file','roteiros-achadinhos-da-lu.json'))
    if not file.exists(): return []
    rows=read(file)
    if not isinstance(rows,list): raise Blocked('Banco editorial deve conter uma lista.')
    seen=set(); identities=set()
    for r in rows:
        key=str(r.get('shop_id',''))+'/'+str(r.get('item_id',''))
        if not re.fullmatch(r'\d+/\d+',key) or r.get('source_key')!=key or r.get('product_url')!='https://shopee.com.br/product/'+key:
            raise Blocked('Roteiro com identidade de anúncio divergente.')
        identity=(key,r.get('script_variant'))
        if not r.get('id') or r['id'] in seen or identity in identities or identity[1] not in tuple('ABCDEFGH'):
            raise Blocked('ID ou alternativa de roteiro duplicada/inválida.')
        if not r.get('speech') or not r.get('caption') or not r.get('blocked_reason'):
            raise Blocked('Roteiro sem texto ou sem retenção editorial explícita.')
        seen.add(r['id']);identities.add(identity)
    return rows


def editorial_index():
    rows=script_bank()
    cfg=config().get('editorial',{})
    selected_file=path_for(cfg.get('selection_file','roteiros-selecionados.json'))
    selected=read(selected_file) if selected_file.exists() else {}
    automatic={}
    production=path_for(cfg.get('production_file','proximos-videos.json'))
    if cfg.get('selection_mode')=='auto_diversity' and production.exists():
        automatic={r['source_key']:r['source_script_id'] for r in read(production).get('products',[])}
    index={}
    for key in sorted({r['source_key'] for r in rows}):
        options=[r for r in rows if r['source_key']==key]
        ident=selected.get(key)
        if ident and ident not in {r['id'] for r in options}:
            raise Blocked('Seleção editorial aponta para roteiro de outro anúncio ou inexistente.')
        if cfg.get('selection_mode')=='auto_diversity':
            ident=automatic.get(key)
            if ident and ident not in {r['id'] for r in options}:
                raise Blocked('Trabalho editorial aponta para roteiro inexistente ou outro anúncio.')
        default=next((r for r in options if r['script_variant']=='A'),options[0])
        index[key]={'selected_script_id':ident or default['id'],
            'available_alternatives':sorted(r['script_variant'] for r in options),
            'bank_file':cfg.get('bank_file','roteiros-achadinhos-da-lu.json'),
            'status':'rascunho editorial; não aprovado para publicar'}
    return index


def lookup_script(key,alternative=None,select=False):
    if not re.fullmatch(r'\d+/\d+',key): raise Blocked('Use a chave loja/produto, com IDs numéricos.')
    options=[r for r in script_bank() if r['source_key']==key]
    if not options: raise Blocked('Nenhum roteiro disponível para este anúncio.')
    selected_id=editorial_index()[key]['selected_script_id']
    result=next((r for r in options if r['script_variant']==alternative),None) if alternative else next(r for r in options if r['id']==selected_id)
    if result is None: raise Blocked('Alternativa não disponível para este anúncio.')
    if select:
        file=path_for(config().get('editorial',{}).get('selection_file','roteiros-selecionados.json'))
        saved=read(file) if file.exists() else {}
        saved[key]=result['id'];write(file,saved)
    return dict(result,editorial_status='rascunho; conferir atributos, variante, estoque, vídeo e fala antes de aprovar',
        auto_cta_adaptation=False,auto_video_generation=False)




def generate_video_scripts(catalog):
    cfg=config().get('editorial',{})
    if not cfg.get('auto_generate',False): return None
    models=read(path_for(cfg.get('models_file','modelos-roteiros.json')))
    bank,report=gerador_roteiros.generate(catalog,script_bank(),products(),models)
    if report['added_scripts']:
        write(path_for(cfg.get('bank_file','roteiros-achadinhos-da-lu.json')),bank)
    report['generated_at']=now().isoformat()
    write(ROOT/'geracao-roteiros-status.json',report)
    print(f"Geração editorial local: {report['added_scripts']} roteiros novos; {len(report['pending'])} anúncios sem modelo.")
    return report


def prepare_video_scripts(catalog=None):
    """Aplicar roteiros aos próximos trabalhos, sem selecionar ou aprovar manualmente."""
    cfg=config().get('editorial',{})
    if not cfg.get('auto_apply',False): return []
    if catalog is None:
        cache=ROOT/'catalogo-shopee.json'
        if not cache.exists(): return []
        catalog=read(cache)
    generate_video_scripts(catalog)
    known={str(p.get('shop_id'))+'/'+str(p.get('item_id')):p for p in products()}
    index=editorial_index()
    destination=path_for(cfg.get('production_file','proximos-videos.json'))
    previous=read(destination) if destination.exists() else {}
    saved={r['source_key']:r for r in previous.get('products',[])}
    diverse=cfg.get('selection_mode')=='auto_diversity'
    rows=script_bank()
    usage={}
    for r in saved.values():
        if previous.get('selection_mode')=='auto_diversity' or r.get('video'):
            family=r.get('approach',r.get('script_variant'))
            usage[family]=usage.get(family,0)+1
    jobs=[]
    seen=set()
    last_family=None
    for offer in sorted(catalog.get('products',[]),key=lambda r:r.get('key','')):
        key=str(offer.get('shop_id'))+'/'+str(offer.get('item_id'))
        if key in seen or key not in index or not offer.get('affiliate_offer_active'): continue
        seen.add(key)
        current=known.get(key,{})
        # Nunca mudar texto de um vídeo existente ou reaproveitar um anúncio publicado.
        if current.get('already_published') or current.get('video'): continue
        prior=saved.get(key)
        if diverse and prior and (previous.get('selection_mode')=='auto_diversity' or prior.get('video')):
            # Fixar o trabalho já escolhido: coleta nova não troca sua narração/cenas.
            valid=any(r['id']==prior.get('source_script_id') and r['source_key']==key for r in rows)
            if not valid: raise Blocked('Trabalho preparado perdeu o roteiro de origem; conferir antes de continuar.')
            jobs.append(prior)
            last_family=prior.get('approach',prior.get('script_variant'))
            continue
        if diverse:
            options=[r for r in rows if r['source_key']==key]
            def rank(r):
                family=r.get('approach',r['script_variant'])
                tie=hashlib.sha256((key+'/'+r['id']).encode()).hexdigest()
                return (family==last_family,usage.get(family,0),tie)
            script=min(options,key=rank)
            last_family=script.get('approach',script['script_variant'])
            usage[last_family]=usage.get(last_family,0)+1
        else:
            script=lookup_script(key)
        job=dict(script)
        job.update(affiliate_link=offer.get('affiliate_link',script['affiliate_link']),
            name=offer.get('name',script['name']),
            editorial_applied_automatically=True,
            production_status='roteiro aplicado; aguardando conferência e produção do vídeo',
            catalog_observed_at=offer.get('observed_at'),
            source_script_id=script['id'])
        jobs.append(job)
    write(destination,{'generated_at':now().isoformat(),'auto_apply':True,
        'selection_mode':cfg.get('selection_mode','default'),
        'auto_video_generation':False,'products':jobs})
    print(f'Roteiros aplicados automaticamente: {len(jobs)} próximos vídeos preparados.')
    return jobs


def import_intelligence(file):
    data=recomendador.validate(read(file),now())
    known={p['id']:p for p in products()}
    if any(r.get('product_id') and r['product_id'] not in known for r in data['observations']):
        raise Blocked('Observação cita produto que não está na fila.')
    for r in data['observations']:
        if r['kind']=='listing' and (r['source_url'] != known[r['product_id']]['product_url']
                or r['variant'] != known[r['product_id']]['variant']):
            raise Blocked('Oferta não corresponde à URL e variante exatas da fila.')
    write(ROOT/'inteligencia.json',data)
    print(len(data['observations']),'observações validadas. Importação substitui o retrato anterior.')


def recommend(day):
    catalog=[]; blocked=[]
    for p in products():
        try: eligible(p,now()); catalog.append(p)
        except (Blocked,ValueError,KeyError) as ex: blocked.append({'product':p['id'],'reason':str(ex)})
    data=load_intelligence()
    hours={c:peak_hours(c,day)[0] for c in CHANNELS}
    ranking=recomendador.select(catalog,CHANNELS,hours,day,data,now())
    selected={r['product'] for r in ranking}
    report={'generated_at':now().isoformat(),'day':str(day),'selection':ranking,'blocked':blocked,
        'schedule':{c:{'basis':peak_hours(c,day)[1], 'assignments':recomendador.assign(
            [p for p in catalog if p['id'] in selected],c,hours[c],day,data,now())} for c in CHANNELS},
        'note':'Prévia; não reserva horários nem publica. Score não é previsão de vendas.'}
    write(ROOT/'recomendacoes.json',report)
    e=lambda value:html.escape(str(value))
    rows=''.join('<tr><td>'+e(r['product'])+'</td><td>'+e(round(r['score'],2))+'</td></tr>' for r in ranking)
    blocks=''.join('<li><b>'+e(r['product'])+'</b>: '+e(r['reason'])+'</li>' for r in blocked)
    boards=''
    for channel,schedule in report['schedule'].items():
        boards+='<h3>'+e(channel)+' — '+e(schedule['basis'])+'</h3>'
        boards+=''.join('<p><b>'+e(r['hour'])+' · '+e(r['product'])+'</b> — '+e(r['confidence'])+
            '<br>'+e(' '.join(r['reasons']))+'</p>' for r in schedule['assignments']) or '<p>Sem produtos elegíveis.</p>'
    metrics=read(ROOT/'metricas-buffer.json') if (ROOT/'metricas-buffer.json').exists() else {}
    page='''<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
    <title>Seleção de produtos — Achadinhos da Lu</title><style>
    body{font:16px/1.6 system-ui;background:#faf6fa;color:#29212b;max-width:1000px;margin:32px auto;padding:20px}
    article{background:white;border:1px solid #e1d5e2;border-radius:16px;padding:24px;margin:20px 0}
    h1,h2,h3{line-height:1.2}small{color:#655a68}li{margin:12px 0}td,th{text-align:left;padding:8px 24px 8px 0}
    .notice{border-left:5px solid #a53c55}a{color:#793565}</style><h1>Achadinhos da Lu</h1>
    <p>Seleção e horários · '''+e(day)+'''</p><article class="notice"><h2>Prévia de decisão</h2>
    <p>Esta página não publica nem libera produtos. Tendência não comprova fidelidade do vídeo ao anúncio.
    Os três picos precisam de audiência medida; a divulgação comercial nativa continua pendente no Buffer/TikTok.</p>
    <p>Publicações retornadas pelo Buffer: '''+e(len(metrics.get('posts',[])))+'''.
    Coleta: '''+e(metrics.get('collected_at','ainda não realizada'))+'''</p></article>
    <article><h2>Seleção do dia</h2><table><tr><th>Produto</th><th>Nota relativa</th></tr>'''+rows+'''</table>
    <p>A nota organiza candidatos; não prevê vendas. Sem aprovação válida e oferta atual, o produto fica retido.</p></article>
    <article><h2>Distribuição por rede</h2>'''+boards+'''</article><article><h2>Produtos retidos</h2><ul>'''+blocks+'''</ul></article>
    <small>Gerado em '''+e(report['generated_at'])+''' · Fontes e componentes: <a href="recomendacoes.json">registro completo</a>.</small></html>'''
    (ROOT/'recomendacoes.html').write_text(page,encoding='utf8')
    print(json.dumps(report,ensure_ascii=False,indent=2))

def package(p, slot):
    dest = ROOT/'pacotes'/slot['day']/p['id']
    dest.mkdir(parents=True, exist_ok=True)
    target = dest/'video.mp4'
    if not target.exists() or sha(target) != sha(path_for(p['video'])):
        shutil.copy2(path_for(p['video']), target)
    text = p['caption']+'\n\n'+p['affiliate_link']
    for channel in CHANNELS:
        (dest/f'{channel}.txt').write_text(text, encoding='utf8')
    write(dest/'vincular-produto.json', {
        'product_url':p['product_url'], 'shop_id':p['shop_id'], 'item_id':p['item_id'],
        'variant':p['variant'], 'quantity':p['quantity'], 'sha256_video':sha(target),
        'due':slot['due'],
        'due_by_channel':{c:{'at':due_for(c,slot)[0].isoformat(),'basis':due_for(c,slot)[1]} for c in CHANNELS},
        'instructions':'Na Shopee, adicionar este produto/variante ao vídeo; link na legenda não substitui o vínculo.'})
    return dest

def http(method, url, token=None, fields=None, body=None, headers=None):
    h = dict(headers or {})
    if token:
        h['Authorization'] = 'Bearer '+token
    if fields is not None:
        body = urllib.parse.urlencode(fields).encode()
        h['Content-Type'] = 'application/x-www-form-urlencoded'
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            result = json.loads(r.read())
    except urllib.error.HTTPError as e:
        # Não registrar corpo de resposta, tokens, URLs de upload ou querystrings.
        raise Blocked(f'API retornou HTTP {e.code}; conferir no painel antes de repetir.') from None
    except (OSError, ValueError):
        raise Blocked('Resposta de rede incerta; conferir no painel antes de repetir.') from None
    if result.get('error') and result['error'].get('code') not in (None, 'ok'):
        raise Blocked('API recusou a operação; verificar permissão e status no painel.')
    return result

def check_buffer_connection():
    cfg = config()['channels']['tiktok']
    token = os.environ.get(cfg.get('token_env', ''), '')
    if not token or not cfg.get('organization_id') or not cfg.get('buffer_channel_id'):
        raise Blocked('Conexão Buffer incompleta; use iniciar.ps1 -ConferirBuffer.')
    query = '''query($input: ChannelsInput!) {
      channels(input: $input) { id name service isDisconnected externalLink }
    }'''
    response = http('POST', 'https://api.buffer.com', token,
        headers={'Content-Type':'application/json'},
        body=json.dumps({'query':query,'variables':{'input':{
            'organizationId':cfg['organization_id']}}}).encode())
    if response.get('errors'):
        raise Blocked('Buffer recusou a consulta; conferir validade e escopos da chave.')
    matches = [c for c in response.get('data',{}).get('channels',[])
               if c.get('id') == cfg['buffer_channel_id']]
    if (len(matches) != 1 or matches[0].get('name') != 'lualuanaachadinhos'
            or matches[0].get('service') != 'tiktok' or matches[0].get('isDisconnected') is not False):
        raise Blocked('TikTok esperado não está conectado ao Buffer.')
    print(json.dumps({'connected':True,'channel':matches[0],
        'read_only_check':True,'publishing_tested':False},ensure_ascii=False,indent=2))
    return matches[0]


def buffer_request(query, variables):
    cfg=config()['channels']['tiktok']
    token=os.environ.get(cfg.get('token_env',''),'')
    if not token:
        raise Blocked('Chave Buffer não carregada.')
    if cfg.get('key_expires_on') and now().date() >= dt.date.fromisoformat(cfg['key_expires_on']):
        raise Blocked('Chave Buffer vencida; renovar antes de enviar.')
    result=http('POST','https://api.buffer.com',token,headers={'Content-Type':'application/json'},
        body=json.dumps({'query':query,'variables':variables}).encode())
    if result.get('errors') or not result.get('data'):
        raise Blocked('Buffer recusou a operação GraphQL; conferir painel antes de repetir.')
    return result['data']


def collect_buffer():
    """Coleta somente leitura; unidades originais preservadas, sem inferir vendas."""
    cfg=config()['channels']['tiktok']
    check_buffer_connection()
    file=ROOT/'metricas-buffer.json'
    if file.exists() and (now()-dt.datetime.fromisoformat(read(file)['collected_at'])).total_seconds()<21600:
        print('Coleta preservada: intervalo mínimo de seis horas para economizar a cota.'); return
    query='''query($input: PostsInput!, $after: String) {
      posts(input:$input, first:100, after:$after) {
        edges { node { id channelId status sentAt text externalLink metrics { type name value unit } metricsUpdatedAt } }
        pageInfo { hasNextPage endCursor }
      }
    }'''
    nodes=[]; cursor=None
    for _ in range(10):
        page=buffer_request(query,{'input':{'organizationId':cfg['organization_id'],
            'filter':{'channelIds':[cfg['buffer_channel_id']],'status':['sent']}},'after':cursor})['posts']
        nodes.extend(e['node'] for e in page['edges'])
        if not page['pageInfo']['hasNextPage']: break
        following=page['pageInfo']['endCursor']
        if not following or following==cursor:
            raise Blocked('Paginação Buffer inconsistente; coleta anterior preservada.')
        cursor=following
    else:
        raise Blocked('Mais de mil posts: refine a janela antes de substituir as métricas.')
    write(file,{'collected_at':now().isoformat(),'channel_id':cfg['buffer_channel_id'],
        'source':'https://developers.buffer.com/examples/get-post-metrics.html','posts':nodes,
        'note':'Métricas experimentais brutas; não representam pedidos ou comissão. Não normalizadas automaticamente.'})
    learn_buffer(nodes)
    print(len(nodes),'posts disponíveis no Buffer coletados; nenhuma publicação criada.')


def learn_buffer(nodes):
    file=ROOT/'aprendizado-buffer.json'
    previous=read(file)['observations'] if file.exists() else []
    previous=[r for r in previous if recomendador.fresh(r,now(),28*24)]
    seen={r['post_id'] for r in previous}
    for node in nodes:
        if node['id'] in seen or not node.get('metricsUpdatedAt') or not node.get('sentAt'): continue
        links=set(re.findall(r'https://s\.shopee\.com\.br/[A-Za-z0-9]+',node.get('text','')))
        matches=[p for p in products() if p.get('affiliate_link') in links and p.get('category')]
        if len(matches)!=1: continue  # Não inferir categoria/produto pela legenda.
        observed=recomendador.stamp(node['metricsUpdatedAt'])
        age=(observed-recomendador.stamp(node['sentAt'])).total_seconds()/3600
        if not 24<=age<=48 or observed>now(): continue
        metrics={r['type']:r for r in node.get('metrics') or []}
        views,rate=metrics.get('views',{}),metrics.get('engagementRate',{})
        if views.get('unit')!='count' or rate.get('unit')!='percentage' or not node.get('externalLink'): continue
        row={'kind':'performance','post_id':node['id'],'product_id':matches[0]['id'],
            'category':matches[0]['category'],'channel':'tiktok','source':'Buffer API — engagementRate',
            'source_url':node['externalLink'],'published_at':node['sentAt'],
            'observed_at':observed.isoformat(),'metric':'engagement_rate',
            'views':views['value'],'engagement_percent':rate['value']}
        recomendador.validate({'version':1,'observations':[row]},now())
        previous.append(row); seen.add(node['id'])
    write(file,{'version':1,'observations':previous})
    print(len(previous),'retratos comparáveis de desempenho (24–48h) disponíveis para o ranking.')


def buffer_payload(p):
    validate_identity(p)
    # A API consultada em 18/10/2026 não expõe divulgação comercial do TikTok.
    # Caption com #Publicidade não substitui o controle nativo exigido no pedido.
    if p.get('affiliate_link'):
        raise Blocked('Buffer/TikTok sem campo documentado de divulgação comercial. '
                      'Concluir as marcações nativas de publicidade e IA no TikTok; envio automático bloqueado.')
    return {'channelId':config()['channels']['tiktok']['buffer_channel_id'],
        'text':p['caption'],'schedulingType':'automatic','mode':'shareNow','needsApproval':False,
        'assets':[{'video':{'url':public_media(p)}}],
        'metadata':{'tiktok':{'isAiGenerated':bool(p.get('ai_generated'))}}}


def buffer_publish(p, save):
    payload=buffer_payload(p)
    check_buffer_connection()
    query='''mutation($input:CreatePostInput!) { createPost(input:$input) {
      ... on PostActionSuccess { post { id status } }
      ... on MutationError { message }
    }}'''
    result=buffer_request(query,{'input':payload})['createPost']
    post=result.get('post')
    if not post or not post.get('id'):
        raise Blocked('Buffer não confirmou criação do post; conferir painel antes de repetir.')
    rid=post['id']; save('submitted',rid)
    # Criação de post não comprova entrega; reconciliar o ID sem reenviar createPost.
    for _ in range(6):
        current=buffer_request('''query($input:PostInput!){post(input:$input){id status externalLink}}''',
            {'input':{'id':rid}})['post']
        if current['status']=='sent':
            save('published',rid,current.get('externalLink') or ''); return
        if current['status']=='error':
            raise Blocked('Buffer registrou erro no envio.')
        time.sleep(4)
    print('Buffer aceitou; entrega ainda pendente. Conferir ID no painel sem reenviar.')


def public_media(p):
    url = p.get('media_url', '')
    if urllib.parse.urlsplit(url).scheme != 'https':
        raise Blocked('O conector requer media_url HTTPS público do MP4 aprovado.')
    digest = hashlib.sha256(); count = 0
    with urllib.request.urlopen(url, timeout=45) as response:
        while data := response.read(1024*1024):
            count += len(data)
            if count > 100*1024*1024:
                raise Blocked('Mídia remota excede 100 MB.')
            digest.update(data)
    if digest.hexdigest() != sha(path_for(p['video'])):
        raise Blocked('Arquivo hospedado não é idêntico ao vídeo aprovado.')
    return url

def set_post(db, pid, channel, status, remote_id='', url=''):
    db.execute('INSERT OR REPLACE INTO posts VALUES(?,?,?,?,?,?)',
               (pid,channel,status,str(remote_id),url,now().isoformat()))
    db.commit()

def meta_publish(p, channel, save):
    cfg = config()['channels'][channel]
    version = config()['meta_api_version']
    if not re.fullmatch(r'v\d+\.\d+', version):
        raise Blocked('Configure a versão Graph API suportada pelo app Meta.')
    token = os.environ.get(cfg.get('token_env',''), '')
    ident = cfg.get('account_id','')
    if not token or not str(ident).isdigit():
        raise Blocked('Conta/token oficial da Meta ainda não configurados.')
    api = f'https://graph.facebook.com/{version}'
    caption = p['caption']+'\n\n'+p['affiliate_link']
    if channel == 'instagram':
        media = public_media(p)
        data = http('POST', f'{api}/{ident}/media', token,
                    fields={'media_type':'REELS','video_url':media,'caption':caption,'share_to_feed':'true'})
        rid = data['id']; save('processing',rid)
        for _ in range(30):
            status = http('GET',f'{api}/{rid}?fields=status_code',token)['status_code']
            if status == 'FINISHED':
                break
            if status in ('ERROR','EXPIRED'):
                raise Blocked('Instagram recusou o processamento; não reenviar automaticamente.')
            time.sleep(4)
        else:
            raise Blocked('Processamento ainda pendente no Instagram; conferir antes de repetir.')
        save('publishing',rid)
        result = http('POST',f'{api}/{ident}/media_publish',token,fields={'creation_id':rid})
        rid = result['id']; save('submitted',rid)
        final = http('GET',f'{api}/{rid}?fields=id,permalink',token)
        save('published',rid,final['permalink'])
    else:
        data = http('POST',f'{api}/{ident}/video_reels',token,fields={'upload_phase':'start'})
        rid = data['video_id']; save('processing',rid)
        upload = data['upload_url']
        parsed = urllib.parse.urlsplit(upload)
        if parsed.scheme != 'https' or parsed.hostname != 'rupload.facebook.com':
            raise Blocked('Destino de upload Meta inesperado.')
        video = path_for(p['video']).read_bytes()
        result = http('POST',upload,headers={'Authorization':'OAuth '+token,'offset':'0',
            'file_size':str(len(video)),'Content-Type':'application/octet-stream'},body=video)
        if result.get('success') is not True:
            raise Blocked('Upload do Facebook não confirmado.')
        save('publishing',rid)
        result = http('POST',f'{api}/{ident}/video_reels',token,fields={
            'upload_phase':'finish','video_state':'PUBLISHED','video_id':rid,'description':caption})
        if result.get('success') is not True:
            raise Blocked('Publicação do Facebook não confirmada.')
        save('submitted',rid)
        for _ in range(30):
            status = http('GET',f'{api}/{rid}?fields=status',token).get('status',{})
            if status.get('publishing_phase',{}).get('status') == 'complete':
                save('published',rid,f'https://www.facebook.com/reel/{rid}/'); return
            if status.get('video_status') == 'error':
                raise Blocked('Erro no processamento do Facebook.')
            time.sleep(4)
        raise Blocked('Publicação aceita mas ainda não confirmada; conferir no painel.')

def run(live=False):
    moment = now()
    with exclusive():
        prepare_video_scripts()
        slots = plan(moment.date())
        with connection() as db:
            for slot in slots:
                p = product(slot['product'])
                try:
                    if eligible(p, moment) != slot['fingerprint']:
                        raise Blocked('Produto alterado após reserva do horário.')
                except Blocked as ex:
                    print(p['id'], 'BLOQUEADO:',ex); continue
                dest = package(p,slot)
                if sha(dest/'video.mp4') != slot['video_hash']:
                    print(p['id'],'BLOQUEADO: vídeo mudou durante a preparação.'); continue
                print(p['id'], slot['due'], 'pacote:', dest)
                if not live:
                    continue
                for channel in CHANNELS:
                    channel_due, basis = due_for(channel,slot)
                    if config().get('require_measured_peaks') and basis != 'medido':
                        print(channel,'aguarda dados dos três picos de audiência para este dia'); continue
                    if moment < channel_due:
                        continue
                    previous = db.execute('SELECT status FROM posts WHERE product=? AND channel=?',
                                          (p['id'],channel)).fetchone()
                    if previous:
                        print(channel, previous['status'], '(não repetir)'); continue
                    mode = config()['channels'][channel]['mode']
                    supported=(mode=='meta_api' and channel in ('instagram','facebook')) or (mode=='buffer_api' and channel=='tiktok')
                    if not supported:
                        set_post(db,p['id'],channel,'manual_pending')
                        print(channel,'aguarda publicação e conferência manual'); continue
                    # Registro durável ANTES da primeira chamada: falha/interrupção não duplica.
                    set_post(db,p['id'],channel,'inflight')
                    save = lambda status,rid='',url='': set_post(db,p['id'],channel,status,rid,url)
                    try:
                        # Publicar a cópia conferida do pacote, não um arquivo de origem mutável.
                        approved=dict(p,video=str(dest/'video.mp4'))
                        if mode=='buffer_api': buffer_publish(approved,save)
                        else: meta_publish(approved,channel,save)
                    except Exception as ex:
                        # Preservar ID remoto para reconciliação humana.
                        prior = db.execute('SELECT remote_id FROM posts WHERE product=? AND channel=?',
                                           (p['id'],channel)).fetchone()
                        save('needs_review',prior['remote_id'] if prior else '')
                        print(channel,'BLOQUEADO:', type(ex).__name__,
                              str(ex) if isinstance(ex,Blocked) else 'Verificar painel; detalhes omitidos para proteger tokens.')
        print('Execução concluída. Pacote/manual_pending não significa publicado.')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='cmd',required=True)
    for name in ('auditar','aprovar'):
        sub.add_parser(name).add_argument('id')
    sub.add_parser('planejar')
    sub.add_parser('executar').add_argument('--publicar',action='store_true')
    sub.add_parser('status')
    sub.add_parser('conferir-buffer')
    sub.add_parser('coletar-buffer')
    sub.add_parser('coletar-shopee')
    sub.add_parser('preparar-roteiros')
    for name in ('consultar-roteiro','selecionar-roteiro'):
        editorial=sub.add_parser(name);editorial.add_argument('chave')
        editorial.add_argument('--alternativa',choices=tuple('ABCDEFGH'),required=name=='selecionar-roteiro')
    rec=sub.add_parser('recomendar'); rec.add_argument('--dia', default=now().date().isoformat())
    intel=sub.add_parser('importar-inteligencia'); intel.add_argument('--arquivo',required=True)
    peak = sub.add_parser('importar-picos')
    peak.add_argument('--csv', required=True); peak.add_argument('--fonte', required=True)
    p = sub.add_parser('confirmar'); p.add_argument('id'); p.add_argument('rede',choices=CHANNELS)
    p.add_argument('--url',required=True)
    args = parser.parse_args()
    if args.cmd == 'auditar':
        print(audit(product(args.id))/'revisao.html')
    elif args.cmd == 'aprovar':
        approve(product(args.id))
    elif args.cmd == 'planejar':
        with exclusive(): print(json.dumps(plan(now().date()),indent=2,ensure_ascii=False))
    elif args.cmd == 'executar':
        run(args.publicar)
    elif args.cmd == 'conferir-buffer':
        check_buffer_connection()
    elif args.cmd == 'coletar-buffer':
        with exclusive(): collect_buffer()
    elif args.cmd == 'preparar-roteiros':
        with exclusive(): prepare_video_scripts()
    elif args.cmd == 'coletar-shopee':
        with exclusive(): collect_shopee()
    elif args.cmd in ('consultar-roteiro','selecionar-roteiro'):
        with exclusive(): result=lookup_script(args.chave,args.alternativa,args.cmd=='selecionar-roteiro')
        print(json.dumps(result,ensure_ascii=False,indent=2))
    elif args.cmd == 'recomendar':
        recommend(dt.date.fromisoformat(args.dia))
    elif args.cmd == 'importar-inteligencia':
        with exclusive(): import_intelligence(args.arquivo)
    elif args.cmd == 'status':
        with connection() as db:
            print(json.dumps({'agenda':[dict(x) for x in db.execute('SELECT * FROM slots')],
                'redes':[dict(x) for x in db.execute('SELECT * FROM posts')]},indent=2,ensure_ascii=False))
    elif args.cmd == 'importar-picos':
        with exclusive(): import_peaks(args.csv,args.fonte)
    elif args.cmd == 'confirmar':
        allowed = {'instagram':('instagram.com','www.instagram.com'),
                   'facebook':('facebook.com','www.facebook.com'),
                   'tiktok':('tiktok.com','www.tiktok.com'),
                   'shopee':('shopee.com.br','www.shopee.com.br','sv.shopee.com.br','s.shopee.com.br')}
        url = urllib.parse.urlsplit(args.url)
        if url.scheme != 'https' or url.hostname not in allowed[args.rede] or url.path in ('','/'):
            raise Blocked('Informe o link HTTPS da publicação na rede correta.')
        with exclusive(), connection() as db:
            if not db.execute('SELECT 1 FROM slots WHERE product=?',(args.id,)).fetchone():
                raise Blocked('Produto ainda não faz parte da agenda.')
            print('Abra a publicação e confira vídeo inteiro, áudio e produto vinculado.')
            if input('Conferiu e está correto? [sim/não] ').lower().strip() != 'sim':
                raise Blocked('Não confirmado.')
            set_post(db,args.id,args.rede,'published','manual',args.url)

if __name__ == '__main__':
    try:
        main()
    except (Blocked,ValueError,KeyError,StopIteration,FileNotFoundError) as ex:
        print('BLOQUEADO:',str(ex) or 'Produto não encontrado.',file=sys.stderr)
        sys.exit(2)
