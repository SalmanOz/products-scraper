"""Bounded read-only probe of Teknosa's advertised first product sitemap.

The sitemap gives URL hints only. It does not prove a selected seller, cash
price, stock or catalog identity, and this module is not connected to writers.
"""

import json
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup


BOT = 'TeknoskorPriceBot'
HOST = 'www.teknosa.com'
ROBOTS_URL = f'https://{HOST}/robots.txt'
INDEX_URL = f'https://{HOST}/siteharitasi.xml'
SOLVER_URL = 'http://localhost:8191/v1'
NS = '{http://www.sitemaps.org/schemas/sitemap/0.9}'
MAX_ROBOTS_BYTES = 64 * 1024
MAX_INDEX_BYTES = 128 * 1024
MAX_PRODUCT_BYTES = 10 * 1024 * 1024
MAX_SOLVER_BYTES = 32 * 1024 * 1024
MAX_URLS = 20_000
MAX_BROWSER_REQUESTS = 3
MAX_CANDIDATES_PER_MODEL = 4

MODEL_PATTERNS = {
    'apple-iphone-17e-8-256': re.compile(r'^apple-iphone-17e-256gb-[a-z0-9-]+-p-\d+$'),
    'apple-iphone-16e-8-128': re.compile(r'^apple-iphone-16e-128gb-[a-z0-9-]+-p-\d+$'),
    'samsung-galaxy-s25-fe-8-256': re.compile(r'^samsung-galaxy-s25-fe-5g-8256gb-[a-z0-9-]+-p-\d+$'),
    'samsung-galaxy-a26-6-128': re.compile(r'^samsung-galaxy-a26-6128gb-[a-z0-9-]+-p-\d+$'),
    'apple-iphone-18-pro-12-256': re.compile(r'^apple-iphone-18-pro-256gb-[a-z0-9-]+-p-\d+$'),
    'apple-iphone-18-pro-max-12-256': re.compile(r'^apple-iphone-18-pro-max-256gb-[a-z0-9-]+-p-\d+$'),
}


def _safe_exact_url(url):
    if not isinstance(url, str) or len(url) > 500 or re.search(r'[\x00-\x20\\]', url):
        return False
    try:
        parsed = urlparse(url)
        return (parsed.scheme == 'https' and parsed.hostname == HOST
                and parsed.username is None and parsed.password is None
                and parsed.port in (None, 443) and not parsed.query
                and not parsed.fragment and parsed.path.startswith('/'))
    except ValueError:
        return False


def _xml_root(raw, expected_tag, maximum, metrics=None):
    if (not isinstance(raw, bytes) or len(raw) > MAX_SOLVER_BYTES
            or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper()):
        raise ValueError('unsafe_or_oversized_xml')
    wrapper = 'raw_xml'
    if re.match(rb'\s*<html\b', raw, re.I):
        # Chromium may render XML twice: once in its hidden source node and
        # again as a pretty tree. Extract only the source bytes without
        # constructing a large HTML DOM or trusting the pretty tree.
        source_tags = list(re.finditer(
            rb'<div\b[^>]*\bid=["\']webkit-xml-viewer-source-xml["\'][^>]*>', raw, re.I))
        if len(source_tags) != 1:
            raise ValueError('invalid_browser_xml_viewer')
        start = source_tags[0].end()
        end = raw.find(b'</div>', start)
        if end < 0:
            raise ValueError('invalid_browser_xml_viewer')
        raw = raw[start:end].strip()
        wrapper = 'chromium_xml_viewer'
    if len(raw) > maximum:
        raise ValueError('extracted_xml_oversized')
    root = ET.fromstring(raw)
    if root.tag != NS + expected_tag:
        raise ValueError('unexpected_xml_root')
    if metrics is not None:
        metrics.update({'wrapper': wrapper, 'xml_bytes': len(raw)})
    return root


def _robots_text(raw):
    text = raw.decode('utf-8-sig', errors='strict')
    if re.search(r'<\s*(?:html|body|pre)\b', text, re.I):
        # Chromium may present text/plain robots.txt inside a single PRE.
        soup = BeautifulSoup(text, 'html.parser')
        blocks = soup.find_all('pre')
        if (len(blocks) != 1 or soup.find(['script', 'form', 'iframe'])
                or ' '.join(soup.get_text(' ', strip=True).split()) !=
                   ' '.join(blocks[0].get_text(' ', strip=True).split())):
            return ''
        text = blocks[0].get_text()
    return text


