"""Read-only first-party offer audit for unpriced catalog phones in the public sitemap.

Sitemap matches are URL hints, never proof of stock, cash price, or seller. Only
the existing strict MediaMarkt PDP reader can turn one into a verified offer.
"""

import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir

import requests
from bs4 import BeautifulSoup

from mediamarkt_source import MAX_BROWSER_REQUESTS, MediaMarktSource, product_url
from retailer_offer_refresh import RetailerOfferRefresher
from reviewed_offer_targets import with_reviewed_radio
from update_prices import PriceUpdater


CATALOG_URL = 'https://teknoskor.com/api/products?limit=200'
MAX_CATALOG_BYTES = 2 * 1024 * 1024
MAX_CATALOG_PRODUCTS = 250
MAX_AUDIT_MODELS = 12
MAX_AUDIT_TARGETS = min(24, MAX_BROWSER_REQUESTS)
OUTPUT_DIR = Path(os.environ.get('MEDIAMARKT_UNPRICED_AUDIT_OUTPUT_DIR', gettempdir()))


def _catalog(get):
    response = get(CATALOG_URL, timeout=(5, 20), stream=True, allow_redirects=False,
                   headers={'Accept': 'application/json',
                            'User-Agent': 'TeknoskorPriceBot/1.0 (+https://teknoskor.com)'})
    try:
        if response.status_code != 200 or response.url != CATALOG_URL:
            raise ValueError('catalog_unavailable_or_redirected')
        declared = str(response.headers.get('Content-Length') or '')
        if declared.isdigit() and int(declared) > MAX_CATALOG_BYTES:
            raise ValueError('catalog_oversized')
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_CATALOG_BYTES:
                raise ValueError('catalog_oversized')
        rows = json.loads(body)
        if (not isinstance(rows, list) or len(rows) < 1
                or len(rows) > MAX_CATALOG_PRODUCTS):
            raise ValueError('invalid_catalog')
        seen = set()
        for row in rows:
            if (not isinstance(row, dict) or type(row.get('id')) is not int
                    or not isinstance(row.get('slug'), str)
                    or not isinstance(row.get('name'), str)
                    or not isinstance(row.get('attributes'), dict)
                    or row['id'] in seen):
                raise ValueError('invalid_catalog_identity')
            seen.add(row['id'])
        return rows
    finally:
        response.close()


def _has_current_price(row):
    try:
        price = float(row.get('fresh_lowest_price'))
        offers = int(row.get('fresh_offer_count') or 0)
        return math.isfinite(price) and price > 0 and offers > 0
    except (TypeError, ValueError):
        return False


def _specs(row):
    specs = PriceUpdater.get_expected_specs(row['attributes'])
    return with_reviewed_radio(row, specs)


def selected_rejection_evidence(body, final_url):
    """Classify one exact selected BuyAction; never turn it into an offer."""
    fallback = {'category': 'unverified_document', 'seller': None,
                'stock': None, 'condition': None,
                'marketplace': False, 'out_of_stock': False}
    if not product_url(final_url):
        return fallback
    soup = BeautifulSoup(body, 'html.parser')
    headings = soup.select('h1')
    canonicals = soup.select('link[rel="canonical"]')
    if (len(headings) != 1 or len(canonicals) != 1
            or canonicals[0].get('href') != final_url):
        return fallback
    title = ' '.join(headings[0].get_text(' ', strip=True).split())
    selected = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            node = json.loads(script.get_text())
        except (TypeError, ValueError):
            continue
        if not isinstance(node, dict) or node.get('@type') != 'BuyAction':
            continue
        product = node.get('object')
        if (not isinstance(product, dict)
                or product.get('@type') not in {'Product', 'ProductGroup'}
                or product.get('url') != final_url or product.get('name') != title):
            continue
        sku = str(product.get('sku', ''))
        if not re.fullmatch(r'\d{6,12}', sku) or not final_url.endswith('-' + sku + '.html'):
            continue
        offers = product.get('offers')
        offers = offers if isinstance(offers, list) else [offers]
        if (len(offers) == 1 and isinstance(offers[0], dict)
                and offers[0].get('@type') == 'Offer'
                and offers[0].get('url') == final_url):
            selected.append((sku, offers[0]))
    if len(selected) != 1:
        return fallback
    sku, offer = selected[0]
    seller = offer.get('seller')
    seller_name = seller.get('name') if isinstance(seller, dict) else None
    seller_name = (' '.join(seller_name.split())[:80] or None
                   if isinstance(seller_name, str) else None)
    stock = str(offer.get('availability') or '').rstrip('/').rsplit('/', 1)[-1] or None
    condition = str(offer.get('itemCondition') or '').rstrip('/').rsplit('/', 1)[-1] or None
    state = next((script.get_text() for script in soup.select('script')
                  if 'window.__PRELOADED_STATE__' in script.get_text()), '')
    marketplace = bool(seller_name and re.search(
        r'"__typename":"CofrPriceFeature","id":"Media:tr:' + re.escape(sku)
        + r'"[^}]{0,1500}"isProductOfTypeMarketplace":true', state))
    out_of_stock = stock in {'OutOfStock', 'SoldOut', 'Discontinued'}
    if marketplace:
        category = 'marketplace_seller'
    elif out_of_stock:
        category = 'out_of_stock'
    elif condition != 'NewCondition':
        category = 'non_new_condition'
    elif seller_name:
        category = 'third_party_seller_unconfirmed'
    else:
        category = 'other_unverified'
    return {'category': category, 'seller': seller_name,
            'stock': stock, 'condition': condition,
            'marketplace': marketplace, 'out_of_stock': out_of_stock}


