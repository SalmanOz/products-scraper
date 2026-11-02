"""Manual, exact-one Fold7 marketplace offer gate.

The default workflow dispatch only audits. A publish dispatch repeats the
selected PDP, legal seller page, and catalog proof in the same run before one
ingestion record. This module never adds a daily refresh seed.
"""

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path

from audit_mediamarkt_fold7_readonly import (
    CATALOG, SELLER, SELLER_URL, TITLE, URL, exact_fold7_offer, run_audit,
)
from coverage_two_marketplace import _collision_evidence
from offer_ingestion_client import OfferIngestionClient
from update_prices import PriceUpdater


PURPOSE = 'manual_exact_fold7_marketplace_offer'
MAX_AGE_SECONDS = 300
PROFILE = {**CATALOG, 'seller_name': SELLER, 'seller_id': '12945',
           'source_title': TITLE, 'sku': '167997743',
           'gtin13': '8806097425007'}
OFFER_IDENTITY_FIELDS = (
    'url', 'platform', 'merchant', 'seller_name', 'seller_id', 'sku',
    'gtin13', 'selected_variant_gtin13', 'source_title', 'ram_gb',
    'storage_gb', 'network_generation', 'condition', 'availability',
    'currency', 'offer_id', 'price',
)


def _utc(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('timezone missing')
    return stamp.astimezone(timezone.utc)


def _catalog(client):
    """Check current unpriced SKU and every stored seller target, not hints."""
    products = client.get_published_products()
    prices = client.get_public_price_priority()
    if not isinstance(products, list) or not isinstance(prices, dict):
        raise ValueError('catalog_unavailable')
    rows = [row for row in products if isinstance(row, dict)
            and row.get('id') == CATALOG['id']]
    if len(rows) != 1:
        raise ValueError('catalog_identity_missing_or_duplicate')
    product = rows[0]
    specs = PriceUpdater.get_expected_specs(product.get('attributes'))
    if (product.get('slug') != CATALOG['slug']
            or product.get('name') != CATALOG['name']
            or specs.get('ram_gb') != CATALOG['ram_gb']
            or specs.get('storage_gb') != CATALOG['storage_gb']
            or specs.get('network_generation') not in (None, '5g')
            or prices.get(CATALOG['id']) != (CATALOG['slug'], False)):
        raise ValueError('catalog_identity_variant_or_unpriced_status_changed')
    initial_targets = product.get('offer_targets')
    if not isinstance(initial_targets, list):
        raise ValueError('catalog_targets_unavailable')
    complete = client.get_complete_offer_targets(CATALOG['id'])
    if (any(complete.get(key) != product.get(key)
            for key in ('id', 'slug', 'name'))
            or complete.get('offer_targets_truncated') is not False
            or complete.get('offer_targets', [])[:len(initial_targets)] != initial_targets):
        raise ValueError('complete_catalog_target_snapshot_changed')
    full_product = {**product, **complete}
    collision = _collision_evidence(full_product, PROFILE, URL)
    if collision['blocked'] or collision['reasons']:
        raise ValueError('catalog_seller_or_target_collision')
    raw = client.get_raw_seller_collision_proof(CATALOG['id'], SELLER, URL)
    if (raw.get('complete') is not True or raw.get('blocked') is not False
            or raw.get('reasons') != []
            or raw.get('unknown_seller_rows') != 0
            or raw.get('invalid_row_identity_rows') != 0
            or raw.get('proposed_seller_other_url_rows') != 0
            or raw.get('refresh_targets_invalid_count') != 0
            or raw.get('refresh_targets_duplicate_count') != 0
            or raw.get('refresh_targets_total') != len(complete['offer_targets'])
            or raw.get('stored_rows_checked') != len(complete['offer_targets'])
            or raw.get('refresh_targets_snapshot') !=
                complete.get('offer_targets_snapshot')):
        raise ValueError('raw_seller_or_target_collision')
    return product, {'target_count': len(complete['offer_targets']),
                     'target_snapshot': complete['offer_targets_snapshot'],
                     'raw_seller_proof': raw}


def _source_offer(source_audit):
    if (not isinstance(source_audit, dict)
            or source_audit.get('purpose') !=
                'read_only_exact_fold7_marketplace_audit'
            or source_audit.get('writes') is not False
            or source_audit.get('catalog') != CATALOG
            or source_audit.get('url') != URL
            or source_audit.get('seller_url') != SELLER_URL
            or source_audit.get('robots_allowed') is not True
            or source_audit.get('verified') is not True
            or source_audit.get('reason') !=
                'selected_new_cash_offer_and_legal_seller_verified'
            or source_audit.get('pdp_http_status') != 200
            or source_audit.get('seller_http_status') != 200
            or not exact_fold7_offer(source_audit.get('offer'))):
        return None
    try:
        observed = _utc(source_audit['pdp_observed_at'])
        checked = _utc(source_audit['checked_at'])
        if not 0 <= (checked - observed).total_seconds() <= MAX_AGE_SECONDS:
            return None
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None
    return {**source_audit['offer'], 'merchant': SELLER,
            'observed_at': source_audit['pdp_observed_at']}


def audit(*, client=None, source=None):
    client = client or OfferIngestionClient.from_env()
    report = {'purpose': PURPOSE, 'writes': False, 'status': 'failed',
              'workflow_run_id': os.environ.get('GITHUB_RUN_ID'),
              'checked_at': None, 'catalog_status': 'failed',
              'catalog_error': None, 'catalog_evidence': None,
              'source_audit': None, 'verified_offer': None,
              'product': {'id': CATALOG['id'], 'slug': CATALOG['slug'],
                          'name': CATALOG['name'], 'expected_specs': {
                              key: CATALOG[key] for key in
                              ('ram_gb', 'storage_gb', 'network_generation')}}}
    try:
        _, report['catalog_evidence'] = _catalog(client)
        report['catalog_status'] = 'passed'
    except Exception as error:
        report['catalog_error'] = (str(error) if isinstance(error, ValueError)
                                   else type(error).__name__)
    if report['catalog_status'] == 'passed':
        report['source_audit'] = run_audit(source)
        report['verified_offer'] = _source_offer(report['source_audit'])
    if report['catalog_status'] == 'passed' and report['verified_offer'] is not None:
        report['status'] = 'passed'
    report['checked_at'] = datetime.now(timezone.utc).isoformat()
    return report


def verified_offer(report, now=None):
    try:
        now = now or datetime.now(timezone.utc)
        if (not isinstance(report, dict) or report.get('purpose') != PURPOSE
                or report.get('writes') is not False
                or report.get('status') != 'passed'
                or report.get('catalog_status') != 'passed'
                or report.get('catalog_error') is not None
                or now.tzinfo is None):
            return None
        run_id = os.environ.get('GITHUB_RUN_ID')
        if run_id and report.get('workflow_run_id') != run_id:
            return None
        product = report.get('product')
        proof = report.get('catalog_evidence')
        offer = report.get('verified_offer')
        source_offer = _source_offer(report.get('source_audit'))
        if (product != {'id': CATALOG['id'], 'slug': CATALOG['slug'],
                        'name': CATALOG['name'], 'expected_specs': {
                            key: CATALOG[key] for key in
                            ('ram_gb', 'storage_gb', 'network_generation')}}
                or not isinstance(proof, dict)
                or type(proof.get('target_count')) is not int
                or not 0 <= proof['target_count'] <= 60
                or not isinstance(proof.get('target_snapshot'), str)
                or not isinstance(proof.get('raw_seller_proof'), dict)
                or proof['raw_seller_proof'].get('blocked') is not False
                or proof['raw_seller_proof'].get('refresh_targets_snapshot') !=
                    proof['target_snapshot']
                or not isinstance(offer, dict) or source_offer is None
                or any(offer.get(key) != source_offer.get(key)
                       for key in (*OFFER_IDENTITY_FIELDS, 'observed_at'))):
            return None
        checked = _utc(report['checked_at'])
        observed = _utc(offer['observed_at'])
        if (not -60 <= (now - checked).total_seconds() <= MAX_AGE_SECONDS
                or not -60 <= (now - observed).total_seconds() <= MAX_AGE_SECONDS
                or not 0 <= (checked - observed).total_seconds() <= MAX_AGE_SECONDS):
            return None
        return offer
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None


def _readback_matches(identity, offer, now=None):
    now = now or datetime.now(timezone.utc)
    if (not isinstance(identity, dict)
            or identity.get('evidenceVersion') != 'product-offer-match-v1'
            or identity.get('mode') != 'shadow_audit_only'
            or not isinstance(identity.get('productCoverage'), list)):
        return False
    try:
        checked = _utc(identity['auditedAt'])
        if (not -60 <= (now - checked).total_seconds() <= MAX_AGE_SECONDS
                or checked < _utc(offer['observed_at'])):
            return False
        coverage = [row for row in identity['productCoverage']
                    if isinstance(row, dict) and row.get('slug') == CATALOG['slug']]
        if len(coverage) != 1 or not isinstance(coverage[0].get('matchedOffers'), list):
            return False
        matches = [row for row in coverage[0]['matchedOffers']
                   if isinstance(row, dict) and row.get('source_url') == URL]
        return (len(matches) == 1
                and matches[0].get('merchant_name') == SELLER
                and Decimal(str(matches[0].get('price'))) ==
                    Decimal(str(offer['price'])))
    except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError):
        return False


