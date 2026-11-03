"""Read only the exact first-party Hepsiburada S25 FE 8/256 buy box.

This page's JSON-LD names Samsung as Offer.seller although the selected buy
box visibly names Hepsiburada. The generic parser cannot safely resolve that
conflict. This reader requires two independent, exact buy-box merchant signals
and a fixed selected SKU. It is not yet connected to production ingestion.
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timezone
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
import requests

from tr_price_scraper import TRPriceScraper


URL = ('https://www.hepsiburada.com/samsung-galaxy-s25-fe-256-gb-8-gb-ram-'
       'samsung-turkiye-garantili-gece-siyahi-pm-HBC00009S6B28')
ROBOTS_URL = 'https://www.hepsiburada.com/robots.txt'
SOLVER_URL = 'http://localhost:8191/v1'
SELLER_URL = 'https://www.hepsiburada.com/magaza/hepsiburada'
PRODUCT_ID = 'HBC00009S6B28'
SKU = 'HBCV00009S5QYI'
NAME = 'Samsung Galaxy S25 FE 256GB'
SPECS = {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'}
BOT = 'TeknoskorPriceBot'
MAX_ROBOTS_BYTES = 64 * 1024
MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_SOLVER_BYTES = 8 * 1024 * 1024
MAX_BROWSER_REQUESTS = 2
HEADERS = {'User-Agent': BOT + '/1.0 (+https://teknoskor.com)',
           'Accept': 'text/html', 'Accept-Language': 'tr-TR,tr;q=0.9'}


def _normalized(value):
    return ' '.join(str(value or '').split())


def _nodes(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.get_text())
        except (TypeError, ValueError):
            continue
        roots = data if isinstance(data, list) else [data]
        for root in roots:
            if isinstance(root, dict):
                yield root
                graph = root.get('@graph')
                if isinstance(graph, list):
                    yield from (node for node in graph if isinstance(node, dict))


def parse_exact_buy_box(html: str, final_url: str = URL):
    """Return (offer, reason); every published price comes from selected PDP."""
    if final_url != URL or not isinstance(html, str) or len(html.encode('utf-8')) > MAX_HTML_BYTES:
        return None, 'wrong_or_oversized_pdp'
    soup = BeautifulSoup(html, 'html.parser')
    if re.search(r'captcha|just a moment|access denied|you have been blocked',
                 _normalized(soup.title.get_text() if soup.title else ''), re.I):
        return None, 'challenge_page'
    headings = soup.select('h1[data-test-id="title"]')
    if len(headings) != 1:
        return None, 'selected_title_missing'
    title = _normalized(headings[0].get_text(' ', strip=True))
    matcher = TRPriceScraper()
    if (not matcher.is_strict_match(NAME, title, SPECS)
            or not re.search(r'\bGece Siyah[ıi]\b', title, re.I)):
        return None, 'selected_variant_mismatch'

    nodes = list(_nodes(soup))
    pages = [node for node in nodes if node.get('@type') == 'WebPage' and node.get('url') == URL]
    products = [node for node in nodes if node.get('@type') == 'Product'
                and node.get('sku') == SKU]
    if len(pages) != 1 or len(products) != 1:
        return None, 'schema_identity_missing'
    product = products[0]
    if (product.get('color') != 'Gece Siyahı'
            or not matcher.is_strict_match(NAME, str(product.get('name') or ''), SPECS)):
        return None, 'schema_selected_variant_mismatch'
    offer = product.get('offers')
    if (not isinstance(offer, dict) or offer.get('@type') != 'Offer'
            or offer.get('url') != URL or offer.get('priceCurrency') != 'TRY'
            or str(offer.get('availability', '')).rstrip('/').endswith('/InStock') is False
            or str(offer.get('itemCondition', '')).rstrip('/').endswith('/NewCondition') is False):
        return None, 'schema_exact_offer_missing'
    # Hepsiburada's current schema incorrectly uses its brand as seller. This
    # one SKU is accepted only when that value stays the known brand AND both
    # buy-box merchant signals below independently identify Hepsiburada.
    schema_seller = offer.get('seller')
    if (not isinstance(schema_seller, dict)
            or schema_seller.get('name') not in {'Samsung', 'Hepsiburada'}):
        return None, 'unexpected_schema_seller'
    try:
        price = float(offer.get('price'))
    except (TypeError, ValueError):
        return None, 'schema_price_missing'
    if not math.isfinite(price) or not 0 < price <= 5_000_000:
        return None, 'schema_price_invalid'

    sellers = [link for link in soup.select('a[href]')
               if link.get('href') == SELLER_URL
               and link.get('title') == 'Hepsiburada'
               and _normalized(link.get_text(' ', strip=True)) == 'Hepsiburada'
               and link.parent is not None
               and re.search(r'\bSatıcı\s*:', _normalized(link.parent.get_text(' ', strip=True)))]
    if len(sellers) != 1:
        return None, 'selected_visible_seller_mismatch'
    selected_capacity = soup.select('button[data-test-id="variant-box-Kapasite-256 GB"]')
    selected_color = soup.select('button[data-test-id="variant-box-Renk-Gece Siyahı"]')
    if (len(selected_capacity) != 1 or selected_capacity[0].get('aria-label') != 'Seçili Kapasite 256 GB'
            or len(selected_color) != 1 or selected_color[0].get('aria-label') != 'Seçili Renk Gece Siyahı'):
        return None, 'selected_controls_mismatch'

    price_regions = soup.select('[data-test-id="default-price"]')
    if len(price_regions) != 1:
        return None, 'selected_price_region_missing'
    price_region = price_regions[0]
    visible = price_region.find('span')
    amount = re.fullmatch(r'(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*TL',
                          _normalized(visible.get_text(' ', strip=True)) if visible else '', re.I)
    if not amount or float(amount[1].replace('.', '').replace(',', '.')) != price:
        return None, 'visible_cash_price_mismatch'
    # Delayed import avoids a module cycle once the exact reader is wired into
    # RetailerOfferRefresher. The shared conditional-price policy remains the
    # authority; this reader never overrides it.
    from retailer_offer_refresh import _is_conditional_price
    if _is_conditional_price(soup, offer, price):
        return None, 'conditional_price'
    events = []
    for node in price_region.select('[data-hbus]'):
        try:
            payload = json.loads(node['data-hbus'])
            if isinstance(payload, dict) and isinstance(payload.get('data'), dict):
                events.append(payload['data'])
        except (TypeError, ValueError):
            continue
    if not any(event.get('page_type') == 'pdp' and event.get('placement') == 'buybox'
               and event.get('merchant_name') == 'Hepsiburada'
               and event.get('product_id') == PRODUCT_ID
               and event.get('page_value') == SKU
               and str(event.get('price')) == str(int(price)) for event in events):
        return None, 'selected_buy_box_merchant_mismatch'
    carts = soup.select('button[data-test-id="addToCart"]')
    if (len(carts) != 1 or carts[0].has_attr('disabled')
            or carts[0].get('aria-disabled') == 'true'
            or _normalized(carts[0].get_text(' ', strip=True)).casefold() != 'sepete ekle'):
        return None, 'selected_cart_unavailable'
    result = {'merchant': 'Hepsiburada', 'price': price, 'currency': 'TRY',
              'availability': 'in_stock', 'source_title': title, 'url': URL,
              'observed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}
    if not matcher.has_observed_offer_evidence(NAME, result, SPECS):
        return None, 'catalog_variant_mismatch'
    return result, 'observed_exact_first_party_cash_offer'


class HepsiburadaS25FeSource:
    """Exact direct GET with bounded browser fallback only after HTTP 403."""

    def __init__(self, get=None, post=None, sleep=time.sleep):
        self._get = get or requests.get
        self._post = post or requests.post
        self._sleep = sleep
        self._browser_requests = 0
        self._redirect_location = None

    def _read(self, url, maximum, *, accept):
        self._redirect_location = None
        response = self._get(url, timeout=(5, 15), allow_redirects=False, stream=True,
                             headers={**HEADERS, 'Accept': accept})
        try:
            if response.url != url:
                return None, 'unexpected_final_url', response.status_code
            if response.status_code != 200:
                if response.status_code in (301, 302, 307, 308):
                    self._redirect_location = response.headers.get('Location')
                return None, f'http_{response.status_code}', response.status_code
            length = response.headers.get('Content-Length', '')
            if length.isdigit() and int(length) > maximum:
                return None, 'oversized', response.status_code
            data = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                data.extend(chunk)
                if len(data) > maximum:
                    return None, 'oversized', response.status_code
            return bytes(data), 'ready', response.status_code
        except (requests.RequestException, ValueError, UnicodeError):
            return None, 'transport_error', None
        finally:
            response.close()

    def _browser_read(self, url, maximum):
        if self._browser_requests >= MAX_BROWSER_REQUESTS:
            return None, 'browser_budget_exhausted', None
        self._browser_requests += 1
        response = None
        try:
            response = self._post(SOLVER_URL, json={
                'cmd': 'request.get', 'url': url, 'maxTimeout': 45000,
            }, timeout=60, stream=True)
            if response.status_code != 200:
                return None, f'solver_http_{response.status_code}', response.status_code
            data = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                data.extend(chunk)
                if len(data) > MAX_SOLVER_BYTES:
                    return None, 'solver_oversized', response.status_code
            payload = json.loads(data)
            solution = payload.get('solution') if isinstance(payload, dict) else None
            if not isinstance(payload, dict) or payload.get('status') != 'ok' or not isinstance(solution, dict):
                return None, 'solver_unavailable', response.status_code
            status = solution.get('status')
            if solution.get('url') != url:
                return None, 'browser_changed_url', status
            if status != 200:
                return None, f'browser_site_http_{status}', status
            text = solution.get('response')
            if not isinstance(text, str):
                return None, 'browser_invalid_body', status
            body = text.encode('utf-8')
            if len(body) > maximum:
                return None, 'browser_oversized', status
            return body, 'ready', status
        except (requests.RequestException, ValueError, TypeError, UnicodeError):
            return None, 'browser_unavailable', None
        finally:
            if response is not None:
                response.close()

    def _read_with_fallback(self, url, maximum, *, accept):
        body, reason, status = self._read(url, maximum, accept=accept)
        return (self._browser_read(url, maximum) if reason == 'http_403'
                else (body, reason, status))

    @staticmethod
    def _robots_text(raw):
        text = raw.decode('utf-8-sig', errors='strict')
        if re.search(r'<\s*(?:html|body|pre)\b', text, re.I):
            # Chromium may wrap a text/plain robots response in a single PRE.
            soup = BeautifulSoup(text, 'html.parser')
            blocks = soup.find_all('pre')
            if (len(blocks) != 1 or soup.find(['script', 'form', 'iframe'])
                    or _normalized(soup.get_text(' ', strip=True)) !=
                       _normalized(blocks[0].get_text(' ', strip=True))):
                return ''
            text = blocks[0].get_text()
        return text

    def read_offer(self):
        try:
            robots, reason, status = self._read_with_fallback(
                ROBOTS_URL, MAX_ROBOTS_BYTES, accept='text/plain')
            if reason != 'ready':
                return None, 'robots_' + reason, status
            text = self._robots_text(robots)
            if not re.search(r'^\s*user-agent\s*:', text, re.I | re.M) or '<html' in text.lower():
                return None, 'robots_invalid', status
            rules = RobotFileParser()
            rules.parse(text.splitlines())
            if not rules.can_fetch(BOT, URL):
                return None, 'robots_disallowed', None
            delay = rules.crawl_delay(BOT) or 0
            if delay > 30:
                return None, 'robots_delay_exceeds_budget', None
            if delay:
                self._sleep(delay)
            html, reason, status = self._read_with_fallback(
                URL, MAX_HTML_BYTES, accept='text/html')
            if reason != 'ready':
                return None, reason, status
            offer, reason = parse_exact_buy_box(html.decode('utf-8-sig', errors='strict'))
            return offer, reason, status
        except (requests.RequestException, ValueError, UnicodeError):
            return None, 'transport_error', None
