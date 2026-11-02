"""Read-only GitHub runner proof of exact Pazarama Android phone offers.

The category provides URL hints. The normal PDP reader verifies identity,
seller, stock and the visible ordinary price. No catalog API, ingestion secret,
price write, cart request, browser impersonation or cached price is used.
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir

from bs4 import BeautifulSoup
import requests

from pazarama_category_source import (CATEGORY_URL, ROBOTS_URL, PazaramaCategorySource,
                                       _category_page_number, pazarama_product_url)
from retailer_offer_refresh import RetailerOfferRefresher, _schema_nodes, _type_is


PROFILES = (
    {'slug': 'samsung-galaxy-a26-5g', 'name': 'Samsung Galaxy A26 5G',
     'ram_gb': 6, 'storage_gb': 128, 'network_generation': '5g',
     'url': 'https://www.pazarama.com/samsung-galaxy-a26-5g-128-gb-6-gb-ram-siyah-cep-telefonu-samsung-turkiye-garantili-p-8806097100638'},
    {'slug': 'samsung-galaxy-a37-5g', 'name': 'Samsung Galaxy A37 5G',
     'ram_gb': 6, 'storage_gb': 128, 'network_generation': '5g',
     'url': 'https://www.pazarama.com/samsung-galaxy-a37-5g-6gb128gb-antrasit-cep-telefonu-p-8806099054977'},
    {'slug': 'samsung-galaxy-s24fe', 'name': 'Samsung Galaxy S24 FE',
     'ram_gb': 8, 'storage_gb': 128, 'network_generation': '5g',
     'url': 'https://www.pazarama.com/samsung-galaxy-s24-fe-gri-p-8806095750101'},
)
OUTPUT_DIR = Path(os.environ.get('PAZARAMA_AUDIT_OUTPUT_DIR', gettempdir()))
REPORT_PATH = OUTPUT_DIR / 'pazarama-source-audit.json'
SUMMARY_PATH = OUTPUT_DIR / 'pazarama-source-audit.md'
SOLVER_URL = 'http://localhost:8191/v1'
MAX_SOLVER_BYTES = 8 * 1024 * 1024


class BrowserResponse:
    def __init__(self, url, status, body):
        self.url, self.status_code, self.data = url, status, body.encode('utf-8')
        self.headers = {'Content-Length': str(len(self.data))}

    def iter_content(self, chunk_size):
        for index in range(0, len(self.data), chunk_size):
            yield self.data[index:index + chunk_size]

    def close(self):
        pass


class BrowserGetTransport:
    """Bounded GET-only runner probe through the existing local browser service.

    The source fetches robots first and checks its policy before any category or
    PDP URL reaches this adapter. No cart, search, login or write URL is allowed.
    """

    def __init__(self, post=None):
        self._post = post or requests.post

    def __call__(self, url, **_kwargs):
        if url != ROBOTS_URL and _category_page_number(url) is None and not pazarama_product_url(url):
            raise requests.RequestException('Unreviewed Pazarama browser GET URL')
        response = self._post(SOLVER_URL, json={
            'cmd': 'request.get', 'url': url, 'maxTimeout': 45000,
        }, timeout=60, stream=True)
        try:
            if response.status_code != 200:
                raise requests.RequestException('Browser GET service unavailable')
            data = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                data.extend(chunk)
                if len(data) > MAX_SOLVER_BYTES:
                    raise requests.RequestException('Oversized browser GET result')
        finally:
            response.close()
        try:
            payload = json.loads(data)
            solution = payload['solution']
            status, final_url, body = (solution['status'], solution['url'], solution['response'])
            if not isinstance(status, int) or not isinstance(body, str) or final_url != url:
                raise ValueError('Invalid browser GET result')
            return BrowserResponse(final_url, status, body)
        except (KeyError, TypeError, ValueError, UnicodeError) as error:
            raise requests.RequestException('Invalid browser GET result') from error


class HttpRecorder:
    """Record statuses only; response bodies and request headers stay private."""

    def __init__(self, fetch=None):
        self._fetch = fetch or requests.get
        self.events = []

    def __call__(self, url, **kwargs):
        response = self._fetch(url, **kwargs)
        self.events.append({'url': url, 'http_status': response.status_code,
                            'final_url': response.url})
        return response


def _amount(text):
    match = re.fullmatch(r'(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*TL', text, re.I)
    return float(match[1].replace('.', '').replace(',', '.')) if match else None


def selected_price_evidence(html, url):
    """Compact selected-panel evidence, fetched from the source's PDP cache."""
    soup = BeautifulSoup(html, 'html.parser')
    products = [node for node in _schema_nodes(soup)
                if _type_is(node, 'Product') and node.get('url') == url]
    detail = soup.select_one('.product-detail')
    if len(products) != 1 or detail is None:
        return {'status': 'selected_product_missing'}
    offer = products[0].get('offers')
    if not isinstance(offer, dict) or not _type_is(offer, 'Offer'):
        return {'status': 'selected_offer_missing'}
    prices = [node for node in detail.select('p') if 'text-4xl' in node.get('class', [])
              and not node.find_parent(attrs={'data-testid': 'product-card-discount'})]
    discounts = detail.select('[data-testid="product-card-discount"]')
    seller = [node.select('span')[1].get_text(' ', strip=True) for node in detail.select('p')
              if len(node.select('span')) == 2
              and node.select('span')[0].get_text(' ', strip=True).rstrip(':').strip() == 'Satıcı']
    cart = [node for node in detail.select('button[data-testid="base-addbasket-button"]')
            if node.get_text(' ', strip=True) == 'Sepete Ekle' and not node.has_attr('disabled')]
    discount_text = discounts[0].get_text(' ', strip=True) if len(discounts) == 1 else None
    cart_price = _amount(discount_text.removeprefix('Sepette').strip()) if discount_text else None
    return {
        'status': 'selected_product_seen',
        'heading': detail.h1.get_text(' ', strip=True) if detail.h1 else None,
        'schema_offer_price': offer.get('price'),
        'schema_currency': offer.get('priceCurrency'),
        'schema_availability': offer.get('availability'),
        'visible_normal_price': _amount(prices[0].get_text(' ', strip=True)) if len(prices) == 1 else None,
        'normal_price_struck': any('line-through' in node.get('class', [])
                                   for price in prices for node in [price, *price.parents] if hasattr(node, 'get')),
        'discount_label': 'Sepette' if discount_text and discount_text.startswith('Sepette') else None,
        'visible_cart_price': cart_price,
        'visible_seller': seller[0] if len(seller) == 1 else None,
        'active_cart_buttons': len(cart),
    }


