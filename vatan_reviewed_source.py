"""Bounded browser reads for reviewed Vatan PDPs when ordinary GET is denied.

This module only returns HTML. The retailer reader must still prove the
selected Product, its own Offer, visible cash price and current stock.
"""

import json
import re
import time
from pathlib import Path
from threading import RLock
from urllib.robotparser import RobotFileParser

import requests

from vatan_sitemap_source import BOT, MAX_ROBOTS_BYTES, ROBOTS_URL, VatanSitemapSource


BATCH_PATH = Path(__file__).parent / 'offer_batches/vatan-exact-2026-11-02.json'
SOLVER_URL = 'http://localhost:8191/v1'
MAX_SOLVER_BYTES = 8 * 1024 * 1024
MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_BROWSER_REQUESTS = 6


def reviewed_profiles():
    """Load reviewed SKU identities and exact URL hints, never observations."""
    return json.loads(BATCH_PATH.read_text())['products']


class VatanReviewedSource:
    def __init__(self, get=None, post=None, sleep=time.sleep, monotonic=time.monotonic):
        self._get = get or requests.get
        self._post = post or requests.post
        self._sleep = sleep
        self._monotonic = monotonic
        self._lock = RLock()
        self._urls = frozenset(
            url for profile in reviewed_profiles() for url in profile['targets']
            if VatanSitemapSource._safe_product_url(url)
        )
        self._loaded = False
        self._rules = None
        self._delay = 0
        self._last_site_request = None
        self._robots_status = 'not_loaded'
        self._browser_requests = 0
        self._browser_successes = 0

    def is_reviewed(self, url):
        return url in self._urls

    def _load_robots(self):
        if self._loaded:
            return
        self._loaded = True
        response = None
        try:
            response = self._get(
                ROBOTS_URL, timeout=(5, 15), allow_redirects=False, stream=True,
                headers={'User-Agent': BOT + '/1.0 (+https://teknoskor.com)',
                         'Accept': 'text/plain'},
            )
            if response.status_code != 200 or response.url != ROBOTS_URL:
                self._robots_status = f'http_{response.status_code}'
                return
            size = str(response.headers.get('Content-Length') or '')
            if size.isdigit() and int(size) > MAX_ROBOTS_BYTES:
                self._robots_status = 'oversized'
                return
            body = bytearray()
            for chunk in response.iter_content(chunk_size=16 * 1024):
                body.extend(chunk)
                if len(body) > MAX_ROBOTS_BYTES:
                    self._robots_status = 'oversized'
                    return
            robots = body.decode('utf-8-sig', errors='strict')
            if (re.search(r'<(?:!doctype|html|head|body)\b', robots, re.I)
                    or not re.search(r'^\s*user-agent\s*:', robots, re.I | re.M)):
                self._robots_status = 'invalid_policy'
                return
            rules = RobotFileParser()
            rules.parse(robots.splitlines())
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
        finally:
            if response is not None:
                response.close()

    def prepare(self, url):
        """Authorize and pace a reviewed exact PDP request, including direct GET."""
        with self._lock:
            if url not in self._urls:
                return False, 'not_reviewed'
            self._load_robots()
            if self._rules is None:
                return False, 'robots_unavailable'
            if not self._rules.can_fetch(BOT, url):
                return False, 'robots_disallowed'
            if self._last_site_request is not None and self._delay:
                remaining = self._delay - (self._monotonic() - self._last_site_request)
                if remaining > 0:
                    self._sleep(remaining)
            self._last_site_request = self._monotonic()
            return True, 'ready'

    def browser_get(self, url):
        """Return exact-URL HTML after a robots check and global request budget."""
        allowed, reason = self.prepare(url)
        if not allowed:
            return None, reason
        with self._lock:
            if self._browser_requests >= MAX_BROWSER_REQUESTS:
                return None, 'browser_budget_exhausted'
            self._browser_requests += 1
        response = None
        try:
            response = self._post(SOLVER_URL, json={
                'cmd': 'request.get', 'url': url, 'maxTimeout': 45000,
            }, timeout=60, stream=True)
            if response.status_code != 200:
                return None, f'solver_http_{response.status_code}'
            data = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                data.extend(chunk)
                if len(data) > MAX_SOLVER_BYTES:
                    return None, 'solver_response_too_large'
            payload = json.loads(data)
            if not isinstance(payload, dict) or payload.get('status') != 'ok':
                return None, 'solver_unavailable'
            solution = payload.get('solution')
            if not isinstance(solution, dict):
                return None, 'invalid_solution'
            if solution.get('status') != 200:
                return None, f"site_http_{solution.get('status')}"
            if solution.get('url') != url:
                return None, 'unexpected_final_url'
            html = solution.get('response')
            if not isinstance(html, str) or len(html.encode('utf-8')) > MAX_HTML_BYTES:
                return None, 'invalid_or_oversized_html'
            with self._lock:
                self._browser_successes += 1
            return html, 'browser_html_ready'
        except (requests.RequestException, ValueError, TypeError, UnicodeError):
            return None, 'browser_unavailable'
        finally:
            if response is not None:
                response.close()

    def report(self):
        with self._lock:
            return {'robots_status': self._robots_status,
                    'reviewed_urls': len(self._urls),
                    'browser_requests': self._browser_requests,
                    'browser_html_responses': self._browser_successes}