def _product_sitemap(index):
    candidates = []
    for row in index:
        if row.tag != NS + 'sitemap':
            continue
        loc = row.find(NS + 'loc')
        url = loc.text.strip() if loc is not None and isinstance(loc.text, str) else ''
        if (re.fullmatch(r'https://www\.teknosa\.com/medias/sys_master/sitemaps/sitemaps/'
                         r'\d+/Product-tr-TRY-0\.xml', url)
                and _safe_exact_url(url)):
            candidates.append(url)
    if len(candidates) != 1:
        raise ValueError('first_product_sitemap_missing_or_ambiguous')
    return candidates[0]


def _product_candidates(root, rules):
    if len(root) > MAX_URLS:
        raise ValueError('too_many_product_urls')
    seen = set()
    results = {key: [] for key in MODEL_PATTERNS}
    for row in root:
        if row.tag != NS + 'url':
            continue
        loc = row.find(NS + 'loc')
        url = loc.text.strip() if loc is not None and isinstance(loc.text, str) else ''
        if not _safe_exact_url(url) or not rules.can_fetch(BOT, url) or url in seen:
            continue
        seen.add(url)
        path = urlparse(url).path.lstrip('/').casefold()
        if 'outlet' in path or 'teshir' in path or 'yenilen' in path:
            continue
        for key, pattern in MODEL_PATTERNS.items():
            if pattern.fullmatch(path) and len(results[key]) < MAX_CANDIDATES_PER_MODEL:
                results[key].append(url)
    return len(seen), results


