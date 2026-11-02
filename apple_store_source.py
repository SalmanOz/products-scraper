"""Four reviewed Apple Store Türkiye iPhone PDPs, with live selected-offer proof.

The static URLs are identity hints. A price is emitted only after robots allows
the exact PDP and a fresh browser GET shows the selected 256 GB black variant,
matching Product/Offer, cash price, and active bag control.
"""

import json
import math
import re
import time
from datetime import datetime, timezone
from threading import RLock
from urllib.parse import unquote, urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
import requests

from audit_apple_store_access import PDP_URLS


BOT = 'TeknoskorPriceBot'
ROBOTS_URL = 'https://www.apple.com/robots.txt'
SOLVER_URL = 'http://localhost:8191/v1'
MAX_ROBOTS_BYTES = 64 * 1024
MAX_HTML_BYTES = 1024 * 1024
MAX_SOLVER_BYTES = 3 * 1024 * 1024
MAX_BROWSER_REQUESTS = 4
_HEADERS = {'User-Agent': BOT + '/1.0 (+https://teknoskor.com)',
            'Accept-Language': 'tr-TR,tr;q=0.9', 'Accept': 'text/plain'}
_NAMES = {
    'apple-iphone-18-pro-max': 'iPhone 18 Pro Max 256 GB Siyah',
    'apple-iphone-18-pro': 'iPhone 18 Pro 256 GB Siyah',
    'apple-iphone-17e': 'iPhone 17e 256 GB Siyah',
    'apple-iphone-17': 'iPhone 17 256 GB Siyah',
}


def _clean(text):
    return ' '.join(str(text).split())


def reviewed_slug(url):
    if not isinstance(url, str) or len(url) > 350 or re.search(r'[\x00-\x20\\]', url):
        return None
    try:
        parsed = urlparse(url)
        if (parsed.scheme != 'https' or parsed.hostname != 'www.apple.com'
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.query or parsed.fragment):
            return None
        for slug, reviewed in PDP_URLS.items():
            if unquote(parsed.path) == unquote(urlparse(reviewed).path):
                return slug
    except (TypeError, ValueError):
        pass
    return None


