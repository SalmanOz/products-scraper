"""Manual-only audit and one-off publication for the exact POCO X6 Pro PDP.

The default workflow dispatch is a read-only audit. The publish command is intended only
for an explicitly approved workflow dispatch that creates its own audit in the
same job; it never reads a remembered or search-result price.
"""

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path

from coverage_two_marketplace import (PROFILES, _collision_evidence,
                                      _offer_matches, _read_selected)
from mediamarkt_source import MediaMarktSource
from offer_ingestion_client import OfferIngestionClient
from update_prices import PriceUpdater


URL = next(url for url, profile in PROFILES.items() if profile['slug'] == 'poco-x6-pro')
PROFILE = PROFILES[URL]
PURPOSE = 'read_only_poco_x6_pro_exact_marketplace_offer'
MAX_AGE_SECONDS = 300


def _utc(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('timezone missing')
    return stamp.astimezone(timezone.utc)


def _catalog(client):
    products = client.get_published_products()
    prices = client.get_public_price_priority()
    if not isinstance(products, list) or not isinstance(prices, dict):
        raise ValueError('catalog_unavailable')
    rows = [row for row in products if isinstance(row, dict)
            and row.get('id') == PROFILE['id']]
    if len(rows) != 1:
        raise ValueError('catalog_identity_missing_or_duplicate')
    product = rows[0]
    specs = PriceUpdater.get_expected_specs(product.get('attributes'))
    expected = {key: PROFILE[key] for key in
                ('ram_gb', 'storage_gb', 'network_generation')}
    if (product.get('slug') != PROFILE['slug']
            or product.get('name') != PROFILE['name']
            or specs != expected
            or prices.get(PROFILE['id']) != (PROFILE['slug'], False)
            or product.get('offer_targets_truncated') is not False):
        raise ValueError('catalog_variant_price_or_target_completeness_changed')
    collision = _collision_evidence(product, PROFILE, URL)
    if collision['blocked']:
        raise ValueError('catalog_seller_or_target_collision')
    return product, collision


def audit(*, client=None, source=None):
    client = client or OfferIngestionClient.from_env()
    source = source or MediaMarktSource()
    report = {'purpose': PURPOSE, 'writes': False, 'status': 'failed',
              'checked_at': None, 'robots_status': None, 'catalog_status': 'failed',
              'catalog_error': None, 'catalog_collision': None,
              'source_attempt': None, 'verified_offer': None, 'source': None,
              'product': {'id': PROFILE['id'], 'slug': PROFILE['slug'],
                          'name': PROFILE['name'],
                          'expected_specs': {key: PROFILE[key] for key in
                                             ('ram_gb', 'storage_gb',
                                              'network_generation')}}}
    try:
        _, report['catalog_collision'] = _catalog(client)
        report['catalog_status'] = 'passed'
    except Exception as error:
        report['catalog_error'] = (str(error) if isinstance(error, ValueError)
                                   else type(error).__name__)
    offer, report['source_attempt'] = _read_selected(source, URL)
    if _offer_matches(offer, URL, PROFILE):
        report['verified_offer'] = offer
    report['robots_status'] = source.report().get('robots_status')
    if (report['catalog_status'] == 'passed'
            and report['verified_offer'] is not None
            and report['robots_status'] == 'ready'):
        report['status'] = 'passed'
    report['checked_at'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    report['source'] = source.report()
    return report


def verified_offer(report, now=None):
    try:
        now = now or datetime.now(timezone.utc)
        if (not isinstance(report, dict) or report.get('purpose') != PURPOSE
                or report.get('writes') is not False or report.get('status') != 'passed'
                or report.get('catalog_status') != 'passed'
                or report.get('catalog_error') is not None
                or report.get('robots_status') != 'ready'
                or now.tzinfo is None):
            return None
        checked = _utc(report['checked_at'])
        if not -60 <= (now - checked).total_seconds() <= MAX_AGE_SECONDS:
            return None
        product = report.get('product')
        collision = report.get('catalog_collision')
        attempt = report.get('source_attempt')
        offer = report.get('verified_offer')
        if (not isinstance(product, dict)
                or any(product.get(key) != PROFILE[key]
                       for key in ('id', 'slug', 'name'))
                or product.get('expected_specs') != {key: PROFILE[key] for key in
                    ('ram_gb', 'storage_gb', 'network_generation')}
                or not isinstance(collision, dict)
                or collision.get('blocked') is not False
                or collision.get('offer_targets_truncated') is not False
                or type(collision.get('target_count')) is not int
                or not 0 <= collision['target_count'] <= 12
                or not isinstance(attempt, dict)
                or attempt.get('source_url') != URL
                or attempt.get('reason') != 'observed_exact_new_marketplace_offer'
                or attempt.get('http_status') != 200
                or not _offer_matches(offer, URL, PROFILE)):
            return None
        observed = _utc(offer['observed_at'])
        if (not -60 <= (now - observed).total_seconds() <= MAX_AGE_SECONDS
                or not -60 <= (checked - observed).total_seconds()
                <= MAX_AGE_SECONDS):
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
        if not -60 <= (now - checked).total_seconds() <= MAX_AGE_SECONDS:
            return False
        if checked < _utc(offer['observed_at']):
            return False
        coverage = [row for row in identity['productCoverage']
                    if isinstance(row, dict) and row.get('slug') == PROFILE['slug']]
        if len(coverage) != 1 or not isinstance(coverage[0].get('matchedOffers'), list):
            return False
        exact = [row for row in coverage[0]['matchedOffers']
                 if isinstance(row, dict) and row.get('source_url') == URL]
        return bool(len(exact) == 1
                    and exact[0].get('merchant_name') == PROFILE['seller_name']
                    and Decimal(str(exact[0].get('price'))) ==
                        Decimal(str(offer['price'])))
    except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError):
        return False


def publish(report, *, client=None, now=None):
    """One narrowly scoped write after same-run audit and current catalog recheck."""
    now = now or datetime.now(timezone.utc)
    offer = verified_offer(report, now)
    if offer is None:
        raise ValueError('exact_poco_audit_missing_or_stale')
    client = client or OfferIngestionClient.from_env()
    product, _ = _catalog(client)
    prepared = PriceUpdater(ingestion_client=client).prepare_product_offers(
        product, [offer], offer['observed_at'], offer['observed_at'])
    if len(prepared) != 1:
        raise ValueError('prepared_offer_count_changed')
    record = prepared[0]
    if (record.get('product_id') != PROFILE['id']
            or record.get('product_slug') != PROFILE['slug']
            or record.get('source_url') != URL
            or record.get('seller') != PROFILE['seller_name']
            or record.get('price') != offer['price']
            or record.get('currency') != 'TRY'
            or record.get('availability') != 'in_stock'
            or record.get('source_title') != PROFILE['source_title']
            or record.get('observed_at') != offer['observed_at']
            or record.get('checked_at') != offer['observed_at']
            or record.get('is_official') is not False):
        raise ValueError('prepared_offer_changed')
    result = client.ingest_offers([record])
    if (not isinstance(result, dict) or result.get('committed') is not True
            or result.get('accepted') != 1 or result.get('stale_ignored') != 0
            or result.get('variant_rejected') != 0):
        raise RuntimeError('exact_poco_offer_not_accepted')
    if not _readback_matches(client.get_offer_identity_readiness(), offer):
        raise RuntimeError('commercial_readback_missing_exact_poco_offer')
    return {'status': 'passed', 'submitted': 1, 'accepted': 1,
            'slug': PROFILE['slug'], 'seller': PROFILE['seller_name'],
            'source_url': URL, 'price': offer['price'],
            'readback': 'exact_selected_offer'}


def summary(report):
    offer = report.get('verified_offer') or {}
    attempt = report.get('source_attempt') or {}
    collision = report.get('catalog_collision') or {}
    return (f"POCO X6 Pro 8/256 MediaMarkt tek SKU denetimi: {report['status']}; "
            f"robots {report.get('robots_status')}; katalog "
            f"{report.get('catalog_status')}.\n"
            f"- Kaynak: {attempt.get('reason')} HTTP {attempt.get('http_status')}; "
            f"{offer.get('seller_name') or '?'} {offer.get('price') or '?'} TRY; "
            f"SKU {offer.get('sku') or '?'}, GTIN {offer.get('gtin13') or '?'}, "
            f"şebeke {offer.get('network_generation') or '?'}, "
            f"stok {offer.get('availability') or '?'}.\n"
            f"- Katalog hedefleri: {collision.get('target_count')}; "
            f"kırpılmış {collision.get('offer_targets_truncated')}; "
            f"{report.get('catalog_error') or ', '.join(collision.get('reasons', [])) or 'çakışma yok'}.\n"
            "Denetimde yazım yok.\n")


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
