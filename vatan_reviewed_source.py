"""Bounded browser reads for catalog-matched Vatan PDPs after a denied GET.

This module only returns HTML. The retailer reader must still prove the
selected Product, its own Offer, visible cash price and current stock.
"""

import json
import time
from pathlib import Path
from threading import RLock

import requests

from vatan_sitemap_source import VatanSitemapSource


BATCH_PATH = Path(__file__).parent / 'offer_batches/vatan-exact-2026-11-02.json'
S25_FE_256_BATCH_PATH = Path(__file__).parent / 'offer_batches/s25-fe-256-exact-2026-11-02.json'
S25_FE_256_SLUG = 'samsung-galaxy-s25-fe-256gb'
S25_FE_256_VATAN_URL = (
    'https://www.vatanbilgisayar.com/'
    'samsung-galaxy-s25-fe-8-256-gb-akilli-telefon-buz-mavisi.html'
)
SOLVER_URL = 'http://localhost:8191/v1'
MAX_SOLVER_BYTES = 8 * 1024 * 1024
MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_BROWSER_REQUESTS = 6


def reviewed_profiles():
    """Load reviewed SKU identities and exact URL hints, never observations."""
    return json.loads(BATCH_PATH.read_text())['products']


def reviewed_s25_fe_variant_urls():
    """Authorize only the runner-proven Vatan URL for the exact new SKU."""
    profiles = json.loads(S25_FE_256_BATCH_PATH.read_text())['products']
    if len(profiles) != 1:
        return ()
    profile = profiles[0]
    if (profile.get('id') == 298
            and profile.get('slug') == S25_FE_256_SLUG
            and profile.get('name') == 'Samsung Galaxy S25 FE 256GB'
            and profile.get('ram_gb') == 8
            and profile.get('storage_gb') == 256
            and profile.get('network_generation') == '5g'
            and S25_FE_256_VATAN_URL in profile.get('targets', ())):
        return (S25_FE_256_VATAN_URL,)
    return ()


class VatanReviewedSource:
    def __init__(self, get=None, post=None, sleep=time.sleep, monotonic=time.monotonic,
                 additional_reviewed_urls=()):
        self._post = post or requests.post
        self._sleep = sleep
        self._monotonic = monotonic
        self._lock = RLock()
        additional_reviewed_urls = tuple(additional_reviewed_urls)
        if any(not VatanSitemapSource._safe_product_url(url) for url in additional_reviewed_urls):
            raise ValueError('Invalid additional reviewed Vatan PDP URL')
        self._urls = frozenset(
            url for profile in reviewed_profiles() for url in profile['targets']
            if VatanSitemapSource._safe_product_url(url)
        ) | frozenset(reviewed_s25_fe_variant_urls()) | frozenset(additional_reviewed_urls)
        self._delay = 0.5
        self._last_site_request = None
        self._browser_requests = 0
        self._browser_successes = 0
        self._catalog_urls = set()
        self._browser_limit = MAX_BROWSER_REQUESTS
        self._browser_failures = 0
        self._browser_blocked = False

    def is_reviewed(self, url):
        return url in self._urls or url in self._catalog_urls

    def register_catalog_targets(self, product_name, targets, expected_specs):
        """Admit exact catalog-matching URL hints; HTML still needs live proof."""
        candidates = set()
        for row in targets:
            url = row.get('source_url') if isinstance(row, dict) else None
            parsed = VatanSitemapSource._safe_product_url(url)
            if parsed and VatanSitemapSource._variant_matches(parsed.path, product_name, expected_specs):
                candidates.add(url)
        with self._lock:
            self._catalog_urls.update(candidates)

    def configure_catalog_budget(self, product_count):
        if type(product_count) is not int or product_count < 1:
            raise ValueError('Catalog product count must be a positive integer')
        with self._lock:
            if self._last_site_request is not None or self._browser_requests:
                raise RuntimeError('Configure the catalog budget before source requests')
            self._browser_limit = max(MAX_BROWSER_REQUESTS, min(80, product_count))

    def prepare(self, url):
        """Authorize and pace a registered exact PDP, including direct GET."""
        with self._lock:
            if self._browser_blocked:
                return False, 'source_circuit_open'
            if not self.is_reviewed(url):
                return False, 'not_reviewed'
            if self._last_site_request is not None and self._delay:
                remaining = self._delay - (self._monotonic() - self._last_site_request)
                if remaining > 0:
                    self._sleep(remaining)
            self._last_site_request = self._monotonic()
            return True, 'ready'

    def browser_get(self, url):
        """Return exact-URL HTML within the global request budget."""
        allowed, reason = self.prepare(url)
        if not allowed:
            return None, reason
        with self._lock:
            if self._browser_requests >= self._browser_limit:
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
                if solution.get('status') in (403, 429, 500, 502, 503, 504):
                    with self._lock:
                        self._browser_failures += 1
                        self._browser_blocked = self._browser_failures >= 2
                return None, f"site_http_{solution.get('status')}"
            if solution.get('url') != url:
                return None, 'unexpected_final_url'
            html = solution.get('response')
            if not isinstance(html, str) or len(html.encode('utf-8')) > MAX_HTML_BYTES:
                return None, 'invalid_or_oversized_html'
            with self._lock:
                self._browser_successes += 1
                self._browser_failures = 0
            return html, 'browser_html_ready'
        except (requests.RequestException, ValueError, TypeError, UnicodeError):
            with self._lock:
                self._browser_failures += 1
                self._browser_blocked = self._browser_failures >= 2
            return None, 'browser_unavailable'
        finally:
            if response is not None:
                response.close()

    def report(self):
        with self._lock:
            return {'reviewed_urls': len(self._urls),
                    'catalog_discovered_urls': len(self._catalog_urls),
                    'browser_requests': self._browser_requests,
                    'browser_request_limit': self._browser_limit,
                    'source_circuit_open': self._browser_blocked,
                    'browser_html_responses': self._browser_successes}
