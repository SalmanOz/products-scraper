"""Offline tests for the fixed-URL read-only runner probe."""

import json
import unittest

from audit_vatan_pdp_access import PROFILES, SOLVER_URL, format_summary, run_audit
from retailer_offer_refresh import RetailerOfferRefresher
from test_vatan_source_audit import Fetch, PRICES, Response, TITLES, product_page
from vatan_sitemap_source import ROBOTS_URL


SKUS = ('153480', '157948', '156532')
URLS = tuple(profile[4] for profile in PROFILES)


class VatanPdpAccessTests(unittest.TestCase):
    def test_direct_get_verifies_all_three_without_browser_requests(self):
        pages = {url: Response(url, product_page(url, title, price, sku))
                 for url, title, price, sku in zip(URLS, TITLES, PRICES, SKUS)}
        fetch = Fetch(pages)
        robots = Fetch({ROBOTS_URL: f'User-agent: *\nDisallow: /arama/*\n'})
        calls = []
        report = run_audit(robots_fetch=robots,
                           reader_factory=lambda: RetailerOfferRefresher(fetch=fetch),
                           post=lambda *args, **kwargs: calls.append(args),
                           sleep=lambda _: None)
        self.assertEqual(report['verified_normal'], 3)
        self.assertEqual(report['verified_browser'], 0)
        self.assertEqual(fetch.calls, list(URLS))
        self.assertEqual(calls, [])

    def test_403_direct_get_can_only_verify_strict_browser_pdp(self):
        fetch = Fetch({url: Response(url, b'blocked', 403) for url in URLS})
        robots = Fetch({ROBOTS_URL: 'User-agent: *\nDisallow: /arama/*\n'})
        browser_calls = []

        def post(endpoint, **kwargs):
            self.assertEqual(endpoint, SOLVER_URL)
            url = kwargs['json']['url']
            browser_calls.append(url)
            index = URLS.index(url)
            html = product_page(url, TITLES[index], PRICES[index], SKUS[index])
            return Response(endpoint, json.dumps({'status': 'ok', 'solution': {
                'status': 200, 'url': url, 'response': html,
            }}))

        report = run_audit(robots_fetch=robots,
                           reader_factory=lambda: RetailerOfferRefresher(fetch=fetch),
                           post=post, sleep=lambda _: None)
        self.assertEqual(report['verified_normal'], 0)
        self.assertEqual(report['verified_browser'], 3)
        self.assertEqual(browser_calls, list(URLS))
        self.assertTrue(all(row['normal_get']['status'] == 'access_denied'
                            for row in report['products']))
        self.assertIn('browser GET 3/3', format_summary(report))

    def test_robots_disallow_prevents_all_pdp_requests(self):
        robots = Fetch({ROBOTS_URL: 'User-agent: *\nDisallow: /\n'})
        pdp = Fetch({})
        report = run_audit(robots_fetch=robots,
                           reader_factory=lambda: RetailerOfferRefresher(fetch=pdp),
                           post=lambda *args, **kwargs: self.fail('browser GET must be skipped'),
                           sleep=lambda _: None)
        self.assertEqual(report['verified_normal'], 0)
        self.assertEqual(report['verified_browser'], 0)
        self.assertEqual(pdp.calls, [])
        self.assertTrue(all(row['normal_get']['status'] == 'robots_unavailable_or_disallowed'
                            for row in report['products']))


if __name__ == '__main__':
    unittest.main()
