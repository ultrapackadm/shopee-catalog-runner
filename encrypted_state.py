"""Authenticated encryption for private collector state stored on a public runner."""
import base64
import gzip
import json
import os
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

FILES = ('config.json', 'fila.json', 'modelos-roteiros.json',
         'roteiros-selecionados.json', 'catalogo-shopee.json',
         'historico-shopee.json', 'candidatos-shopee.json',
         'catalogo-shopee.html', 'roteiros-achadinhos-da-lu.json',
         'proximos-videos.json', 'geracao-roteiros-status.json', 'cloud-status.json')
HEADER = b'SHOPEE_STATE_V1\x00'


def cipher():
    key = base64.b64decode(os.environ['CATALOG_STATE_KEY'], validate=True)
    if len(key) != 32:
        raise ValueError('Invalid encryption key length')
    return AESGCM(key)


def seal(folder, destination):
    rows = {n:(Path(folder)/n).read_text(encoding='utf-8-sig')
            for n in FILES if (Path(folder)/n).exists()}
    raw = gzip.compress(json.dumps(rows, ensure_ascii=False).encode('utf8'))
    nonce = os.urandom(12)
    encrypted = HEADER + nonce + cipher().encrypt(nonce, raw, HEADER)
    target = Path(str(destination) + '.tmp')
    target.write_bytes(encrypted)
    target.replace(destination)


def unseal(source, folder):
    raw = Path(source).read_bytes()
    if not raw.startswith(HEADER):
        raise ValueError('Unknown encrypted state format')
    body = raw[len(HEADER):]
    data = cipher().decrypt(body[:12], body[12:], HEADER)
    rows = json.loads(gzip.decompress(data))
    if not isinstance(rows, dict) or any(n not in FILES for n in rows):
        raise ValueError('Unexpected state file')
    if any(not isinstance(v, str) for v in rows.values()):
        raise ValueError('Invalid state content')
    Path(folder).mkdir(parents=True, exist_ok=True)
    for name, content in rows.items():
        (Path(folder)/name).write_text(content, encoding='utf8')
