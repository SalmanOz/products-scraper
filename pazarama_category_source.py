"""Discover Pazarama phone PDP URLs from its public Android category.

Category cards provide URL hints only. The retailer PDP reader independently
verifies the selected SKU, seller, stock and visible non-cart cash price.
"""

import re
import time
from threading import RLock
from types import SimpleNamespace
from urllib.parse import parse_qsl, unquote, urljoin, urlparse

from bs4 import BeautifulSoup
import requests

from tr_price_scraper import TRPriceScraper


HOST = 'www.pazarama.com'
ROBOTS_URL = f'https://{HOST}/robots.txt'
CATEGORY_URL = f'https://{HOST}/cep-telefonu-k-K03043?isletim-sistemi=android&ft'
BOT = 'TeknoskorPriceBot'
MAX_CATEGORY_PAGES = 16
MAX_PRODUCT_CANDIDATES = 3
MAX_PDP_REQUESTS = 36
MAX_ROBOTS_BYTES = 64 * 1024
MAX_CATEGORY_BYTES = 4 * 1024 * 1024
MAX_PDP_BYTES = 2 * 1024 * 1024


def pazarama_product_url(url):
    """Only clean, exact Pazarama PDPs; no seller selectors or tracking query."""
    if not isinstance(url, str) or len(url) > 600 or re.search(r'[\x00-\x20\\]', url):
        return False
    try:
        parsed = urlparse(url)
        path = unquote(parsed.path)
        return bool(parsed.scheme == 'https' and parsed.hostname == HOST
                    and parsed.username is None and parsed.password is None
                    and parsed.port in (None, 443) and not parsed.query and not parsed.fragment
                    and not re.search(r'[\x00-\x20\\/?#]', path[1:])
                    and re.fullmatch(r'/[a-z0-9][a-z0-9-]*-p-[a-z0-9-]+', path, re.I))
    except (TypeError, ValueError):
        return False


def _category_page_number(url):
    try:
        parsed = urlparse(url)
        if (parsed.scheme != 'https' or parsed.hostname != HOST or parsed.path != '/cep-telefonu-k-K03043'
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.fragment):
            return None
        pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=5)
        if len(pairs) != len(dict(pairs)):
            return None
        query = dict(pairs)
        if query.get('isletim-sistemi') != 'android' or query.get('ft') != '':
            return None
        if set(query) == {'isletim-sistemi', 'ft'}:
            return 1
        if set(query) != {'isletim-sistemi', 'ft', 'sayfa'} or not re.fullmatch(r'\d{1,2}', query['sayfa']):
            return None
        page = int(query['sayfa'])
        return page if 2 <= page <= MAX_CATEGORY_PAGES else None
    except (TypeError, ValueError):
        return None


