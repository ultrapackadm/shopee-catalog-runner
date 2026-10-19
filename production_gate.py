"""Fail-closed cloud production readiness and approval binding. No external writes."""
import datetime as dt
import hashlib
import json
import re
from pathlib import Path

REVIEW_CHECKS = ('listing_variant', 'quantity', 'appearance', 'full_video',
                 'full_portuguese_audio', 'factual_caption', 'rights', 'disclosures')
BOUND_FIELDS = ('id','shop_id','item_id','product_url','variant','quantity',
                'visible_details','speech','caption','affiliate_link','media_url','ai_generated')


class Blocked(ValueError):
    pass


def digest(root, job):
    data = {key:job.get(key) for key in BOUND_FIELDS}
    root = Path(root).resolve()
    for field in ('video','reference','listing_evidence'):
        path = (root/job.get(field,'__missing__')).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise Blocked('Review asset missing or outside the private review bundle.')
        data[field] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def verify(root, job, receipt, moment, technical_audit, listing):
    """A technical audit is not semantic approval. The reviewer must watch/listen fully."""
    if job.get('blocked_reason') or job.get('already_published'):
        raise Blocked('Blocked or previously published product.')
    expected = f'https://shopee.com.br/product/{job.get("shop_id")}/{job.get("item_id")}'
    if job.get('product_url') != expected or not str(job.get('shop_id','')).isdigit() or not str(job.get('item_id','')).isdigit():
        raise Blocked('Listing identity mismatch.')
    if not job.get('variant') or type(job.get('quantity')) is not int or job['quantity'] < 1:
        raise Blocked('Exact variant and quantity are required.')
    for field in ('visible_details','speech','caption','affiliate_link'):
        if not job.get(field): raise Blocked('Incomplete factual product metadata.')
    if 'publicidade' not in job['caption'].lower() or (job.get('ai_generated') and not re.search(r'\bIA\b|inteligência artificial',job['caption'],re.I)):
        raise Blocked('Advertising and AI disclosures are required.')
    fp = digest(root, job)
    if receipt.get('fingerprint') != fp or not receipt.get('reviewer') or receipt.get('origin') != 'authenticated_human_review':
        raise Blocked('Missing human review of these exact assets.')
    if not all(receipt.get('checks',{}).get(k) is True for k in REVIEW_CHECKS):
        raise Blocked('Incomplete factual, visual or audio review.')
    for stamp in (receipt.get('reviewed_at'), listing.get('checked_at')):
        try: age = (moment-dt.datetime.fromisoformat(stamp)).total_seconds()/3600
        except (TypeError, ValueError): raise Blocked('Review timestamp invalid.') from None
        if not 0 <= age <= 48: raise Blocked('Review expired or dated in the future.')
    if listing.get('shop_id') != job['shop_id'] or listing.get('item_id') != job['item_id'] or listing.get('variant') != job['variant']:
        raise Blocked('Reviewed listing does not match this variant.')
    if type(listing.get('stock')) is not int or listing['stock'] <= 0 or listing.get('affiliate_eligible') is not True:
        raise Blocked('Current stock and commission eligibility must be confirmed separately.')
    if technical_audit.get('fingerprint') != fp or technical_audit.get('decode_complete') is not True:
        raise Blocked('Full audio/video decode audit missing.')
    if not 9.5 <= technical_audit.get('seconds',0) <= 10.5:
        raise Blocked('Expected a ten-second video.')
    return fp


def channel_gate(channel, capability):
    # Caption text is not a substitute for the platform's native disclosure controls.
    if capability.get('native_ai_disclosure') is not True or capability.get('native_ad_disclosure') is not True:
        raise Blocked('Native AI and commercial disclosure not verified for this connector.')
    if capability.get('end_to_end_verified') is not True:
        raise Blocked('Connector still requires an end-to-end validation.')


def inspect(root, moment):
    root = Path(root)
    policy = json.loads((root/'production-policy.json').read_text()) if (root/'production-policy.json').exists() else {}
    prepared = json.loads((root/'proximos-videos.json').read_text()).get('products',[])
    generation = policy.get('generation',{})
    reasons=[]
    if generation.get('enabled') is not True or generation.get('paid_api_authorized') is not True:
        reasons.append('video_api_and_spending_not_enabled')
    if not policy.get('private_media_storage_verified'):
        reasons.append('private_video_storage_and_remote_review_not_connected')
    if not policy.get('measured_peaks_verified'):
        reasons.append('audience_peak_schedule_not_verified')
    channels={}
    for name in ('instagram','facebook','tiktok','shopee'):
        try: channel_gate(name,policy.get('channels',{}).get(name,{})); channels[name]='verified'
        except Blocked: channels[name]='blocked_pending_connector_and_native_disclosures'
    return {'checked_at':moment.isoformat(),'production_enabled':False,
            'prepared_jobs':len(prepared),'video_generation_enabled':False,
            'publication_enabled':False,'review_required':True,'daily_product_limit':3,
            'blocking_dependencies':reasons,'channels':channels}
