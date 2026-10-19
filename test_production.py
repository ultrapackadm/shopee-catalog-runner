import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import production_gate as gate
import video_generation as video


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for name in ('video.mp4','photo.jpg','listing.png'):(self.root/name).write_bytes(name.encode())
        self.moment=dt.datetime(2026,10,19,15,tzinfo=dt.timezone(dt.timedelta(hours=-3)))
        self.job=dict(id='test',shop_id='123',item_id='456',product_url='https://shopee.com.br/product/123/456',
          variant='vermelho',quantity=1,visible_details='Detalhes confirmados',speech='Texto em português',
          scenes=['movimento natural'],caption='Publicidade • demonstração gerada por IA.',
          affiliate_link='https://s.shopee.com.br/test',video='video.mp4',reference='photo.jpg',listing_evidence='listing.png',
          ai_generated=True,production_day='2026-10-19',reference_mime='image/jpeg',
          reference_sha256=hashlib.sha256((self.root/'photo.jpg').read_bytes()).hexdigest())
        self.fp=gate.digest(self.root,self.job)
        self.receipt=dict(fingerprint=self.fp,reviewer='Human',origin='authenticated_human_review',
          reviewed_at=self.moment.isoformat(),checks={k:True for k in gate.REVIEW_CHECKS})
        self.audit=dict(fingerprint=self.fp,decode_complete=True,seconds=10)
        self.listing=dict(shop_id='123',item_id='456',variant='vermelho',stock=1,affiliate_eligible=True,
          checked_at=self.moment.isoformat())

    def verify(self):return gate.verify(self.root,self.job,self.receipt,self.moment,self.audit,self.listing)

    def test_complete_bound_review_passes(self):self.assertEqual(self.fp,self.verify())

    def test_asset_or_text_changes_invalidate_review(self):
        for field in ('speech','caption','variant','quantity','media_url'):
            original=copy.deepcopy(self.job); self.job[field]='changed'
            with self.assertRaises(gate.Blocked):self.verify()
            self.job=original
        for field in ('video','reference','listing_evidence'):
            p=self.root/self.job[field]; original=p.read_bytes();p.write_bytes(b'changed')
            with self.assertRaises(gate.Blocked):self.verify()
            p.write_bytes(original)

    def test_incomplete_audio_review_blocks(self):
        self.receipt['checks']['full_portuguese_audio']=False
        with self.assertRaises(gate.Blocked):self.verify()

    def test_ai_report_cannot_replace_human_review(self):
        self.receipt['origin']='automatic_vision_model'
        with self.assertRaises(gate.Blocked):self.verify()

    def test_expired_review_blocks(self):
        self.receipt['reviewed_at']=(self.moment-dt.timedelta(hours=49)).isoformat()
        with self.assertRaises(gate.Blocked):self.verify()

    def test_unknown_stock_blocks(self):
        self.listing['stock']=None
        with self.assertRaises(gate.Blocked):self.verify()

    def test_wrong_listing_blocks(self):
        self.listing['item_id']='999'
        with self.assertRaises(gate.Blocked):self.verify()

    def test_technical_decode_is_not_product_approval(self):
        self.receipt={}
        with self.assertRaises(gate.Blocked):self.verify()

    def test_wrong_duration_blocks(self):
        self.audit['seconds']=8
        with self.assertRaises(gate.Blocked):self.verify()

    def test_buffer_ai_flag_does_not_replace_ad_flag(self):
        with self.assertRaises(gate.Blocked):gate.channel_gate('tiktok',dict(native_ai_disclosure=True,end_to_end_verified=True))

    def test_failed_reference_validation_happens_before_payment(self):
        self.job['reference_sha256']='wrong'
        with self.assertRaises(video.Blocked):video.payload(self.job,self.root/'photo.jpg')

    def test_provider_payload_uses_image_and_vertical_format(self):
        payload=video.payload(self.job,self.root/'photo.jpg')
        self.assertEqual(payload['input'][0]['type'],'image')
        self.assertEqual(payload['response_format']['aspect_ratio'],'9:16')
        self.assertIn('10 segundos',payload['input'][1]['text'])

    def test_spending_disabled_prevents_network(self):
        with patch.object(video.urllib.request,'build_opener') as network:
            with self.assertRaises(video.Blocked):video.generate(self.job,self.root/'photo.jpg',self.root/'out.mp4',{}, {},lambda x:None)
            network.assert_not_called()

    def test_uncertain_outcome_is_reserved_and_not_retried(self):
        policy=dict(enabled=True,paid_api_authorized=True,reserved_usd_per_attempt=1.25,daily_budget_usd=3.75)
        ledger={}; saved=[]
        with patch.dict(os.environ,{'GEMINI_API_KEY':'test-only'}),patch.object(video.urllib.request,'build_opener') as network:
            network.return_value.open.side_effect=TimeoutError()
            with self.assertRaises(video.Blocked):video.generate(self.job,self.root/'photo.jpg',self.root/'out.mp4',policy,ledger,lambda x:saved.append(copy.deepcopy(x)))
            self.assertEqual(saved[0]['test']['status'],'outcome_unknown')
            with self.assertRaises(video.Blocked):video.generate(self.job,self.root/'photo.jpg',self.root/'out.mp4',policy,ledger,lambda x:None)
            self.assertEqual(network.return_value.open.call_count,1)

    def test_failed_durable_reservation_prevents_network(self):
        policy=dict(enabled=True,paid_api_authorized=True,reserved_usd_per_attempt=1.25,daily_budget_usd=3.75)
        def fail(x):raise OSError()
        with patch.dict(os.environ,{'GEMINI_API_KEY':'test-only'}),patch.object(video.urllib.request,'build_opener') as network:
            with self.assertRaises(OSError):video.generate(self.job,self.root/'photo.jpg',self.root/'out.mp4',policy,{},fail)
            network.assert_not_called()

    def test_three_attempts_daily_limit(self):
        policy=dict(enabled=True,paid_api_authorized=True,reserved_usd_per_attempt=1.25,daily_budget_usd=100)
        ledger={str(i):dict(production_day='2026-10-19',reserved_usd=1.25) for i in range(3)}
        with patch.dict(os.environ,{'GEMINI_API_KEY':'test-only'}),patch.object(video.urllib.request,'build_opener') as network:
            with self.assertRaises(video.Blocked):video.generate(self.job,self.root/'photo.jpg',self.root/'out.mp4',policy,ledger,lambda x:None)
            network.assert_not_called()


if __name__=='__main__':unittest.main()
