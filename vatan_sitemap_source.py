"""Discover Vatan product URLs from its published sitemap, never prices.

Vatan disallows ``/arama/*`` in robots.txt. Its advertised sitemap gives us
bounded, public URL hints; the existing retailer PDP reader remains the only
source of live price, availability and product identity evidence.
"""

import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlparse
from urllib.robotparser import RobotFileParser

import requests

from tr_price_scraper import TRPriceScraper


HOST = 'www.vatanbilgisayar.com'
ROBOTS_URL = f'https://{HOST}/robots.txt'
SITEMAP_URL = f'https://{HOST}/sitemap.axd'
BOT = 'TeknoskorPriceBot'
MAX_ROBOTS_BYTES = 64 * 1024
MAX_SITEMAP_BYTES = 3 * 1024 * 1024
MAX_SITEMAP_URLS = 20_000
MAX_PRODUCT_CANDIDATES = 4
_SITEMAP_NS = '{http://www.sitemaps.org/schemas/sitemap/0.9}'


class VatanSitemapSource:
    """One robots read and one sitemap read per updater run, including failure."""

    def __init__(self, fetch=None, sleep=time.sleep):
        self._fetch = fetch or requests.get
        self._sleep = sleep
        self._loaded = False
        self._urls = []
        self._rules = None
        self._status = 'not_loaded'

    def _read(self, url, limit):
        response = self._fetch(
            url, timeout=(5, 15), allow_redirects=False, stream=True,
            headers={
                'User-Agent': BOT + '/1.0 (+https://teknoskor.com)',
                'Accept': 'text/plain' if url == ROBOTS_URL else 'application/xml,text/xml',
                'Accept-Language': 'tr-TR,tr;q=0.9',
            },
        )
        try:
            if response.status_code != 200 or response.url != url:
                raise ValueError('unavailable_or_redirected')
            size = str(response.headers.get('Content-Length') or '')
            if size.isdigit() and int(size) > limit:
                raise ValueError('oversized_response')
            chunks = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                chunks.extend(chunk)
                if len(chunks) > limit:
                    raise ValueError('oversized_response')
            return bytes(chunks)
        finally:
            response.close()

    @staticmethod
    def _safe_product_url(url):
        if not isinstance(url, str) or len(url) > 500 or re.search(r'[\x00-\x20\\]', url):
            return None
        try:
            parsed = urlparse(url)
            if (parsed.scheme != 'https' or parsed.hostname != HOST
                    or parsed.username is not None or parsed.password is not None
                    or parsed.port not in (None, 443) or parsed.query or parsed.fragment):
                return None
            path = unquote(parsed.path)
            if (not path.endswith('.html') or 'akilli-telefon' not in path.lower()
                    or re.search(r'[\x00-\x20\\]|/arama/|(?:kilif|ekran-koruyucu|yenilenmis|outlet)', path, re.I)):
                return None
            return parsed
        except (TypeError, ValueError):
            return None

    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            raw_robots = self._read(ROBOTS_URL, MAX_ROBOTS_BYTES)
            robots = raw_robots.decode('utf-8-sig', errors='strict')
            if (re.search(r'<(?:!doctype|html|head|body)\b', robots, re.I)
                    or not re.search(r'^\s*user-agent\s*:', robots, re.I | re.M)
                    or not re.search(r'^\s*sitemap\s*:\s*' + re.escape(SITEMAP_URL) + r'\s*$', robots, re.I | re.M)):
                raise ValueError('robots_policy_unavailable')
            rules = RobotFileParser()
            rules.parse(robots.splitlines())
            if not rules.can_fetch(BOT, SITEMAP_URL):
                raise ValueError('sitemap_disallowed')
            self._rules = rules

            delay = float(rules.crawl_delay(BOT) or 0)
            rate = rules.request_rate(BOT)
            if rate:
                delay = max(delay, rate.seconds / max(1, rate.requests))
            if delay > 30:
                raise ValueError('crawl_delay_exceeds_budget')
            if delay:
                self._sleep(delay)

            raw_sitemap = self._read(SITEMAP_URL, MAX_SITEMAP_BYTES)
            if b'<!DOCTYPE' in raw_sitemap.upper() or b'<!ENTITY' in raw_sitemap.upper():
                raise ValueError('unsafe_xml')
            root = ET.fromstring(raw_sitemap)
            if root.tag != _SITEMAP_NS + 'urlset':
                raise ValueError('not_urlset')
            if len(root) > MAX_SITEMAP_URLS:
                raise ValueError('too_many_urls')
            urls = []
            for row in root:
                if row.tag != _SITEMAP_NS + 'url':
                    continue
                loc = row.find(_SITEMAP_NS + 'loc')
                url = loc.text.strip() if loc is not None and isinstance(loc.text, str) else ''
                if self._safe_product_url(url) and rules.can_fetch(BOT, url):
                    urls.append(url)
            self._urls = list(dict.fromkeys(urls))
            self._status = 'ready'
        except (requests.RequestException, UnicodeError, ET.ParseError, ValueError, AttributeError) as error:
            self._urls = []
            self._status = str(error) if isinstance(error, ValueError) else type(error).__name__

    @staticmethod
    def _variant_matches(path, product_name, expected_specs):
        storage = TRPriceScraper._spec_int(expected_specs.get('storage_gb'))
        ram = TRPriceScraper._spec_int(expected_specs.get('ram_gb'))
        if not storage or storage < 32:
            return False
        slug = unquote(path).lower().removesuffix('.html').strip('/')
        title = slug.replace('-', ' ').replace('_', ' ')
        matcher = TRPriceScraper()
        if not matcher.is_strict_match(product_name, title, expected_specs):
            return False
        # Explicit capacity in the URL prevents a generic family URL from
        # silently selecting the first colour/storage option on the PDP.
        if not re.search(rf'(?:^|[-_]){storage}(?:[-_]?gb)(?:[-_]|$)', slug):
            return False
        is_iphone = 'iphone' in matcher._normalize_words(product_name).split()
        if not is_iphone:
            if not ram:
                return False
            pair = rf'(?:^|[-_]){ram}(?:[-_]?gb)?[-_]{storage}(?:[-_]?gb)(?:[-_]|$)'
            labelled = rf'(?:^|[-_]){ram}[-_]?gb[-_]?ram(?:[-_]|$)|(?:^|[-_])ram[-_]?{ram}[-_]?gb(?:[-_]|$)'
            if not (re.search(pair, slug) or re.search(labelled, slug)):
                return False
        # Catalog summaries often omit Connectivity. A sitemap URL is only a
        # hint: the PDP reader later requires the observed title/offer to pass
        # its stricter radio and variant checks. Never admit an explicit clash.
        radio = expected_specs.get('network_generation') or matcher._radio_generation(product_name)
        observed = matcher._radio_generation(title)
        return not (radio and observed and observed != radio)

    def discover(self, product_name, expected_specs):
        if not isinstance(product_name, str) or not isinstance(expected_specs, dict):
            return []
        self._load()
        if not self._rules:
            return []
        candidates = []
        for url in self._urls:
            parsed = urlparse(url)
            if self._variant_matches(parsed.path, product_name, expected_specs):
                candidates.append({'source_url': url})
                if len(candidates) == MAX_PRODUCT_CANDIDATES:
                    break
        return candidates

    def report(self):
        return {'status': self._status, 'eligible_urls': len(self._urls),
                'robots_url': ROBOTS_URL, 'sitemap_url': SITEMAP_URL}
