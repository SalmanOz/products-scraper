"""Manual-only audit and one-off publication for the exact Redmi PDP.

The default workflow dispatch is a read-only audit. The publish command is intended only
for an explicitly approved workflow dispatch that creates its own audit in the
same job; it never reads a remembered or search-result price.
"""

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path

from coverage_two_marketplace import PROFILES, _offer_matches, _read_selected
from mediamarkt_source import MediaMarktSource
from offer_ingestion_client import OfferIngestionClient
from update_prices import PriceUpdater


URL = next(url for url, profile in PROFILES.items()
           if profile['slug'] == 'xiaomi-redmi-note-15-pro')
PROFILE = PROFILES[URL]
PURPOSE = 'read_only_redmi_note_15_pro_exact_marketplace_offer'
EXPECTED_PRICE = Decimal('30499')
MAX_AGE_SECONDS = 300
OFFER_IDENTITY_FIELDS = ('url', 'platform', 'merchant', 'seller_name',
                         'seller_id', 'sku', 'gtin13', 'source_title', 'ram_gb',
                         'storage_gb', 'network_generation', 'condition',
                         'availability', 'currency', 'offer_id', 'price')


def _exact_price(offer):
    try:
        return not isinstance(offer.get('price'), bool) and Decimal(str(offer['price'])) == EXPECTED_PRICE
    except (KeyError, TypeError, InvalidOperation, AttributeError):
        return False


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
            or prices.get(PROFILE['id']) != (PROFILE['slug'], False)):
        raise ValueError('catalog_variant_or_unpriced_status_changed')
    default_targets = product.get('offer_targets')
    if (not isinstance(default_targets, list) or len(default_targets) != 12
            or product.get('offer_targets_truncated') is not True):
        raise ValueError('default_target_count_or_truncation_changed')
    inspected = client.get_offer_target_inspection(PROFILE['id'])
    if (inspected.get('id') != PROFILE['id']
            or inspected.get('slug') != PROFILE['slug']
            or inspected.get('name') != PROFILE['name']
            or inspected.get('attributes') != product.get('attributes')
            or inspected.get('offer_targets') != default_targets
            or inspected.get('offer_targets_total') != 12
            or inspected.get('offer_targets_invalid_count') != 1
            or inspected.get('offer_targets_duplicate_count') != 0
            or inspected.get('offer_targets_truncated') is not True):
        raise ValueError('target_page_or_rejected_row_count_changed')
    reasons = inspected['offer_targets_invalid_reasons']
    # With zero generally invalid URLs and zero unknown sellers in the raw
    # proof, only these stricter refresh-policy rejects explain the lost row.
    if (sum(reasons.values()) != 1
            or reasons['unsupported_host'] + reasons['retailer_query_restricted'] != 1):
        raise ValueError('target_rejection_reason_count_changed')
    # Safe refresh hints cannot attest to the rejected row's seller. The
    # separate raw proof inspects all 13 stored sellers before URL filtering.
    if any((row.get('seller_name') or row.get('merchant_name') or '').strip().casefold()
           == PROFILE['seller_name'].casefold() and row.get('source_url') != URL
           for row in default_targets):
        raise ValueError('safe_target_seller_collision')
    raw = client.get_raw_seller_collision_proof(
        PROFILE['id'], PROFILE['seller_name'], URL)
    if (raw.get('complete') is not True or raw.get('blocked') is not False
            or raw.get('reasons') != [] or raw.get('stored_rows_checked') != 13
            or raw.get('unknown_seller_rows') != 0
            or raw.get('invalid_url_rows') != 0
            or raw.get('invalid_row_identity_rows') != 0
            or raw.get('proposed_seller_other_url_rows') != 0
            or raw.get('refresh_targets_total') != 12
            or raw.get('refresh_targets_invalid_count') != 1
            or raw.get('refresh_targets_duplicate_count') != 0
            or raw.get('refresh_targets_invalid_reasons') != reasons
            or raw.get('refresh_targets_snapshot') != inspected.get('offer_targets_snapshot')):
        raise ValueError('raw_seller_or_target_snapshot_changed')
    return product, {'target_count': 12, 'invalid_count': 1,
                     'duplicate_count': 0, 'invalid_reasons': reasons,
                     'target_snapshot': inspected['offer_targets_snapshot'],
                     'raw_seller_proof': raw}