def run_audit(recorder=None, source=None, refresher=None):
    browser_get = os.environ.get('PAZARAMA_AUDIT_BROWSER_GET') == '1'
    recorder = recorder if recorder is not None else HttpRecorder(
        fetch=BrowserGetTransport() if browser_get else None)
    source = source if source is not None else PazaramaCategorySource(fetch=recorder)
    refresher = refresher if refresher is not None else RetailerOfferRefresher(fetch=recorder)
    refresher.pazarama_source = source
    rows = []
    for profile in PROFILES:
        specs = {key: profile[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
        row = {'slug': profile['slug'], 'name': profile['name'], 'expected_specs': specs,
               'exact_pdp_url': profile['url'], 'discovered_urls': [], 'discovered_exact_url': False,
               'source_attempts': [], 'verified_offers': [], 'price_evidence': None}
        rows.append(row)
        try:
            targets = source.discover(profile['name'], specs)
            row['discovered_urls'] = [target['source_url'] for target in targets]
            row['discovered_exact_url'] = profile['url'] in row['discovered_urls']
            # Also probe the reviewed exact PDP if category ordering drifted.
            # It cannot make the discovery gate pass by itself.
            reviewed = [{'source_url': profile['url']}]
            offers = refresher.refresh_existing_offer_targets(profile['name'], reviewed, specs)
            row['source_attempts'] = refresher.target_report(profile['name'], reviewed, specs)
            row['verified_offers'] = [
                {key: offer[key] for key in ('merchant', 'price', 'currency',
                                              'availability', 'source_title', 'url', 'observed_at')}
                for offer in offers if offer.get('merchant') == 'Pazarama'
                and offer.get('availability') == 'in_stock' and offer.get('url') == profile['url']
            ]
            response = source.fetch(profile['url'])
            if response.status_code == 200:
                row['price_evidence'] = selected_price_evidence(response.text, profile['url'])
        except Exception as error:
            row['error'] = type(error).__name__
        evidence = row['price_evidence'] or {}
        offer = row['verified_offers'][0] if row['verified_offers'] else None
        schema_price = evidence.get('schema_offer_price')
        visible_price = evidence.get('visible_normal_price')
        discount = evidence.get('discount_label')
        price_separated = (discount == 'Sepette' and schema_price == evidence.get('visible_cart_price')
                           and isinstance(schema_price, (int, float))
                           and isinstance(visible_price, (int, float)) and visible_price > schema_price)
        ordinary = discount is None and schema_price == visible_price
        row['status'] = ('verified' if row['discovered_exact_url'] and offer
                         and evidence.get('visible_seller') and evidence.get('active_cart_buttons')
                         and not evidence.get('normal_price_struck')
                         and offer['price'] == visible_price and (price_separated or ordinary)
                         else 'pdp_verified_but_not_discovered' if offer and not row['discovered_exact_url']
                         else 'not_verified')
    verified = sum(row['status'] == 'verified' for row in rows)
    return {'purpose': 'read_only_runner_access_probe',
            'transport': 'browser_get' if browser_get else 'normal_http_get',
            'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'status': 'passed' if verified == len(PROFILES) else 'partial' if verified else 'failed',
            'verified_products': verified, 'required_products': len(PROFILES),
            'category': source.report(), 'http_events': recorder.events, 'products': rows}


def format_summary(report):
    events = report['http_events']
    robots = next((event['http_status'] for event in events if event['url'] == ROBOTS_URL), None)
    category = [event['http_status'] for event in events if event['url'] == CATEGORY_URL
                or '/cep-telefonu-k-K03043?' in event['url']]
    lines = ['## Pazarama Android offer source audit',
             f"Status: {report['status']} — {report['verified_products']}/{report['required_products']} exact variants verified.",
             f"Transport: {report.get('transport', 'normal_http_get')}.",
             f"Robots HTTP {robots}; category pages {report['category']['category_pages']} "
             f"(statuses: {', '.join(map(str, sorted(set(category)))) or 'none'}); "
             f"eligible cards {report['category']['eligible_card_urls']}."]
    for row in report['products']:
        pdp_status = next((event['http_status'] for event in events
                           if event['url'] == row['exact_pdp_url']), None)
        evidence = row['price_evidence'] or {}
        offer = row['verified_offers'][0] if row['verified_offers'] else None
        cart_detail = (f"Sepette={evidence.get('visible_cart_price')} TRY"
                       if evidence.get('discount_label') == 'Sepette' else 'no cart discount')
        lines.append(
            f"- {row['slug']}: {row['status']}; discovered={row['discovered_exact_url']}; "
            f"PDP HTTP {pdp_status}; normal={evidence.get('visible_normal_price')} TRY; "
            f"JSON-LD={evidence.get('schema_offer_price')} TRY; "
            f"{cart_detail}; "
            f"seller={evidence.get('visible_seller')}; "
            f"accepted={offer['price'] if offer else None} TRY"
        )
    lines.append('Read-only: no catalog API, cart action or offer ingestion was used.')
    return '\n'.join(lines) + '\n'


def write_outputs(report, report_path=REPORT_PATH, summary_path=SUMMARY_PATH):
    report_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    summary_path.write_text(format_summary(report))


def main():
    report = run_audit()
    write_outputs(report)
    print(format_summary(report), end='')
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
