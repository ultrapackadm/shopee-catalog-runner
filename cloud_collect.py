"""Coleta na nuvem, sem produção de vídeos ou acesso a redes sociais."""
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import tempfile

import publicador as app
import shopee_affiliate

ROOT = Path(__file__).resolve().parent
STATE = ('catalogo-shopee.json', 'historico-shopee.json',
         'candidatos-shopee.json', 'catalogo-shopee.html',
         'roteiros-achadinhos-da-lu.json', 'proximos-videos.json',
         'geracao-roteiros-status.json')
INPUTS = STATE + ('config.json', 'fila.json', 'modelos-roteiros.json',
                  'roteiros-selecionados.json')


def validate_config(cfg):
    if cfg.get('daily_products') != 3:
        raise ValueError('Limite de três produtos deve ser preservado.')
    if any(c.get('mode') != 'manual' or c.get('token_dpapi')
           for c in cfg.get('channels', {}).values()):
        raise ValueError('Este serviço não pode habilitar publicação.')
    if cfg.get('shopee_api', {}).get('interval_hours', 0) < 6:
        raise ValueError('O intervalo mínimo da coleta é de seis horas.')
    if cfg.get('shopee_api', {}).get('credential_dpapi'):
        raise ValueError('Credencial Windows não pertence ao serviço cloud.')


def run(probe=False):
    validate_config(app.read(ROOT / 'config.json'))
    # Preparar em área temporária: uma falha preserva o último estado completo.
    with tempfile.TemporaryDirectory() as temporary:
        stage = Path(temporary)
        for name in INPUTS:
            if (ROOT / name).exists():
                shutil.copy2(ROOT / name, stage / name)
        previous_root = app.ROOT
        try:
            app.ROOT = stage
            if probe:
                # Testa autenticação do runner sem forçar nova coleta dentro do cache.
                result = shopee_affiliate.Client().offers(limit=1)
                if not result.get('nodes'):
                    raise ValueError('Consulta de conexão retornou vazia.')
                print('Conexão Shopee autenticada a partir do servidor GitHub.')
            catalog = app.collect_shopee()
            report = app.read(stage / 'geracao-roteiros-status.json')
            status = {
                'status': 'success', 'checked_at': app.now().isoformat(),
                'collected_at': catalog['collected_at'],
                'offers': len(catalog['products']),
                'trend_signals': len(catalog.get('trend_signals', [])),
                'missing_known_offers': catalog.get('missing_known_offers', []),
                'scripts': report['total_scripts'],
                'pending_models': len(report.get('pending', [])),
                'stock_by_variant_available': False,
                'publishing_enabled': False,
                'run_url': ('https://github.com/' + os.environ['GITHUB_REPOSITORY'] +
                            '/actions/runs/' + os.environ['GITHUB_RUN_ID'])
                           if os.environ.get('GITHUB_RUN_ID') else None,
            }
            for name in STATE:
                if (stage / name).exists():
                    target = ROOT / (name + '.tmp')
                    shutil.copy2(stage / name, target)
                    target.replace(ROOT / name)
            app.write(ROOT / 'cloud-status.json', status)
            print(json.dumps(status, ensure_ascii=False))
        finally:
            app.ROOT = previous_root


if __name__ == '__main__':
    import sys
    try:
        run(probe='--probe' in sys.argv)
    except Exception:
        # Não serializar respostas, tokens ou mensagens de exceções de terceiros.
        app.write(ROOT / 'cloud-status.json', {
            'status': 'failed', 'checked_at': app.now().isoformat(),
            'reason': 'Coleta não concluída; estado anterior preservado. Conferir execução.',
            'publishing_enabled': False,
        })
        print('Coleta cloud falhou; segredo e detalhes da resposta omitidos.', file=sys.stderr)
        sys.exit(1)