def audit(*, client=None, source=None):
    client = client or OfferIngestionClient.from_env()
    source = source or MediaMarktSource()
    report = {'purpose': PURPOSE, 'writes': False, 'status': 'failed',
              'checked_at': None, 'robots_status': None, 'catalog_status': 'failed',
              'catalog_error': None, 'catalog_evidence': None,
              'source_attempt': None, 'verified_offer': None, 'source': None,
              'product': {'id': PROFILE['id'], 'slug': PROFILE['slug'],
                          'name': PROFILE['name'],
                          'expected_specs': {key: PROFILE[key] for key in
                                             ('ram_gb', 'storage_gb',
                                              'network_generation')}}}
    try:
        _, report['catalog_evidence'] = _catalog(client)
        report['catalog_status'] = 'passed'
    except Exception as error:
        report['catalog_error'] = (str(error) if isinstance(error, ValueError)
                                   else type(error).__name__)
    offer, report['source_attempt'] = _read_selected(source, URL)
    if _offer_matches(offer, URL, PROFILE) and _exact_price(offer):
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
        evidence = report.get('catalog_evidence')
        attempt = report.get('source_attempt')
        offer = report.get('verified_offer')
        if (not isinstance(product, dict)
                or any(product.get(key) != PROFILE[key]
                       for key in ('id', 'slug', 'name'))
                or product.get('expected_specs') != {key: PROFILE[key] for key in
                    ('ram_gb', 'storage_gb', 'network_generation')}
                or not isinstance(evidence, dict)
                or evidence.get('target_count') != 12
                or evidence.get('invalid_count') != 1
                or evidence.get('duplicate_count') != 0
                or not isinstance(evidence.get('invalid_reasons'), dict)
                or sum(evidence['invalid_reasons'].values()) != 1
                or evidence['invalid_reasons'].get('unsupported_host', 0) +
                   evidence['invalid_reasons'].get('retailer_query_restricted', 0) != 1
                or not isinstance(evidence.get('raw_seller_proof'), dict)
                or evidence.get('target_snapshot') !=
                    evidence['raw_seller_proof'].get('refresh_targets_snapshot')
                or not isinstance(attempt, dict)
                or attempt.get('source_url') != URL
                or attempt.get('reason') != 'observed_exact_new_marketplace_offer'
                or attempt.get('http_status') != 200
                or not _offer_matches(offer, URL, PROFILE)
                or not _exact_price(offer)):
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
                    and Decimal(str(exact[0].get('price'))) == EXPECTED_PRICE)
    except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError):
        return False


def publish(report, *, client=None, source=None, now=None):
    """One write after a second selected PDP and every current proof recheck."""
    now = now or datetime.now(timezone.utc)
    audited = verified_offer(report, now)
    if audited is None:
        raise ValueError('exact_redmi_audit_missing_or_stale')
    source = source or MediaMarktSource()
    offer, attempt = _read_selected(source, URL)
    if (attempt.get('reason') != 'observed_exact_new_marketplace_offer'
            or attempt.get('http_status') != 200
            or source.report().get('robots_status') != 'ready'
            or not _offer_matches(offer, URL, PROFILE)
            or not _exact_price(offer)
            or any(offer.get(key) != audited.get(key)
                   for key in OFFER_IDENTITY_FIELDS)):
        raise ValueError('exact_redmi_live_offer_changed')
    client = client or OfferIngestionClient.from_env()
    product, evidence = _catalog(client)
    if evidence != report.get('catalog_evidence'):
        raise ValueError('exact_redmi_catalog_or_raw_proof_changed')
    prepared = PriceUpdater(ingestion_client=client).prepare_product_offers(
        product, [offer], offer['observed_at'], offer['observed_at'])
    if len(prepared) != 1:
        raise ValueError('prepared_offer_count_changed')
    record = prepared[0]
    if (record.get('product_id') != PROFILE['id']
            or record.get('product_slug') != PROFILE['slug']
            or record.get('source_url') != URL
            or record.get('seller') != PROFILE['seller_name']
            or Decimal(str(record.get('price'))) != EXPECTED_PRICE
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
        raise RuntimeError('exact_redmi_offer_not_accepted')
    if not _readback_matches(client.get_offer_identity_readiness(), offer):
        raise RuntimeError('commercial_readback_missing_exact_redmi_offer')
    return {'status': 'passed', 'submitted': 1, 'accepted': 1,
            'slug': PROFILE['slug'], 'seller': PROFILE['seller_name'],
            'source_url': URL, 'price': offer['price'],
            'readback': 'exact_selected_offer'}


def summary(report):
    offer = report.get('verified_offer') or {}
    attempt = report.get('source_attempt') or {}
    evidence = report.get('catalog_evidence') or {}
    return (f"Redmi Note 15 Pro 5G 8/256 MediaMarkt tek SKU denetimi: {report['status']}; "
            f"robots {report.get('robots_status')}; katalog "
            f"{report.get('catalog_status')}.\n"
            f"- Kaynak: {attempt.get('reason')} HTTP {attempt.get('http_status')}; "
            f"{offer.get('seller_name') or '?'} {offer.get('price') or '?'} TRY; "
            f"SKU {offer.get('sku') or '?'}, GTIN {offer.get('gtin13') or '?'}, "
            f"şebeke {offer.get('network_generation') or '?'}, "
            f"stok {offer.get('availability') or '?'}.\n"
            f"- Katalog hedefleri: {evidence.get('target_count')}; "
            f"reddedilen {evidence.get('invalid_count')}; "
            f"nedenler {evidence.get('invalid_reasons') or '?'}; "
            f"{report.get('catalog_error') or 'ham satıcı kanıtı eşleşti'}.\n"
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