class TeknosaSitemapAudit:
    def __init__(self, get=None, post=None, sleep=time.sleep):
        self._get = get or requests.get
        self._post = post or requests.post
        self._sleep = sleep
        self._browser_requests = 0
        self._transports = {}
        self._direct_statuses = {}
        self._solver_diagnostics = {}

    def _read(self, url, maximum, accept, *, rules=None):
        if not _safe_exact_url(url) or (rules is not None and not rules.can_fetch(BOT, url)):
            raise ValueError('url_not_robots_allowed')
        response = self._get(
            url, timeout=(5, 20), allow_redirects=False, stream=True,
            headers={'User-Agent': BOT + '/1.0 (+https://teknoskor.com)',
                     'Accept': accept, 'Accept-Language': 'tr-TR,tr;q=0.9'},
        )
        try:
            self._direct_statuses[url] = response.status_code
            if response.url != url:
                raise ValueError('unexpected_final_url')
            if response.status_code == 200:
                length = str(response.headers.get('Content-Length') or '')
                if length.isdigit() and int(length) > maximum:
                    raise ValueError('direct_oversized')
                body = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    body.extend(chunk)
                    if len(body) > maximum:
                        raise ValueError('direct_oversized')
                self._transports[url] = 'direct'
                return bytes(body)
            if response.status_code != 403:
                raise ValueError(f'direct_http_{response.status_code}')
        finally:
            response.close()
        if self._browser_requests >= MAX_BROWSER_REQUESTS:
            raise ValueError('browser_budget_exhausted')
        self._browser_requests += 1
        self._sleep(0.5)
        response = self._post(SOLVER_URL, json={
            'cmd': 'request.get', 'url': url, 'maxTimeout': 45000,
        }, timeout=65, stream=True)
        diagnostic = {'http_status': response.status_code,
                      'declared_bytes': response.headers.get('Content-Length'),
                      'received_bytes': 0, 'solution_status': None,
                      'response_utf8_bytes': None, 'response_kind': None}
        self._solver_diagnostics[url] = diagnostic
        try:
            if response.status_code != 200:
                raise ValueError(f'solver_http_{response.status_code}')
            body = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                body.extend(chunk)
                diagnostic['received_bytes'] = len(body)
                if len(body) > MAX_SOLVER_BYTES:
                    raise ValueError('solver_oversized')
        finally:
            response.close()
        try:
            payload = json.loads(body)
        except (ValueError, TypeError) as error:
            raise ValueError('solver_invalid_json') from error
        solution = payload.get('solution') if isinstance(payload, dict) else None
        if not isinstance(payload, dict) or payload.get('status') != 'ok' or not isinstance(solution, dict):
            raise ValueError('solver_unavailable')
        if solution.get('url') != url:
            raise ValueError('browser_changed_url')
        diagnostic['solution_status'] = solution.get('status')
        if solution.get('status') != 200:
            raise ValueError(f"browser_site_http_{solution.get('status')}")
        text = solution.get('response')
        if not isinstance(text, str):
            raise ValueError('browser_oversized_or_invalid_body')
        raw = text.encode('utf-8')
        diagnostic['response_utf8_bytes'] = len(raw)
        prefix = raw.lstrip()[:4096].lower()
        diagnostic['response_kind'] = (
            'xml' if prefix.startswith((b'<?xml', b'<sitemapindex', b'<urlset'))
            else 'chromium_xml_viewer' if prefix.startswith(b'<html') and
                 b'webkit-xml-viewer-source-xml' in prefix
            else 'html' if prefix.startswith(b'<html') else 'other'
        )
        if len(raw) > (MAX_SOLVER_BYTES if diagnostic['response_kind'] ==
                       'chromium_xml_viewer' else maximum):
            raise ValueError('browser_oversized_or_invalid_body')
        self._transports[url] = 'browser'
        return raw

    def run(self):
        report = {'purpose': 'read_only_teknosa_sitemap_access_and_exact_url_hints',
                  'writes': False, 'status': 'failed', 'stage': 'robots',
                  'index_url': INDEX_URL, 'product_sitemap_url': None,
                  'product_url_count': 0, 'candidate_urls': {},
                  'direct_http_statuses': self._direct_statuses,
                  'transports': self._transports,
                  'solver_diagnostics': self._solver_diagnostics,
                  'xml_diagnostics': {}, 'browser_requests': 0}
        try:
            raw_robots = self._read(ROBOTS_URL, MAX_ROBOTS_BYTES, 'text/plain')
            robots = _robots_text(raw_robots)
            if (re.search(r'<(?:!doctype|html|head|body)\b', robots, re.I)
                    or not re.search(r'^\s*user-agent\s*:', robots, re.I | re.M)
                    or not re.search(r'^\s*sitemap\s*:\s*' + re.escape(INDEX_URL) +
                                     r'\s*$', robots, re.I | re.M)):
                raise ValueError('invalid_robots_policy')
            rules = RobotFileParser()
            rules.parse(robots.splitlines())
            if not rules.can_fetch(BOT, INDEX_URL):
                raise ValueError('index_disallowed')
            delay = float(rules.crawl_delay(BOT) or 0)
            rate = rules.request_rate(BOT)
            if rate:
                delay = max(delay, rate.seconds / max(1, rate.requests))
            if delay > 30:
                raise ValueError('crawl_delay_exceeds_budget')
            if delay:
                self._sleep(delay)
            report['stage'] = 'index'
            raw_index = self._read(INDEX_URL, MAX_INDEX_BYTES,
                                   'application/xml,text/xml', rules=rules)
            index = _xml_root(raw_index, 'sitemapindex', MAX_INDEX_BYTES,
                              report['xml_diagnostics'].setdefault(INDEX_URL, {}))
            if len(index) > 100:
                raise ValueError('too_many_sitemaps')
            child_url = _product_sitemap(index)
            if not rules.can_fetch(BOT, child_url):
                raise ValueError('product_sitemap_disallowed')
            report['product_sitemap_url'] = child_url
            if delay:
                self._sleep(delay)
            report['stage'] = 'product_sitemap'
            raw_product = self._read(child_url, MAX_PRODUCT_BYTES,
                                     'application/xml,text/xml', rules=rules)
            root = _xml_root(raw_product, 'urlset', MAX_PRODUCT_BYTES,
                             report['xml_diagnostics'].setdefault(child_url, {}))
            count, candidates = _product_candidates(root, rules)
            report['product_url_count'] = count
            report['candidate_urls'] = candidates
            report['status'] = 'completed'
            report['stage'] = None
        except (requests.RequestException, ET.ParseError, UnicodeError,
                TypeError, AttributeError, ValueError) as error:
            report['error'] = (str(error) if isinstance(error, ValueError)
                               else type(error).__name__)
        report['browser_requests'] = self._browser_requests
        return report