def selected_offer(html, final_url):
    slug = reviewed_slug(final_url)
    if slug is None:
        return None
    soup = BeautifulSoup(html, 'html.parser')
    expected_name = _NAMES[slug]
    products = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.get_text())
        except (ValueError, TypeError):
            continue
        for product in value if isinstance(value, list) else [value]:
            if not isinstance(product, dict) or product.get('@type') != 'Product':
                continue
            if (reviewed_slug(product.get('url')) != slug
                    or reviewed_slug(product.get('mainEntityOfPage')) != slug
                    or _clean(product.get('name')) != expected_name
                    or _clean(product.get('color')) != 'Siyah'):
                continue
            offers = product.get('offers')
            if isinstance(offers, list) and len(offers) == 1 and isinstance(offers[0], dict):
                products.append(offers[0])
    if len(products) != 1:
        return None
    offer = products[0]
    if (offer.get('@type') != 'Offer' or offer.get('priceCurrency') != 'TRY'
            or offer.get('seller') is not None
            or str(offer.get('itemCondition', 'NewCondition')).rstrip('/').rsplit('/', 1)[-1]
            != 'NewCondition'
            or not re.fullmatch(r'[A-Z0-9]{5,16}TU/A', str(offer.get('sku', '')))):
        return None
    raw_price = offer.get('price')
    if isinstance(raw_price, bool) or not re.fullmatch(r'\d+(?:\.\d{1,2})?', str(raw_price)):
        return None
    price = float(raw_price)
    if not math.isfinite(price) or not 0 < price <= 5_000_000 or price != int(price):
        return None
    selected_color = soup.select('input[type="radio"][name="dimensionColor"][checked]')
    selected_capacity = soup.select('input[type="radio"][name="dimensionCapacity"][checked]')
    if (len(selected_color) != 1 or selected_color[0].get('value') != 'black'
            or len(selected_capacity) != 1 or selected_capacity[0].get('value') != '256gb'):
        return None
    selected_screen = soup.select('input[type="radio"][name="dimensionScreensize"][checked]')
    expected_screen = {'apple-iphone-18-pro-max': '6_9inch',
                       'apple-iphone-18-pro': '6_3inch'}.get(slug)
    if expected_screen and (len(selected_screen) != 1
                            or selected_screen[0].get('value') != expected_screen):
        return None
    if not expected_screen and selected_screen:
        return None
    summary = soup.select('.rf-bfe-summary')
    if len(summary) != 1:
        return None
    names = summary[0].select('[data-autom="summary-productName"]')
    prices = summary[0].select('[data-autom="full-price"]')
    visible_price = f'{price:,.2f}'.replace(',', '_').replace('.', ',').replace('_', '.') + ' TL'
    if (len(names) != 1 or _clean(names[0].get_text(' ', strip=True)) != expected_name
            or len(prices) != 1 or _clean(prices[0].get_text(' ', strip=True)) != visible_price):
        return None
    buttons = summary[0].select('button[data-autom="add-to-cart"]')
    if (len(buttons) != 1 or _clean(buttons[0].get_text(' ', strip=True)) != 'Çantaya Ekleyin'
            or buttons[0].has_attr('disabled') or buttons[0].get('aria-disabled') == 'true'):
        return None
    return {'merchant': 'Apple Store', 'price': price, 'currency': 'TRY',
            'availability': 'in_stock', 'source_title': 'Apple ' + expected_name,
            'url': final_url,
            'observed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}


class AppleStoreSource:
    def __init__(self, get=None, post=None, sleep=time.sleep, monotonic=time.monotonic):
        self._get, self._post = get or requests.get, post or requests.post
        self._sleep, self._monotonic = sleep, monotonic
        self._lock = RLock()
        self._rules = None
        self._robots_status = 'not_loaded'
        self._robots_loaded = False
        self._delay = 0.5
        self._last_request = None
        self._browser_count = 0
        self._browser_successes = 0

    def _load_robots(self):
        with self._lock:
            if self._robots_loaded:
                return
            self._robots_loaded = True
            try:
                response = self._get(ROBOTS_URL, timeout=(5, 20), stream=True,
                                     allow_redirects=False, headers=_HEADERS)
                try:
                    size = str(response.headers.get('Content-Length') or '')
                    if response.status_code != 200 or response.url != ROBOTS_URL or (
                            size.isdigit() and int(size) > MAX_ROBOTS_BYTES):
                        self._robots_status = 'http_or_oversized'
                        return
                    body = bytearray()
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        body.extend(chunk)
                        if len(body) > MAX_ROBOTS_BYTES:
                            self._robots_status = 'oversized'
                            return
                finally:
                    response.close()
                policy = body.decode('utf-8-sig', errors='strict')
                if (re.search(r'<(?:!doctype|html|head|body)\b', policy, re.I)
                        or not re.search(r'^\s*user-agent\s*:', policy, re.I | re.M)):
                    self._robots_status = 'invalid_policy'
                    return
                rules = RobotFileParser()
                rules.parse(policy.splitlines())
                delay = float(rules.crawl_delay(BOT) or 0)
                rate = rules.request_rate(BOT)
                if rate:
                    delay = max(delay, rate.seconds / max(1, rate.requests))
                if delay > 30:
                    self._robots_status = 'delay_exceeds_budget'
                    return
                self._delay = max(0.5, delay)
                self._rules = rules
                self._robots_status = 'ready'
            except (requests.RequestException, TypeError, ValueError, UnicodeError):
                self._robots_status = 'unavailable'

    def read_offer(self, source_url):
        slug = reviewed_slug(source_url)
        if slug is None:
            return None, 'unreviewed_apple_pdp', None
        self._load_robots()
        if self._rules is None or not self._rules.can_fetch(BOT, source_url):
            return None, 'robots_unavailable_or_disallowed', None
        with self._lock:
            if self._browser_count >= MAX_BROWSER_REQUESTS:
                return None, 'browser_budget_exhausted', None
            self._browser_count += 1
            if self._last_request is not None:
                wait = self._delay - (self._monotonic() - self._last_request)
                if wait > 0:
                    self._sleep(wait)
            self._last_request = self._monotonic()
        try:
            response = self._post(SOLVER_URL, json={
                'cmd': 'request.get', 'url': source_url, 'maxTimeout': 45000,
            }, timeout=60, stream=True)
            try:
                size = str(response.headers.get('Content-Length') or '')
                if response.status_code != 200 or (size.isdigit() and int(size) > MAX_SOLVER_BYTES):
                    return None, 'solver_http_or_oversized', response.status_code
                body = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    body.extend(chunk)
                    if len(body) > MAX_SOLVER_BYTES:
                        return None, 'solver_oversized', response.status_code
            finally:
                response.close()
            payload = json.loads(body)
            solution = payload.get('solution') if isinstance(payload, dict) else None
            if not isinstance(solution, dict) or payload.get('status') != 'ok':
                return None, 'solver_unavailable', None
            final_url = solution.get('url')
            if (solution.get('status') != 200 or reviewed_slug(final_url) != slug
                    or not self._rules.can_fetch(BOT, final_url)):
                return None, 'unsafe_or_unavailable_final_pdp', solution.get('status')
            html = solution.get('response')
            if not isinstance(html, str) or len(html.encode('utf-8')) > MAX_HTML_BYTES:
                return None, 'invalid_or_oversized_html', 200
            self._browser_successes += 1
            candidate = selected_offer(html, final_url)
            return (candidate, 'observed_exact_offer' if candidate else 'no_exact_product_offer', 200)
        except (requests.RequestException, ValueError, TypeError, UnicodeError):
            return None, 'transport_or_parse_error', None

    def report(self):
        return {'robots_status': self._robots_status,
                'browser_requests': self._browser_count,
                'browser_html_responses': self._browser_successes}
