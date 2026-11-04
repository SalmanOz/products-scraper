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


class CategoryDiagnosticTests(unittest.TestCase):
    def test_one_changed_category_url_does_not_starve_other_observed_branches_or_override_a_source_block(self):
        root = 'https://www.hepsiburada.com/cep-telefonlari-c-371965'
        changed = 'https://www.hepsiburada.com/samsung-galaxy-s25-fe-android-telefonlar-xc-60005201-b8848-t6765'
        healthy = 'https://www.hepsiburada.com/samsung-cep-telefonlari-xc-371965-b8848'
        for blocked in (False, True):
            client = Client();client.get_public_price_priority = lambda: {}
            calls = []
            class CategoryTransport(Transport):
                def read_hepsiburada_category(self, url):
                    calls.append(url)
                    return (None, 'browser_changed_url', 200) if url == changed else (url, 'document_read', 200)
                def report(self):
                    return {'Hepsiburada': {'blocked': blocked and changed in calls}}
            def parse(html, url, diagnostic=None):
                diagnostic.update(status='parsed')
                return [], None
            with patch('hepsiburada_catalog_discovery.parse_category_document', side_effect=parse), \
                    patch('hepsiburada_catalog_discovery.discovery_links',
                          side_effect=lambda html, url, products: [changed, healthy] if url == root else []), \
                    patch('sweep_priority_catalog.inspect_target') as pdp, \
                    patch('sweep_priority_catalog.refresh_candidates') as writer:
                result = sweep(client=client, transport=CategoryTransport(), discover_hepsiburada=True,
                               category_audit_only=True)
            self.assertEqual(calls, [root, changed] if blocked else [root, changed, healthy])
            self.assertEqual(result['hepsiburada_category_discovery']['incomplete_pages'], [changed])
            self.assertFalse(result['hepsiburada_category_discovery']['branch_queue_complete'])
            pdp.assert_not_called();writer.assert_not_called()

    def test_readonly_category_probe_uses_same_routing_without_any_pdp_read_or_publication(self):
        client = Client()
        client.get_public_price_priority = lambda: {}
        url = 'https://www.hepsiburada.com/samsung-galaxy-a36-5g-8gb-ram-128gb-p-HBCV0000TEST01'
        class CategoryTransport(Transport):
            def read_hepsiburada_category(self, url):
                return '<h1>Public category</h1>', 'document_read', 200
            def read_document(self, url):
                raise AssertionError('A routing audit must not fetch a PDP')
        def parse(html, url, diagnostic=None):
            diagnostic.update(status='parsed')
            return [{'source_url': 'https://www.hepsiburada.com/samsung-galaxy-a36-5g-8gb-ram-128gb-p-HBCV0000TEST01',
                     'discovery_title': 'Samsung Galaxy A36 5G 8GB RAM 128GB', 'stock_priority': False}], None
        with patch('hepsiburada_catalog_discovery.parse_category_document', side_effect=parse), \
                patch('hepsiburada_catalog_discovery.discovery_links', return_value=[]), \
                patch('sweep_priority_catalog.inspect_target') as pdp, \
                patch('sweep_priority_catalog.refresh_candidates') as writer:
            result = sweep(client=client, transport=CategoryTransport(), discover_hepsiburada=True,
                           category_audit_only=True)
        self.assertEqual(result['status'], 'category_audit_completed')
        self.assertEqual(result['category_candidates'], [{'slug': client.product['slug'], 'source_url': url}])
        self.assertEqual(result['rows'], [])
        self.assertEqual((result['queued'], result['accepted'], result['writes']), (1, 0, False))
        pdp.assert_not_called()
        writer.assert_not_called()
        with self.assertRaises(ValueError):
            sweep(client=client, publish=True, category_audit_only=True)
        with self.assertRaises(ValueError):
            sweep(client=client, discover_n11=True, category_audit_only=True)

    def test_parser_and_transport_failures_remain_visible_without_exporting_document_or_offer_payloads(self):
        from sweep_priority_catalog import category_page_diagnostics
        data = {'hepsiburada_category_discovery': {'retailer': 'Hepsiburada', 'pages': [
            {'url': 'https://www.hepsiburada.com/cep-telefonlari-c-371965',
             'reason': 'document_read', 'http_status': 200, 'html': 'must-not-export',
             'parser': {'status': 'category_identity_missing', 'observed_headings': ['Observed category'],
                        'private_state': 'must-not-export'}},
            {'url': 'https://www.hepsiburada.com/cep-telefonlari-c-371965?sayfa=2',
             'reason': 'source_cooldown', 'http_status': None}],
             'prepared_offers': [{'price': 1, 'seller': 'must-not-export'}]}}
        before = deepcopy(data)
        events = category_page_diagnostics(data)
        self.assertEqual([(r['category_page'], r['reason'], r['parser_status']) for r in events],
            [(1, 'document_read', 'category_identity_missing'), (2, 'source_cooldown', None)])
        self.assertEqual(events[0]['observed_headings'], ['Observed category'])
        self.assertNotIn('must-not-export', str(events))
        self.assertEqual(data, before)


