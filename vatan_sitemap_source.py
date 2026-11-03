"""Discover Vatan product URLs from its published sitemap, never prices.

Vatan disallows ``/arama/*`` in robots.txt. Its advertised sitemap gives us
bounded, public URL hints; the existing retailer PDP reader remains the only
source of live price, availability and product identity evidence. A denied
direct sitemap GET may retry once through the already running browser service.
"""

import json
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup, Tag

from tr_price_scraper import TRPriceScraper


HOST = 'www.vatanbilgisayar.com'
ROBOTS_URL = f'https://{HOST}/robots.txt'
SITEMAP_URL = f'https://{HOST}/sitemap.axd'
BOT = 'TeknoskorPriceBot'
MAX_ROBOTS_BYTES = 64 * 1024
MAX_SITEMAP_BYTES = 3 * 1024 * 1024
# Chrome's measured XML viewer is ~10.7 MiB; the original XML is much smaller.
# Bound the browser envelope separately from the source sitemap.
MAX_SOLVER_BYTES = 16 * 1024 * 1024
MAX_BROWSER_DOCUMENT_BYTES = 12 * 1024 * 1024
MAX_SITEMAP_URLS = 20_000
MAX_PRODUCT_CANDIDATES = 4
MAX_BROWSER_REQUESTS = 1
SOLVER_URL = 'http://localhost:8191/v1'
_SITEMAP_NS = '{http://www.sitemaps.org/schemas/sitemap/0.9}'


