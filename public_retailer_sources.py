"""Bounded, robots-aware public catalog discovery; prices never come from cards.

Reviewed consumer Apple retailers, not marketplace/search snippets. No account,
affiliate feed, cart request, browser fingerprint, proxy or challenge bypass.
"""

import re
import time
from threading import RLock
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
import requests

from tr_price_scraper import TRPriceScraper


SOURCES = {'pt.com.tr': 'Pozitif Teknoloji', 'troyestore.com': 'Troy'}
BOT = 'TeknoskorPriceBot'
MAX_REQUESTS_PER_SOURCE = 150


def public_source(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or '').removeprefix('www.')
        if (host in SOURCES and parsed.scheme == 'https' and not parsed.query
                and not parsed.fragment and parsed.username is None and parsed.password is None
                and parsed.port in (None, 443) and not re.search(r'[\x00-\x20\\]', url)
                and not re.search(r'%2f|%5c|%3f|%23|%00', parsed.path, re.I)):
            return host
    except (ValueError, TypeError):
        pass
    return None


class PublicRetailerSources:
    def __init__(self, fetch=None, sleep=time.sleep, clock=time.monotonic):
        self._fetch = fetch or requests.get
        self._sleep, self._clock = sleep, clock
        self._robots, self._pages, self._blocked = {}, {}, set()
        self._counts, self._last, self._attempts = {}, {}, {}
        self._lock = RLock()
        self._matcher = TRPriceScraper()

    def _request(self, url, host):
        if host in self._blocked or self._counts.get(host, 0) >= MAX_REQUESTS_PER_SOURCE:
            raise requests.RequestException('Public source circuit/budget open')
        rules = self._robots.get(host)
        interval = max(0.5, float(rules.crawl_delay(BOT) or 0)) if rules else 0.5
        rate = rules.request_rate(BOT) if rules else None
        if rate:
            interval = max(interval, rate.seconds / max(1, rate.requests))
        if interval > 30:
            raise requests.RequestException('Source crawl delay exceeds run budget')
        self._sleep(max(0, interval - (self._clock() - self._last.get(host, -1000))))
        self._counts[host] = self._counts.get(host, 0) + 1
        self._last[host] = self._clock()
        try:
            response = self._fetch(url, timeout=(5, 15), allow_redirects=False,
                                   headers={'User-Agent': BOT + '/1.0 (+https://teknoskor.com)',
                                            'Accept': 'text/plain' if url.endswith('/robots.txt') else 'text/html',
                                            'Accept-Language': 'tr-TR,tr;q=0.9'})
        except requests.RequestException:
            self._blocked.add(host)
            self._attempts[url] = {'reason': 'transport_error', 'http_status': None}
            raise
        self._attempts[url] = {'reason': 'http_response', 'http_status': response.status_code}
        if response.status_code in (403, 429, 500, 502, 503, 504):
            self._blocked.add(host)
        if len(response.text.encode('utf-8')) > 5 * 1024 * 1024:
            raise requests.RequestException('Oversized public response')
        return response

    def fetch(self, url, **_kwargs):
        """GET only. Redirect destinations are independently robots-checked."""
        host = public_source(url)
        if not host:
            raise requests.RequestException('Not a clean reviewed public URL')
        with self._lock:
            if host not in self._robots:
                robots_url = f'https://www.{host}/robots.txt'
                response = self._request(robots_url, host)
                if response.status_code != 200:
                    self._blocked.add(host)
                    raise requests.RequestException('Robots policy unavailable')
                rules = RobotFileParser()
                rules.parse(response.text.splitlines())
                self._robots[host] = rules
            if not self._robots[host].can_fetch(BOT, url):
                self._attempts[url] = {'reason': 'robots_disallowed', 'http_status': None}
                raise requests.RequestException('Robots disallows URL')
            if url not in self._pages:
                self._pages[url] = self._request(url, host)
            return self._pages[url]

    def _links(self, url):
        try:
            response = self.fetch(url)
            if response.status_code != 200:
                return []
            soup = BeautifulSoup(response.text, 'html.parser')
            if re.search(r'captcha|just a moment|access denied',
                         ' '.join(e.get_text(' ', strip=True) for e in soup.select('title,h1')), re.I):
                self._blocked.add(public_source(url))
                return []
            return list(dict.fromkeys(urljoin(url, a['href']) for a in soup.select('a[href]')
                                      if public_source(urljoin(url, a['href'])) == public_source(url)))
        except (requests.RequestException, ValueError, TypeError):
            return []

    def discover(self, product_name, expected_specs):
        """Root -> linked exact family -> linked exact SKU; max 6 colors/source.

        A family path is only selected if observed as a live homepage link. A
        generated/guessed PDP, card price or missing storage never becomes proof.
        """
        family = re.fullmatch(r'(?:Apple\s+)?(iPhone\s+(?:\d{1,2}[eE]?|Air|Duo)(?:\s+(?:Pro Max|Pro|Plus))?)',
                              product_name.strip(), re.I)
        if not family or not expected_specs.get('storage_gb'):
            return []
        slug = re.sub(r'\s+', '-', family[1].lower())
        targets = []
        for host in SOURCES:
            root = f'https://www.{host}/'
            family_path = '/' + slug if host == 'pt.com.tr' else '/iphone/' + slug
            family_url = next((u for u in self._links(root)
                               if urlparse(u).path.rstrip('/') == family_path), None)
            if not family_url:
                continue
            candidates = []
            for url in self._links(family_url):
                path = urlparse(url).path
                # Exclude used/demo/outlet and non-product family/navigation links.
                if re.search(r'teshir|teşhir|outlet|yenilen|ikinci|aksesuar', path, re.I):
                    continue
                title = path.replace('-', ' ').replace('_', ' ')
                if not re.search(r'\d+\s*(?:gb|tb)\b', title, re.I):
                    continue
                if self._matcher.is_strict_match(product_name, title, expected_specs):
                    candidates.append({'source_url': url})
                if len(candidates) == 6:
                    break
            targets.extend(candidates)
        return targets

    def report(self):
        return [{'source_url': url, **detail} for url, detail in self._attempts.items()]
