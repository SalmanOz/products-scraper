"""Offline research boundaries. Candidate observations can NEVER write prices."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import requests

from audit_android_retailer_candidates import audit, document_evidence, load_candidates, probe_host, safe_candidate
from recover_reviewed_offers import load_profiles
from test_retailer_offer_refresh import Response


URL = 'https://www.firsatkurdu.com/urun/poco-x7-pro-8-256gb-siyah-poco-turkiye-garantili-11364'
PROFILE = next(p for p in load_profiles() if p['slug'] == 'poco-x7-pro')
CANDIDATE = {'slug': PROFILE['slug'], 'url': URL, 'review_status': 'research_only'}


class CandidateAuditTests(unittest.TestCase):
    def test_reviewed_batch_covers_exactly_eight_models_without_prices_or_approval(self):
        candidates = load_candidates()
        self.assertEqual({r['slug'] for r in candidates}, {p['slug'] for p in load_profiles()})
        self.assertTrue(all('price' not in r and 'observed_at' not in r and 'availability' not in r for r in candidates))

    def test_network_scope_rejects_secrets_cart_queries_foreign_and_private_hosts(self):
        self.assertTrue(safe_candidate(URL))
        for url in (URL.replace('https:', 'http:'), URL + '?add-to-cart=1', URL + '?token=private',
                    URL.replace('www.firsatkurdu.com', 'www.firsatkurdu.com.evil.test'),
                    URL.replace('https://', 'https://user@'), 'https://127.0.0.1/phone',
                    URL + '?gid=4223707987', URL + '#variant'):
            self.assertFalse(safe_candidate(url), url)

    def test_unavailable_or_html_robots_never_causes_product_request(self):
        for content, status in [('User-agent: *\nDisallow: /', 200), ('<html>404</html>', 200), ('', 403)]:
            fetch = Mock(return_value=Response(content, status, URL))
            rows = probe_host([CANDIDATE], {PROFILE['slug']: PROFILE}, fetch=fetch, sleep=lambda _: None)
            self.assertEqual(fetch.call_count, 1)
            self.assertFalse(rows[0]['candidate_is_approved_for_ingestion'])

    def test_stock_proof_stays_research_only_and_crawl_delay_is_respected(self):
        fetch = Mock(side_effect=[Response('User-agent: *\nCrawl-delay: 30\nAllow: /', url=URL),
                                  Response('<h1>POCO X7 Pro 8GB RAM 256GB</h1>', url=URL)])
        sleep = Mock()
        rows = probe_host([CANDIDATE], {PROFILE['slug']: PROFILE}, fetch=fetch, sleep=sleep, clock=lambda: 0)
        sleep.assert_called_once_with(30)
        self.assertTrue(rows[0]['heading_matches_full_memory_and_radio'])
        self.assertFalse(rows[0]['candidate_is_approved_for_ingestion'])
        self.assertTrue(all(c.kwargs['allow_redirects'] is False for c in fetch.call_args_list))

    def test_denial_or_transport_error_opens_circuit_and_no_redirect_is_followed(self):
        second = {**CANDIDATE, 'url': URL + '-another-color'}
        for response in (Response('', 403, URL), requests.ReadTimeout()):
            fetch = Mock(side_effect=[Response('User-agent: *\nAllow: /', url=URL), response])
            rows = probe_host([CANDIDATE, second], {PROFILE['slug']: PROFILE}, fetch=fetch, sleep=lambda _: None)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(rows[1]['reason'], 'host_circuit_open')
        fetch = Mock(side_effect=[Response('User-agent: *\nAllow: /', url=URL),
                                  Response('', 302, URL, {'Location': 'https://evil.test/'})])
        rows = probe_host([CANDIDATE], {PROFILE['slug']: PROFILE}, fetch=fetch, sleep=lambda _: None)
        self.assertEqual(fetch.call_count, 2)
        self.assertFalse(rows[0]['candidate_is_approved_for_ingestion'])

    def test_wrong_capacity_and_unqualified_ram_never_become_identity_proof(self):
        self.assertFalse(document_evidence('<h1>POCO X7 Pro 12GB RAM 512GB</h1>', URL, PROFILE)['heading_matches_full_memory_and_radio'])
        profile = next(p for p in load_profiles() if p['slug'] == 'samsung-galaxy-a37-5g')
        title = 'Samsung Galaxy A37 5G 6 GB 128 GB (Samsung Türkiye Garantili) Antrasit 128 GB'
        url = 'https://www.ayazgrupiletisim.com.tr/urun/samsung-galaxy-a37-5g-6-gb-128-gb-samsung-turkiye-garantili-antrasit-128-gb'
        self.assertFalse(document_evidence(f'<h1>{title}</h1>', url, profile)['heading_matches_full_memory_and_radio'])

    def test_runner_uses_actual_read_only_daily_code_and_cannot_turn_missing_pairs_green(self):
        updater = SimpleNamespace(run_update=AsyncMock(), last_report={'coverage': {'with_two_merchants': 0}})
        with patch('audit_android_retailer_candidates.PriceUpdater', return_value=updater), \
             patch('audit_android_retailer_candidates.probe_host', return_value=[]), \
             patch('audit_android_retailer_candidates.REPORT') as report:
            with self.assertRaisesRegex(RuntimeError, 'NOT closed'):
                asyncio.run(audit())
        args = updater.run_update.call_args.kwargs
        self.assertIs(args['dry_run'], True)
        self.assertIs(args['known_pdp_only'], True)
        self.assertEqual(len(args['product_slugs']), 8)
        self.assertIn('"new_candidates_approved": 0', report.write_text.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
