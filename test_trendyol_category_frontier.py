from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from hepsiburada_category_frontier import VERSION as HB_VERSION
from trendyol_category_frontier import VERSION, MAX_AGE_SECONDS, MAX_BYTES, catalog_fingerprint, load_frontier, save_frontier
from trendyol_catalog_discovery import CATEGORY_URL
from sweep_priority_catalog import sweep

BRAND = 'https://www.trendyol.com/xiaomi-android-cep-telefonu-x-b101939-c164461'
TARGET = 'https://www.trendyol.com/xiaomi/13t-256-gb-12-gb-ram-siyah-cep-telefonu-xiaomi-turkiye-garantili-p-767729000'
PRODUCT = {'id': 215, 'slug': 'xiaomi-13t', 'name': 'Xiaomi 13T',
           'attributes': {'ram_gb': 12, 'storage_gb': 256, 'Connectivity': {'5G': True}},
           'offer_targets': [], 'offer_targets_truncated': False}


class TrendyolCategoryFrontierTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.products = [deepcopy(PRODUCT)]

    def payload(self):
        return {'version': VERSION, 'catalog_fingerprint': catalog_fingerprint(self.products),
                'cycle_started_at': self.now.isoformat(), 'updated_at': self.now.isoformat(),
                'pending': [BRAND + '?pi=2'], 'visited': [CATEGORY_URL, BRAND]}

    def test_next_run_reads_unread_native_page_and_requires_a_new_selected_pdp_instead_of_repeating_prefix(self):
        pages = [Path('fixtures/trendyol-xiaomi-public-' + part + '-page-routing.html').read_text()
                 for part in ('first', 'second')]
        class Client:
            def get_published_products(self): return deepcopy(self.products)
            def get_public_price_priority(self): return {215: ('xiaomi-13t', False)}
        client = Client(); client.products = self.products
        class Transport:
            def __init__(self): self.calls = []
            def report(self): return {}
            def read_trendyol_category(self, url):
                self.calls.append(('category', url))
                if url == CATEGORY_URL:
                    body = f'<h1 class="title">Cep Telefonu</h1><a href="{CATEGORY_URL}">1</a><a href="{BRAND}">Xiaomi</a>'
                elif url in (BRAND, BRAND + '?pi=2'):
                    body = pages[0 if url == BRAND else 1]
                else:
                    assert url == BRAND + '?pi=3'
                    body = f'<h1 class="title">Xiaomi Android Cep Telefonu</h1><a href="{BRAND}">1</a>'
                return body, 'document_read', 200
            def read_document(self, url):
                self.calls.append(('pdp', url))
                return '<h1>No complete selected offer proof</h1>', 'document_read', 200
            def resolved_url(self, url): return url
        with TemporaryDirectory() as folder, patch('trendyol_catalog_discovery.MAX_CATEGORY_PAGES', 2), \
                patch('sweep_priority_catalog.refresh_candidates') as writer:
            path = Path(folder) / 'trendyol.json'
            first = Transport()
            one = sweep(client=client, transport=first, discover_trendyol=True, trendyol_frontier_path=path)
            self.assertEqual(first.calls, [('category', CATEGORY_URL), ('category', BRAND)])
            self.assertEqual(one['trendyol_category_discovery']['pending_observed_urls'], [BRAND + '?pi=2'])
            self.assertEqual(one['trendyol_category_frontier_saved']['status'], 'saved')
            second = Transport()
            two = sweep(client=client, transport=second, discover_trendyol=True, trendyol_frontier_path=path)
            self.assertEqual([url for kind, url in second.calls if kind == 'category'],
                             [CATEGORY_URL, BRAND + '?pi=2'])
            self.assertIn(('pdp', TARGET), second.calls)
            self.assertEqual(two['trendyol_category_continuation']['status'], 'ready')
            self.assertEqual(two['trendyol_category_discovery']['pending_observed_urls'], [BRAND + '?pi=3'])
            self.assertTrue(all('apigw.trendyol.com' not in url for _, url in second.calls))
            self.assertEqual((two['accepted'], two['writes']), (0, False))
            writer.assert_not_called()
            saved = json.loads(path.read_text())
            self.assertEqual(set(saved), {'version', 'catalog_fingerprint', 'cycle_started_at', 'updated_at', 'pending', 'visited'})
            self.assertNotIn('price', path.read_text())
            third = Transport()
            sweep(client=client, transport=third, discover_trendyol=True, trendyol_frontier_path=path)
            self.assertEqual(third.calls, [('category', CATEGORY_URL), ('category', BRAND + '?pi=3')])
            self.assertEqual(load_frontier(path, self.products)[1], 'drained')

    def test_new_physical_variants_keep_unread_urls_but_discard_old_visited_exclusions(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'trendyol.json'; path.write_text(json.dumps(self.payload()))
            changed = self.products + [{**PRODUCT, 'id': 999, 'slug': 'xiaomi-13t-8gb-256gb',
                                       'attributes': {**PRODUCT['attributes'], 'ram_gb': 8}}]
            state, status = load_frontier(path, changed, now=self.now)
            self.assertEqual(status, 'catalog_changed_pending_retained')
            self.assertEqual(state['pending'], [BRAND + '?pi=2'])
            self.assertEqual(state['visited'], [])
            self.assertEqual(state['catalog_fingerprint'], catalog_fingerprint(changed))
            self.assertEqual(state['cycle_started_at'], self.payload()['cycle_started_at'])

    def test_completed_category_routes_survive_a_later_selected_pdp_failure(self):
        page = Path('fixtures/trendyol-xiaomi-public-second-page-routing.html').read_text()
        products = self.products
        class Client:
            def get_published_products(self): return deepcopy(products)
            def get_public_price_priority(self): return {215: ('xiaomi-13t', False)}
        class Transport:
            def report(self): return {}
            def read_trendyol_category(self, url):
                if url == CATEGORY_URL:
                    return f'<h1 class="title">Cep Telefonu</h1><a href="{CATEGORY_URL}">1</a>', 'document_read', 200
                assert url == BRAND + '?pi=2'
                return page, 'document_read', 200
        with TemporaryDirectory() as folder, patch('trendyol_catalog_discovery.MAX_CATEGORY_PAGES', 2), \
                patch('sweep_priority_catalog.inspect_target', side_effect=RuntimeError('later PDP failure')) as selected:
            path = Path(folder) / 'trendyol.json'; path.write_text(json.dumps(self.payload()))
            transport = Transport()
            with self.assertRaises(RuntimeError):
                sweep(client=Client(), transport=transport, discover_trendyol=True, trendyol_frontier_path=path)
            selected.assert_called_once_with(transport, TARGET, products[0])
            state, status = load_frontier(path, products)
            self.assertEqual(status, 'ready')
            self.assertEqual(state['pending'], [BRAND + '?pi=3'])
            self.assertIn(BRAND + '?pi=2', state['visited'])

    def test_price_and_score_changes_do_not_restart_the_route_cycle(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'trendyol.json'; path.write_text(json.dumps(self.payload()))
            products = [{**PRODUCT, 'fresh_lowest_price': 100, 'value_score': 99, 'fresh_merchant_count': 4}]
            state, status = load_frontier(path, products, now=self.now)
            self.assertEqual(status, 'ready')
            self.assertEqual(state['visited'], [CATEGORY_URL, BRAND])

    def test_stale_foreign_malformed_price_bearing_and_other_retailer_state_cannot_route_requests(self):
        cases = []
        stale = self.payload(); stale['cycle_started_at'] = (self.now - timedelta(seconds=MAX_AGE_SECONDS + 1)).isoformat()
        cases.append((stale, 'expired'))
        future = self.payload(); future['updated_at'] = (self.now + timedelta(minutes=2)).isoformat()
        cases.append((future, 'invalid'))
        priced = self.payload(); priced['prepared_offer'] = {'price': 1}; cases.append((priced, 'invalid'))
        wrong = self.payload(); wrong['version'] = HB_VERSION; cases.append((wrong, 'invalid'))
        for url in ['https://evil.example/phone', TARGET, BRAND + '?token=private', BRAND + '?pi=1000',
                    BRAND + '#price', BRAND + '\n', BRAND.replace('https:', 'http:'),
                    'https://www.hepsiburada.com/cep-telefonlari-c-371965']:
            wrong = self.payload(); wrong['pending'] = [url]; cases.append((wrong, 'invalid'))
        duplicate = self.payload(); duplicate['pending'] *= 2; cases.append((duplicate, 'invalid'))
        overlap = self.payload(); overlap['pending'] = [BRAND]; cases.append((overlap, 'invalid'))
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'trendyol.json'
            for data, reason in cases:
                path.write_text(json.dumps(data))
                state, status = load_frontier(path, self.products, now=self.now)
                self.assertEqual(status, reason, data)
                self.assertEqual(state['pending'], [])
                self.assertEqual(state['visited'], [])
            path.write_bytes(b'x' * (MAX_BYTES + 1))
            self.assertEqual(load_frontier(path, self.products)[1], 'oversized')
            path.write_text('{"version":1,"version":2}')
            self.assertEqual(load_frontier(path, self.products)[1], 'invalid')

    def test_save_excludes_prices_documents_and_pdp_destinations(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'trendyol.json'
            payload = self.payload()
            result = {'trendyol_category_continuation': {'status': 'absent',
                'catalog_fingerprint': payload['catalog_fingerprint'], 'cycle_started_at': payload['cycle_started_at']},
                'trendyol_category_discovery': {'pending_observed_urls': payload['pending'],
                                              'visited_category_urls': payload['visited']},
                'rows': [{'price': 1, 'document': 'not cached', 'prepared_offer': {'seller': 'not cached'}}]}
            self.assertEqual(save_frontier(path, result, now=self.now)['status'], 'saved')
            self.assertEqual(json.loads(path.read_text()), payload)
            result['trendyol_category_discovery']['pending_observed_urls'] = [TARGET]
            self.assertEqual(save_frontier(path, result, now=self.now)['status'], 'invalid_or_oversized_queue')


if __name__ == '__main__':
    unittest.main()
