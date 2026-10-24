"""Scoped known-PDP recovery. Audit is GET-only; apply uses the normal API.

No schema changes, product creation, technical claims, source-score promotion,
aggregator prices, challenge solving, or direct database writes are performed.
"""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re

from offer_ingestion_client import OfferIngestionClient
from retailer_offer_refresh import ALLOWED_HOSTS, RetailerOfferRefresher, _safe_url
from source_ingestion import SourceIngestionClient
from tr_price_scraper import TRPriceScraper


BATCH = Path(__file__).parent / 'offer_batches/budget-eight-2026-10-25.json'
REPORT = Path('reviewed-offer-report.json')
BROWSER_EVIDENCE = Path(__file__).parent / 'offer_batches/browser-observations-2026-10-25.json'
EXPECTED_REFERENCES = {
    'tecno-spark-slim-5g': 244, 'tecno-camon-20-pro': 218,
    'tecno-camon-40': 267, 'samsung-galaxy-a27-5g': 279,
    'samsung-galaxy-a37-5g': 278, 'samsung-galaxy-a57-5g': 264,
    'samsung-galaxy-a54-5g': 31, 'poco-x7-pro': 273,
}


def load_profiles():
    profiles = json.loads(BATCH.read_text())['products']
    if (len(profiles) != len(EXPECTED_REFERENCES)
            or {p['slug']: p['id'] for p in profiles} != EXPECTED_REFERENCES
            or any(not p['targets'] or any(not _safe_url(u) for u in p['targets'])
                   for p in profiles)):
        raise ValueError('Reviewed eight-product scope is invalid')
    return profiles


def validate_catalog(profiles, catalog):
    by_slug = {p.slug: p for p in catalog}
    for profile in profiles:
        product = by_slug.get(profile['slug'])
        if not product or product.id != profile['id'] or product.name != profile['name']:
            raise ValueError(f"Catalog reference changed: {profile['slug']}")
        for key in ('ram_gb', 'storage_gb'):
            if (isinstance(product.attributes.get(key), bool)
                    or TRPriceScraper._spec_int(product.attributes.get(key)) != profile[key]):
                raise ValueError(f"Catalog variant changed: {profile['slug']} / {key}")
    return by_slug


def rejection_reason(profile, offer, now):
    scraper = TRPriceScraper()
    title = offer.get('source_title', '')
    if not scraper.has_observed_offer_evidence(profile['name'], offer, profile):
        return 'identity_or_variant_not_verified'
    if profile.get('require_explicit_radio') and scraper._radio_generation(title) != profile['network_generation']:
        return 'ambiguous_radio_variant'
    parsed = _safe_url(offer.get('url'))
    if not parsed or offer.get('merchant') != ALLOWED_HOSTS[parsed.hostname]:
        return 'merchant_or_url_not_verified'
    # A literal, reviewed warranty field on the same selected PDP is equally
    # valid evidence; never append it to or manufacture the retailer title.
    warranty = ''
    if (offer.get('evidence_method') == 'live_browser_selected_pdp'
            and isinstance(offer.get('warranty_evidence'), str)
            and offer['warranty_evidence'] in offer.get('proof_quote', '')):
        warranty = offer['warranty_evidence']
    normalized = scraper._normalize_words(title + ' ' + warranty)
    if re.search(r'\b(?:ithalatci|ithalat|yurt disi|global garanti)\b', normalized):
        return 'warranty_conflict'
    if not re.search(r'\b(?:turkiye|tr)\s+garantili\b', normalized):
        return 'turkey_warranty_not_explicit'
    if offer.get('availability') not in {'in_stock', 'limited'}:
        return 'not_currently_purchasable'
    price = offer.get('price')
    if (isinstance(price, bool) or not isinstance(price, (int, float))
            or not math.isfinite(price) or not 0 < price <= 5_000_000
            or offer.get('currency') != 'TRY'):
        return 'not_an_exact_try_cash_price'
    try:
        observed = datetime.fromisoformat(offer['observed_at'].replace('Z', '+00:00'))
        age = (now - observed).total_seconds()
        if age < -60 or age > 900:
            return 'observation_not_from_this_run'
    except (KeyError, TypeError, ValueError):
        return 'invalid_observation_time'
    return None


def validate_browser_evidence(profiles, observations, now):
    """Manual live UI observations, never cached search snippets or daily seeds.

    This opt-in batch expires in 15 minutes just like HTTP observations. The
    selected title/price/buy control and actual URL were read in the browser;
    they are not invented to repair missing retailer structured data.
    """
    profiles_by_slug = {p['slug']: p for p in profiles}
    for row in observations:
        profile = profiles_by_slug.get(row.get('product_slug'))
        if (not profile or row.get('product_id') != profile['id']
                or row.get('url') not in profile['targets']
                or row.get('evidence_method') != 'live_browser_selected_pdp'
                or row.get('purchase_enabled') is not True
                or not isinstance(row.get('selected_seller'), str)
                or not row['selected_seller'].strip()):
            raise ValueError('Unreviewed browser evidence scope or purchase state')
        proof = row.get('proof_quote', '')
        price_text = row.get('displayed_cash_price', '')
        if (not isinstance(proof, str) or row.get('source_title', '') not in proof
                or row['selected_seller'] not in proof or price_text not in proof
                or not re.search(r'sepete ekle', proof, re.I)
                or not re.fullmatch(r'\d{1,3}(?:\.\d{3})*(?:,\d{2})? TL', price_text)
                or float(price_text[:-3].replace('.', '').replace(',', '.')) != row.get('price')):
            raise ValueError('Browser quote does not support exact selected cash offer')
        reason = rejection_reason(profile, row, now)
        if reason:
            raise ValueError(f"Browser evidence rejected: {profile['slug']} / {reason}")


