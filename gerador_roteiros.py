"""Rascunhos locais por tipo de produto; sem rede, IA externa ou aprovação."""
import hashlib
import itertools
import re
import unicodedata

VERSION=2
CTA={
    'shopee':'Toque no produto vinculado para conferir os detalhes.',
    'instagram':'Confira este produto no link da bio, se disponível.',
    'facebook':'Confira os detalhes no link da publicação.',
    'tiktok':'Confira este produto no link do perfil, se disponível.'}


def normalized(value):
    return ''.join(c for c in unicodedata.normalize('NFKD',value.casefold()) if not unicodedata.combining(c))


def classify(name,models):
    text=normalized(name)
    if any(re.search(pattern,text) for pattern in models.get('exclude_patterns',[])):
        return None,'Tipo fora dos modelos editoriais disponíveis.'
    matches=[m for m in models['types'] if re.search(m['pattern'],text)]
    if not matches:return None,'Sem modelo correspondente; adicionar tipo em modelos-roteiros.json.'
    # Regras específicas primeiro, com generalizações explicitamente ordenadas no arquivo.
    return matches[0],None


def compose(model,variant,key,seen):
    label=model['label']
    checks=model['checks']
    context=model['context']
    opening={
      'C':[f'Procurando {label}?',f'Como você escolheria {label}?',f'Já pensou em {label}?',
           f'{label.capitalize()} entraria na sua lista?',f'O que você procura ao escolher {label}?',f'Que tal conhecer {label}?'],
      'D':[f'Antes de escolher {label}, vale conferir alguns pontos.',f'Vai comparar opções de {label}?',
           f'Uma pausa antes de decidir por {label}.',f'Na sua lista tem {label}?',
           f'Quer avaliar {label} com mais calma?',f'Alguns detalhes ajudam na escolha de {label}.'],
      'F':[f'Vamos observar {label} por inteiro?',f'Um olhar mais perto: conheça {label}.',
           f'Agora o foco está em {label}.',f'Repare na proposta desse anúncio de produto: {label}.',
           f'Que detalhes você observaria em {label}?',f'A referência de hoje mostra {label}.'],
      'G':[f'Pensando {context}?',f'Organizando suas opções {context}?',f'Buscando uma ideia {context}?',
           f'Se você está pensando {context}, veja esta opção.',f'Uma inspiração {context}: conheça {label}.',
           f'Está montando uma lista {context}?']
    }[variant]
    middle={
      'C':[f'Esse anúncio apresenta uma opção para conhecer; confira {checks}.',
           f'Compare a proposta com o que você procura e confira {checks}.',
           f'Pense no uso que você pretende fazer e confira {checks}.',
           f'Antes de decidir, veja a opção inteira e confira {checks}.',
           f'Observe a referência e compare {checks}.',
           f'Para conhecer melhor a opção, confira {checks}.'],
      'D':[f'Comece pela conferência de {checks}.',f'Confira {checks} na variante escolhida.',
           f'Na descrição, procure as informações de {checks}.',f'Compare {checks} com o que você precisa.',
           f'A opção exata faz diferença: confira {checks}.',f'Vale observar {checks} antes de decidir.'],
      'F':['Veja a referência completa e depois os detalhes reais do modelo.',
           'Observe o formato na referência e depois volte ao produto inteiro.',
           'Comece pelo enquadramento geral e aproxime os detalhes da referência.',
           'Veja o que aparece na imagem do produto antes de comparar as opções.',
           'Acompanhe a referência por inteiro, sem perder de vista a opção anunciada.',
           'Observe os detalhes visíveis e compare com a opção da descrição.'],
      'G':[f'Essa é uma alternativa para conhecer; confira {checks}.',
           f'Inclua esse modelo entre as opções e confira {checks}.',
           f'Pense no que faz sentido para você e confira {checks}.',
           f'Veja a proposta do anúncio e confira {checks}.',
           f'Compare com a sua ideia antes de escolher; confira {checks}.',
           f'A referência apresenta uma possibilidade para comparar; confira {checks}.']
    }[variant]
    ending=['Conheça os detalhes no anúncio.','Abra o anúncio para comparar as opções.',
            'Confira a descrição da opção antes de escolher.','Veja as informações do produto no anúncio.',
            'Consulte os detalhes da variante no anúncio.','Encontre as informações completas na página do produto.']
    choices=list(itertools.product(opening,middle,ending))
    offset=int(hashlib.sha256(f'{key}/{variant}/{VERSION}'.encode()).hexdigest(),16)%len(choices)
    for shift in range(len(choices)):
        parts=choices[(offset+shift)%len(choices)]
        speech=' '.join(parts)
        if speech not in seen:
            seen.add(speech)
            return parts[0],speech
    return None,None


