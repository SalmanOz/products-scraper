"""Two fixed, Vatan phone listings as exact PDP URL hints.

Listing prices are deliberately ignored. Only the existing selected-PDP reader
can turn a discovered URL into a price and availability observation.
"""

import re
import time

import requests
from bs4 import BeautifulSoup

from vatan_sitemap_source import BOT, VatanSitemapSource


OPPO_CATEGORY = 'https://www.vatanbilgisayar.com/oppo/cep-telefonu-modelleri/'
VIVO_CATEGORY = 'https://www.vatanbilgisayar.com/vivo/cep-telefonu-modelleri/'
CATEGORIES = frozenset((OPPO_CATEGORY, VIVO_CATEGORY))
MAX_CATEGORY_BYTES = 3 * 1024 * 1024
MAX_CARDS = 48


def parse_category_cards(html, category_url):
    """Keep only product-list cards with safe exact PDP URLs."""
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
                or not VatanSitemapSource._safe_product_url(url)):
            continue
        seen.add(url)
        result.append({'title': name, 'source_url': url})
    return result


class VatanCategorySource:
    """At most one direct GET per fixed category per run."""

    def __init__(self, fetch=None, sleep=time.sleep):
        self._fetch = fetch or requests.get
        self._sleep = sleep
        self._delay = 0.5
        self._last_request = None
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

    def read_category(self, category_url):
        if category_url not in CATEGORIES:
            return []
        if category_url in self._category_reports:
            return self._category_reports[category_url]['cards']
        row = {'status': 'not_attempted', 'http_status': None, 'cards': []}
        self._category_reports[category_url] = row
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
            cards = parse_category_cards(html, category_url)
            row['status'] = 'ready' if cards else 'no_valid_product_cards'
            row['cards'] = cards
            return cards
        except UnicodeError:
            row['status'] = 'invalid_encoding'
            return []

    def report(self):
        return {'categories': {url: {'status': row['status'],
                                     'http_status': row['http_status'],
                                     'card_count': len(row['cards'])}
                               for url, row in self._category_reports.items()}}
