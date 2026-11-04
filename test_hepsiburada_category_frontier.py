from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from hepsiburada_category_frontier import (
    VERSION, MAX_AGE_SECONDS, MAX_BYTES, catalog_fingerprint, load_frontier, save_frontier,
)
from hepsiburada_catalog_discovery import CATEGORY_URL
from sweep_priority_catalog import sweep, coverage_readback
from test_priority_candidates import Client

BRAND = 'https://www.hepsiburada.com/samsung-cep-telefonlari-xc-371965-b8848'
PRODUCT = {'id': 1, 'slug': 'samsung-galaxy-s26', 'name': 'Samsung Galaxy S26',
           'attributes': {'ram_gb': 12, 'storage_gb': 256}, 'offer_targets': []}


class FrontierTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.products = [deepcopy(PRODUCT)]

    def payload(self):
        return {'version': VERSION, 'catalog_fingerprint': catalog_fingerprint(self.products),
                'cycle_started_at': self.now.isoformat(), 'updated_at': self.now.isoformat(),
                'pending': [BRAND + '?sayfa=2'], 'visited': [CATEGORY_URL, BRAND]}

    def test_physical_catalog_changes_invalidate_visited_exclusions_but_preserve_unread_urls(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'frontier.json'
            path.write_text(json.dumps(self.payload()))
            for patch_values in [{'fresh_lowest_price': 1000, 'fresh_merchant_count': 2},
                                 {'teknoskor_score': 95, 'value_score': 12}]:
                products = [{**PRODUCT, **patch_values}]
                state, status = load_frontier(path, products, now=self.now)
                self.assertEqual(status, 'ready')
                self.assertEqual(state['pending'], [BRAND + '?sayfa=2'])
            products = deepcopy(self.products)
            products[0]['attributes']['ram_gb'] = 8
            state, status = load_frontier(path, products, now=self.now)
            self.assertEqual(status, 'catalog_changed_pending_retained')
            self.assertEqual(state['visited'], [])
            self.assertEqual(state['pending'], [BRAND + '?sayfa=2'])
            self.assertEqual(state['catalog_fingerprint'], catalog_fingerprint(products))
            self.assertEqual(state['cycle_started_at'], self.payload()['cycle_started_at'])

    def test_a_new_variant_keeps_the_unread_tail_and_still_requires_its_new_selected_pdp_read(self):
        catalog = self.products + [{**PRODUCT, 'id': 2, 'slug': PRODUCT['slug'] + '-12gb-512gb',
            'name': PRODUCT['name'] + ' 12GB 512GB',
            'attributes': {'ram_gb': 12, 'storage_gb': 512}}]
        client = Client()
        client.get_published_products = lambda: catalog
        client.get_public_price_priority = lambda: {}
        target = 'https://www.hepsiburada.com/samsung-galaxy-s26-512-gb-12-gb-ram-p-HBCV0000TEST52'
        class Transport:
            def __init__(self): self.calls = []
            def report(self): return {}
            def read_hepsiburada_category(self, url):
                self.calls.append(url)
                if url == CATEGORY_URL:
                    html = '<h1>Cep Telefonu Fiyatları ve Modelleri</h1><a href="' + BRAND + '">Samsung</a>'
                else:
                    html = '<h1>Samsung Android Telefonlar</h1><h2 data-test-id="title-1"><a title="Samsung Galaxy S26 512 GB 12 GB Ram" href="' + target + '">' + \
                        'Samsung Galaxy S26 512 GB 12 GB Ram</a></h2>'
                return html, 'document_read', 200
        def inspect(transport, url, product):
            return {'slug': product['slug'], 'url': url, 'observed_url': url,
                    'reason': 'selected_proof_missing', 'http_status': 200, 'prepared_offer': None}
        with TemporaryDirectory() as folder, patch('hepsiburada_catalog_discovery.MAX_CATEGORY_PAGES', 2), \
                patch('sweep_priority_catalog.inspect_target', side_effect=inspect) as selected_read, \
                patch('sweep_priority_catalog.refresh_candidates') as writer:
            path = Path(folder) / 'frontier.json'
            path.write_text(json.dumps(self.payload()))
            transport = Transport()
            result = sweep(client=client, transport=transport, discover_hepsiburada=True,
                           category_frontier_path=path)
            self.assertEqual(transport.calls, [CATEGORY_URL, BRAND + '?sayfa=2'])
            self.assertEqual(result['hepsiburada_category_continuation']['restored_visited'], 0)
            self.assertEqual(result['hepsiburada_category_continuation']['restored_pending'], 1)
            self.assertIn(BRAND, result['hepsiburada_category_discovery']['pending_observed_urls'])
            selected_read.assert_called_once_with(transport, target, catalog[1])
            self.assertFalse(result['writes'])
            self.assertEqual(result['accepted'], 0)
            writer.assert_not_called()

    def test_stale_future_malformed_foreign_and_price_bearing_state_never_enters_the_queue(self):
        cases = []
        stale = self.payload(); stale['cycle_started_at'] = (self.now - timedelta(seconds=MAX_AGE_SECONDS + 1)).isoformat()
        cases.append((stale, 'expired'))
        future = self.payload(); future['updated_at'] = (self.now + timedelta(minutes=2)).isoformat()
        cases.append((future, 'invalid'))
        extra = self.payload(); extra['prepared_offer'] = {'price': 1}
        cases.append((extra, 'invalid'))
        for url in ['https://evil.test/phone', 'https://www.hepsiburada.com/phone-p-HBCV0000TEST01',
                    BRAND + '?token=private', BRAND + '\n', BRAND + '#fragment']:
            altered = self.payload(); altered['pending'] = [url]
            cases.append((altered, 'invalid'))
        duplicate = self.payload(); duplicate['pending'] *= 2
        cases.append((duplicate, 'invalid'))
        overlap = self.payload(); overlap['pending'] = [BRAND]
        cases.append((overlap, 'invalid'))
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'frontier.json'
            for data, reason in cases:
                path.write_text(json.dumps(data))
                restored, status = load_frontier(path, self.products, now=self.now)
                self.assertEqual(status, reason, data)
                self.assertEqual(restored['pending'], [])
                self.assertEqual(restored['visited'], [])
            path.write_bytes(b'x' * (MAX_BYTES + 1))
            self.assertEqual(load_frontier(path, self.products, now=self.now)[1], 'oversized')
            path.write_text('{"version":1,"version":2}')
            self.assertEqual(load_frontier(path, self.products, now=self.now)[1], 'invalid')

    def test_saved_state_contains_only_category_url_hints_and_starts_fresh_after_drain(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'nested/frontier.json'
            result = {'hepsiburada_category_continuation': {'status': 'absent',
                'catalog_fingerprint': catalog_fingerprint(self.products), 'cycle_started_at': self.now.isoformat()},
                'hepsiburada_category_discovery': {'pending_observed_urls': [BRAND + '?sayfa=2'],
                    'visited_category_urls': [CATEGORY_URL, BRAND]},
                'rows': [{'prepared_offer': {'price': 12345, 'seller': 'must-not-cache', 'html': 'must-not-cache'}}]}
            saved = save_frontier(path, result, now=self.now)
            self.assertEqual(saved['status'], 'saved')
            raw = path.read_text()
            self.assertNotIn('must-not-cache', raw)
            self.assertNotIn('"price"', raw)
            self.assertEqual(load_frontier(path, self.products, now=self.now)[1], 'ready')
            result['hepsiburada_category_discovery']['pending_observed_urls'] = []
            save_frontier(path, result, now=self.now)
            state, status = load_frontier(path, self.products, now=self.now)
            self.assertEqual(status, 'drained')
            self.assertEqual(state['visited'], [])

    def test_three_bounded_invocations_reach_the_unread_tail_without_replaying_the_brand_prefix(self):
        client = Client()
        client.get_published_products = lambda: self.products
        client.get_public_price_priority = lambda: {}
        class Transport:
            def __init__(self): self.calls = []
            def report(self): return {}
            def read_hepsiburada_category(self, url):
                self.calls.append(url)
                if url == CATEGORY_URL:
                    body = '<h1>Cep Telefonu Fiyatları ve Modelleri</h1><a href="' + BRAND + '">Samsung</a>'
                else:
                    page = int(url.rsplit('=', 1)[1]) if '?sayfa=' in url else 1
                    body = '<h1>Samsung Android Telefonlar</h1>'
                    if page < 3:
                        body += '<link rel="next" href="' + BRAND + f'?sayfa={page+1}">'
                return body, 'document_read', 200
        with TemporaryDirectory() as folder, patch('hepsiburada_catalog_discovery.MAX_CATEGORY_PAGES', 2), \
                patch('sweep_priority_catalog.refresh_candidates') as writer:
            path = Path(folder) / 'frontier.json'
            for index, target in enumerate([BRAND, BRAND + '?sayfa=2', BRAND + '?sayfa=3']):
                transport = Transport()
                result = sweep(client=client, transport=transport, discover_hepsiburada=True,
                               category_frontier_path=path)
                self.assertEqual(transport.calls, [CATEGORY_URL, target])
                self.assertEqual(len(result['hepsiburada_category_discovery']['pages']), 2)
                self.assertFalse(result['writes'])
                self.assertEqual(result['accepted'], 0)
                self.assertFalse(coverage_readback(result, {1: (PRODUCT['slug'], False)})['passed'])
                self.assertEqual(save_frontier(path, result)['status'], 'saved')
                self.assertEqual(result['hepsiburada_category_continuation']['status'], 'absent' if index == 0 else 'ready')
            self.assertEqual(result['hepsiburada_category_discovery']['remaining_observed_pages'], 0)
            self.assertEqual(load_frontier(path, self.products)[1], 'drained')
            writer.assert_not_called()

    def test_completed_category_progress_survives_an_interrupted_later_source_without_a_final_save(self):
        client = Client()
        client.get_published_products = lambda: self.products
        client.get_public_price_priority = lambda: {}
        class Transport:
            def __init__(self): self.calls = []
            def report(self): return {}
            def read_hepsiburada_category(self, url):
                self.calls.append(url)
                if url == CATEGORY_URL:
                    body = '<h1>Cep Telefonu Fiyatları ve Modelleri</h1><a href="' + BRAND + '">Samsung</a>'
                else:
                    page = int(url.rsplit('=', 1)[1]) if '?sayfa=' in url else 1
                    body = '<h1>Samsung Android Telefonlar</h1>'
                    body += '<link rel="next" href="' + BRAND + f'?sayfa={page+1}">'
                return body, 'document_read', 200
            def read_teknosa_category(self, url):
                raise TimeoutError('later_source_interrupted')
        with TemporaryDirectory() as folder, patch('hepsiburada_catalog_discovery.MAX_CATEGORY_PAGES', 2), \
                patch('sweep_priority_catalog.refresh_candidates') as writer:
            path = Path(folder) / 'frontier.json'
            first = Transport()
            with self.assertRaisesRegex(TimeoutError, 'later_source_interrupted'):
                sweep(client=client, transport=first, discover_hepsiburada=True,
                      discover_teknosa=True, category_frontier_path=path)
            saved, status = load_frontier(path, self.products)
            self.assertEqual(status, 'ready')
            self.assertEqual(saved['pending'], [BRAND + '?sayfa=2'])
            self.assertEqual(saved['visited'], [CATEGORY_URL, BRAND])
            self.assertNotIn('price', path.read_text())
            second = Transport()
            result = sweep(client=client, transport=second, discover_hepsiburada=True,
                           category_frontier_path=path)
            self.assertEqual(second.calls, [CATEGORY_URL, BRAND + '?sayfa=2'])
            self.assertEqual(result['hepsiburada_category_frontier_saved']['status'], 'saved')
            self.assertEqual(load_frontier(path, self.products)[0]['cycle_started_at'], saved['cycle_started_at'])
            self.assertFalse(result['writes'])
            writer.assert_not_called()

    def test_readonly_category_audit_cannot_restore_a_pricing_sweeps_frontier(self):
        client = Client(); client.get_published_products = lambda: self.products
        client.get_public_price_priority = lambda: {}
        class Transport:
            def __init__(self): self.calls = []
            def report(self): return {}
            def read_hepsiburada_category(self, url):
                self.calls.append(url)
                return '<h1>Cep Telefonu Fiyatları ve Modelleri</h1>', 'document_read', 200
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'frontier.json'; path.write_text(json.dumps(self.payload()))
            before = path.read_bytes(); transport = Transport()
            result = sweep(client=client, transport=transport, discover_hepsiburada=True,
                           category_audit_only=True, category_frontier_path=path)
            self.assertEqual(transport.calls, [CATEGORY_URL])
            self.assertNotIn('hepsiburada_category_continuation', result)
            self.assertEqual(save_frontier(path, result)['status'], 'not_enabled')
            self.assertEqual(path.read_bytes(), before)

    def test_cli_saves_the_observed_queue_before_a_failing_catalog_price_gate(self):
        from sweep_priority_catalog import main
        client = Client()
        client.get_public_price_priority = lambda: {1: (PRODUCT['slug'], False)}
        result = {'purpose': 'systematic_priority_catalog_recovery', 'catalog_count': 1,
            'publish': False, 'writes': False, 'status': 'completed', 'rows': [], 'accepted': 0, 'queued': 0,
            'models_without_priority_hints': [],
            'hepsiburada_category_continuation': {'status': 'absent',
                'catalog_fingerprint': catalog_fingerprint(self.products), 'cycle_started_at': self.now.isoformat()},
            'hepsiburada_category_discovery': {'pending_observed_urls': [BRAND + '?sayfa=2'],
                'visited_category_urls': [CATEGORY_URL, BRAND]}}
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'frontier.json'
            with patch('sys.argv', ['sweep']), \
                    patch('sweep_priority_catalog.OfferIngestionClient.from_env', return_value=client), \
                    patch.object(client, 'get_offer_identity_readiness', return_value={}, create=True), \
                    patch('sweep_priority_catalog.sweep', return_value=result), \
                    patch('sweep_priority_catalog.save_report'), \
                    patch('sweep_priority_catalog.Path') as report, \
                    patch('hepsiburada_category_frontier.STATE_PATH', path), patch('builtins.print'):
                report.return_value.read_text.return_value = ''
                with self.assertRaisesRegex(SystemExit, 'Public catalog coverage gate failed'):
                    main()
            self.assertTrue(path.exists())
            restored, status = load_frontier(path, self.products)
            self.assertEqual(status, 'ready')
            self.assertEqual(restored['pending'], [BRAND + '?sayfa=2'])
            self.assertFalse(result['public_coverage']['passed'])


if __name__ == '__main__':
    unittest.main()
