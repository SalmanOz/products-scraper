"""MediaMarkt's public phone sitemap and selected first-party PDP offers.

Sitemap entries are URL hints only. Each price requires a fresh, robots-allowed
product GET and matching selected Product/Offer, visible price and stock.
"""

import json
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import unescape
from threading import RLock
from urllib.parse import unquote, urljoin, urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
import requests

from vatan_sitemap_source import VatanSitemapSource
from xiaomi_17t_preflight import PROFILES as XIAOMI_17T_PREFLIGHT_URLS, load_verified_offer


BOT = 'TeknoskorPriceBot'
HOST = 'www.mediamarkt.com.tr'
ROBOTS_URL = f'https://{HOST}/robots.txt'
SITEMAP_URLS = tuple(
    f'https://{HOST}/sitemaps/sitemap-productdetailspages-00{i}.xml'
    for i in (0, 1)
)
SOLVER_URL = 'http://localhost:8191/v1'
MAX_ROBOTS_BYTES = 64 * 1024
MAX_SITEMAP_BYTES = 3 * 1024 * 1024
MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_SOLVER_BYTES = 8 * 1024 * 1024
MAX_DIRECT_REQUESTS = 80
# The reviewed-source daily follow-up can cover fourteen MediaMarkt seeds plus
# currently stored exact Android PDPs in one bounded run.
MAX_BROWSER_REQUESTS = 24
# Full-catalog runs need capacity beyond a reviewed seed batch. These are
# run-wide ceilings, not additional retries; pacing and failure circuits stay.
MAX_CATALOG_DIRECT_REQUESTS = 320
MAX_CATALOG_BROWSER_REQUESTS = 160
MAX_CANDIDATES_PER_PRODUCT = 3
_NS = '{http://www.sitemaps.org/schemas/sitemap/0.9}'
_HEADERS = {'User-Agent': BOT + '/1.0 (+https://teknoskor.com)',
            'Accept-Language': 'tr-TR,tr;q=0.9'}
_USED = re.compile(r'outlet|yenilenmi[sş]|refurbish|ikinci[- ]el|te[sş]hir|kullan[iı]lm[iı][sş]', re.I)
_PRICE = re.compile(r'₺\s*(\d{1,3}(?:\.\d{3})*|\d+),')


class SourceBudgetExhausted(requests.RequestException):
    def __init__(self, transport):
        self.transport = transport
        super().__init__(f'MediaMarkt {transport} request budget exhausted')


class SourceCircuitOpen(requests.RequestException):
    pass


REVIEWED_COMPACT_PHONE_URLS = frozenset({
    'https://www.mediamarkt.com.tr/tr/product/_samsung-galaxy-s25fe-8256gb-lacivert-1249060.html',
    'https://www.mediamarkt.com.tr/tr/product/_samsung-galaxy-s25fe-8256gb-buz-mavisi-1249062.html',
})


def product_url(url):
    """Accept only a clean HTTPS MediaMarkt product document URL."""
    if not isinstance(url, str) or len(url) > 500 or re.search(r'[\x00-\x20\\]', url):
        return False
    try:
        parsed = urlparse(url)
        path = unquote(parsed.path)
        return bool(parsed.scheme == 'https' and parsed.hostname == HOST
                    and parsed.username is None and parsed.password is None
                    and parsed.port in (None, 443) and not parsed.query and not parsed.fragment
                    and re.fullmatch(r'/tr/product/_[a-z0-9-]+-\d{6,12}\.html', path)
                    and ('akilli-telefon' in path or url in REVIEWED_COMPACT_PHONE_URLS)
                    and not _USED.search(path))
    except (TypeError, ValueError):
        return False


