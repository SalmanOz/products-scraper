"""Read-only catalog-to-stock-card audit; cards never become price evidence."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import gettempdir

import requests

from reviewed_offer_targets import with_reviewed_radio
from tr_price_scraper import TRPriceScraper
from turk_telekom_source import TurkTelekomSource
from update_prices import PriceUpdater


CATALOG_URL = 'https://teknoskor.com/api/products?limit=200'
TARGET = (293, 'samsung-galaxy-z-fold8-ultra', 'Samsung Galaxy Z Fold8 Ultra',
          12, 256, 'https://emagaza.turktelekom.com.tr/tr/products/samsung/samsung-galaxy-z-fold8-ultra?v=2908')
MAX_CATALOG_BYTES = 128 * 1024
MAX_CATALOG_ROWS = 200
OUTPUT_DIR = Path(os.environ.get('TT_COVERAGE_AUDIT_OUTPUT_DIR', gettempdir()))


def _catalog(get):
    response = get(CATALOG_URL, timeout=(5, 12), allow_redirects=False, stream=True,
                   headers={'Accept': 'application/json',
                            'User-Agent': 'TeknoskorPriceBot/1.0 (+https://teknoskor.com)'})
    try:
        if response.status_code != 200 or response.url != CATALOG_URL:
            raise ValueError('catalog_unavailable')
        if 'application/json' not in response.headers.get('Content-Type', '').casefold():
            raise ValueError('catalog_not_json')
        length = response.headers.get('Content-Length', '')
        if length.isdigit() and int(length) > MAX_CATALOG_BYTES:
            raise ValueError('catalog_oversized')
        body = bytearray()
        for chunk in response.iter_content(chunk_size=16 * 1024):
            body.extend(chunk)
            if len(body) > MAX_CATALOG_BYTES:
                raise ValueError('catalog_oversized')
        rows = json.loads(body)
        if not isinstance(rows, list) or not 0 < len(rows) <= MAX_CATALOG_ROWS:
            raise ValueError('catalog_invalid')
        return rows
    finally:
        response.close()


def run_audit(source=None, catalog_get=None):
    source = source if source is not None else TurkTelekomSource()
    get = catalog_get or requests.get
    matcher = TRPriceScraper()
    report = {'purpose': 'read_only_turk_telekom_catalog_coverage',
              'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
              'status': 'failed', 'catalog_total': 0, 'catalog_unpriced': 0,
              'matched_models': 0, 'unpriced_candidates': [],
              'verified_unpriced_offers': [], 'target': None,
              'source': None, 'writes': False}
    try:
        products = _catalog(get)
        report['catalog_total'] = len(products)
        report['catalog_unpriced'] = sum(not (isinstance(p, dict) and
              isinstance(p.get('fresh_lowest_price'), (int, float)) and
              p['fresh_lowest_price'] > 0) for p in products)
        target_id, target_slug, target_name, ram, storage, target_url = TARGET
        target_products = [p for p in products if isinstance(p, dict) and p.get('id') == target_id]
        if len(target_products) != 1:
            raise ValueError('target_catalog_identity_missing')
        target_product = target_products[0]
        if (target_product.get('slug') != target_slug or target_product.get('name') != target_name or
                PriceUpdater.get_expected_specs(target_product.get('attributes')).get('ram_gb') != ram or
                PriceUpdater.get_expected_specs(target_product.get('attributes')).get('storage_gb') != storage):
            raise ValueError('target_catalog_identity_or_variant_changed')
        for product in products:
            if not isinstance(product, dict) or not isinstance(product.get('name'), str):
                continue
            specs = with_reviewed_radio(
                product, PriceUpdater.get_expected_specs(product.get('attributes')))
            targets = source.discover(product['name'], specs)
            if not targets:
                continue
            report['matched_models'] += 1
            if isinstance(product.get('fresh_lowest_price'), (int, float)) and product['fresh_lowest_price'] > 0:
                continue
            row = {'id': product.get('id'), 'slug': product.get('slug'),
                   'name': product['name'], 'specs': specs,
                   'candidate_urls': [target['source_url'] for target in targets],
                   'source_attempts': [], 'verified_offer': None}
            report['unpriced_candidates'].append(row)
            # This pilot reads at most the one pinned, previously reviewed PDP.
            if product.get('id') != target_id or row['candidate_urls'] != [target_url]:
                continue
            offer, reason, status = source.read_offer(target_url)
            row['source_attempts'].append({'reason': reason, 'http_status': status})
            if (offer and reason == 'observed_exact_cash_offer' and status == 200
                    and offer.get('merchant') == 'Türk Telekom eMağaza'
                    and offer.get('currency') == 'TRY'
                    and offer.get('availability') == 'in_stock'
                    and matcher.has_observed_offer_evidence(product['name'], offer, specs)):
                row['verified_offer'] = {key: offer.get(key) for key in
                                         ('merchant', 'price', 'currency', 'availability',
                                          'source_title', 'url', 'observed_at')}
                report['verified_unpriced_offers'].append(row['verified_offer'])
            report['target'] = row
        report['source'] = source.report()
        if (report['target'] and report['target']['verified_offer'] and
                report['source']['robots_status'] == 'allowed' and
                report['source']['status'] == 'ready'):
            report['status'] = 'passed'
        else:
            report['status'] = 'no_exact_target_offer'
    except (requests.RequestException, ValueError, TypeError, KeyError, UnicodeError) as error:
        report['error'] = str(error) if isinstance(error, ValueError) else type(error).__name__
        report['source'] = source.report()
    return report


def summary(report):
    source = report.get('source') or {}
    lines = ['## Türk Telekom catalog-to-stock-card coverage (read only)',
             f"Status: {report['status']}; catalog {report['catalog_total']}, "
             f"unpriced {report['catalog_unpriced']}, exact stock-card models "
             f"{report['matched_models']}, unpriced exact URL candidates "
             f"{len(report['unpriced_candidates'])}.",
             f"Source: robots {source.get('robots_status')}, category "
             f"{source.get('category_requests')} requests, "
             f"{source.get('stock_card_urls')} stock-card URLs, "
             f"PDP {source.get('pdp_requests')} request."]
    for row in report['unpriced_candidates']:
        offer = row['verified_offer'] or {}
        attempt = row['source_attempts'][0] if row['source_attempts'] else {}
        lines.append(f"- {row['slug']}: {row['candidate_urls'][0]}; "
                     f"{attempt.get('reason', 'URL hint only')} HTTP {attempt.get('http_status')}; "
                     f"selected cash TRY {offer.get('price', 'unverified')}.")
    if report.get('error'):
        lines.append('Error: ' + report['error'])
    lines.append('No price, catalog, image, or score writes. Stock cards are URL hints only.')
    return '\n'.join(lines) + '\n'


def main():
    report = run_audit()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / 'turk-telekom-coverage.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    (OUTPUT_DIR / 'turk-telekom-coverage.md').write_text(summary(report))
    print(summary(report), end='')
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
