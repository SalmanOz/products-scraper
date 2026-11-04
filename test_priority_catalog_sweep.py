from copy import deepcopy
import unittest
from unittest.mock import patch

from sweep_priority_catalog import (catalog_queue, coverage_readback, sweep, target_preflight,
                                    recovery_hepsiburada_seeds, priority_store_coverage_readback)
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


class TargetPreflightTests(unittest.TestCase):
    def test_teknosa_supplement_requires_fresh_missing_platform_audit_and_keeps_two_target_limit(self):
        from datetime import datetime, timedelta, timezone
        base = 'https://www.teknosa.com/samsung-galaxy-a36-5g-8gb128gb-akilli-telefon-p-12507984'
        urls = [base + str(i) for i in range(3)]
        class CategoryTransport(Transport):
            def read_teknosa_category(self, url):
                return '<h1 class="plp-title">Cep Telefonu Fiyatları ve Modelleri</h1>', 'document_read', 200
        for mode, expected_reads in [('missing', 2), ('present', 0), ('stale', 3), ('none', 3)]:
            with self.subTest(mode=mode):
                client = Client()
                client.product['offer_targets'] = [{'source_url': url} for url in urls]
                client.get_public_price_priority = lambda: {client.product['id']: (client.product['slug'], True)}
                records = [{'source_url': target, 'merchant_name': name, 'price': 30000} for target, name in [
                    ('https://www.hepsiburada.com/samsung-galaxy-a36-p-HBCV0000OLD001', 'Shop One'),
                    (urls[0] if mode == 'present' else 'https://www.hepsiburada.com/samsung-galaxy-a36-p-HBCV0000OLD002', 'Shop Two')]]
                inventory = {'evidenceVersion': 'product-offer-match-v1', 'mode': 'shadow_audit_only',
                    'auditedAt': (datetime.now(timezone.utc) - timedelta(minutes=6 if mode == 'stale' else 0)).isoformat(),
                    'productCoverage': [{'slug': client.product['slug'], 'matchedMerchants': 2, 'matchedOffers': records}]}
                def inspect(transport, url, product):
                    return {'slug': product['slug'], 'retailer': 'Teknosa', 'url': url, 'observed_url': url,
                            'reason': 'http_404', 'http_status': 404, 'prepared_offer': None}
                with patch('sweep_priority_catalog.inspect_target', side_effect=inspect) as inspector:
                    result = sweep(client=client, transport=CategoryTransport(), discover_teknosa=True,
                        coverage_first=True, current_listing_inventory=None if mode == 'none' else inventory)
                self.assertEqual(inspector.call_count, expected_reads)
                self.assertEqual(result['teknosa_store_gap_discovery']['eligible_models'],
                                 [client.product['slug']] if mode == 'missing' else [])
                self.assertFalse(result['writes'])

    def test_missing_teknosa_platform_is_recovered_after_primary_gaps_from_existing_or_fresh_category_hints(self):
        from datetime import datetime, timezone
        from html import escape
        from pathlib import Path
        from teknosa_reviewed_source import TeknosaCatalogSource
        from retailer_offer_refresh import RetailerOfferRefresher
        html = Path(__file__).with_name('fixtures').joinpath('teknosa-selected-s25-ultra-512-physical-table.html').read_text()
        url = 'https://www.teknosa.com/samsung-galaxy-s25-ultra-5g-512gb-titanyum-gumus-akilli-telefon-p-125079845'
        title = 'Samsung Galaxy S25 Ultra 5G 512GB Titanyum Gümüş Akıllı Telefon'
        missing_url = 'https://www.hepsiburada.com/honor-90-256-gb-8-gb-ram-p-HBCV0000MIS001'
        missing = {'id': 1000, 'name': 'Honor 90', 'slug': 'honor-90',
                   'attributes': {'ram_gb': 8, 'storage_gb': 256, 'Connectivity': {'5G': True}},
                   'offer_targets': [{'source_url': missing_url}]}
        class ScopedClient(Client):
            def get_published_products(self):
                return deepcopy([self.product, missing])
            def get_public_price_priority(self):
                return {self.product['id']: (self.product['slug'], True), missing['id']: (missing['slug'], False)}
            def get_offer_identity_readiness(self):
                rows = [{'source_url': 'https://www.hepsiburada.com/samsung-galaxy-s25-ultra-p-HBCV0000OLD001',
                         'merchant_name': 'Shop One', 'price': 80000},
                        {'source_url': 'https://www.hepsiburada.com/samsung-galaxy-s25-ultra-p-HBCV0000OLD002',
                         'merchant_name': 'Shop Two', 'price': 81000}]
                rows += [{'source_url': r['source_url'], 'merchant_name': r['seller'], 'price': r['price']}
                         for batch in self.writes for r in batch]
                return {'evidenceVersion': 'product-offer-match-v1', 'mode': 'shadow_audit_only',
                    'auditedAt': datetime.now(timezone.utc).isoformat(), 'productCoverage': [
                        {'slug': self.product['slug'], 'matchedMerchants': len(rows), 'matchedOffers': rows},
                        {'slug': missing['slug'], 'matchedMerchants': 0, 'matchedOffers': []}]}
            def ingest_listing_offers(self, rows, *, require_all_accepted):
                return {**super().ingest_offers(rows, require_all_accepted=require_all_accepted),
                        'offer_identity_contract': 'seller-source-url-v1', 'server_scores_recalculated': True}
        class SourceTransport(Transport):
            def __init__(self, category_hint):
                self.calls, self.category_hint = [], category_hint
            def configure_catalog_budget(self, count):
                pass
            def read_document(self, target):
                self.calls.append(target)
                return (html, 'document_read', 200) if target == url else (None, 'http_404', 404)
            def read_teknosa_category(self, target):
                card = ('<div data-product-url="' + url + '" data-product-name="' + escape(title) +
                    '" data-product-id="125079845" data-product-ean="8806097015154" '
                    'data-product-page-type="CATEGORY" data-product-stock="Y"><h3 class="prd-title">' +
                    escape(title) + '</h3></div>') if self.category_hint else ''
                return '<h1 class="plp-title">Cep Telefonu Fiyatları ve Modelleri</h1>' + card, 'document_read', 200
        for category_hint in (False, True):
            with self.subTest(category_hint=category_hint):
                client = ScopedClient()
                client.product.update(name='Samsung Galaxy S25 Ultra 12GB 512GB',
                    slug='samsung-galaxy-s25-ultra-12gb-512gb',
                    attributes={'ram_gb': 12, 'storage_gb': 512, 'Connectivity': {'5G': True}},
                    offer_targets=[] if category_hint else [{'source_url': url, 'merchant_name': 'hepsi10line'}])
                transport = SourceTransport(category_hint)
                reader = RetailerOfferRefresher(teknosa_catalog_source=TeknosaCatalogSource(transport=transport))
                result = sweep(client=client, transport=transport, reader=reader, publish=True,
                    coverage_first=True, discover_teknosa=True,
                    current_listing_inventory=client.get_offer_identity_readiness())
                self.assertEqual(transport.calls, [missing_url, url, url])
                self.assertEqual(result['status'], 'completed')
                self.assertEqual(result['accepted'], 1)
                self.assertEqual(len(client.writes), 1)
                self.assertTrue(result['rows'][-1]['publication']['commercial_readback_verified'])
                self.assertEqual(result['teknosa_store_gap_discovery']['admitted_models'], [client.product['slug']])
                self.assertFalse(result['teknosa_store_gap_discovery']['prices_or_offer_evidence_reused'])
                self.assertEqual(client.writes[0][0]['source_variant_evidence']['ram_gb'], 12)

    def test_expired_prewrite_candidate_is_skipped_and_the_next_target_gets_fresh_full_publication(self):
        from datetime import datetime,timedelta,timezone
        from urllib.parse import urlencode
        from test_hepsiburada_catalog_source import document,TARGET,NAME,SPECS,parse_catalog_buy_box
        from hepsiburada_catalog_source import HepsiburadaCatalogSource
        from retailer_offer_refresh import RetailerOfferRefresher
        class SourceTransport(Transport):
            def configure_catalog_budget(self, count):
                pass
            def read_document(self, url):
                return document(), 'document_read', 200
        class ScopedClient(Client):
            def ingest_listing_offers(self, rows, *, require_all_accepted):
                return {**super().ingest_offers(rows, require_all_accepted=require_all_accepted),
                        'offer_identity_contract': 'seller-source-url-v1'}
        client=ScopedClient()
        client.get_public_price_priority = lambda: {client.product['id']: (client.product['slug'], False)}
        observed,_=parse_catalog_buy_box(document(),TARGET,NAME,SPECS)
        other=TARGET+'?'+urlencode({'magaza':observed['merchant']})
        client.product['offer_targets']=[{'source_url':u,'merchant_name':observed['merchant']} for u in (TARGET,other)]
        def inspect(transport,url,product):
            offer,_=parse_catalog_buy_box(document(),url,NAME,SPECS)
            if url==TARGET:
                offer['observed_at']=(datetime.now(timezone.utc)-timedelta(minutes=6)).isoformat()
            return {'slug':product['slug'],'url':url,'observed_url':url,'retailer':'Hepsiburada',
                'reason':'document_read','http_status':200,'prepared_offer':offer}
        with patch('sweep_priority_catalog.inspect_target',side_effect=inspect):
            source = SourceTransport()
            refresher = RetailerOfferRefresher(hepsiburada_catalog_source=HepsiburadaCatalogSource(transport=source))
            result=sweep(client=client,publish=True,transport=source,reader=refresher)
        self.assertEqual(result['status'],'completed')
        self.assertEqual((len(result['rows']),result['queued']),(2,2))
        self.assertEqual(result['rows'][0]['publication'],{'status':'expired_before_write','writes':False})
        self.assertTrue(result['rows'][1]['publication']['commercial_readback_verified'])
        self.assertEqual((result['accepted'],len(client.writes)),(1,1))
        self.assertEqual(client.writes[0][0]['source_url'],other)

    def test_old_n11_selector_hint_cannot_create_a_none_queue_or_hide_the_catalog_model(self):
        base = 'https://www.n11.com/urun/samsung-galaxy-a37-8-gb-256-gb-123382261'
        catalog = [{'id': 1, 'slug': 'samsung-galaxy-a37-5g', 'name': 'Samsung Galaxy A37 5G',
            'attributes': {'ram_gb': 8, 'storage_gb': 256, 'Connectivity': {'5G': True}},
            'offer_targets': [{'source_url': base + suffix} for suffix in (
                '?renk=lavanta-moru', '?magaza=Old%20Store', '?magaza=n11&renk=lavanta-moru')]}]
        before = deepcopy(catalog)
        diagnostics = []
        queue = catalog_queue(catalog, {1: (catalog[0]['slug'], False)}, diagnostic=diagnostics)
        self.assertEqual([url for url, _ in queue], [base + '?magaza=n11&renk=lavanta-moru'])
        self.assertEqual(len(diagnostics), 2)
        self.assertTrue(all(row['reason'] == 'unroutable_public_pdp_selector' for row in diagnostics))
        self.assertTrue(all('url' not in row for row in diagnostics))
        client = Client()
        client.get_published_products = lambda: deepcopy([{**catalog[0], 'offer_targets': catalog[0]['offer_targets'][:2]}])
        client.get_public_price_priority = lambda: {1: (catalog[0]['slug'], False)}
        result = sweep(client=client, transport=Transport())
        self.assertEqual((result['catalog_count'], result['queued'], result['status']), (1, 0, 'completed'))
        self.assertEqual(result['models_without_priority_hints'], [catalog[0]['slug']])
        self.assertEqual(len(result['unroutable_hint_exclusions']), 2)
        self.assertEqual(result['conflicting_hint_exclusions'], [])
        self.assertFalse(result['writes'])
        self.assertEqual(catalog, before)

    def test_n11_observed_other_shops_are_separately_reread_published_and_commercially_verified(self):
        import json
        from pathlib import Path
        from urllib.parse import parse_qs, urlparse
        from bs4 import BeautifulSoup
        from n11_catalog_source import N11CatalogSource
        from retailer_offer_refresh import RetailerOfferRefresher
        base = 'https://www.n11.com/urun/samsung-galaxy-a37-8-gb-256-gb-akilli-telefon-samsung-turkiye-garantili-123382261'
        fixture = Path(__file__).with_name('fixtures').joinpath('n11-a37-selected-teknosa-public-seller.html').read_text()
        shops = {'teknosa': ('Teknosa', 4951862), 'n11': ('n11', 10001),
                 'mxshop': ('MXshop', 10002), 'ickaleiletisim': ('İÇKALEİLETİSİM', 10003)}
        calls = []
        class SellerTransport(Transport):
            def configure_catalog_budget(self, count):
                pass
            def resolved_url(self, url):
                return url
            def read_document(self, url):
                calls.append(url)
                slug = parse_qs(urlparse(url).query).get('magaza', ['teknosa'])[0]
                name, shop_id = shops[slug]
                soup = BeautifulSoup(fixture, 'html.parser')
                script = next(s for s in soup.select('script') if s.text.startswith('window.model = '))
                state = json.loads(script.text.removeprefix('window.model = ').rstrip(';'))
                state['product']['seller'] = {'id': shop_id, 'nickName': name}
                state['productMeta']['sellerId'] = str(shop_id)
                state['skus'][0]['storeName'] = name
                script.string = 'window.model = ' + json.dumps(state) + ';'
                link = soup.select_one('.sidebarSellerArea-top-name')
                link.string, link['title'], link['href'] = name, name, '/magaza/' + slug
                return str(soup), 'document_read', 200
        class ScopedClient(Client):
            def ingest_listing_offers(self, rows, *, require_all_accepted):
                return {**super().ingest_offers(rows, require_all_accepted=require_all_accepted),
                        'offer_identity_contract': 'seller-source-url-v1'}
        client = ScopedClient()
        client.product.update(slug='samsung-galaxy-a37-5g-8gb-256gb', name='Samsung Galaxy A37 5G 8GB 256GB',
            attributes={'ram_gb': 8, 'storage_gb': 256, 'Connectivity': {'5G': True}},
            offer_targets=[{'merchant_name': 'Teknosa', 'source_url': base}])
        client.get_public_price_priority = lambda: {client.product['id']: (client.product['slug'], False)}
        before = deepcopy(client.product)
        transport = SellerTransport()
        reader = RetailerOfferRefresher(n11_catalog_source=N11CatalogSource(transport=transport))
        result = sweep(client=client, transport=transport, reader=reader, publish=True)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual((result['queued'], len(result['rows']), result['accepted']), (4, 4, 4))
        self.assertEqual(len(client.writes), 4)
        self.assertEqual({rows[0]['seller'] for rows in client.writes}, {name for name, _ in shops.values()})
        self.assertEqual(len(calls), 8)  # Every selector is fetched again before its own publication.
        self.assertTrue(all(r['publication']['commercial_readback_verified'] for r in result['rows']))
        for batch in client.writes:
            offer = batch[0]
            if '?' in offer['source_url']:
                query = parse_qs(urlparse(offer['source_url']).query)
                self.assertEqual(offer['source_variant_evidence']['merchant_slug'], query['magaza'][0])
                self.assertEqual(offer['source_variant_evidence']['color_slug'], query['renk'][0])
        self.assertEqual(client.product, before)

    def test_existing_hb_price_and_second_seller_gaps_are_interleaved_and_bounded_without_mutation(self):
        catalog = [{'id': i, 'slug': str(i)} for i in range(1, 17)]
        targets = [(f'https://www.hepsiburada.com/phone-p-HBCV0000000{i:02d}', p)
                   for i, p in enumerate(catalog)]
        prices = {p['id']: (p['slug'], p['id'] > 8) for p in catalog}
        before = deepcopy((targets, prices))
        ordered = recovery_hepsiburada_seeds(targets + targets, prices)
        self.assertEqual([p['id'] for _, p in ordered], [1, 9, 2, 10, 3, 11, 4, 12, 5, 13, 6, 14])
        self.assertEqual(len({p['slug'] for _, p in ordered}), 12)
        self.assertEqual((targets, prices), before)

    def test_existing_hb_quote_is_verified_before_a_category_denial_and_never_retried_after_it(self):
        from test_hepsiburada_catalog_source import document, TARGET, NAME, SPECS
        from hepsiburada_catalog_source import HepsiburadaCatalogSource
        from retailer_offer_refresh import RetailerOfferRefresher
        client = Client()
        client.get_public_price_priority = lambda: {client.product['id']: (client.product['slug'], False)}
        seen = []
        class DenyingCategoryTransport(Transport):
            blocked = False
            def configure_catalog_budget(self, count):
                pass
            def read_document(self, url):
                seen.append(('pdp', url))
                return (None, 'source_circuit_open', None) if self.blocked else (document(), 'document_read', 200)
            def read_hepsiburada_category(self, url):
                seen.append(('category', url))
                self.blocked = True
                return None, 'http_403', 403
            def report(self):
                return {'Hepsiburada': {'blocked': self.blocked}}
        before = deepcopy(client.product)
        transport = DenyingCategoryTransport()
        source_reader = RetailerOfferRefresher(hepsiburada_catalog_source=HepsiburadaCatalogSource(transport=transport))
        result = sweep(client=client, transport=transport, reader=source_reader,
                       discover_hepsiburada=True, coverage_first=True, publish=True)
        self.assertEqual(seen[0], ('pdp', TARGET))
        self.assertEqual([kind for kind, _ in seen], ['pdp', 'pdp', 'category'])
        self.assertEqual(len(client.writes), 1)
        self.assertTrue(result['rows'][0]['publication']['commercial_readback_verified'])
        self.assertEqual(result['accepted'], 1)
        self.assertTrue(result['sources']['Hepsiburada']['blocked'])
        self.assertEqual(client.product, before)
        self.assertFalse(result['hepsiburada_pdp_priority']['stored_prices_reused'])

    def test_n11_other_memory_pdp_allowance_is_shared_and_bounded_across_the_catalog(self):
        catalog = [{'id': n, 'slug': f'samsung-galaxy-a{n}', 'name': f'Samsung Galaxy A{n}',
                    'offer_targets': [], 'attributes': {'ram_gb': 6, 'storage_gb': 128,
                                                       'Connectivity': {'5G': True}}} for n in range(30, 43)]
        client = Client()
        client.get_published_products = lambda: deepcopy(catalog)
        client.get_public_price_priority = lambda: {}
        class SearchTransport(Transport):
            def read_n11_search(self, query):
                number = int(query.rsplit('A', 1)[1])
                return (''.join(f'<a href="https://www.n11.com/urun/samsung-galaxy-a{number}-{ram}-gb-{storage}-gb-{number}1234{ram}">'
                               f'<h2 class="product-item-title">{query} {ram} GB RAM {storage} GB</h2></a>'
                               for ram, storage in [(8, 256), (12, 512)]), 'document_read', 200)
        def inspect(transport, url, product):
            return {'slug': product['slug'], 'retailer': 'n11', 'url': url, 'observed_url': url,
                    'http_status': 200, 'reason': 'document_read', 'prepared_offer': None}
        with patch('sweep_priority_catalog.inspect_target', side_effect=inspect) as inspector, \
                patch('sweep_priority_catalog.refresh_candidates') as writer:
            result = sweep(client=client, transport=SearchTransport(), discover_n11=True, memory_discovery=True)
        self.assertEqual(inspector.call_count, 24)
        self.assertEqual(len(result['rows']), 24)
        self.assertEqual(len(result['discovery']), 13)
        self.assertTrue(all(len(d.get('memory_variant_hints', [])) <= 2 for d in result['discovery']))
        self.assertFalse(result['writes'])
        writer.assert_not_called()

    def test_n11_memory_search_is_capacity_independent_but_the_original_sku_stays_unpriced(self):
        from test_n11_catalog_source import document, URL, NAME
        from automatic_memory_variants import observed_variant_candidates
        client = Client()
        client.product.update(name=NAME, slug='samsung-galaxy-a57', offer_targets=[],
                              attributes={'ram_gb': 6, 'storage_gb': 128, 'Connectivity': {'5G': True}})
        client.get_public_price_priority = lambda: {}
        queries, documents = [], []
        class SearchTransport(Transport):
            def read_n11_search(self, query):
                queries.append(query)
                return (f'<a href="{URL}"><h2 class="product-item-title">'
                        f'{NAME} 8 GB RAM 256 GB</h2><b>1 TL</b></a>', 'document_read', 200)
            def read_document(self, url):
                documents.append(url)
                return document(), 'document_read', 200
            def resolved_url(self, url):
                return url
        with patch('sweep_priority_catalog.refresh_candidates') as writer:
            result = sweep(client=client, transport=SearchTransport(), discover_n11=True,
                           memory_discovery=True, publish=True)
        self.assertEqual(queries, [NAME])
        self.assertEqual(documents, [URL])
        self.assertIsNone(result['rows'][0]['prepared_offer'])
        self.assertEqual(result['rows'][0]['other_memory_variant']['observed_specs'],
                         {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'})
        self.assertEqual(observed_variant_candidates(result, [client.product])[0]['targets'], [URL])
        self.assertEqual((result['accepted'], result['writes']), (0, False))
        self.assertFalse(coverage_readback(result, {client.product['id']: (client.product['slug'], False)})['passed'])
        writer.assert_not_called()

    def test_full_selected_offer_is_only_readonly_parser_proof_for_the_current_published_sku(self):
        from test_hepsiburada_catalog_source import document, NAME, SPECS, TARGET
        client = Client()
        client.get_published_products = lambda: [{'id': 1, 'slug': 'a36-test', 'name': NAME, 'attributes': SPECS}]
        visited = []
        class TargetTransport(Transport):
            def read_document(self, url):
                visited.append(url)
                return document(), 'document_read', 200
        with patch('sweep_priority_catalog.refresh_candidates') as writer, \
                patch.object(client, 'get_public_price_priority', create=True) as coverage:
            result = target_preflight(client=client, url=TARGET, slug='a36-test', transport=TargetTransport())
        self.assertEqual(result['status'], 'completed')
        self.assertIsNotNone(result['rows'][0]['prepared_offer'])
        self.assertEqual((result['publish'], result['writes'], result['accepted']), (False, False, 0))
        self.assertEqual(visited, [TARGET])
        self.assertNotIn('public_coverage', result)
        self.assertEqual(result['public_parser_proof']['url'], TARGET)
        self.assertEqual(result['public_parser_proof']['storage_gb'], 128)
        self.assertEqual(result['public_parser_proof']['availability'], 'in_stock')
        extra = dict(result['rows'][0])
        extra['prepared_offer'] = dict(extra['prepared_offer'], private_state='must-not-be-logged')
        with patch('sweep_priority_catalog.inspect_target', return_value=extra):
            projected = target_preflight(client=client, url=TARGET, slug='a36-test', transport=TargetTransport())
        self.assertNotIn('must-not-be-logged', str(projected['public_parser_proof']))
        self.assertNotIn('private_state', projected['public_parser_proof'])
        writer.assert_not_called(); coverage.assert_not_called()
        with self.assertRaises(ValueError):
            target_preflight(client=client, url=TARGET, slug='not-published', transport=TargetTransport())
        for url in [TARGET.replace('www.hepsiburada.com', 'evil.test'),
                    TARGET.replace('a36-5g', 'a35-5g'), TARGET + '?token=private']:
            with self.assertRaises(ValueError):
                target_preflight(client=client, url=url, slug='a36-test', transport=TargetTransport())
        self.assertEqual(visited, [TARGET])

    def test_selected_capacity_conflict_is_rejected_and_cli_cannot_publish_or_start_another_mode(self):
        from test_hepsiburada_catalog_source import document, NAME, SPECS, TARGET
        from sweep_priority_catalog import main
        client = Client()
        client.get_published_products = lambda: [{'id': 1, 'slug': 'a36-test', 'name': NAME, 'attributes': SPECS}]
        class TargetTransport(Transport):
            def read_document(self, url):
                return document().replace('8 GB RAM', '12 GB RAM'), 'document_read', 200
        result = target_preflight(client=client, url=TARGET, slug='a36-test', transport=TargetTransport())
        self.assertEqual(result['status'], 'rejected')
        self.assertIsNone(result['rows'][0]['prepared_offer'])
        self.assertIsNone(result['public_parser_proof'])
        self.assertFalse(result['writes'])
        for flag in ['--publish', '--coverage-first', '--n11-preflight', '--hepsiburada-category-audit']:
            with patch('sys.argv', ['sweep', '--target-preflight', TARGET, '--target-slug', 'a36-test', flag]), \
                    patch('sweep_priority_catalog.OfferIngestionClient.from_env') as factory:
                with self.assertRaises(ValueError):
                    main()
                factory.assert_not_called()


class CategoryDiagnosticTests(unittest.TestCase):
    def test_newly_discovered_hb_gap_is_freshly_published_before_the_next_category_denial(self):
        from test_hepsiburada_catalog_source import document, TARGET, NAME
        from hepsiburada_catalog_discovery import CATEGORY_URL
        from hepsiburada_catalog_source import HepsiburadaCatalogSource
        from retailer_offer_refresh import RetailerOfferRefresher
        client = Client()
        client.product['offer_targets'] = []
        client.get_public_price_priority = lambda: {client.product['id']: (client.product['slug'], False)}
        original = deepcopy(client.product)
        calls = []
        next_page = CATEGORY_URL + '?sayfa=2'
        class Source(Transport):
            blocked = False
            def configure_catalog_budget(self, count):
                pass
            def read_hepsiburada_category(self, url):
                calls.append(('category', url))
                if url == next_page:
                    self.blocked = True
                    return None, 'http_403', 403
                return ('<h1>Cep Telefonu Fiyatları ve Modelleri</h1>'
                        f'<h2 data-test-id="title-1"><a href="{TARGET}" title="{NAME} 8 GB RAM 128 GB">'
                        f'{NAME} 8 GB RAM 128 GB</a></h2><link rel="next" href="{next_page}">'), 'document_read', 200
            def read_document(self, url):
                if self.blocked:
                    raise AssertionError('No PDP may be requested after the denial')
                calls.append(('pdp', url))
                return document(), 'document_read', 200
            def report(self):
                return {'Hepsiburada': {'blocked': self.blocked}}
        transport = Source()
        reader = RetailerOfferRefresher(hepsiburada_catalog_source=HepsiburadaCatalogSource(transport=transport))
        result = sweep(client=client, transport=transport, reader=reader,
                       discover_hepsiburada=True, coverage_first=True, publish=True)
        self.assertEqual(calls, [('category', CATEGORY_URL), ('pdp', TARGET),
                                 ('pdp', TARGET), ('category', next_page)])
        self.assertEqual(result['accepted'], 1)
        self.assertEqual(len(client.writes), 1)
        self.assertTrue(result['rows'][0]['publication']['commercial_readback_verified'])
        self.assertTrue(result['hepsiburada_category_discovery']['early_gap_reads'])
        self.assertEqual(client.product, original)

    def test_streamed_capacity_reads_share_final_pass_model_and_retailer_caps(self):
        from collections import Counter
        from hepsiburada_catalog_discovery import CATEGORY_URL
        catalog = [{'id': n, 'slug': f'samsung-galaxy-a{n:02}', 'name': f'Samsung Galaxy A{n:02} 5G',
                    'offer_targets': [], 'attributes': {'ram_gb': 6, 'storage_gb': 128,
                                                       'Connectivity': {'5G': True}}}
                   for n in range(1, 14)]
        client = Client()
        client.get_published_products = lambda: deepcopy(catalog)
        client.get_public_price_priority = lambda: {p['id']: (p['slug'], False) for p in catalog}
        calls = []
        class Source(Transport):
            def read_hepsiburada_category(self, url):
                calls.append(('category', url))
                return 'public-category', 'document_read', 200
        def parse(html, url, diagnostic=None):
            diagnostic.update(status='parsed')
            page = 2 if '?' in url else 1
            cards = [{'source_url': f'https://www.hepsiburada.com/{p["slug"]}-8gb-{storage}gb-p-HBCV0000{p["id"]:04}{storage}',
                      'discovery_title': f'{p["name"]} 8 GB RAM {storage} GB', 'stock_priority': False}
                     for p in catalog for storage in (128, 256)]
            return cards, CATEGORY_URL + '?sayfa=2' if page == 1 else None
        def inspect(transport, url, product):
            calls.append(('pdp', product['slug']))
            return {'slug': product['slug'], 'retailer': 'Hepsiburada', 'url': url,
                    'observed_url': url, 'reason': 'document_read', 'http_status': 200,
                    'prepared_offer': None}
        with patch('hepsiburada_catalog_discovery.parse_category_document', side_effect=parse), \
                patch('hepsiburada_catalog_discovery.discovery_links', return_value=[]), \
                patch('sweep_priority_catalog.inspect_target', side_effect=inspect), \
                patch('sweep_priority_catalog.refresh_candidates') as writer:
            result = sweep(client=client, transport=Source(), discover_hepsiburada=True, memory_discovery=True)
        hints = result['hepsiburada_category_discovery']['memory_variant_hints']
        self.assertEqual(len(hints), 24)
        self.assertEqual(len(result['rows']), 24)
        self.assertEqual(max(Counter(r['slug'] for r in result['rows']).values()), 2)
        next_at = calls.index(('category', CATEGORY_URL + '?sayfa=2'))
        self.assertEqual([slug for kind, slug in calls[1:next_at]], [p['slug'] for p in catalog])
        self.assertFalse(result['writes'])
        writer.assert_not_called()

    def test_missing_model_gets_category_capacity_budget_before_extra_options_of_a_priced_family(self):
        base = {'id': 33, 'slug': 'samsung-galaxy-a34-5g', 'name': 'Samsung Galaxy A34 5G',
                'attributes': {'ram_gb': 6, 'storage_gb': 128, 'Connectivity': {'5G': True}}, 'offer_targets': []}
        sibling = {'id': 301, 'slug': 'samsung-galaxy-a34-5g-8gb-128gb',
                   'name': 'Samsung Galaxy A34 5G 8GB 128GB',
                   'attributes': {'ram_gb': 8, 'storage_gb': 128, 'Connectivity': {'5G': True}}, 'offer_targets': []}
        missing = {'id': 34, 'slug': 'samsung-galaxy-a35-5g', 'name': 'Samsung Galaxy A35 5G',
                   'attributes': {'ram_gb': 6, 'storage_gb': 128, 'Connectivity': {'5G': True}}, 'offer_targets': []}
        catalog = [base, sibling, missing]
        client = Client()
        client.get_published_products = lambda: deepcopy(catalog)
        client.get_public_price_priority = lambda: {p['id']: (p['slug'], p['id'] == sibling['id']) for p in catalog}
        cards = [{'source_url': 'https://www.hepsiburada.com/samsung-galaxy-a34-5g-8gb-256gb-p-HBCV000040I6N3',
                  'discovery_title': 'Samsung Galaxy A34 5G 8 GB RAM 256 GB', 'stock_priority': True},
                 {'source_url': 'https://www.hepsiburada.com/samsung-galaxy-a35-5g-8gb-128gb-p-HBCV000040I6N4',
                  'discovery_title': 'Samsung Galaxy A35 5G 8 GB RAM 128 GB', 'stock_priority': True}]
        class CategoryTransport(Transport):
            def read_hepsiburada_category(self, url):
                return 'category-fixture', 'document_read', 200
        def parse(html, url, diagnostic=None):
            diagnostic.update(status='parsed')
            return cards, None
        def inspect(transport, url, product):
            return {'slug': product['slug'], 'retailer': 'Hepsiburada', 'url': url, 'observed_url': url,
                    'reason': 'document_read', 'http_status': 200, 'prepared_offer': None}
        with patch('hepsiburada_catalog_discovery.parse_category_document', side_effect=parse), \
                patch('hepsiburada_catalog_discovery.discovery_links', return_value=[]), \
                patch('sweep_priority_catalog.inspect_target', side_effect=inspect) as inspector, \
                patch('sweep_priority_catalog.refresh_candidates') as writer:
            result = sweep(client=client, transport=CategoryTransport(), discover_hepsiburada=True, memory_discovery=True)
        self.assertEqual([c.args[2]['slug'] for c in inspector.call_args_list], [missing['slug'], base['slug']])
        self.assertEqual(result['catalog_count'], 3)
        self.assertEqual(result['memory_variant_priority']['models_without_current_price'], 1)
        self.assertFalse(result['writes'])
        writer.assert_not_called()

    def test_one_retailers_capacity_hints_do_not_spend_the_other_retailers_allowance(self):
        client = Client()
        client.product.update(offer_targets=[], attributes={'ram_gb': 6, 'storage_gb': 128,
                                                           'Connectivity': {'5G': True}})
        client.get_public_price_priority = lambda: {}
        urls = ['https://www.hepsiburada.com/samsung-galaxy-a36-5g-8gb-128gb-p-HBCV0000001234',
                'https://www.hepsiburada.com/samsung-galaxy-a36-5g-8gb-256gb-p-HBCV0000001235',
                'https://www.teknosa.com/samsung-galaxy-a36-5g-8gb-128gb-p-100000123456',
                'https://www.teknosa.com/samsung-galaxy-a36-5g-8gb-256gb-p-100000123457']
        def parse(html, url, diagnostic=None):
            diagnostic.update(status='parsed')
            chosen = urls[:2] if 'hepsiburada' in url else urls[2:]
            return [{'source_url': u, 'discovery_title': f'{client.product["name"]} 8 GB RAM {storage} GB',
                     'stock_priority': True} for u, storage in zip(chosen, [128, 256])], None
        class CategoryTransport(Transport):
            def read_hepsiburada_category(self, url):
                return '', 'document_read', 200
            def read_teknosa_category(self, url):
                return '', 'document_read', 200
        def inspect(transport, url, product):
            return {'slug': product['slug'], 'retailer': 'Teknosa' if 'teknosa' in url else 'Hepsiburada',
                    'url': url, 'observed_url': url, 'http_status': 200,
                    'reason': 'document_read', 'prepared_offer': None}
        with patch('hepsiburada_catalog_discovery.parse_category_document', side_effect=parse), \
                patch('teknosa_catalog_discovery.parse_category_document', side_effect=parse), \
                patch('hepsiburada_catalog_discovery.discovery_links', return_value=[]), \
                patch('sweep_priority_catalog.inspect_target', side_effect=inspect) as inspector:
            result = sweep(client=client, transport=CategoryTransport(), discover_hepsiburada=True,
                           discover_teknosa=True, memory_discovery=True)
        self.assertEqual([c.args[1] for c in inspector.call_args_list], urls)
        self.assertEqual(len(result['category_discovery']['memory_variant_hints']), 2)
        self.assertEqual(len(result['hepsiburada_category_discovery']['memory_variant_hints']), 2)
        self.assertFalse(result['writes'])

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
    def test_phone_category_family_query_reads_observed_capacities_without_copying_radio_or_prices(self):
        from pathlib import Path
        client = Client()
        client.product.update(name='Samsung Galaxy A37 5G', slug='samsung-galaxy-a37-5g', offer_targets=[],
            attributes={'ram_gb': 6, 'storage_gb': 128, 'Connectivity': {'5G': True}})
        original = deepcopy(client.product)
        client.get_public_price_priority = lambda: {client.product['id']: (client.product['slug'], False)}
        queries, documents = [], []
        class SearchTransport(Transport):
            def read_n11_phone_search(self, query):
                queries.append(query)
                return Path('fixtures/n11-phone-category-family-cards.html').read_text(), 'document_read', 200
            def read_n11_search(self, query):
                raise AssertionError('A phone category query must not fall back to accessory search')
        def inspect(transport, url, product):
            documents.append(url)
            return {'slug': product['slug'], 'retailer': 'n11', 'url': url, 'observed_url': url,
                    'reason': 'document_read', 'http_status': 200, 'prepared_offer': None}
        with patch('sweep_priority_catalog.inspect_target', side_effect=inspect), \
                patch('sweep_priority_catalog.refresh_candidates') as writer:
            result = sweep(client=client, transport=SearchTransport(), discover_n11=True, memory_discovery=True)
        self.assertEqual(queries, ['Samsung Galaxy A37'])
        self.assertEqual(len(documents), 3)
        self.assertTrue(any(url.endswith('-123891572') for url in documents))
        self.assertTrue(any(url.endswith('-123382261') for url in documents))
        self.assertTrue(any(url.endswith('-127133909') for url in documents))
        self.assertEqual(len(result['discovery'][0]['memory_variant_hints']), 2)
        self.assertEqual(result['discovery'][0]['discovery_scope'], 'public_phone_category_family_query')
        self.assertEqual(client.product, original)
        self.assertFalse(result['writes'])
        writer.assert_not_called()

    def test_storage_only_search_hint_must_read_actual_selected_ram_and_never_fill_it_from_parent(self):
        from test_n11_catalog_source import document, URL, NAME
        for missing_ram in (False, True):
            client = Client()
            client.product.update(name=NAME, slug='samsung-galaxy-a57', offer_targets=[],
                attributes={'ram_gb': 6, 'storage_gb': 128, 'Connectivity': {'5G': True}})
            client.get_public_price_priority = lambda: {}
            documents = []
            def remove_ram(state, soup):
                state['product']['productAttributes'] = [a for a in state['product']['productAttributes']
                                                        if a['key'] != 'Bellek Kapasitesi']
            class SearchTransport(Transport):
                def read_n11_search(self, query):
                    return f'<a href="{URL}"><h2 class="product-item-title">{NAME} 256 GB</h2></a>', 'document_read', 200
                def read_document(self, url):
                    documents.append(url)
                    return document(remove_ram if missing_ram else None), 'document_read', 200
                def resolved_url(self, url): return url
            with patch('sweep_priority_catalog.refresh_candidates') as writer:
                result = sweep(client=client, transport=SearchTransport(), discover_n11=True,
                               memory_discovery=True, publish=True)
            self.assertEqual(documents, [URL])
            self.assertIsNone(result['rows'][0]['prepared_offer'])
            self.assertFalse(result['writes'])
            self.assertEqual(client.product['attributes']['ram_gb'], 6)
            if missing_ram:
                self.assertIsNone(result['rows'][0].get('other_memory_variant'))
            else:
                self.assertEqual(result['rows'][0]['other_memory_variant']['observed_specs'],
                                 {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'})
            writer.assert_not_called()

    def test_a_fresh_n11_other_capacity_routes_to_its_existing_sku_before_duplicate_query_is_skipped(self):
        from test_n11_catalog_source import document, URL, NAME
        client = Client()
        original = {'id': 999, 'name': NAME, 'slug': 'samsung-galaxy-a57', 'offer_targets': [],
                    'attributes': {'ram_gb': 6, 'storage_gb': 128, 'Connectivity': {'5G': True}}}
        sibling = {'id': 1000, 'name': NAME + ' 8GB 256GB', 'slug': 'samsung-galaxy-a57-8gb-256gb',
                   'offer_targets': [], 'attributes': {'ram_gb': 8, 'storage_gb': 256, 'Connectivity': {'5G': True}}}
        catalog = [original, sibling]
        before = deepcopy(catalog)
        client.get_published_products = lambda: deepcopy(catalog)
        client.get_public_price_priority = lambda: {}
        queries, documents = [], []
        class SearchTransport(Transport):
            def read_n11_search(self, query):
                if query in queries:
                    return None, 'already_read', None
                queries.append(query)
                return f'<a href="{URL}"><h2 class="product-item-title">{NAME} 8 GB RAM 256 GB</h2></a>', 'document_read', 200
            def read_document(self, url):
                documents.append(url)
                return document(), 'document_read', 200
            def resolved_url(self, url): return url
        def publish(audit, **kwargs):
            row = audit['rows'][0]
            self.assertEqual(row['slug'], sibling['slug'])
            self.assertEqual((row['prepared_offer']['ram_gb'], row['prepared_offer']['storage_gb']), (8, 256))
            self.assertTrue(row['document_reused'])
            return {'status': 'completed', 'writes': True, 'ingestion': {'accepted': 1},
                    'commercial_readback_verified': True}
        with patch('sweep_priority_catalog.refresh_candidates', side_effect=publish) as writer:
            result = sweep(client=client, transport=SearchTransport(), reader=sweep_reader(),
                           discover_n11=True, memory_discovery=True, publish=True)
        self.assertEqual(queries, [NAME])
        self.assertEqual(documents, [URL])
        self.assertEqual(writer.call_count, 1)
        self.assertIsNone(result['rows'][0]['prepared_offer'])
        self.assertEqual(result['rows'][0]['routed_to_existing_variant']['slug'], sibling['slug'])
        self.assertEqual(result['rows'][1]['slug'], sibling['slug'])
        self.assertEqual(result['rows'][1]['publication']['commercial_readback_verified'], True)
        self.assertEqual(result['accepted'], 1)
        self.assertEqual(result['catalog_count'], 2)
        self.assertEqual(catalog, before)

    def test_n11_memory_quota_reaches_missing_families_before_an_unpriced_old_capacity_with_priced_sibling(self):
        client = Client()
        old = {'id': 1, 'name': 'Samsung Galaxy A35 5G', 'slug': 'a35-old', 'offer_targets': [],
               'attributes': {'ram_gb': 6, 'storage_gb': 128, 'Connectivity': {'5G': True}}}
        priced = {**old, 'id': 3, 'name': 'Samsung Galaxy A35 5G 8GB 128GB', 'slug': 'a35-8-128',
                  'attributes': {'ram_gb': 8, 'storage_gb': 128, 'Connectivity': {'5G': True}}}
        missing = {**old, 'id': 2, 'name': 'Samsung Galaxy A57 5G', 'slug': 'a57-missing'}
        client.get_published_products = lambda: [old, missing, priced]
        client.get_public_price_priority = lambda: {p['id']: (p['slug'], p['id'] == 3) for p in [old, missing, priced]}
        queries = []
        class SearchTransport(Transport):
            def read_n11_search(self, query):
                queries.append(query)
                return None, 'no_results', 200
        result = sweep(client=client, transport=SearchTransport(), discover_n11=True, memory_discovery=True)
        self.assertEqual(queries[0], missing['name'])
        self.assertFalse(result['writes'])

    def test_definitely_wrong_brand_hint_does_not_block_the_urls_actual_owner_or_other_capacities(self):
        apple = 'https://www.n11.com/urun/apple-iphone-17-pro-max-2-tb-apple-turkiye-garantili-100819983'
        xiaomi = 'https://www.n11.com/urun/xiaomi-17-pro-max-16-gb-1-tb-100819984'
        brandless = 'https://www.n11.com/urun/17-pro-max-12-gb-512-gb-100819985'
        redmi = 'https://www.n11.com/urun/xiaomi-redmi-note-13-8-gb-256-gb-100819986'
        catalog = [
            {'id': 1, 'slug': 'xiaomi-17-pro-max', 'name': 'Xiaomi 17 Pro Max',
             'attributes': {'ram_gb': 12, 'storage_gb': 512},
             'offer_targets': [{'source_url': url} for url in [apple, xiaomi, brandless]]},
            {'id': 2, 'slug': 'apple-iphone-17-pro-max', 'name': 'Apple iPhone 17 Pro Max',
             'attributes': {'ram_gb': 12, 'storage_gb': 2048}, 'offer_targets': [{'source_url': apple}]},
            {'id': 3, 'slug': 'redmi-note-13', 'name': 'Redmi Note 13',
             'attributes': {'ram_gb': 6, 'storage_gb': 128}, 'offer_targets': [{'source_url': redmi}]},
        ]
        before, excluded = deepcopy(catalog), []
        rows = catalog_queue(catalog, {}, diagnostic=excluded)
        self.assertEqual({(url, product['id']) for url, product in rows},
                         {(apple, 2), (xiaomi, 1), (brandless, 1), (redmi, 3)})
        self.assertEqual(excluded, [{'slug': 'xiaomi-17-pro-max', 'url': apple, 'reason': 'url_brand_conflict'}])
        self.assertEqual(catalog, before)
        self.assertFalse(coverage_readback({'catalog_count': 3, 'rows': []},
                                          {1: (catalog[0]['slug'], False), 2: (catalog[1]['slug'], True),
                                           3: (catalog[2]['slug'], False)})['passed'])

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
            self.assertEqual(len(transport.queries), 5)
            if retained:
                self.assertTrue(transport.queries[-1].startswith('Samsung Galaxy S25'))
                self.assertEqual(result['n11_store_gap_discovery']['admitted_models'], retained)
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


class N11PlatformCoverageTests(unittest.TestCase):
    @staticmethod
    def inventory(catalog, covered):
        from datetime import datetime, timezone
        return {'evidenceVersion': 'product-offer-match-v1', 'mode': 'shadow_audit_only',
            'auditedAt': datetime.now(timezone.utc).isoformat(), 'productCoverage': [
                {'slug': p['slug'], 'matchedMerchants': 2 if p['slug'] in covered else 0,
                 'matchedOffers': [{'merchant_name': seller, 'price': 30000, 'source_url': url}
                    for seller, url in [('MediaMarkt', 'https://www.mediamarkt.com.tr/tr/product/_phone-123456.html'),
                        ('Trendyol', 'https://www.trendyol.com/samsung/phone-p-123456')]] if p['slug'] in covered else []}
                for p in catalog]}

    def test_one_family_query_can_recover_an_actual_n11_offer_for_a_retained_sibling(self):
        from pathlib import Path
        from urllib.parse import urlparse
        from n11_catalog_source import N11CatalogSource
        from retailer_offer_refresh import RetailerOfferRefresher
        parent = {'id': 1, 'slug': 'samsung-galaxy-a37-5g', 'name': 'Samsung Galaxy A37 5G', 'offer_targets': [],
            'attributes': {'ram_gb': 6, 'storage_gb': 128, 'Connectivity': {'5G': True}}}
        child = {'id': 2, 'slug': 'samsung-galaxy-a37-5g-8gb-256gb', 'name': 'Samsung Galaxy A37 5G 8GB 256GB',
            'offer_targets': [], 'attributes': {'ram_gb': 8, 'storage_gb': 256, 'Connectivity': {'5G': True}}}
        catalog = [parent, child]
        before = deepcopy(catalog)
        inventory = self.inventory(catalog, {child['slug']})
        class ScopedClient(Client):
            def get_published_products(self):
                return deepcopy(catalog)
            def get_public_price_priority(self):
                return {p['id']: (p['slug'], p['id'] == 2) for p in catalog}
            def get_offer_identity_readiness(self):
                current = deepcopy(inventory)
                for batch in self.writes:
                    for r in batch:
                        row = next(row for row in current['productCoverage'] if row['slug'] == r['product_slug'])
                        row['matchedOffers'].append({'merchant_name': r['seller'], 'price': r['price'], 'source_url': r['source_url']})
                return current
            def ingest_listing_offers(self, rows, *, require_all_accepted):
                return {**super().ingest_offers(rows, require_all_accepted=require_all_accepted),
                        'offer_identity_contract': 'seller-source-url-v1'}
        client = ScopedClient()
        fixtures = Path(__file__).with_name('fixtures')
        cards = fixtures.joinpath('n11-phone-category-family-cards.html').read_text().replace('</h2>', '</h2><b>1 TL</b>')
        selected = fixtures.joinpath('n11-a37-selected-teknosa-public-seller.html').read_text()
        base = 'https://www.n11.com/urun/samsung-galaxy-a37-8-gb-256-gb-akilli-telefon-samsung-turkiye-garantili-123382261'
        queries, reads = [], []
        class FamilyTransport(Transport):
            def configure_catalog_budget(self, count):
                pass
            def read_n11_phone_search(self, query):
                queries.append(query)
                return cards, 'document_read', 200
            def read_document(self, url):
                reads.append(url)
                return (selected, 'document_read', 200) if url == base else (None, 'fixture_unavailable', 404)
            def resolved_url(self, url):
                return url
        transport = FamilyTransport()
        reader = RetailerOfferRefresher(n11_catalog_source=N11CatalogSource(transport=transport))
        result = sweep(client=client, transport=transport, reader=reader, publish=True,
            current_listing_inventory=inventory, coverage_first=True, memory_discovery=True, discover_n11=True)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(queries, ['Samsung Galaxy A37'])
        self.assertGreaterEqual(reads.count(base), 2)  # Preview plus independent publication GET.
        self.assertEqual((result['catalog_count'], result['accepted']), (2, 1))
        self.assertEqual(client.writes[0][0]['product_slug'], child['slug'])
        self.assertEqual(client.writes[0][0]['price'], 26999)
        self.assertEqual(client.writes[0][0]['source_url'], base)
        self.assertTrue(any(r.get('publication', {}).get('commercial_readback_verified') for r in result['rows']))
        self.assertEqual(result['n11_store_gap_discovery']['admitted_models'], [child['slug']])
        self.assertEqual(result['n11_store_gap_discovery']['same_run_query_reuses'], 1)
        self.assertFalse(result['n11_store_gap_discovery']['prices_or_offer_evidence_reused'])
        self.assertEqual(catalog, before)

    def test_whole_platform_gap_scan_preserves_primary_recovery_and_requires_a_current_audit(self):
        catalog = [{'id': n, 'slug': f'samsung-galaxy-a{n}', 'name': f'Samsung Galaxy A{n}', 'offer_targets': [],
            'attributes': {'ram_gb': 8, 'storage_gb': 256, 'Connectivity': {'5G': True}}} for n in range(40, 65)]
        missing = {'id': 200, 'slug': 'honor-200', 'name': 'Honor 200', 'offer_targets': [],
            'attributes': {'ram_gb': 12, 'storage_gb': 512, 'Connectivity': {'5G': True}}}
        catalog.append(missing)
        covered = {p['slug'] for p in catalog if p['id'] != 200}
        client = Client()
        client.get_published_products = lambda: deepcopy(catalog)
        client.get_public_price_priority = lambda: {p['id']: (p['slug'], p['slug'] in covered) for p in catalog}
        class SearchTransport(Transport):
            def __init__(self): self.queries = []
            def read_n11_phone_search(self, query):
                self.queries.append(query)
                return '<h1>Cep Telefonu</h1>', 'document_read', 200
        for mode in ('current', 'missing_audit', 'n11_already_present'):
            inventory = self.inventory(catalog, covered) if mode != 'missing_audit' else None
            if mode == 'n11_already_present':
                inventory['productCoverage'][0]['matchedOffers'][0]['source_url'] = 'https://www.n11.com/urun/samsung-galaxy-a40-256-gb-1234567'
            transport = SearchTransport()
            result = sweep(client=client, transport=transport, discover_n11=True, coverage_first=True,
                           current_listing_inventory=inventory)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(len(transport.queries), 25 if mode == 'n11_already_present' else 26)
            self.assertTrue(transport.queries[0].startswith('Honor 200'))
            self.assertEqual(len(result['n11_store_gap_discovery']['admitted_models']),
                             0 if mode == 'missing_audit' else 24 if mode == 'n11_already_present' else 25)
            if mode == 'n11_already_present':
                self.assertNotIn('samsung-galaxy-a40', result['n11_store_gap_discovery']['eligible_models'])
                self.assertFalse(any(q.startswith('Samsung Galaxy A40 ') for q in transport.queries))
            self.assertEqual(result['catalog_count'], 26)
            self.assertFalse(result['writes'])

        from priority_retailer_transport import PriorityRetailerTransport
        from test_pasaj_source import Response
        requests = []
        def get(url, **kwargs):
            requests.append(url)
            return Response(url, 'User-agent: *\nAllow: /\n' if url.endswith('robots.txt') else '<h1>Cep Telefonu</h1>')
        bounded = PriorityRetailerTransport(get=get, sleep=lambda _: None, maximum_per_host=2)
        result = sweep(client=client, transport=bounded, discover_n11=True, coverage_first=True,
                       current_listing_inventory=self.inventory(catalog, covered))
        self.assertEqual(len(requests), 3)  # Robots plus two actual search requests.
        self.assertEqual(bounded.report()['n11']['document_requests'], 2)
        self.assertEqual(sum(d['reason'] == 'pdp_budget_exhausted' for d in result['discovery']), 24)
        self.assertEqual(result['catalog_count'], 26)
        self.assertFalse(result['writes'])


class PriorityStoreCoverageReadbackTests(unittest.TestCase):
    def test_each_current_platform_counts_a_sku_once_and_lists_remain_distinct_from_sellers(self):
        from datetime import datetime, timezone
        catalog = [{'slug': slug} for slug in ('alpha', 'beta', 'unpriced')]
        n11 = 'https://www.n11.com/urun/phone-123456'
        hb = 'https://www.hepsiburada.com/phone-p-HBCV0000TEST01'
        ty = 'https://www.trendyol.com/brand/phone-p-123456'
        def row(url): return {'merchant_name': 'Same seller', 'price': 10000, 'source_url': url}
        identity = {'evidenceVersion': 'product-offer-match-v1', 'mode': 'shadow_audit_only',
            'auditedAt': datetime.now(timezone.utc).isoformat(), 'productCoverage': [
                {'slug': 'alpha', 'matchedOffers': [row(n11 + '?magaza=shop&renk=gri'),
                    row(n11 + '?magaza=shop&renk=siyah'), row(n11 + '?magaza=other'), row(hb)]},
                {'slug': 'beta', 'matchedOffers': [row(n11.replace('123456', '123457')), row(ty)]},
                {'slug': 'unpriced', 'matchedOffers': []}]}
        before = deepcopy((identity, catalog))
        report = priority_store_coverage_readback(identity, catalog)
        self.assertEqual(report['catalog'], 3)
        self.assertEqual(report['retailers']['n11'], {'models_with_current_listing': 2, 'current_listing_rows': 4})
        self.assertEqual(report['retailers']['Hepsiburada']['models_with_current_listing'], 1)
        self.assertEqual(report['retailers']['Trendyol']['models_with_current_listing'], 1)
        self.assertEqual(report['retailers']['Teknosa']['models_with_current_listing'], 0)
        self.assertFalse(report['listing_rows_are_independent_seller_counts'])
        self.assertNotIn('Same seller', str(report))
        self.assertNotIn('source_url', str(report))
        self.assertEqual((identity, catalog), before)

    def test_missing_stale_or_partial_audit_never_becomes_zero_platform_coverage(self):
        from datetime import datetime, timezone
        catalog = [{'slug': 'alpha'}, {'slug': 'beta'}]
        valid = {'evidenceVersion': 'product-offer-match-v1', 'mode': 'shadow_audit_only',
            'auditedAt': datetime.now(timezone.utc).isoformat(),
            'productCoverage': [{'slug': p['slug'], 'matchedOffers': []} for p in catalog]}
        for bad in ({}, {**valid, 'auditedAt': '1990-01-01T00:00:00Z'},
                    {**valid, 'productCoverage': valid['productCoverage'][:1]},
                    {**valid, 'productCoverage': valid['productCoverage'] + valid['productCoverage'][:1]}):
            with self.assertRaises(ValueError):
                priority_store_coverage_readback(bad, catalog)
        with self.assertRaises(ValueError):
            priority_store_coverage_readback(valid, [catalog[0], catalog[0]])


if __name__ == '__main__':
    unittest.main()