class PazaramaCategorySource:
    """At most sixteen category reads, cached per run."""

    def __init__(self, fetch=None, sleep=time.sleep, clock=time.monotonic):
        self._fetch = fetch or requests.get
        self._sleep, self._clock = sleep, clock
        self._lock = RLock()
        self._blocked = False
        self._loaded = False
        self._cards = []
        self._pages = {}
        self._last_request = None
        self._pdp_requests = 0
        self._category_requests = 0
        self._status = 'not_loaded'
        self._failed_stage = None
        self._http_status = None
        self._matcher = TRPriceScraper()

    def _request(self, url, limit):
        if self._blocked:
            raise requests.RequestException('Pazarama source circuit open')
        if self._last_request is not None:
            self._sleep(max(0, 0.5 - (self._clock() - self._last_request)))
        self._last_request = self._clock()
        response = self._fetch(
            url, timeout=(5, 15), allow_redirects=False, stream=True,
            headers={
                'User-Agent': BOT + '/1.0 (+https://teknoskor.com)',
                'Accept': 'text/html',
                'Accept-Language': 'tr-TR,tr;q=0.9',
            },
        )
        try:
            self._http_status = response.status_code
            if response.url != url:
                self._blocked = True
                raise requests.RequestException('Unexpected Pazarama final URL')
            if response.status_code in (403, 429, 500, 502, 503, 504):
                self._blocked = True
            size = str(response.headers.get('Content-Length') or '')
            if size.isdigit() and int(size) > limit:
                raise requests.RequestException('Oversized Pazarama response')
            body = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                body.extend(chunk)
                if len(body) > limit:
                    raise requests.RequestException('Oversized Pazarama response')
            return SimpleNamespace(status_code=response.status_code, url=url,
                                   headers=dict(response.headers),
                                   text=body.decode('utf-8-sig', errors='strict'))
        finally:
            response.close()

    def fetch(self, url, **_kwargs):
        """Bounded PDP read; no redirects, repeated reads or query URLs."""
        if not pazarama_product_url(url):
            raise requests.RequestException('Not a clean Pazarama product URL')
        with self._lock:
            if url not in self._pages:
                if self._pdp_requests >= MAX_PDP_REQUESTS:
                    raise requests.RequestException('Pazarama PDP budget exhausted')
                self._pdp_requests += 1
                self._pages[url] = self._request(url, MAX_PDP_BYTES)
            return self._pages[url]

    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            url = CATEGORY_URL
            for expected_page in range(1, MAX_CATEGORY_PAGES + 1):
                if _category_page_number(url) != expected_page:
                    raise ValueError('Unexpected Pazarama category page')
                self._failed_stage = 'category'
                self._category_requests += 1
                response = self._request(url, MAX_CATEGORY_BYTES)
                if response.status_code != 200:
                    raise ValueError(f'category_http_{response.status_code}')
                soup = BeautifulSoup(response.text, 'html.parser')
                heading = ' '.join(node.get_text(' ', strip=True) for node in soup.select('title,h1'))
                if re.search(r'captcha|just a moment|access denied', heading, re.I):
                    self._blocked = True
                    raise ValueError('category_challenge')
                for anchor in soup.select('a[href][title]'):
                    if not anchor.select_one('.product-card__swiper'):
                        continue
                    product_url = urljoin(url, anchor['href'])
                    if pazarama_product_url(product_url):
                        self._cards.append((product_url, anchor['title'].strip()))
                next_page = next((urljoin(url, anchor['href']) for anchor in soup.select('a[href]')
                                  if anchor.get_text(' ', strip=True) == 'Daha Çok Ürün Göster'
                                  and _category_page_number(urljoin(url, anchor['href'])) == expected_page + 1), None)
                if not next_page:
                    break
                url = next_page
            self._cards = list(dict.fromkeys(self._cards))
            self._status = 'ready'
            self._failed_stage = None
        except (requests.RequestException, UnicodeError, ValueError, TypeError) as error:
            self._cards = []
            self._status = str(error) if isinstance(error, ValueError) else type(error).__name__

    def discover(self, product_name, expected_specs):
        if not isinstance(product_name, str) or not isinstance(expected_specs, dict):
            return []
        if 'iphone' in self._matcher._normalize_words(product_name).split():
            return []
        if (not self._matcher._spec_int(expected_specs.get('ram_gb'))
                or not self._matcher._spec_int(expected_specs.get('storage_gb'))):
            return []
        with self._lock:
            self._load()
            candidates = []
            for url, title in self._cards:
                hint = {'source_title': title, 'url': url}
                if self._matcher.has_observed_offer_evidence(product_name, hint, expected_specs):
                    candidates.append({'source_url': url})
                    if len(candidates) == MAX_PRODUCT_CANDIDATES:
                        break
            return candidates

    def report(self):
        with self._lock:
            return {'status': self._status, 'category_pages': self._category_requests,
                    'eligible_card_urls': len(self._cards), 'pdp_requests': self._pdp_requests,
                    'category_url': CATEGORY_URL,
                    'failed_stage': self._failed_stage,
                    'http_status': self._http_status if self._failed_stage else None}