def publish(report, *, client=None, source=None, now=None):
    """Submit at most one offer after a second exact live source check."""
    audited = verified_offer(report, now)
    if audited is None:
        raise ValueError('exact_fold7_audit_missing_or_stale')
    live = _source_offer(run_audit(source))
    if (live is None or any(live.get(key) != audited.get(key)
                            for key in OFFER_IDENTITY_FIELDS)):
        raise ValueError('exact_fold7_live_offer_changed')
    if not -60 <= (datetime.now(timezone.utc) - _utc(live['observed_at'])).total_seconds() <= MAX_AGE_SECONDS:
        raise ValueError('exact_fold7_live_offer_stale')
    client = client or OfferIngestionClient.from_env()
    product, proof = _catalog(client)
    if proof != report.get('catalog_evidence'):
        raise ValueError('exact_fold7_catalog_or_seller_proof_changed')
    prepared = PriceUpdater(ingestion_client=client).prepare_product_offers(
        product, [live], live['observed_at'], live['observed_at'])
    if len(prepared) != 1:
        raise ValueError('prepared_offer_count_changed')
    record = prepared[0]
    if (record.get('product_id') != CATALOG['id']
            or record.get('product_slug') != CATALOG['slug']
            or record.get('source_url') != URL
            or record.get('seller') != SELLER
            or Decimal(str(record.get('price'))) != Decimal(str(live['price']))
            or record.get('currency') != 'TRY'
            or record.get('availability') != 'in_stock'
            or record.get('source_title') != TITLE
            or record.get('observed_at') != live['observed_at']
            or record.get('checked_at') != live['observed_at']
            or record.get('is_official') is not False):
        raise ValueError('prepared_offer_changed')
    result = client.ingest_offers([record])
    if (not isinstance(result, dict) or result.get('committed') is not True
            or type(result.get('accepted')) is not int or result['accepted'] != 1
            or type(result.get('stale_ignored')) is not int
            or result['stale_ignored'] != 0
            or type(result.get('variant_rejected')) is not int
            or result['variant_rejected'] != 0):
        raise RuntimeError('exact_fold7_offer_not_accepted')
    if not _readback_matches(client.get_offer_identity_readiness(), live):
        raise RuntimeError('commercial_readback_missing_exact_fold7_offer')
    return {'status': 'passed', 'submitted': 1, 'accepted': 1,
            'slug': CATALOG['slug'], 'seller': SELLER,
            'source_url': URL, 'price': live['price'],
            'readback': 'exact_selected_offer'}


def summary(report):
    source = report.get('source_audit') or {}
    offer = report.get('verified_offer') or {}
    return (f"Fold7 12/256 tek SKU denetimi: {report['status']}; "
            f"katalog {report['catalog_status']}; "
            f"kaynak {source.get('reason') or 'okunmadı'}; "
            f"{offer.get('price') or '?'} TRY. "
            f"{report.get('catalog_error') or ''}\nDenetimde yazım yok.\n")


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='action', required=True)
    read = sub.add_parser('audit')
    read.add_argument('--audit-file', type=Path, required=True)
    write = sub.add_parser('publish')
    write.add_argument('--audit-file', type=Path, required=True)
    write.add_argument('--output-file', type=Path, required=True)
    write.add_argument('--publish-exact-one', action='store_true', required=True)
    args = parser.parse_args(argv)
    if args.action == 'audit':
        report = audit()
        args.audit_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        args.audit_file.with_suffix('.md').write_text(summary(report))
        print(summary(report), end='')
        return 0 if report['status'] == 'passed' else 1
    report = json.loads(args.audit_file.read_text())
    result = publish(report)
    args.output_file.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
