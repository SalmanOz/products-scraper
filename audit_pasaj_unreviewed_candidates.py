"""Bounded, read-only audit of clean Pasaj URLs omitted by the reviewed reader.

Stored URLs are discovery hints. This module never feeds an offer to the price
writer, changes the reviewed URL allowlist, or accepts seller/color query URLs.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from tempfile import gettempdir
from urllib.parse import urlparse

from bs4 import BeautifulSoup
import requests

from offer_ingestion_client import OfferIngestionClient
from pasaj_source import (MAX_HTML_BYTES, MAX_PDP_REQUESTS, SELLER,
                          PasajSource, _cash_text, _feature, _money, product_url)
from tr_price_scraper import TRPriceScraper
from update_prices import PriceUpdater


PURPOSE = 'read_only_pasaj_unreviewed_clean_pdp_candidates'
OUTPUT_DIR = Path(os.environ.get('PASAJ_CANDIDATE_AUDIT_OUTPUT_DIR', gettempdir()))
_PATH = re.compile(
    r'/pasaj/cep-telefonu/(?:android-telefonlar|ios-telefonlar|'
    r'yapay-zeka-telefonlar)/(?:[a-z0-9-]+/){0,2}[a-z0-9-]+'
)
_USED = re.compile(r'outlet|yenilenmi[sş]|refurbish|ikinci[- ]el|te[sş]hir', re.I)
_BRANDS = {'apple': 'Apple', 'samsung': 'Samsung', 'xiaomi': 'Xiaomi',
           'tecno': 'Tecno', 'poco': 'POCO', 'oppo': 'OPPO',
           'vivo': 'Vivo', 'honor': 'Honor', 'realme': 'realme'}


def clean_candidate_url(url):
    """Admit only a clean canonical Pasaj phone PDP, never a seller selection."""
    if (not isinstance(url, str) or len(url) > 500
            or re.search(r'[\x00-\x20\\%]', url)):
        return False
    try:
        parsed = urlparse(url)
        return bool(parsed.scheme == 'https'
                    and parsed.netloc == 'www.turkcell.com.tr'
                    and not parsed.query and not parsed.fragment
                    and _PATH.fullmatch(parsed.path))
    except (TypeError, ValueError):
        return False


class PasajCandidateSource(PasajSource):
    """Reuse the existing redirect, byte, pace, and six-PDP boundaries."""

    def read_document(self, url):
        if not clean_candidate_url(url) or product_url(url):
            return None, 'not_unreviewed_clean_pdp', None
        with self._lock:
            try:
                if self._pdp_requests >= MAX_PDP_REQUESTS:
                    return None, 'pdp_budget_exhausted', None
                self._pdp_requests += 1
                body, reason, status = self._request(url, MAX_HTML_BYTES)
                return (body, 'document_read', status) if reason == 'ok' else (
                    None, reason, status)
            except ValueError as error:
                return None, str(error), None
            except (requests.RequestException, TypeError, UnicodeError) as error:
                return None, type(error).__name__, None


def _expected_specs(product):
    specs = PriceUpdater.get_expected_specs(product.get('attributes'))
    return {key: TRPriceScraper._spec_int(specs.get(key))
            if key != 'network_generation' else specs.get(key)
            for key in ('ram_gb', 'storage_gb', 'network_generation')}


def _candidate_priority(product, url):
    specs = _expected_specs(product)
    path = urlparse(url).path
    complete = (specs['ram_gb'] is not None and specs['storage_gb'] is not None
                and specs['network_generation'] in {'4g', '5g'})
    return (0 if complete and '/android-telefonlar/' in path else
            1 if '/android-telefonlar/' in path else 2)


def _queue(products, prices, priced=False):
    """Exact unpriced catalog IDs only; no target URL is treated as an offer."""
    by_url = {}
    excluded_query = 0
    for product in products:
        if (not isinstance(product, dict) or type(product.get('id')) is not int
                or not isinstance(product.get('slug'), str)
                or not isinstance(product.get('name'), str)
                or prices.get(product['id']) != (product['slug'], priced)):
            continue
        targets = product.get('offer_targets')
        if not isinstance(targets, list):
            continue
        for target in targets:
            url = target.get('source_url') if isinstance(target, dict) else None
            if not isinstance(url, str) or 'turkcell.com.tr' not in url:
                continue
            if clean_candidate_url(url) and not product_url(url):
                by_url.setdefault(url, {})[product['id']] = product
            elif urlparse(url).query:
                excluded_query += 1
    unique = []
    ambiguous = []
    for url, matches in by_url.items():
        if len(matches) == 1:
            unique.append((url, next(iter(matches.values()))))
        else:
            ambiguous.append({'url': url, 'catalog_slugs': sorted(
                {product['slug'] for product in matches.values()})})
    unique.sort(key=lambda item: (_candidate_priority(item[1], item[0]),
                                  item[1]['slug'], item[0]))
    return unique, sorted(ambiguous, key=lambda item: item['url']), excluded_query


def selected_cash_evidence(html, url, product):
    """Prove selected first-party cash and exact catalog variant for review only."""
    if not isinstance(html, str) or not clean_candidate_url(url):
        return {'status': 'invalid_document'}
    soup = BeautifulSoup(html, 'html.parser')
    canonicals = soup.select('link[rel="canonical"]')
    headings = [text for node in soup.select('h1')
                if (text := node.get_text(' ', strip=True))]
    if (len(canonicals) != 1 or canonicals[0].get('href') != url
            or len(headings) != 1 or not headings[0] or _USED.search(headings[0])):
        return {'status': 'canonical_or_selected_title_unproven'}
    title = headings[0]
    nodes = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            node = json.loads(script.string or script.get_text())
        except (TypeError, ValueError):
            continue
        if isinstance(node, dict) and node.get('@type') == 'Product':
            nodes.append(node)
    if len(nodes) != 1:
        return {'status': 'selected_product_jsonld_unproven'}
    node = nodes[0]
    sku = str(node.get('sku') or '')
    expected_brand = _BRANDS.get(product['name'].split()[0].casefold())
    brand = node.get('brand')
    if (node.get('url') != url or node.get('name') != title
            or not re.fullmatch(r'[1-9]\d{3,10}', sku)
            or not expected_brand or not isinstance(brand, dict)
            or str(brand.get('name') or '').casefold() != expected_brand.casefold()):
        return {'status': 'selected_sku_brand_or_url_unproven'}
    specs = _expected_specs(product)
    if (not TRPriceScraper().is_strict_match(product['name'], title, specs)
            or specs['ram_gb'] is None or specs['storage_gb'] is None
            or specs['network_generation'] not in {'4g', '5g'}
            or _feature(soup, 'Ram (bellek) Kapasitesi') != f"{specs['ram_gb']} GB"
            or _feature(soup, 'Dahili Hafıza') != f"{specs['storage_gb']} GB"
            or _feature(soup, 'Hücresel Plan') != specs['network_generation'].upper()):
        return {'status': 'catalog_variant_unproven', 'sku': sku,
                'source_title': title}
    aggregate = node.get('offers')
    offers = aggregate.get('offers') if isinstance(aggregate, dict) else None
    if (not isinstance(aggregate, dict) or aggregate.get('@type') != 'AggregateOffer'
            or aggregate.get('offerCount') != 1
            or aggregate.get('priceCurrency') != 'TRY'
            or aggregate.get('availability') != 'https://schema.org/InStock'
            or not isinstance(offers, list) or len(offers) != 1
            or not isinstance(offers[0], dict)
            or offers[0].get('@type') != 'Offer'
            or offers[0].get('priceCurrency') != 'TRY'
            or offers[0].get('offeredBy') !=
                {'@type': 'Organization', 'name': SELLER}):
        return {'status': 'selected_first_party_stock_unproven', 'sku': sku,
                'source_title': title}
    price = _money(offers[0].get('price'))
    if (price is None or price != _money(aggregate.get('lowPrice'))
            or price != _money(aggregate.get('highPrice'))):
        return {'status': 'selected_cash_jsonld_unproven', 'sku': sku,
                'source_title': title}
    cash = soup.select('form.m-form .price-radio-cash')
    if len(cash) != 1:
        return {'status': 'selected_cash_radio_unproven', 'sku': sku,
                'source_title': title}
    cash = cash[0]
    form = cash.find_parent('form')
    selected = cash.select('input.product-basket-type[type="radio"][data-type="cash"][checked]')
    sellers = cash.select('.m-card-offer__seller-container .a-price-radio-b__title')
    amounts = cash.select('.a-price-radio-b__price-box .a-price-val')
    currencies = cash.select('.a-price-radio-b__price-box .a-price__currency')
    buttons = form.select('button.device-available.add-to-basket-non-login') if form else []
    colors = form.select('select.m-select-color option[selected]') if form else []
    if (len(selected) != 1 or selected[0].get('name') != 'price'
            or len(sellers) != 1 or sellers[0].get_text(' ', strip=True) != SELLER
            or len(amounts) != 1 or len(currencies) != 1
            or currencies[0].get_text(' ', strip=True) != 'TL'
            or _cash_text(amounts[0].get_text(' ', strip=True)) != price
            or len(buttons) != 1 or buttons[0].has_attr('disabled')
            or buttons[0].get('aria-disabled') == 'true'
            or 'display:none' in buttons[0].get('style', '').replace(' ', '').casefold()
            or buttons[0].get_text(' ', strip=True) != 'Sepete Ekle'
            or buttons[0].get('id') != 'p-' + sku
            or len(colors) != 1
            or colors[0].get('data-stock-available') != 'true'
            or colors[0].get('data-at-basket') != 'false'
            or colors[0].get('data-seller-name') != SELLER
            or _cash_text(colors[0].get('data-price', '')) != price
            or not re.fullmatch(re.escape(sku) + r'-\d+-\d+',
                                 colors[0].get('data-device-product-uid', ''))):
        return {'status': 'selected_cash_seller_color_or_basket_unproven',
                'sku': sku, 'source_title': title}
    return {'status': 'source_verified_pending_manual_review', 'sku': sku,
            'source_title': title, 'seller': SELLER, 'cash_price_try': float(price),
            'availability': 'in_stock', 'expected_specs': specs}


def run_audit(*, client=None, source=None, offset=0, priced=False):
    if type(offset) is not int or not 0 <= offset <= 1000:
        raise ValueError('offset_must_be_0_to_1000')
    report = {'purpose': PURPOSE, 'writes': False, 'status': 'failed',
              'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
              'offset': offset, 'priced_catalog': priced, 'probe_budget': MAX_PDP_REQUESTS,
              'candidate_urls': 0, 'query_urls_excluded': 0,
              'ambiguous_urls': [], 'probed': 0, 'source_verified': 0,
              'automatic_prepared_offers': 0, 'candidates': [], 'source': {}}
    try:
        client = client or OfferIngestionClient.from_env()
        source = source or PasajCandidateSource()
        products = client.get_published_products()
        prices = client.get_public_price_priority()
        if not isinstance(products, list) or not isinstance(prices, dict):
            raise ValueError('catalog_unavailable')
        queue, ambiguous, query_count = _queue(products, prices, priced=priced)
        parser = PasajSource()
        updater = PriceUpdater(ingestion_client=client)
        report.update(candidate_urls=len(queue), ambiguous_urls=ambiguous,
                      query_urls_excluded=query_count)
        for url, product in queue[offset:offset + MAX_PDP_REQUESTS]:
            html, reason, status = source.read_document(url)
            evidence = (selected_cash_evidence(html, url, product)
                        if reason == 'document_read' and status == 200 else
                        {'status': reason})
            specs = PriceUpdater.get_expected_specs(product.get('attributes'))
            parser.register_catalog_targets(product['name'], [{'source_url': url}], specs)
            automatic = parser.parse_offer(html, url) if reason == 'document_read' and status == 200 else None
            accepted = ([automatic] if automatic and updater.live_offer_is_valid(product, automatic, specs)
                        else [])
            now = datetime.now(timezone.utc).isoformat()
            prepared = updater.prepare_product_offers(product, accepted, now, now)
            report['automatic_prepared_offers'] += len(prepared)
            report['candidates'].append({
                'id': product['id'], 'slug': product['slug'], 'url': url,
                'http_status': status, **evidence, 'automatic_prepared': prepared})
        report['probed'] = len(report['candidates'])
        report['source_verified'] = sum(row['status'] ==
                                        'source_verified_pending_manual_review'
                                        for row in report['candidates'])
        report['status'] = 'completed'
    except Exception as error:
        # Client failures can include response bodies and request metadata.
        report['error'] = (str(error) if isinstance(error, ValueError) and
                           str(error) in {'catalog_unavailable'} else
                           type(error).__name__)
    report['source'] = source.report() if source is not None else {}
    return report


def summary(report):
    source = report.get('source') or {}
    lines = ['## Pasaj clean PDP candidate audit (read only)',
             f"Status: {report['status']}; clean unpriced URLs: "
             f"{report['candidate_urls']}; query URLs excluded: "
             f"{report['query_urls_excluded']}; page offset {report['offset']}; "
             f"probed {report['probed']}/{report['probe_budget']}; "
             f"legacy proof verified: {report['source_verified']}; "
             f"automatic prepared offers: {report.get('automatic_prepared_offers', 0)}; "
             f"priced catalog: {report.get('priced_catalog', False)}.",
             f"PDP requests: {source.get('pdp_requests', 0)}."]
    for row in report['candidates']:
        prepared = '; '.join(f"{o['seller']} {o['price']} TRY" for o in row.get('automatic_prepared', []))
        lines.append(f"- {row['slug']}: {row['status']} (HTTP {row['http_status']}); "
                     f"automatic offers {prepared or 'none'}; {row['url']}")
    if report.get('error'):
        lines.append('Error: ' + report['error'])
    lines.append('No price, catalog, or score writes; no automatic publish.')
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--priced', action='store_true')
    args = parser.parse_args()
    report = run_audit(offset=args.offset, priced=args.priced)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / 'pasaj-candidates.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    (OUTPUT_DIR / 'pasaj-candidates.md').write_text(summary(report))
    print(summary(report), end='')
    return 0 if report['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
