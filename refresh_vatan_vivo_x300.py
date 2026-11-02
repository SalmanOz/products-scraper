"""Optional one-SKU Vatan offer write after a same-run exact PDP audit."""

import argparse
import json
import os
from pathlib import Path
from tempfile import gettempdir

from audit_vatan_vivo_exact_readonly import PROFILE, SPECS, URL, exact_catalog_product
from offer_ingestion_client import OfferIngestionClient
from update_prices import PriceUpdater
from vatan_vivo_x300_preflight import load_verified_offer


OUT = Path(os.environ.get('VATAN_VIVO_REFRESH_OUTPUT_DIR', gettempdir()))


class ScopedRefreshError(RuntimeError):
    pass


def readback(client, record):
    """Check the stored exact target and the site's matched-price projection."""
    product = exact_catalog_product(client.get_published_products())
    if (product is None or URL not in {target.get('source_url') for target in
                                    product.get('offer_targets', [])
                                    if isinstance(target, dict)}):
        raise ScopedRefreshError('exact_vivo_offer_target_not_stored')
    readiness = client._request_json(
        'GET', '/api/ingestion/price-index-readiness',
        headers={'Authorization': f'Bearer {client.secret}',
                 'Accept': 'application/json'},
    )
    identity = readiness.get('offerIdentity') if isinstance(readiness, dict) else None
    coverage = identity.get('productCoverage') if isinstance(identity, dict) else None
    rows = [row for row in coverage if isinstance(row, dict)
            and row.get('slug') == PROFILE['slug']] if isinstance(coverage, list) else []
    if len(rows) != 1:
        raise ScopedRefreshError('vivo_price_readback_missing')
    row = rows[0]
    lowest = row.get('matchedLowestPrice')
    matched = row.get('matchedOffers')
    exact = ([offer for offer in matched if isinstance(offer, dict)
              and offer.get('source_url') == URL]
             if isinstance(matched, list) else [])
    if (not isinstance(row.get('matchedMerchants'), int)
            or row['matchedMerchants'] < 1
            or not isinstance(lowest, (int, float)) or isinstance(lowest, bool)
            or not 0 < lowest <= record['price']
            or len(exact) != 1
            or exact[0].get('merchant_name') != 'Vatan Bilgisayar'
            or not isinstance(exact[0].get('price'), (int, float))
            or isinstance(exact[0]['price'], bool)
            or exact[0]['price'] != record['price']):
        raise ScopedRefreshError('vivo_offer_not_commercially_visible')
    return {'exact_target_stored': True, 'matched_merchants': row['matchedMerchants'],
            'matched_lowest_price': lowest, 'exact_offer_price': exact[0]['price']}


def prepare(audit_file, *, client=None, updater=None, now=None):
    offer = load_verified_offer(audit_file, now=now)
    if offer is None:
        raise ScopedRefreshError('same_run_exact_vivo_audit_invalid_or_stale')
    client = client or OfferIngestionClient.from_env()
    updater = updater or PriceUpdater(ingestion_client=client)
    product = exact_catalog_product(client.get_published_products())
    if product is None:
        raise ScopedRefreshError('published_catalog_vivo_variant_changed')
    if not updater.live_offer_is_valid(product, offer, SPECS):
        raise ScopedRefreshError('selected_vivo_offer_no_longer_valid')
    timestamp = updater.utc_timestamp()
    records = updater.prepare_product_offers(product, [offer], timestamp, timestamp)
    if (len(records) != 1 or records[0].get('product_id') != PROFILE['id']
            or records[0].get('product_slug') != PROFILE['slug']
            or records[0].get('seller') != 'Vatan Bilgisayar'
            or records[0].get('source_url') != URL
            or records[0].get('source_title') != offer['source_title']
            or records[0].get('availability') != 'in_stock'):
        raise ScopedRefreshError('vivo_offer_payload_scope_changed')
    return client, records[0]


def publish(audit_file, *, client=None, updater=None, now=None):
    client, record = prepare(audit_file, client=client, updater=updater, now=now)
    result = client.ingest_offers([record])
    if (result.get('committed') is not True or result.get('accepted') != 1
            or result.get('stale_ignored') != 0 or result.get('variant_rejected') != 0):
        raise ScopedRefreshError('exact_vivo_offer_not_accepted')
    return result, record, readback(client, record)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--audit-file', required=True)
    parser.add_argument('--publish', action='store_true',
                        help='Enable one scoped offer write after same-run preflight')
    args = parser.parse_args(argv)
    report = {'purpose': 'vatan_vivo_x300_scoped_offer_refresh',
              'mode': 'publish' if args.publish else 'preflight',
              'slug': PROFILE['slug'], 'writes': False}
    try:
        if args.publish:
            report['writes'] = True
            result, record, verified_readback = publish(args.audit_file)
            report['ingestion'] = {key: result.get(key) for key in
                                   ('committed', 'accepted', 'stale_ignored',
                                    'variant_rejected')}
            report['readback'] = verified_readback
        else:
            _, record = prepare(args.audit_file)
        report['offer'] = {key: record.get(key) for key in
                           ('product_id', 'product_slug', 'seller', 'price',
                            'currency', 'availability', 'source_url',
                            'source_title', 'observed_at')}
        report['status'] = 'passed'
        code = 0
    except Exception as error:
        report['status'] = 'failed'
        report['reason'] = (str(error) if isinstance(error, ScopedRefreshError)
                            else type(error).__name__)
        code = 1
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'vatan-vivo-x300-refresh.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    lines = ['## Vivo X300 16/512GB Vatan offer refresh',
             f"Mode: {report['mode']}; status: {report['status']}; writes: {report['writes']}."]
    if 'offer' in report:
        lines.append(f"Cash: {report['offer']['price']} TRY; {URL}")
    if 'ingestion' in report:
        lines.append(f"Accepted: {report['ingestion']['accepted']}/1.")
    if 'readback' in report:
        lines.append(f"Commercial readback: {report['readback']['matched_merchants']} merchant; "
                     f"exact Vatan {report['readback']['exact_offer_price']} TRY; "
                     f"lowest {report['readback']['matched_lowest_price']} TRY.")
    if 'reason' in report:
        lines.append('Reason: ' + report['reason'])
    (OUT / 'vatan-vivo-x300-refresh.md').write_text('\n'.join(lines) + '\n')
    print('\n'.join(lines))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