def sweep_reader():
    source = reader()
    source.hepsiburada_catalog_source.transport.configure_catalog_budget = lambda count: None
    return source


class SweepTests(unittest.TestCase):
    def test_hb_seller_seeds_keep_exact_selection_and_also_queue_the_default_sku_once(self):
        client = Client()
        base = client.product['offer_targets'][0]['source_url']
        client.product['offer_targets'] = [{'source_url': base + '?magaza=Shop%20One&'},
                                           {'source_url': base + '?magaza=Shop+One'},
                                           {'source_url': base + '?magaza=Shop+Two'}]
        self.assertEqual([url for url, _ in catalog_queue([client.product], {})],
                         [base, base + '?magaza=Shop+One', base + '?magaza=Shop+Two'])

    def test_other_hb_sellers_are_separately_read_once_with_a_shared_two_seller_budget(self):
        client = Client()
        client.get_public_price_priority = lambda: {}
        root = 'https://www.hepsiburada.com/samsung-galaxy-a54-128-gb-p-HBCV0000TEST01'
        links = [root + '?magaza=Shop+' + str(i) for i in range(3)]
        client.product['offer_targets'] = [{'source_url': root}]
        inspected = []
        def inspect(transport, url, product):
            inspected.append(url)
            return {'slug': product['slug'], 'retailer': 'Hepsiburada', 'url': url, 'observed_url': url,
                'http_status': 200, 'reason': 'document_read', 'prepared_offer': {'url': root} if url == root else None,
                'discovered_sellers': [{'source_url': target, 'source_document_url': url,
                                       'discovery_scope': 'public_other_seller_selector'} for target in links]}
        with patch('sweep_priority_catalog.inspect_target', side_effect=inspect), \
                patch('sweep_priority_catalog.refresh_candidates') as writer:
            result = sweep(client=client, transport=Transport(), publish=False, reader=sweep_reader())
        self.assertEqual(inspected, [root, links[0], links[1]])
        self.assertEqual(result['queued'], 3)
        self.assertEqual(result['accepted'], 0)
        writer.assert_not_called()  # A row hint never supplies a quote to publish.

    def test_wrong_variant_root_cannot_spend_other_seller_discovery_slots(self):
        client = Client()
        client.get_public_price_priority = lambda: {}
        root = 'https://www.hepsiburada.com/samsung-galaxy-a54-128-gb-p-HBCV0000TEST01'
        client.product['offer_targets'] = [{'source_url': root}]
        row = {'slug': client.product['slug'], 'retailer': 'Hepsiburada', 'url': root, 'observed_url': root,
            'http_status': 200, 'reason': 'document_read', 'reader_reason': 'selected_radio_mismatch',
            'prepared_offer': None, 'discovered_sellers': [{'source_url': root + '?magaza=Other',
                'source_document_url': root, 'discovery_scope': 'public_other_seller_selector'}]}
        with patch('sweep_priority_catalog.inspect_target', return_value=row) as reader:
            result = sweep(client=client, transport=Transport())
        reader.assert_called_once()
        self.assertEqual(result['queued'], 1)
        self.assertFalse(result['writes'])

    def test_observed_siblings_get_separate_fresh_reads_with_two_per_model_budget_and_no_recursion(self):
        client = Client()
        client.get_public_price_priority = lambda: {}
        root = 'https://www.hepsiburada.com/samsung-galaxy-a54-128-gb-p-HBCV0000TEST01'
        links = [root.replace('TEST01', f'TEST0{i}') for i in range(2, 6)]
        client.product['offer_targets'] = [{'source_url': root}]
        inspected = []
        def inspect(transport, url, product):
            inspected.append(url)
            siblings = links if url == root else [links[-1]]
            return {'slug': product['slug'], 'retailer': 'Hepsiburada', 'url': url, 'observed_url': url,
                'http_status': 200, 'reason': 'document_read',
                'reader_reason': 'selected_color_or_gtin_missing' if url != links[1] else 'selected_new_cash_offer_observed',
                'prepared_offer': {'url': url} if url == links[1] else None,
                'discovered_siblings': [{'source_url': target, 'discovery_scope': 'public_product_variant_link'}
                                        for target in siblings]}
        with patch('sweep_priority_catalog.inspect_target', side_effect=inspect), \
                patch('sweep_priority_catalog.refresh_candidates', return_value={
                    'writes': True, 'status': 'completed', 'ingestion': {'accepted': 1}}) as writer:
            result = sweep(client=client, transport=Transport(), publish=True, reader=sweep_reader())
        self.assertEqual(inspected, [root, links[0], links[1]])
        self.assertEqual((len(result['rows']), result['queued'], result['accepted']), (3, 3, 1))
        writer.assert_called_once()
        self.assertEqual(writer.call_args.args[0]['rows'][0]['url'], links[1])

    def test_coverage_priority_retains_only_current_confirmed_multi_seller_models_and_keeps_full_gate(self):
        from datetime import datetime, timezone
        catalog = products(5)
        for i, product in enumerate(catalog, 25):
            product.update(name=f'Samsung Galaxy S{i}', attributes={'ram_gb': 8, 'storage_gb': 128})
        prices = {p['id']: (p['slug'], p['id'] != 2) for p in catalog}
        client = Client()
        client.get_published_products = lambda: deepcopy(catalog)
        client.get_public_price_priority = lambda: deepcopy(prices)
        inventory = {'evidenceVersion': 'product-offer-match-v1', 'mode': 'shadow_audit_only',
            'auditedAt': datetime.now(timezone.utc).isoformat(), 'productCoverage': [
                {'slug': p['slug'], 'matchedMerchants': count, 'matchedOffers': [
                    {'source_url': p['offer_targets'][0]['source_url'], 'merchant_name': seller, 'price': 10000}
                    for seller in ['First shop', 'Second shop']]} for p, count in zip(catalog, [2, 2, 1, True, 3])]}
        class SearchTransport(Transport):
            def __init__(self):
                self.queries = []
            def read_n11_search(self, query):
                self.queries.append(query)
                return None, 'no_candidates', 200
        def inspect(transport, url, product):
            return {'slug': product['slug'], 'retailer': 'Trendyol', 'url': url, 'observed_url': url,
                    'http_status': 200, 'reason': 'document_read', 'prepared_offer': None}
        for mode in ['current', 'stale', 'missing']:
            current = deepcopy(inventory)
            if mode == 'stale':
                current['auditedAt'] = '1990-01-01T00:00:00Z'
            elif mode == 'missing':
                current = None
            before = deepcopy(current)
            transport = SearchTransport()
            with patch('sweep_priority_catalog.inspect_target', side_effect=inspect), \
                    patch('sweep_priority_catalog.refresh_candidates') as writer:
                result = sweep(client=client, transport=transport, discover_n11=True,
                               current_listing_inventory=current, coverage_first=True)
            retained = ['1'] if mode == 'current' else []
            self.assertEqual(result['coverage_priority']['retained_current_multi_merchant_models'], retained)
            self.assertEqual(result['catalog_count'], 5)
            self.assertEqual(len(result['rows']), 8 if retained else 10)
            self.assertEqual(result['queued'], len(result['rows']))
            self.assertEqual(len(transport.queries), 4 if retained else 5)
            self.assertEqual(current, before)
            self.assertFalse(result['coverage_priority']['retailer_observation_timestamps_renewed'])
            self.assertFalse(result['writes'])
            self.assertEqual(coverage_readback(result, prices)['minimum_ratio'], 0.8)
            writer.assert_not_called()

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