def recover(profiles, catalog, target_catalog, refresher, writer=None, now=None, browser_evidence=()):
    by_slug = validate_catalog(profiles, catalog)  # fail whole batch before any writes
    validate_browser_evidence(profiles, browser_evidence, now or datetime.now(timezone.utc))
    targets_by_slug = {p['slug']: p for p in target_catalog}
    rows, payload = [], []
    for profile in profiles:
        product = by_slug[profile['slug']]
        existing = targets_by_slug.get(profile['slug'], {})
        if existing.get('id') != product.id:
            raise ValueError(f"Offer catalog reference changed: {profile['slug']}")
        # Reviewed new merchants first, then stored PDPs. Neither is price proof.
        targets = list(dict.fromkeys(profile['targets'] + [
            t['source_url'] for t in existing.get('offer_targets', [])
            if _safe_url(t.get('source_url'))]))[:12]
        observations = refresher.refresh_existing_offer_targets(profile['name'], targets, profile)
        observations += [dict(row) for row in browser_evidence if row['product_slug'] == profile['slug']]
        decisions, best = [], {}
        for offer in observations:
            reason = rejection_reason(profile, offer, now or datetime.now(timezone.utc))
            reviewed_selected = next((b for b in browser_evidence
                if b['product_slug'] == profile['slug'] and b['url'] == offer.get('url')), None)
            if (reviewed_selected and offer.get('evidence_method') != 'live_browser_selected_pdp'
                    and offer.get('price') != reviewed_selected['price']):
                # A known conditional basket/coupon price must not win the
                # minimum merely because JSON-LD omits its conditions.
                reason = 'price_conflicts_with_reviewed_selected_cash_offer'
            decisions.append({**offer, 'decision': reason or 'eligible'})
            if reason:
                continue
            merchant = offer['merchant']
            if merchant not in best or offer['price'] < best[merchant]['price']:
                best[merchant] = offer
        for offer in best.values():
            payload.append({
                'product_id': product.id, 'product_slug': product.slug,
                'merchant_name': offer['merchant'], 'price': offer['price'],
                'currency': 'TRY', 'availability': offer['availability'],
                'source_url': offer['url'], 'source_title': offer['source_title'],
                'observed_at': offer['observed_at'], 'checked_at': offer['observed_at'],
                'is_official': False,
            })
        rows.append({
            'slug': product.slug, 'id': product.id,
            'variant': {key: profile[key] for key in ('ram_gb', 'storage_gb', 'network_generation')},
            'quality_status': product.data_quality_status,
            'quality_issues': list(product.data_quality_issues),
            'spec_verified_at': product.spec_verified_at,
            'targets': targets, 'observations': decisions,
            'eligible_merchants': sorted(best),
            'fresh_pair_observed': len(best) >= 2,
            'unresolved': ([] if observations else ['no_unambiguous_live_pdp_offer'])
                + ([] if len(best) >= 2 else ['two_current_same_variant_merchants_not_verified']),
        })
    # Recheck manual warranty evidence and freshness at the actual write boundary.
    validate_browser_evidence(profiles, browser_evidence, now or datetime.now(timezone.utc))
    for profile in profiles:
        for row in [p for p in payload if p['product_slug'] == profile['slug']]:
            original_browser = next((b for b in browser_evidence
                if b['product_slug'] == row['product_slug'] and b['url'] == row['source_url']
                and b['source_title'] == row['source_title'] and b['observed_at'] == row['observed_at']), {})
            reason = rejection_reason(profile, {
                **original_browser, **row, 'merchant': row['merchant_name'], 'url': row['source_url'],
            }, now or datetime.now(timezone.utc))
            if reason:
                raise ValueError(f"Observation expired before submission: {profile['slug']} / {reason}")
    report = {'mode': 'apply' if writer else 'audit', 'products': rows,
              'eligible_offers': len(payload), 'submitted_offers': 0,
              'ingestion': None}
    if writer and payload:
        report['ingestion'] = writer.ingest_offers(payload)
        report['submitted_offers'] = len(payload)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Submit only fresh exact observed offers')
    parser.add_argument('--include-reviewed-browser-evidence', action='store_true',
                        help='Opt in to this session\'s reviewed live UI observations; expired evidence fails closed')
    args = parser.parse_args()
    offer_client = OfferIngestionClient.from_env()
    source_client = SourceIngestionClient.from_env()
    report = recover(load_profiles(), source_client.fetch_catalog(page_size=500),
                     offer_client.get_published_products(), RetailerOfferRefresher(),
                     offer_client if args.apply else None,
                     browser_evidence=(json.loads(BROWSER_EVIDENCE.read_text())['observations']
                                       if args.include_reviewed_browser_evidence else ()))
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
