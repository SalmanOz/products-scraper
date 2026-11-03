"""Bounded public document reads for the four requested priority retailers.

An HTML document is discovery/diagnostic input, never an offer. The caller
must independently prove the selected SKU, seller, cash price and stock.
"""

import re
import time
from threading import RLock
from urllib.parse import parse_qsl, urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
import requests

from hepsiburada_s25_fe_source import HepsiburadaS25FeSource


HOSTS = {'www.hepsiburada.com': 'Hepsiburada', 'www.trendyol.com': 'Trendyol',
         'www.n11.com': 'n11', 'www.teknosa.com': 'Teknosa'}
BOT = 'TeknoskorPriceBot'
MAX_HTML_BYTES = 5 * 1024 * 1024


def product_host(url):
    if not isinstance(url, str) or len(url) > 1000 or re.search(r'[\x00-\x20\\]', url):
        return None
    try:
        parsed = urlparse(url)
        if (parsed.scheme != 'https' or parsed.netloc not in HOSTS or parsed.fragment
                or parsed.username is not None or parsed.password is not None):
            return None
        patterns = {
            'www.hepsiburada.com': r'/[a-zA-Z0-9-]+-p(?:m)?-(?:HBCV?[A-Z0-9]{5,30}|[A-Z0-9]{5,30})',
            'www.trendyol.com': r'/[a-z0-9-]+/[a-z0-9-]+-p-[1-9]\d{3,11}',
            'www.n11.com': r'/urun/[a-z0-9-]+-[1-9]\d{3,12}',
            'www.teknosa.com': r'/[a-z0-9-]+-p-[1-9]\d{8,13}',
        }
        if not re.fullmatch(patterns[parsed.netloc], parsed.path):
            return None
        pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=4)
        if len(pairs) != len(dict(pairs)):
            return None
        for key, value in pairs:
            if parsed.netloc == 'www.trendyol.com':
                if key == 'v' and value == 'tek-ebat':
                    continue
                if key not in {'merchantId', 'boutiqueId'} or not re.fullmatch(r'[1-9]\d{0,11}', value):
                    return None
            elif parsed.netloc in {'www.hepsiburada.com', 'www.n11.com'}:
                allowed = {'magaza'} if parsed.netloc == 'www.hepsiburada.com' else {'magaza', 'renk', 'dahili-hafiza'}
                if key not in allowed or not re.fullmatch(r'[\w .&()-]{1,100}', value):
                    return None
            else:
                return None
        return parsed.netloc
    except (TypeError, ValueError):
        return None


def challenge_document(html):
    soup = BeautifulSoup(html, 'html.parser')
    title = ' '.join(n.get_text(' ', strip=True) for n in soup.select('title,h1'))
    return bool(re.search(r'captcha|just a moment|access denied|robot check|'
                          r'you have been blocked|güvenlik|security check|verify.*human', title, re.I)
                or soup.select('#challenge-form, #captchacharacters'))


class PriorityRetailerTransport:
    def __init__(self, *, get=None, post=None, sleep=time.sleep, clock=time.monotonic,
                 maximum_per_host=2):
        if type(maximum_per_host) is not int or not 1 <= maximum_per_host <= 60:
            raise ValueError('Invalid priority retailer request budget')
        self._get, self._post = get, post
        self._sleep, self._clock = sleep, clock
        self._maximum = maximum_per_host
        self._states = {}
        self._lock = RLock()

    def configure_catalog_budget(self, count):
        if type(count) is not int or count < 1:
            raise ValueError('Invalid catalog size')
        with self._lock:
            if self._states:
                raise RuntimeError('Configure before source reads')
            self._maximum = max(self._maximum, min(60, count))

    def _read(self, url, limit, accept, state):
        last = state.get('last')
        if last is not None:
            self._sleep(max(0, state['delay'] - (self._clock() - last)))
        state['last'] = self._clock()
        transport = HepsiburadaS25FeSource(get=self._get, post=self._post, sleep=self._sleep)
        try:
            return transport._read_with_fallback(url, limit, accept=accept)
        finally:
            state['browser_requests'] += transport._browser_requests

    def read_document(self, url):
        host = product_host(url)
        if host is None:
            return None, 'invalid_priority_pdp', None
        with self._lock:
            state = self._states.setdefault(host, {'rules': None, 'robots': 'not_loaded',
                'pdp_requests': 0, 'browser_requests': 0, 'delay': 0.5, 'blocked': False, 'seen': set()})
            if state['blocked']:
                return None, 'source_circuit_open', None
            try:
                if state['robots'] == 'not_loaded':
                    raw, reason, status = self._read('https://' + host + '/robots.txt',
                                                     64 * 1024, 'text/plain', state)
                    state['robots'] = reason
                    text = HepsiburadaS25FeSource._robots_text(raw) if raw is not None else ''
                    if reason != 'ready' or not re.search(r'^\s*User-agent\s*:', text, re.I | re.M):
                        state['blocked'] = True
                        return None, 'robots_' + (reason if reason != 'ready' else 'invalid'), status
                    rules = RobotFileParser()
                    rules.parse(text.splitlines())
                    delay = max(0.5, float(rules.crawl_delay(BOT) or 0))
                    rate = rules.request_rate(BOT)
                    if rate:
                        delay = max(delay, rate.seconds / max(1, rate.requests))
                    if delay > 30:
                        state['blocked'] = True
                        return None, 'robots_delay_exceeds_budget', None
                    state.update(rules=rules, delay=delay)
                if not state['rules'].can_fetch(BOT, url):
                    return None, 'robots_disallowed', None
                if url in state['seen']:
                    return None, 'already_read', None
                if state['pdp_requests'] >= self._maximum:
                    return None, 'pdp_budget_exhausted', None
                state['seen'].add(url)
                state['pdp_requests'] += 1
                raw, reason, status = self._read(url, MAX_HTML_BYTES, 'text/html', state)
                if reason != 'ready':
                    if status in (403, 429, 500, 502, 503, 504):
                        state['blocked'] = True
                    return None, reason, status
                html = raw.decode('utf-8-sig', errors='strict')
                if challenge_document(html):
                    state['blocked'] = True
                    return None, 'challenge_page', status
                return html, 'document_read', status
            except (requests.RequestException, ValueError, TypeError, UnicodeError):
                state['blocked'] = True
                return None, 'transport_error', None

    def report(self):
        return {HOSTS[host]: {key: value for key, value in state.items()
                              if key in {'robots', 'pdp_requests', 'browser_requests', 'blocked'}}
                for host, state in self._states.items()}