class VatanSitemapSource:
    """One robots read and one sitemap read per updater run, including failure."""

    def __init__(self, fetch=None, post=None, sleep=time.sleep):
        self._fetch = fetch or requests.get
        self._post = post or requests.post
        self._sleep = sleep
        self._loaded = False
        self._urls = []
        self._rules = None
        self._status = 'not_loaded'
        self._failed_stage = None
        self._http_status = None
        self._crawl_delay = 0
        self._sitemap_transport = 'not_attempted'
        self._sitemap_direct_status = None
        self._browser_requests = 0
        self._browser_xml_unwrapped = False

    def _browser_sitemap(self):
        """One exact-URL retry, only after a robots-approved sitemap GET is 403."""
        if (self._rules is None or not self._rules.can_fetch(BOT, SITEMAP_URL)
                or self._browser_requests >= MAX_BROWSER_REQUESTS):
            raise ValueError('sitemap_browser_not_allowed')
        self._browser_requests += 1
        self._sleep(max(0.5, self._crawl_delay))
        response = self._post(SOLVER_URL, json={
            'cmd': 'request.get', 'url': SITEMAP_URL, 'maxTimeout': 45000,
        }, timeout=60, stream=True)
        try:
            if response.status_code != 200:
                raise ValueError(f'sitemap_solver_http_{response.status_code}')
            data = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                data.extend(chunk)
                if len(data) > MAX_SOLVER_BYTES:
                    raise ValueError('sitemap_solver_oversized')
        finally:
            response.close()
        try:
            payload = json.loads(data)
        except (TypeError, ValueError) as error:
            raise ValueError('sitemap_solver_invalid_json') from error
        solution = payload.get('solution') if isinstance(payload, dict) else None
        if not isinstance(payload, dict) or payload.get('status') != 'ok' or not isinstance(solution, dict):
            raise ValueError('sitemap_solver_unavailable')
        if solution.get('url') != SITEMAP_URL:
            raise ValueError('sitemap_browser_changed_url')
        if solution.get('status') != 200:
            raise ValueError(f"sitemap_browser_http_{solution.get('status')}")
        xml = solution.get('response')
        if not isinstance(xml, str):
            raise ValueError('sitemap_browser_missing_xml')
        body = xml.encode('utf-8')
        if len(body) > MAX_BROWSER_DOCUMENT_BYTES:
            raise ValueError('sitemap_browser_oversized')
        if 'webkit-xml-viewer-source-xml' in xml:
            if re.search(r'<!DOCTYPE(?!\s+html\b)|<!ENTITY', xml, re.I):
                raise ValueError('unsafe_xml')
            soup = BeautifulSoup(xml, 'html.parser')
            containers = soup.select('#webkit-xml-viewer-source-xml')
            roots = ([node for node in containers[0].children if isinstance(node, Tag)]
                     if len(containers) == 1 else [])
            if (len(roots) != 1 or roots[0].name != 'urlset' or
                    roots[0].get('xmlns') != _SITEMAP_NS[1:-1]):
                raise ValueError('sitemap_browser_invalid_xml_wrapper')
            body = str(roots[0]).encode('utf-8')
            self._browser_xml_unwrapped = True
        if len(body) > MAX_SITEMAP_BYTES:
            raise ValueError('sitemap_browser_oversized')
        self._sitemap_transport = 'browser'
        self._http_status = 200
        return body

    def _read(self, url, limit):
        stage = 'robots' if url == ROBOTS_URL else 'sitemap'
        self._failed_stage = stage
        self._http_status = None
        response = self._fetch(
            url, timeout=(5, 15), allow_redirects=False, stream=True,
            headers={
                'User-Agent': BOT + '/1.0 (+https://teknoskor.com)',
                'Accept': 'text/plain' if url == ROBOTS_URL else 'application/xml,text/xml',
                'Accept-Language': 'tr-TR,tr;q=0.9',
            },
        )
        retry_browser = False
        try:
            self._http_status = response.status_code
            if stage == 'sitemap':
                self._sitemap_direct_status = response.status_code
            if response.url != url:
                raise ValueError(f'{stage}_unexpected_final_url')
            if response.status_code != 200:
                if stage == 'sitemap' and response.status_code == 403:
                    retry_browser = True
                else:
                    raise ValueError(f'{stage}_http_{response.status_code}')
            else:
                size = str(response.headers.get('Content-Length') or '')
                if size.isdigit() and int(size) > limit:
                    raise ValueError('oversized_response')
                chunks = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    chunks.extend(chunk)
                    if len(chunks) > limit:
                        raise ValueError('oversized_response')
                if stage == 'sitemap':
                    self._sitemap_transport = 'direct'
                return bytes(chunks)
        finally:
            response.close()
        if retry_browser:
            return self._browser_sitemap()
        raise ValueError(f'{stage}_unavailable')

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
            self._crawl_delay = delay
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
            self._failed_stage = None
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
        slug = re.sub(r'ram(?=\d)', 'ram-', slug)
        title = slug.replace('-', ' ').replace('_', ' ')
        matcher = TRPriceScraper()
        if not matcher.is_strict_match(product_name, title, expected_specs):
            return False
        # Some Vatan phone slugs publish the exact pair as 16-512-akilli-telefon,
        # with no GB suffix. This is a discovery hint only: the selected PDP's
        # actual title/Offer still has to prove both capacities before use.
        unitless_pair = bool(ram and re.search(
            rf'(?:^|[-_]){ram}[-_]{storage}[-_]akilli[-_]telefon(?:[-_]|$)', slug))
        # Explicit capacity in the URL prevents a generic family URL from
        # silently selecting the first colour/storage option on the PDP.
        if not (re.search(rf'(?:^|[-_]){storage}(?:[-_]?gb)(?:[-_]|$)', slug)
                or unitless_pair):
            return False
        is_iphone = 'iphone' in matcher._normalize_words(product_name).split()
        if not is_iphone:
            if not ram:
                return False
            pair = rf'(?:^|[-_]){ram}(?:[-_]?gb)?[-_]{storage}(?:[-_]?gb)(?:[-_]|$)'
            reversed_pair = rf'(?:^|[-_]){storage}[-_]?gb[-_]{ram}[-_]?gb(?:[-_]|$)'
            labelled = rf'(?:^|[-_]){ram}[-_]?gb[-_]?ram(?:[-_]|$)|(?:^|[-_])ram[-_]?{ram}[-_]?gb(?:[-_]|$)'
            if not (re.search(pair, slug) or re.search(reversed_pair, slug)
                    or re.search(labelled, slug) or unitless_pair):
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
                'robots_url': ROBOTS_URL, 'sitemap_url': SITEMAP_URL,
                'sitemap_transport': self._sitemap_transport,
                'sitemap_direct_http_status': self._sitemap_direct_status,
                'browser_requests': self._browser_requests,
                'browser_xml_viewer_unwrapped': self._browser_xml_unwrapped,
                'failed_stage': self._failed_stage,
                'http_status': self._http_status if self._failed_stage else None}