def generate(catalog,bank,known,models):
    """Não tocar em roteiros existentes; um grupo de quatro por anúncio novo reconhecido."""
    result=list(bank)
    existing={r['source_key'] for r in bank}
    unavailable={str(p.get('shop_id'))+'/'+str(p.get('item_id')) for p in known
                 if p.get('already_published') or p.get('video')}
    seen={r['speech'] for r in bank}
    pending=[]
    added=[]
    for offer in sorted(catalog.get('products',[]),key=lambda r:r.get('key','')):
        key=str(offer.get('shop_id',''))+'/'+str(offer.get('item_id',''))
        if key in existing or key in unavailable or not offer.get('affiliate_offer_active'):continue
        if not re.fullmatch(r'\d+/\d+',key) or offer.get('product_url')!='https://shopee.com.br/product/'+key:
            pending.append({'key':key,'reason':'Identidade ou URL do anúncio inválida.'});continue
        model,reason=classify(offer.get('name',''),models)
        if model is None:
            pending.append({'key':key,'name':offer.get('name'),'reason':reason});continue
        new=[]
        for variant in 'CDFG':
            hook,speech=compose(model,variant,key,seen)
            if not speech:break
            ident='AUTO_'+key.replace('/','_')+'_'+variant
            checks='Confirmar '+model['checks']+'. Não inferir material, desempenho, estoque ou preço pelo título.'
            if model.get('editorial_notes'): checks+=' '+model['editorial_notes']
            new.append(dict(id=ident,source_key=key,shop_id=offer['shop_id'],item_id=offer['item_id'],
                name=offer['name'],product_url=offer['product_url'],affiliate_link=offer.get('affiliate_link',''),
                variant='',quantity=None,visible_details='Pendente de conferência. '+checks,
                speech=speech,hook_text=hook,caption=hook+' Confira os detalhes da opção no anúncio. Publicidade • link de afiliada.\nConteúdo gerado por IA.\n#AchadinhosDaLu #ShopeeAchadinhos',
                video='',reference='',listing_evidence='',media_url='',ai_generated=True,already_published=False,
                blocked_reason='Rascunho por modelo local. Conferir anúncio, variante, evidências e vídeo antes de aprovar.',
                category=model['category'],script_variant=variant,
                approach={'C':'pergunta','D':'checklist','F':'detalhe','G':'situacao'}[variant],
                scenes=[{'time':'0–3s','direction':'Abrir com a referência real e a frase de abertura.','overlay':hook},
                  {'time':'3–17s','direction':'Mostrar apenas a referência e detalhes confirmados. Não simular teste, resultado ou uso pessoal.','overlay':'Conferir '+model['checks']},
                  {'time':'17–26s','direction':'Produto inteiro e chamada conforme o destino real.','overlay':'Veja os detalhes no anúncio'}],
                cta_by_channel=CTA,verification_notes=checks,
                estimated_duration_seconds='15–26, ajustar após ouvir a narração',
                source_collected_at=catalog.get('collected_at'),generation_method='local_templates',
                generator_version=VERSION,model_id=model['id']))
        if len(new)==4:
            result.extend(new);added.extend(new);existing.add(key)
        else:pending.append({'key':key,'reason':'Variações únicas esgotadas; ampliar modelos.'})
    return result,{'generation_method':'local_templates','added_scripts':len(added),
        'added_products':len({r['source_key'] for r in added}),'total_scripts':len(result),
        'total_products':len({r['source_key'] for r in result}),'pending':pending}