def _selected_offer(html, final_url):
    soup = BeautifulSoup(html, 'html.parser')
    headings = soup.select('h1')
    if len(headings) != 1:
        return None
    title = ' '.join(headings[0].get_text(' ', strip=True).split())
    if not title or _USED.search(title) or not product_url(final_url):
        return None
    canonical = soup.select('link[rel="canonical"]')
    if len(canonical) != 1 or canonical[0].get('href') != final_url:
        return None
    selected = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            node = json.loads(script.get_text())
        except (TypeError, ValueError):
            continue
        if not isinstance(node, dict) or node.get('@type') != 'BuyAction':
            continue
        product = node.get('object')
        if not isinstance(product, dict) or product.get('@type') not in {'Product', 'ProductGroup'}:
            continue
        if product.get('url') != final_url or product.get('name') != title:
            continue
        offers = product.get('offers')
        offers = offers if isinstance(offers, list) else [offers]
        if len(offers) == 1 and isinstance(offers[0], dict):
            selected.append((product, offers[0]))
    if len(selected) != 1:
        return None
    product, offer = selected[0]
    sku = str(product.get('sku', ''))
    if not re.fullmatch(r'\d{6,12}', sku) or not final_url.endswith('-' + sku + '.html'):
        return None
    brand = product.get('brand')
    if not isinstance(brand, dict) or not isinstance(brand.get('name'), str):
        return None
    if (offer.get('@type') != 'Offer' or offer.get('url') != final_url
            or offer.get('priceCurrency') != 'TRY'
            or str(offer.get('itemCondition', '')).rstrip('/').rsplit('/', 1)[-1] != 'NewCondition'
            or offer.get('seller') is not None):
        return None
    availability = str(offer.get('availability', '')).rstrip('/').rsplit('/', 1)[-1]
    if availability not in {'InStock', 'LimitedAvailability'}:
        return None
    raw_price = offer.get('price')
    if isinstance(raw_price, bool) or not re.fullmatch(r'\d+(?:\.\d{1,2})?', str(raw_price)):
        return None
    price = float(raw_price)
    if not math.isfinite(price) or not 0 < price <= 5_000_000 or price != int(price):
        return None
    # The selected MediaMarkt price feature must say first-party for this exact
    # SKU; marketplace sellers and related-product price features are excluded.
    state = next((script.get_text() for script in soup.select('script')
                  if 'window.__PRELOADED_STATE__' in script.get_text()), '')
    feature = re.search(
        r'"__typename":"CofrPriceFeature","id":"Media:tr:' + re.escape(sku)
        + r'","currency":"TRY","isProductOfTypeMarketplace":false.{0,1200}?"marketplaceSeller":null',
        state,
    )
    if not feature:
        return None
    online = re.search(
        r'"__typename":"CofrOnlineStatusFeature","id":"Media:tr:' + re.escape(sku)
        + r'"[^}]*"isAvailableAndBuyable":true[^}]*"onlineStatus":"AVAILABLE"',
        state,
    )
    if not online:
        return None
    prices = soup.select('[data-test="cofr-price mms-branded-price"]')
    if len(prices) != 1:
        return None
    price_region = prices[0]
    if re.search(r'sepette|kupon|üyelere|uyelere|üyelik|uyelik|loyalty|cashback',
                 price_region.get_text(' ', strip=True), re.I):
        return None
    visible = price_region.select('[data-test="branded-price-whole-value"]')
    if len(visible) != 1:
        return None
    match = _PRICE.fullmatch(visible[0].get_text(' ', strip=True))
    if not match or float(match[1].replace('.', '')) != price:
        return None
    # There may be desktop/mobile duplicates, but at least one selected PDP
    # basket control must be active. A card button elsewhere is not sufficient.
    buttons = [button for button in soup.select(
                   '[data-test="mms-anchor-navigation-bar"] '
                   'button[data-test*="cofr-add-to-basket-button"]')
               if button.get_text(' ', strip=True) == 'Sepete Ekle']
    if not any(button.get('aria-disabled') == 'false' and not button.has_attr('disabled')
               for button in buttons):
        return None
    return {'merchant': 'MediaMarkt', 'price': price, 'currency': 'TRY',
            'availability': 'in_stock' if availability == 'InStock' else 'limited',
            'source_title': unescape(title), 'url': final_url,
            'observed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}


class MediaMarktSource:
    def __init__(self, get=None, post=None, sleep=time.sleep, monotonic=time.monotonic,
                 preflight_file=None, now=None, rejected_offer_observer=None):
        self._get, self._post = get or requests.get, post or requests.post
        self._sleep, self._monotonic = sleep, monotonic
        self._lock = RLock()
        self._rules = None
        self._robots_loaded = False
        self._sitemaps_loaded = False
        self._urls = []
        self._robots_status = 'not_loaded'
        self._sitemap_status = 'not_loaded'
        self._delay = 0.5
        self._last_request = None
        self._direct_count = 0
        self._browser_count = 0
        self._direct_limit = MAX_DIRECT_REQUESTS
        self._browser_limit = MAX_BROWSER_REQUESTS
        self._browser_successes = 0
        self._browser_failures = 0
        self._blocked = False
        self._preflight_file = (preflight_file if preflight_file is not None else
                                os.environ.get('XIAOMI_17T_VERIFIED_AUDIT_FILE'))
        self._now = now or (lambda: datetime.now(timezone.utc))
        # Read-only audits may inspect rejected selected-offer evidence from
        # the HTML already fetched. Normal price refreshes do no extra work.
        self._rejected_offer_observer = rejected_offer_observer

    def configure_catalog_budget(self, product_count):
        """Allocate at most two browser PDP reads per published catalog model."""
        if isinstance(product_count, bool) or not isinstance(product_count, int) or product_count < 1:
            raise ValueError('Catalog product count must be a positive integer')
        with self._lock:
            if self._direct_count or self._browser_count:
                raise RuntimeError('Configure the catalog budget before source requests')
            self._direct_limit = min(MAX_CATALOG_DIRECT_REQUESTS,
                                     max(MAX_DIRECT_REQUESTS, 3 + product_count * 3))
            self._browser_limit = min(MAX_CATALOG_BROWSER_REQUESTS,
                                      max(MAX_BROWSER_REQUESTS, product_count * 2))

    def _pace(self, browser=False):
        with self._lock:
            if self._blocked:
                raise SourceCircuitOpen('MediaMarkt circuit open')
            if browser:
                if self._browser_count >= self._browser_limit:
                    raise SourceBudgetExhausted('browser')
                self._browser_count += 1
            else:
                if self._direct_count >= self._direct_limit:
                    raise SourceBudgetExhausted('direct')
                self._direct_count += 1
            if self._last_request is not None:
                remaining = self._delay - (self._monotonic() - self._last_request)
                if remaining > 0:
                    self._sleep(remaining)
            self._last_request = self._monotonic()

    def _direct(self, url, max_bytes, accept):
        self._pace()
        response = self._get(url, timeout=(5, 20), stream=True, allow_redirects=False,
                             headers={**_HEADERS, 'Accept': accept})
        try:
            if response.url != url:
                return None, 'unexpected_final_url', response.status_code
            if response.status_code in {301, 302, 303, 307, 308}:
                return urljoin(url, response.headers.get('Location', '')), 'redirect', response.status_code
            if response.status_code != 200:
                return None, f'http_{response.status_code}', response.status_code
            length = str(response.headers.get('Content-Length') or '')
            if length.isdigit() and int(length) > max_bytes:
                return None, 'oversized', response.status_code
            data = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                data.extend(chunk)
                if len(data) > max_bytes:
                    return None, 'oversized', response.status_code
            return bytes(data), 'ready', response.status_code
        finally:
            response.close()

    def _load_robots(self):
        if self._robots_loaded:
            return
        with self._lock:
            if self._robots_loaded:
                return
            self._robots_loaded = True
            try:
                body, reason, _ = self._direct(ROBOTS_URL, MAX_ROBOTS_BYTES, 'text/plain')
                if reason != 'ready':
                    self._robots_status = reason
                    return
                policy = body.decode('utf-8-sig', errors='strict')
                if (re.search(r'<(?:!doctype|html|head|body)\b', policy, re.I)
                        or not re.search(r'^\s*user-agent\s*:', policy, re.I | re.M)):
                    self._robots_status = 'invalid_policy'
                    return
                advertised = set(re.findall(r'^\s*Sitemap:\s*(\S+)\s*$', policy, re.I | re.M))
                if not set(SITEMAP_URLS).issubset(advertised):
                    self._robots_status = 'sitemaps_not_advertised'
                    return
                rules = RobotFileParser()
                rules.parse(policy.splitlines())
                delay = float(rules.crawl_delay(BOT) or 0)
                rate = rules.request_rate(BOT)
                if rate:
                    delay = max(delay, rate.seconds / max(1, rate.requests))
                if delay > 30:
                    self._robots_status = 'crawl_delay_exceeds_budget'
                    return
                self._rules = rules
                self._delay = max(0.5, delay)
                self._robots_status = 'ready'
            except (requests.RequestException, ValueError, UnicodeError):
                self._robots_status = 'unavailable'

    def _load_sitemaps(self):
        if self._sitemaps_loaded:
            return
        with self._lock:
            if self._sitemaps_loaded:
                return
            self._sitemaps_loaded = True
            self._load_robots()
            if self._rules is None:
                self._sitemap_status = 'robots_unavailable'
                return
            urls = []
            try:
                for url in SITEMAP_URLS:
                    if not self._rules.can_fetch(BOT, url):
                        raise ValueError('sitemap_disallowed')
                    body, reason, _ = self._direct(url, MAX_SITEMAP_BYTES,
                                                   'application/xml,text/xml')
                    if reason != 'ready':
                        raise ValueError('sitemap_' + reason)
                    if b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():
                        raise ValueError('unsafe_xml')
                    root = ET.fromstring(body)
                    if root.tag != _NS + 'urlset' or len(root) > 20_000:
                        raise ValueError('invalid_sitemap')
                    for item in root:
                        loc = item.find(_NS + 'loc')
                        candidate = loc.text.strip() if loc is not None and loc.text else ''
                        if product_url(candidate) and self._rules.can_fetch(BOT, candidate):
                            urls.append(candidate)
                self._urls = list(dict.fromkeys(urls))
                self._sitemap_status = 'ready'
            except (requests.RequestException, UnicodeError, ET.ParseError, ValueError) as error:
                self._urls = []
                self._sitemap_status = str(error) if isinstance(error, ValueError) else type(error).__name__

    def discover(self, product_name, expected_specs):
        if not isinstance(product_name, str) or not isinstance(expected_specs, dict):
            return []
        self._load_sitemaps()
        if self._sitemap_status != 'ready':
            return []
        candidates = []
        for url in self._urls:
            if VatanSitemapSource._variant_matches(urlparse(url).path, product_name, expected_specs):
                candidates.append({'source_url': url})
                if len(candidates) == MAX_CANDIDATES_PER_PRODUCT:
                    break
        return candidates

    def _browser_get(self, url):
        self._pace(browser=True)
        response = self._post(SOLVER_URL, json={
            'cmd': 'request.get', 'url': url, 'maxTimeout': 45000,
        }, timeout=60, stream=True)
        try:
            if response.status_code != 200:
                return None, None, f'solver_http_{response.status_code}'
            data = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                data.extend(chunk)
                if len(data) > MAX_SOLVER_BYTES:
                    return None, None, 'solver_oversized'
            payload = json.loads(data)
            solution = payload.get('solution') if isinstance(payload, dict) else None
            if not isinstance(payload, dict) or payload.get('status') != 'ok' or not isinstance(solution, dict):
                return None, None, 'solver_unavailable'
            final_url = solution.get('url')
            if not product_url(final_url) or not self._rules.can_fetch(BOT, final_url):
                return None, None, 'unsafe_browser_redirect'
            if solution.get('status') != 200:
                return None, None, f"site_http_{solution.get('status')}"
            html = solution.get('response')
            if not isinstance(html, str):
                return None, None, 'invalid_html'
            body = html.encode('utf-8')
            if len(body) > MAX_HTML_BYTES:
                return None, None, 'html_oversized'
            self._browser_successes += 1
            return body, final_url, 'ready'
        finally:
            response.close()

    def read_offer(self, source_url):
        if not product_url(source_url):
            return None, 'invalid_url', None
        if self._preflight_file and source_url in XIAOMI_17T_PREFLIGHT_URLS:
            offer = load_verified_offer(self._preflight_file, source_url, self._now())
            return (offer, 'observed_exact_offer_from_preflight', 200) if offer else (
                None, 'preflight_invalid_or_stale', None)
        self._load_robots()
        if self._rules is None:
            return None, 'robots_unavailable', None
        current = source_url
        for redirects in range(3):
            if not product_url(current) or not self._rules.can_fetch(BOT, current):
                return None, 'disallowed_or_changed_url', None
            try:
                body, reason, status = self._direct(current, MAX_HTML_BYTES, 'text/html')
                if reason == 'redirect':
                    if redirects == 2 or not product_url(body) or not self._rules.can_fetch(BOT, body):
                        return None, 'unsafe_redirect', status
                    current = body
                    continue
                if reason == 'http_403':
                    body, current, reason = self._browser_get(current)
                    if reason != 'ready':
                        self._browser_failures += 1
                        if self._browser_failures >= 2:
                            self._blocked = True
                        return None, reason, status
                elif reason != 'ready':
                    return None, reason, status
                offer = _selected_offer(body, current)
                if offer is None:
                    # The same fetched document can contain a selected marketplace
                    # seller. Preserve that identity instead of calling it MediaMarkt.
                    from mediamarkt_marketplace_audit import selected_marketplace_offer
                    evidence = (selected_marketplace_offer(body, current) or
                                selected_marketplace_offer(body, current, allow_cents=True))
                    if evidence is not None:
                        offer = {**evidence, 'merchant': evidence['seller_name'],
                                 'observed_at': self._now().isoformat().replace('+00:00', 'Z')}
                if offer is None and self._rejected_offer_observer is not None:
                    try:
                        self._rejected_offer_observer(current, body)
                    except Exception:
                        # Diagnostics must never make a price reader succeed
                        # or fail differently from its strict offer contract.
                        pass
                return offer, 'observed_exact_offer' if offer else 'no_exact_product_offer', 200
            except SourceBudgetExhausted as error:
                # A run-wide request quota is not a retailer transport outage.
                # Do not open the failure circuit: a later exact PDP may still
                # return a direct 200 without spending the browser quota.
                return None, f'{error.transport}_budget_exhausted', None
            except SourceCircuitOpen:
                return None, 'source_circuit_open', None
            except (requests.RequestException, ValueError, TypeError, UnicodeError):
                self._browser_failures += 1
                if self._browser_failures >= 2:
                    self._blocked = True
                return None, 'transport_error', None
        return None, 'redirect_limit', None

    def report(self):
        return {'robots_status': self._robots_status, 'sitemap_status': self._sitemap_status,
                'eligible_urls': len(self._urls), 'direct_requests': self._direct_count,
                'browser_requests': self._browser_count,
                'direct_request_limit': self._direct_limit,
                'browser_request_limit': self._browser_limit,
                'browser_html_responses': self._browser_successes}
