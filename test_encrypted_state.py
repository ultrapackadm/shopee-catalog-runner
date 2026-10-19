import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import base64
import encrypted_state as state


class EncryptionTests(unittest.TestCase):
    def test_roundtrip_and_no_plaintext(self):
        with tempfile.TemporaryDirectory() as t, patch.dict(os.environ,{'CATALOG_STATE_KEY':base64.b64encode(os.urandom(32)).decode()}):
            root=Path(t); src=root/'source'; src.mkdir()
            (src/'config.json').write_text('{"private":"sensitive-marker"}')
            state.seal(src,root/'state.enc')
            self.assertNotIn(b'sensitive-marker',(root/'state.enc').read_bytes())
            state.unseal(root/'state.enc',root/'restored')
            self.assertEqual((src/'config.json').read_bytes(),(root/'restored/config.json').read_bytes())
            body=bytearray((root/'state.enc').read_bytes()); body[-1]^=1
            (root/'state.enc').write_bytes(body)
            with self.assertRaises(Exception): state.unseal(root/'state.enc',root/'tampered')
            self.assertFalse((root/'tampered').exists())

    def test_wrong_key_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t)
            with patch.dict(os.environ,{'CATALOG_STATE_KEY':base64.b64encode(os.urandom(32)).decode()}): state.seal(root,root/'state.enc')
            with patch.dict(os.environ,{'CATALOG_STATE_KEY':base64.b64encode(os.urandom(32)).decode()}):
                with self.assertRaises(Exception): state.unseal(root/'state.enc',root/'bad')


if __name__=='__main__': unittest.main()
