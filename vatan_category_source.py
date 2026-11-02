"""Two fixed, robots-approved Vatan phone listings as exact PDP URL hints.

Listing prices are deliberately ignored. Only the existing selected-PDP reader
can turn a discovered URL into a price and availability observation.
"""

import re
import time
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from vatan_sitemap_source import BOT, MAX_ROBOTS_BYTES, ROBOTS_URL, VatanSitemapSource


OPPO_CATEGORY = 'https://www.vatanbilgisayar.com/oppo/cep-telefonu-modelleri/'
VIVO_CATEGORY = 'https://www.vatanbilgisayar.com/vivo/cep-telefonu-modelleri/'
CATEGORIES = frozenset((OPPO_CATEGORY, VIVO_CATEGORY))
MAX_CATEGORY_BYTES = 3 * 1024 * 1024
MAX_CARDS = 48


def parse_category_cards(html, rules, category_url):
    """Keep only product-list cards with safe, robots-allowed exact PDP URLs."""
    if category_url not in CATEGORIES or not isinstance(html, str):
        return []
    soup = BeautifulSoup(html, 'html.parser')
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    if re.search(r'captcha|robot check|just a moment|access denied', title, re.I):
        return []
    cards = soup.select('#productsLoad a.product-list-link[href]')
    if not cards or len(cards) > MAX_CARDS:
        return []
    seen = set()
    result = []
    for card in cards:
        url = card.get('href')
        heading = card.select_one('.product-list__product-name h3')
        name = heading.get_text(' ', strip=True) if heading else ''
        if (url in seen or not name or len(name) > 240
                or not VatanSitemapSource._safe_product_url(url)
                or not rules.can_fetch(BOT, url)):
            continue
        seen.add(url)
        result.append({'title': name, 'source_url': url})
    return result


class VatanCategorySource:
    """One robots GET and at most one direct GET per fixed category per run."""

    def __init__(self, fetch=None, sleep=time.sleep):
        self._fetch = fetch or requests.get
        self._sleep = sleep
        self._rules = None
        self._loaded = False
        self._delay = 0
        self._last_request = None
        self._robots_status = 'not_loaded'
        self._category_reports = {}

    def _read(self, url, limit, accept):
        try:
            response = self._fetch(
                url, timeout=(5, 15), allow_redirects=False, stream=True,
                headers={'User-Agent': BOT + '/1.0 (+https://teknoskor.com)',
                         'Accept': accept, 'Accept-Language': 'tr-TR,tr;q=0.9'},
            )
        except requests.RequestException:
            return None, 'request_error', None
        try:
            status = response.status_code
            if status != 200 or response.url != url:
                return None, f'http_{status}' if status != 200 else 'unexpected_final_url', status
            size = str(response.headers.get('Content-Length') or '')
            if size.isdigit() and int(size) > limit:
                return None, 'oversized', status
            body = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                body.extend(chunk)
                if len(body) > limit:
                    return None, 'oversized', status
            return bytes(body), 'ready', status
        except (requests.RequestException, ValueError, TypeError):
            return None, 'read_error', None
        finally:
            response.close()

    def _load_robots(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            raw, reason, _ = self._read(ROBOTS_URL, MAX_ROBOTS_BYTES, 'text/plain')
            if raw is None:
                self._robots_status = reason
                return
            policy = raw.decode('utf-8-sig', errors='strict')
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
                self._robots_status = 'crawl_delay_exceeds_budget'
                return
            self._rules = rules
            self._delay = delay
            self._robots_status = 'ready'
        except (requests.RequestException, ValueError, UnicodeError):
            self._robots_status = 'unavailable'

    def read_category(self, category_url):
        if category_url not in CATEGORIES:
            return []
        if category_url in self._category_reports:
            return self._category_reports[category_url]['cards']
        self._load_robots()
        row = {'status': 'robots_unavailable', 'http_status': None, 'cards': []}
        self._category_reports[category_url] = row
        if self._rules is None:
            return []
        if not self._rules.can_fetch(BOT, category_url):
            row['status'] = 'robots_disallowed'
            return []
        if self._last_request is not None and self._delay:
            remaining = self._delay - (time.monotonic() - self._last_request)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request = time.monotonic()
        try:
            raw, reason, status = self._read(category_url, MAX_CATEGORY_BYTES, 'text/html')
            row['http_status'] = status
            if raw is None:
                row['status'] = reason
                return []
            html = raw.decode('utf-8-sig', errors='strict')
            cards = parse_category_cards(html, self._rules, category_url)
            row['status'] = 'ready' if cards else 'no_valid_product_cards'
            row['cards'] = cards
            return cards
        except UnicodeError:
            row['status'] = 'invalid_encoding'
            return []

    def report(self):
        return {'robots_status': self._robots_status,
                'categories': {url: {'status': row['status'],
                                     'http_status': row['http_status'],
                                     'card_count': len(row['cards'])}
                               for url, row in self._category_reports.items()}}
