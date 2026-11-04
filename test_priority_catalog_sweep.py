from copy import deepcopy
import unittest
from unittest.mock import patch

from sweep_priority_catalog import catalog_queue, coverage_readback, sweep
from publish_priority_candidates import CandidatePublicationError
from test_priority_candidates import Client, audit, reader


def products(count):
    return [{'id': i, 'slug': str(i), 'offer_targets': [
        {'source_url': f'https://www.trendyol.com/samsung/galaxy-a54-128-gb-p-{10000 + i}'},
        {'source_url': f'https://www.trendyol.com/samsung/galaxy-a54-black-128-gb-p-{20000 + i}'}]}
        for i in range(1, count + 1)]


class Transport:
    def report(self):
        return {}


def sweep_reader():
    source = reader()
    source.hepsiburada_catalog_source.transport.configure_catalog_budget = lambda count: None
    return source


class SweepTests(unittest.TestCase):
    def test_current_full_listing_inventory_frees_discovery_slots_but_never_skips_normal_refresh(self):
        urls = [f'https://www.n11.com/urun/samsung-galaxy-a36-5g-8gb-128gb-{12345671+i}' for i in range(4)]
        for mode in ['current', 'stale', 'changed_catalog', 'wrong_version']:
            client = Client()
            client.product['offer_targets'] = [{'source_url': urls[0], 'merchant_name': 'n11'}]
            client.get_public_price_priority = lambda: {}
            client.existing = [{'source_url': urls[1], 'seller': 'Already verified shop', 'price': 1}]
            inventory = client.get_offer_identity_readiness()
            if mode == 'stale':
                inventory['auditedAt'] = '1990-01-01T00:00:00Z'
            elif mode == 'changed_catalog':
                inventory['productCoverage'] = []
            elif mode == 'wrong_version':
                inventory['evidenceVersion'] = 'unknown'
            class SearchTransport(Transport):
                def read_n11_search(self, query):
                    html = ''.join(f'<a href="{url}"><h2 class="product-item-title">'
                                   f'{client.product["name"]} 8 GB RAM 128 GB</h2><b>1 TL</b></a>' for url in urls)
                    return html, 'document_read', 200
            def inspect(transport, url, product):
                return {'slug': product['slug'], 'retailer': 'n11', 'url': url, 'observed_url': url,
                        'http_status': 200, 'reason': 'document_read', 'prepared_offer': None}
            with patch('sweep_priority_catalog.inspect_target', side_effect=inspect) as inspected, \
                    patch('sweep_priority_catalog.refresh_candidates') as writer:
                result = sweep(client=client, transport=SearchTransport(), discover_n11=True,
                               current_listing_inventory=inventory)
            expected = [urls[0], *urls[2:]] if mode == 'current' else urls[:3]
            self.assertEqual([call.args[1] for call in inspected.call_args_list], expected, mode)
            self.assertEqual(result['discovery_inventory']['status'], 'current' if mode == 'current' else 'unavailable')
            self.assertFalse(result['writes'])
            writer.assert_not_called()

    def test_missing_catalog_hint_is_discovered_automatically_without_creating_products_or_prices(self):
        client = Client()
        client.product['offer_targets'] = []
        client.get_public_price_priority = lambda: {}
        url = 'https://www.n11.com/urun/samsung-galaxy-a36-5g-8gb-128gb-12345678'
        class SearchTransport(Transport):
            def read_n11_search(self, query):
                return (f'<a href="{url}"><h2 class="product-item-title">'
                        f'{client.product["name"]} 8 GB RAM 128 GB</h2><b>1 TL</b></a>',
                        'document_read', 200)
        row = {'slug': client.product['slug'], 'retailer': 'n11', 'url': url,
               'observed_url': url, 'http_status': 200, 'reason': 'document_read', 'prepared_offer': None}
        with patch('sweep_priority_catalog.inspect_target', return_value=row) as inspect, \
                patch('sweep_priority_catalog.refresh_candidates') as writer:
            result = sweep(client=client, transport=SearchTransport(), discover_n11=True)
        writer.assert_not_called()
        inspect.assert_called_once()
        self.assertEqual(result['queued'], 1)
        self.assertEqual(result['models_without_priority_hints'], [])
        self.assertEqual(result['discovery'][0]['new_targets'][0]['source_url'], url)

    def test_exhausting_the_queue_is_insufficient_until_four_fifths_have_live_prices(self):
        result = {'catalog_count': 110, 'rows': [{'reason': 'document_read'},
                                               {'reason': 'source_circuit_open'}]}
        current = {i: (str(i), i < 48) for i in range(110)}
        report = coverage_readback(result, current)
        self.assertFalse(report['passed'])
        self.assertEqual((report['priced'], report['documents_read'], report['source_circuit_skips']), (48, 1, 1))
        current.update({i: (str(i), True) for i in range(55)})
        self.assertFalse(coverage_readback(result, current)['passed'])
        current.update({i: (str(i), True) for i in range(88)})
        self.assertTrue(coverage_readback(result, current)['passed'])
        self.assertEqual(coverage_readback(result, current)['minimum_ratio'], 0.8)
        self.assertFalse(coverage_readback(result, {})['passed'])

    def test_all_110_models_are_visited_before_any_second_color(self):
        catalog = products(110)
        rows = catalog_queue(catalog, {i: ('', i % 2 == 0) for i in range(1, 111)})
        self.assertEqual(len(rows), 220)
        self.assertEqual({p['id'] for _, p in rows[:110]}, set(range(1, 111)))
        # Changing price status reorders work, but never drops any model.
        self.assertEqual({url for url, _ in rows}, {url for url, _ in catalog_queue(catalog, {})})
        self.assertEqual(len(set(url for url, _ in rows)), len(rows))

    def test_seller_aliases_canonicalize_and_cross_model_url_ownership_is_rejected(self):
        catalog = products(2)
        url = catalog[0]['offer_targets'][0]['source_url']
        catalog[0]['offer_targets'].append({'source_url': url + '?merchantId=123'})
        self.assertEqual(len(catalog_queue(catalog, {})), 4)
        catalog[1]['offer_targets'].append({'source_url': url + '?merchantId=456'})
        rows = catalog_queue(catalog, {})
        self.assertNotIn(url, [target for target, _ in rows])
        self.assertEqual(len(rows), 3)

    def test_actual_live_candidate_is_published_before_next_queue_read(self):
        client = Client()
        second = deepcopy(client.product)
        second.update(id=1000, slug='second-model', offer_targets=[{
            'source_url': 'https://www.trendyol.com/samsung/galaxy-a54-128-gb-p-123456'}])
        catalog = [client.product, second]
        client.get_published_products = lambda: deepcopy(catalog)
        client.get_public_price_priority = lambda: {}
        source = sweep_reader()
        def inspect(transport, url, product):
            if product['id'] == 1000:
                self.assertEqual(len(client.writes), 1)
                return {'slug': product['slug'], 'retailer': 'Trendyol', 'url': url,
                        'http_status': 200, 'reason': 'document_read', 'prepared_offer': None}
            return deepcopy(audit()['rows'][0])
        checkpoints = []
        with patch('sweep_priority_catalog.inspect_target', side_effect=inspect):
            result = sweep(client=client, publish=True, transport=Transport(), reader=source,
                           checkpoint=lambda value: checkpoints.append(deepcopy(value)))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual((result['accepted'], result['writes']), (1, True))
        self.assertTrue(result['rows'][0]['publication']['commercial_readback_verified'])
        self.assertEqual(len(checkpoints), 3)

    def test_read_only_never_calls_publisher_and_no_hints_are_reported(self):
        client = Client()
        client.product['offer_targets'] = []
        client.get_public_price_priority = lambda: {}
        with patch('sweep_priority_catalog.refresh_candidates') as writer:
            result = sweep(client=client, transport=Transport())
        writer.assert_not_called()
        self.assertEqual(result['models_without_priority_hints'], [client.product['slug']])
        self.assertEqual((result['queued'], result['accepted'], result['writes']), (0, 0, False))

    def test_write_or_readback_failure_stops_next_candidate_and_checkpoints_failure(self):
        client = Client()
        client.get_public_price_priority = lambda: {}
        checkpoints = []
        with patch('sweep_priority_catalog.inspect_target', return_value=audit()['rows'][0]), \
                patch('sweep_priority_catalog.refresh_candidates', side_effect=ValueError('failed_readback')):
            with self.assertRaisesRegex(ValueError, 'failed_readback'):
                sweep(client=client, publish=True, transport=Transport(), reader=sweep_reader(),
                      checkpoint=lambda value: checkpoints.append(deepcopy(value)))
        self.assertEqual(checkpoints[-1]['status'], 'failed')

    def test_uncertain_write_stops_further_writes_but_finishes_independent_readonly_queue(self):
        client = Client()
        catalog = products(2)
        for row in catalog:
            row.update(name=client.product['name'], attributes=client.product['attributes'])
        client.get_published_products = lambda: deepcopy(catalog)
        client.get_public_price_priority = lambda: {}
        failure = {'status': 'publication_failed', 'writes': True,
                   'ingestion': {'committed': True, 'accepted': 1}}
        with patch('sweep_priority_catalog.inspect_target', return_value=audit()['rows'][0]) as inspect, \
                patch('sweep_priority_catalog.refresh_candidates',
                      side_effect=CandidatePublicationError('readback_failed', failure)) as writer:
            result = sweep(client=client, publish=True, transport=Transport(), reader=sweep_reader())
        self.assertEqual(inspect.call_count, 4)
        self.assertEqual(writer.call_count, 1)
        self.assertEqual((result['status'], result['accepted']), ('publication_failed', 1))


if __name__ == '__main__':
    unittest.main()
