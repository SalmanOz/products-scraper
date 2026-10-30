"""Offline pilot contracts: no live HTTP, no quality relaxation, no writes."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import socket
import unittest
from unittest.mock import Mock, patch
from urllib.robotparser import RobotFileParser

import requests

import scrapling_pilot as pilot
from recover_reviewed_offers import load_profiles


PROFILE = next(p for p in load_profiles() if p['slug'] == 'samsung-galaxy-a57-5g')
URL = PROFILE['targets'][0]
TITLE = 'Samsung Galaxy A57 5G 128 GB 8 GB RAM Samsung Türkiye Garantili'


def doc(url=URL, title=TITLE, *, price='28699', stock='InStock', currency='TRY', age=0, **offer_extra):
    schema = {'@type': 'Product', 'url': url, 'name': title,
              'offers': {'@type': 'Offer', 'url': url, 'price': price,
                         'priceCurrency': currency, 'availability': 'https://schema.org/' + stock, **offer_extra}}
    return pilot.Document(url, 200, '<h1>' + title + '</h1><script type="application/ld+json">'
                          + json.dumps(schema) + '</script>', (pilot.utcnow() - timedelta(seconds=age)).isoformat())


def rules(text='User-agent: *\nAllow: /'):
    parsed = RobotFileParser()
    parsed.parse(text.splitlines())
    return parsed


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.net = patch.object(socket.socket, 'connect', side_effect=AssertionError('No live network'))
        self.http = patch.object(requests.Session, 'request', side_effect=AssertionError('No live HTTP'))
        self.net.start(); self.http.start()
        self.addCleanup(self.net.stop); self.addCleanup(self.http.stop)

    def test_scope_is_eight_reviewed_models_and_nineteen_urls(self):
        profiles = load_profiles()
        self.assertEqual(len(profiles), 8)
        self.assertEqual(sum(len(p['targets']) for p in profiles), 19)

    def test_exact_offer_passes_without_retiming(self):
        document = doc(age=30)
        result = pilot.assess(PROFILE, URL, document)
        self.assertEqual(result['reason'], 'eligible')
        self.assertEqual(result['offer']['observed_at'], document.observed_at)

    def test_identity_memory_radio_warranty_and_refurbishment_fail(self):
        for title in (TITLE.replace('128 GB', '256 GB'), TITLE.replace('8 GB RAM', '12 GB RAM'),
                      TITLE.replace('5G', '4G'), TITLE.replace('A57', 'A56'), TITLE + ' Yenilenmiş',
                      TITLE.replace('Türkiye Garantili', 'İthalatçı Garantili'), TITLE.replace('Türkiye Garantili', '')):
            with self.subTest(title=title):
                self.assertNotEqual(pilot.assess(PROFILE, URL, doc(title=title))['reason'], 'eligible')

    def test_price_stock_currency_age_and_coupon_fail(self):
        for kwargs in ({'price': True}, {'price': '0'}, {'stock': 'OutOfStock'}, {'currency': 'USD'},
                       {'age': 901}, {'age': -121}, {'name': 'Kuponla fiyat'}, {'validForMemberTier': 'Premium'}):
            with self.subTest(kwargs=kwargs):
                self.assertNotEqual(pilot.assess(PROFILE, URL, doc(**kwargs))['reason'], 'eligible')

    def test_recommendations_aggregate_price_and_changed_destination_fail(self):
        self.assertEqual(pilot.assess(PROFILE, URL, doc(url=PROFILE['targets'][1]))['reason'], 'changed_destination_or_variant')
        document = doc()
        document.text = document.text.replace('"@type": "Offer"', '"@type": "AggregateOffer"')
        self.assertEqual(pilot.assess(PROFILE, URL, document)['reason'], 'no_exact_product_offer')

    def test_challenge_and_http_denial_never_become_offer(self):
        for status, text in ((403, ''), (200, '<title>Just a moment</title>')):
            d = pilot.Document(URL, status, text, pilot.utcnow().isoformat())
            self.assertNotEqual(pilot.assess(PROFILE, URL, d)['reason'], 'eligible')
            p = pilot.Policy()
            p.observe(d)
            self.assertTrue(p.denied)

    def test_robots_unavailable_and_disallowed_stop_before_pdp(self):
        for text, status in (('<html>404</html>', 200), ('User-agent: *\nDisallow: /', 200), ('', 403)):
            get = Mock(return_value=pilot.Document(URL, status, text, ''))
            p = pilot.Policy(get=get, sleep=lambda _: None)
            self.assertIsNotNone(p.prepare(URL))
            self.assertEqual(get.call_count, 1)
            self.assertTrue(get.call_args.args[0].endswith('/robots.txt'))

    def test_crawl_delay_and_host_budget(self):
        get = Mock(return_value=pilot.Document(URL, 200, 'User-agent: *\nAllow: /\nCrawl-delay: 60', ''))
        p = pilot.Policy(get=get, sleep=lambda _: None)
        self.assertEqual(p.prepare(URL), 'crawl_delay_exceeds_budget')
        p.rules['https://www.teknosa.com'] = rules()
        p.counts['https://www.teknosa.com'] = 24
        self.assertEqual(p.prepare(URL), 'host_request_budget_reached')

    def test_browser_guard_blocks_writes_auth_private_and_new_pages(self):
        for url, method, kind in ((URL, 'POST', 'document'),
                                  ('https://teknoskor.com/api/ingestion/offers', 'POST', 'fetch'),
                                  ('https://127.0.0.1/x', 'GET', 'script'),
                                  ('https://www.teknosa.com/sepet/add', 'GET', 'fetch'),
                                  ('https://www.teknosa.com/api?token=secret', 'GET', 'fetch'),
                                  (PROFILE['targets'][1], 'GET', 'document'),
                                  ('https://example.com/api', 'GET', 'fetch'),
                                  ('https://challenges.cloudflare.com/x', 'GET', 'document')):
            with self.subTest(url=url, method=method):
                self.assertFalse(pilot.browser_request_allowed(url, method, kind, URL, rules(),
                                  host_check=lambda h: h != '127.0.0.1'))
        self.assertTrue(pilot.browser_request_allowed(URL, 'GET', 'document', URL, rules(), host_check=lambda _: True))
        self.assertFalse(pilot.browser_request_allowed(URL, 'GET', 'document', URL,
                         rules('User-agent: *\nDisallow: /'), host_check=lambda _: True))

    def test_catalog_reference_and_variant_drift_fail_closed(self):
        product = {**PROFILE, 'attributes': {'ram_gb': 8, 'storage_gb': 128}}
        self.assertTrue(pilot.catalog_profile_matches(PROFILE, product))
        for key, value in (('id', 99), ('slug', 'other'), ('attributes', {'ram_gb': 12, 'storage_gb': 128})):
            self.assertFalse(pilot.catalog_profile_matches(PROFILE, {**product, key: value}))

    def test_same_merchant_colors_are_not_increment(self):
        observation = pilot.assess(PROFILE, URL, doc())
        attempts = [{'url': URL, **{lane: deepcopy(observation) for lane in pilot.LANES}} for _ in range(2)]
        result = pilot.summarize_product(PROFILE, attempts)
        self.assertEqual(result['baseline_merchants'], ['Teknosa'])
        self.assertEqual(result['additional_merchants'], [])
        self.assertFalse(result['combined_pair'])

    def test_second_merchant_found_only_after_render_counts_once(self):
        first = pilot.assess(PROFILE, URL, doc())
        other_url = PROFILE['targets'][1]
        second = pilot.assess(PROFILE, other_url, doc(url=other_url))
        rows = [{'url': URL, **{lane: deepcopy(first) for lane in pilot.LANES}},
                {'url': other_url, 'requests': {'reason': 'no_exact_product_offer'},
                 'scrapling_http': deepcopy(second), 'scrapling_browser': deepcopy(second)}]
        result = pilot.summarize_product(PROFILE, rows)
        self.assertEqual(result['additional_merchants'], ['Hepsiburada'])
        self.assertFalse(result['baseline_pair']); self.assertTrue(result['combined_pair'])

    def test_conflicting_price_or_stock_is_not_increment(self):
        first, second = pilot.assess(PROFILE, URL, doc()), pilot.assess(PROFILE, URL, doc(price='26999'))
        rows = [{'url': URL, 'requests': {'reason': 'no_exact_product_offer'},
                 'scrapling_http': first, 'scrapling_browser': second}]
        result = pilot.summarize_product(PROFILE, rows)
        self.assertEqual(result['additional_merchants'], [])
        self.assertTrue(result['paired_conflicts'])

    def test_outlier_cannot_create_second_merchant(self):
        first = pilot.assess(PROFILE, URL, doc())
        other = PROFILE['targets'][1]
        second = pilot.assess(PROFILE, other, doc(url=other, price='1000'))
        rows = [{'url': URL, **{lane: deepcopy(first) for lane in pilot.LANES}},
                {'url': other, 'requests': {'reason': 'no_exact_product_offer'},
                 'scrapling_http': deepcopy(second), 'scrapling_browser': deepcopy(second)}]
        result = pilot.summarize_product(PROFILE, rows)
        self.assertEqual(result['additional_merchants'], [])
        self.assertFalse(result['combined_pair'])

    def test_blocked_run_has_zero_writes_and_is_not_advertised_as_ab_success(self):
        profile = {**PROFILE, 'targets': [URL]}
        catalog = {**profile, 'attributes': {'ram_gb': 8, 'storage_gb': 128}}
        get = Mock(return_value=pilot.Document(URL, 403, '', ''))
        reader = Mock()
        report = pilot.run([profile], pilot.Policy(get=get, sleep=lambda _: None), reader,
                           catalog_get=lambda _: pilot.Document(URL, 200, json.dumps(catalog), ''))
        reader.http.assert_not_called(); reader.browser.assert_not_called()
        self.assertEqual(report['submitted_offers'], 0)
        self.assertEqual(report['database_writes'], 0)
        self.assertEqual(report['comparison_status'], 'not_evaluated_no_policy_eligible_targets')

    def test_workflow_has_no_production_secrets_or_write_step_or_schedule(self):
        text = Path('.github/workflows/scrapling-pilot.yml').read_text()
        self.assertNotIn('secrets.', text)
        self.assertNotIn('schedule:', text)
        self.assertIn('contents: read', text)
        self.assertIn('persist-credentials: false', text)
        self.assertNotIn('reconcile_scores.py', text)
        self.assertNotIn('--apply', text)

    def test_production_credentials_refused(self):
        with patch.dict('os.environ', {'SCRAPER_INGESTION_SECRET': 'do-not-use'}), self.assertRaises(RuntimeError):
            pilot.main()


if __name__ == '__main__':
    unittest.main()