def run_audit(*, catalog_get=None, source=None, refresher=None):
    get = catalog_get or requests.get
    rejection_evidence = {}
    source = source or MediaMarktSource(
        rejected_offer_observer=lambda url, body: rejection_evidence.__setitem__(
            url, selected_rejection_evidence(body, url)))
    refresher = refresher or RetailerOfferRefresher(mediamarkt_source=source)
    report = {'purpose': 'read_only_mediamarkt_unpriced_first_party_offer_audit',
              'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
              'writes': False, 'status': 'failed', 'catalog_products': 0,
              'priced_products': 0, 'unpriced_products': 0,
              'sitemap_matched_priced': 0, 'sitemap_matched_unpriced': 0,
              'candidate_urls': 0, 'verified_unpriced_products': 0,
              'marketplace_seller_candidates': 0, 'out_of_stock_candidates': 0,
              'products': [], 'mediamarkt_source': {}}
    try:
        catalog = _catalog(get)
        report['catalog_products'] = len(catalog)
        report['priced_products'] = sum(_has_current_price(row) for row in catalog)
        report['unpriced_products'] = len(catalog) - report['priced_products']
        candidates = []
        for row in catalog:
            urls = source.discover(row['name'], _specs(row))
            if not urls:
                continue
            if _has_current_price(row):
                report['sitemap_matched_priced'] += 1
            else:
                report['sitemap_matched_unpriced'] += 1
                candidates.append((row, urls))
        report['candidate_urls'] = sum(len(urls) for _, urls in candidates)
        if source.report().get('sitemap_status') != 'ready':
            raise ValueError('mediamarkt_sitemap_unavailable')
        if (len(candidates) > MAX_AUDIT_MODELS
                or report['candidate_urls'] > MAX_AUDIT_TARGETS):
            raise ValueError('candidate_budget_exceeded')
        validator = PriceUpdater(ingestion_client=object(), offer_refresher=refresher)
        for row, urls in candidates:
            specs = _specs(row)
            offers = refresher.refresh_existing_offer_targets(row['name'], urls, specs)
            verified = [offer for offer in offers
                        if validator.live_offer_is_valid(row, offer, specs)
                        and offer.get('availability') in {'in_stock', 'limited'}]
            report['products'].append({
                'id': row['id'], 'slug': row['slug'], 'name': row['name'],
                'ram_gb': specs.get('ram_gb'), 'storage_gb': specs.get('storage_gb'),
                'network_generation': specs.get('network_generation'),
                'candidate_urls': [target['source_url'] for target in urls],
                'source_attempts': refresher.target_report(row['name'], urls, specs),
                'rejection_evidence': [
                    {'source_url': target['source_url'], **rejection_evidence[target['source_url']]}
                    for target in urls if target['source_url'] in rejection_evidence],
                'verified_offers': [{key: offer.get(key) for key in
                                     ('merchant', 'price', 'currency', 'availability',
                                      'source_title', 'url', 'observed_at')}
                                    for offer in verified],
            })
        report['verified_unpriced_products'] = sum(
            bool(row['verified_offers']) for row in report['products'])
        report['marketplace_seller_candidates'] = sum(
            evidence['marketplace']
            for row in report['products'] for evidence in row['rejection_evidence'])
        report['out_of_stock_candidates'] = sum(
            evidence['out_of_stock']
            for row in report['products'] for evidence in row['rejection_evidence'])
        report['status'] = 'completed'
    except (requests.RequestException, ValueError, TypeError, KeyError) as error:
        report['error'] = (str(error) if isinstance(error, ValueError)
                           else type(error).__name__)
    report['mediamarkt_source'] = source.report()
    return report


def summary(report):
    source = report['mediamarkt_source']
    lines = ['## MediaMarkt unpriced catalog coverage (read only)',
             f"Status: {report['status']}; catalog {report['catalog_products']} "
             f"({report['priced_products']} priced, {report['unpriced_products']} unpriced); "
             f"sitemap hints for {report['sitemap_matched_priced']} priced and "
             f"{report['sitemap_matched_unpriced']} unpriced models.",
             f"Unpriced URL hints: {report['candidate_urls']}; verified selected "
             f"first-party offers: {report['verified_unpriced_products']}; "
             f"selected marketplace sellers: {report['marketplace_seller_candidates']}; "
             f"out of stock: {report['out_of_stock_candidates']}; "
             f"MediaMarkt sitemap {source.get('sitemap_status')}, browser GET "
             f"{source.get('browser_html_responses')}/{source.get('browser_requests')}."]
    for product in report['products']:
        reasons = ', '.join(f"{attempt.get('reason')} HTTP {attempt.get('http_status')}"
                            for attempt in product['source_attempts'])
        evidence = ', '.join(
            f"{row['category']} seller {row['seller'] or '?'} stock {row['stock'] or '?'}"
            for row in product['rejection_evidence'])
        offers = ', '.join(f"{offer['price']} TRY"
                           for offer in product['verified_offers']) or 'none'
        lines.append(f"- {product['slug']}: {len(product['candidate_urls'])} hints; "
                     f"{reasons}; {evidence or 'no selected rejection'}; verified {offers}")
    if report.get('error'):
        lines.append('Error: ' + report['error'])
    lines.append('Sitemap hints alone never establish price or seller. No writes.')
    return '\n'.join(lines) + '\n'


def main():
    report = run_audit()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / 'mediamarkt-unpriced-coverage.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    text = summary(report)
    (OUTPUT_DIR / 'mediamarkt-unpriced-coverage.md').write_text(text)
    print(text, end='')
    return 0 if report['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
