from copy import deepcopy
import unittest
from unittest.mock import patch

from hepsiburada_catalog_discovery import CATEGORY_URL, card_destination, category_identity, parse_category_document, discovery_links, discover_product_siblings
from priority_retailer_transport import PriorityRetailerTransport
from test_pasaj_source import Response
from test_teknosa_catalog_discovery import PRODUCT

PDP = 'https://www.hepsiburada.com/apple-iphone-15-128-gb-mavi-p-HBCV00004X9ZCK'
SAMSUNG = 'https://www.hepsiburada.com/samsung-cep-telefonlari-xc-371965-b8848'


def document(brand=False):
    # Whitelisted public Nov 4 category structure; no prices or ad/session IDs.
    return (f'<h1>{"Samsung Telefonlar" if brand else "Cep Telefonu Fiyatları ve Modelleri"}</h1>'
            f'<h2 data-test-id="title-1"><a title="Apple iPhone 15 128 GB Mavi" href="{PDP}">'
            'Apple iPhone 15 128 GB Mavi</a></h2>'
            f'<a href="{SAMSUNG}">Samsung</a><a href="{SAMSUNG}">Samsung</a>'
            '<a href="/colezium-cep-telefonlari-xc-371965-b6647">Colezium</a>')


class HepsiburadaDiscoveryTests(unittest.TestCase):
    def test_sibling_destinations_require_explicit_same_memory_phone_links_not_variant_prices(self):
        import json
        root = 'https://www.hepsiburada.com/apple-iphone-11-64-gb-beyaz-p-HBV0000120X66'
        target = 'https://www.hepsiburada.com/apple-iphone-11-64-gb-siyah-p-HBV0000120X65'
        product = {'name': 'Apple iPhone 11', 'slug': 'apple-iphone-11',
                   'attributes': {'ram_gb': 4, 'storage_gb': 64}}
        sibling = {'name': 'Apple iPhone 11 64 GB Siyah', 'sku': 'HBV0000120X65',
                   'url': target, 'isDefault': False, 'isInStock': True, 'isPreOrder': False,
                   'price': 1, 'properties': [{'name': 'Kapasite', 'valueObject': {'actualValue': '64 GB'}}]}
        def html(row=sibling, sku='HBV0000120X66'):
            return '<script id="reduxStore">' + json.dumps({'productState': {'product': {
                'productId': 'HB00000NSBZ4', 'sku': sku, 'variants': [row]}}}) + '</script>'
        found = discover_product_siblings(html(), root, product)
        self.assertEqual([r['source_url'] for r in found], [target])
        self.assertNotIn('price', found[0])
        self.assertNotIn('merchant', found[0])
        for patch in [{'url': target + '?magaza=Other'}, {'url': target.replace('.com/', '.com.evil.test/')},
                      {'url': None}, {'sku': 'HBV0000120X67'}, {'isDefault': True},
                      {'isInStock': False}, {'isPreOrder': True}, {'name': 'Apple iPhone 11 Pro 64 GB Siyah'},
                      {'name': 'Apple Yenilenmiş iPhone 11 64 GB Siyah'},
                      {'name': 'Apple iPhone 11 128 GB Siyah'},
                      {'properties': [{'name': 'Kapasite', 'valueObject': {'actualValue': '128 GB'}}]}]:
            self.assertEqual(discover_product_siblings(html({**sibling, **patch}), root, product), [], patch)
        self.assertEqual(discover_product_siblings(html(sku='HBV0000120X67'), root, product), [])
        self.assertEqual(discover_product_siblings(html() + html(), root, product), [])

    def test_public_ad_link_discovers_only_its_explicit_canonical_product_without_tracking(self):
        from urllib.parse import urlencode
        tracker = 'https://adservice.hepsiburada.com/event/api/v1/track?' + urlencode({
            'event': 'not-retained', 'anonymousId': 'not-retained',
            'redirect': PDP + '?adsUniqueId=not-retained&magaza=OldSeller'})
        self.assertEqual(card_destination(tracker), PDP)
        rows, _ = parse_category_document(document().replace(PDP, tracker.replace('&', '&amp;')), CATEGORY_URL)
        self.assertEqual([r['source_url'] for r in rows], [PDP])
        self.assertNotIn('not-retained', str(rows))
        for bad in [tracker.replace('adservice.hepsiburada.com', 'evil.test'),
                    tracker.replace('event/api/v1/track', 'private/track'),
                    tracker + '&redirect=https%3A%2F%2Fevil.test',
                    'https://adservice.hepsiburada.com/event/api/v1/track?' + urlencode({'redirect':'https://evil.test/phone'})]:
            self.assertIsNone(card_destination(bad))

    def test_public_card_and_observed_brand_links_never_supply_prices(self):
        diagnostic = {}
        cards, next_page = parse_category_document(document() + '<b>1 TL</b>', CATEGORY_URL, diagnostic)
        self.assertEqual([c['source_url'] for c in cards], [PDP])
        self.assertNotIn('price', cards[0])
        self.assertNotIn('seller', cards[0])
        self.assertIsNone(next_page)
        self.assertEqual(diagnostic['scope'], 'bounded_observed_category_brand_and_model_pages')
        self.assertEqual(discovery_links(document(), CATEGORY_URL, [{'name': 'Samsung Galaxy S26'}]), [SAMSUNG])
        self.assertEqual(discovery_links(document(), CATEGORY_URL, [PRODUCT]), [])
        self.assertEqual(len(parse_category_document(document(True), SAMSUNG)[0]), 1)
        # Live Apple/Honor brand pages use this longer first-page heading.
        for brand, url in [('Apple', 'https://www.hepsiburada.com/apple-cep-telefonlari-xc-371965-b8849'),
                           ('HONOR', 'https://www.hepsiburada.com/honor-cep-telefonlari-xc-371965-b8852')]:
            html = document().replace('Cep Telefonu Fiyatları ve Modelleri', brand + ' Cep Telefonu Fiyatları ve Modelleri')
            self.assertEqual(len(parse_category_document(html, url)[0]), 1)

    def test_ads_selectors_changed_titles_and_nonphone_branches_are_rejected(self):
        for changed in [document().replace(PDP, 'https://adservice.hepsiburada.com/event/api/v1/track'),
                        document().replace(PDP, PDP + '?magaza=SomeShop'),
                        document().replace('title="Apple', 'title="Samsung'),
                        document().replace('<h1>', '<h3>').replace('</h1>', '</h3>')]:
            self.assertEqual(parse_category_document(changed, CATEGORY_URL)[0], [])
        for url in [CATEGORY_URL + '?sayfa=02', CATEGORY_URL + '#x', SAMSUNG + '?filtreler=satici:x',
                    SAMSUNG.replace('www.hepsiburada.com', 'evil.test'), SAMSUNG.replace('371965', '123456'),
                    SAMSUNG.replace('samsung-', 'colezium-')]:
            self.assertIsNone(category_identity(url))

    def test_observed_model_taxonomies_prioritize_catalog_needs_and_preserve_model_boundaries(self):
        iphone = 'https://www.hepsiburada.com/iphone-11-iphone-ios-telefonlar-xc-60005202-t12'
        pro = 'https://www.hepsiburada.com/iphone-16-pro-iphone-ios-telefonlar-xc-60005202-t29'
        pro_max = 'https://www.hepsiburada.com/iphone-16-pro-max-iphone-ios-telefonlar-xc-60005202-t27'
        a35 = 'https://www.hepsiburada.com/samsung-galaxy-a35-android-telefonlar-xc-60005201-b8848-t317'
        links = ''.join(f'<a href="{u}">Phone</a>' for u in [a35, pro_max, pro, iphone])
        products = [{'name': 'Apple iPhone 11'}, {'name': 'Apple iPhone 16 Pro'},
                    {'name': 'Samsung Galaxy A35 5G'}]
        self.assertEqual(discovery_links(document() + links, CATEGORY_URL, products),
                         [iphone, pro, a35, SAMSUNG])
        for url, heading in [(iphone, 'iPhone 11 İndirimleri'),
                             (a35, 'Samsung Galaxy A35 Android Telefon Modelleri ve Özellikleri')]:
            html = document().replace('Cep Telefonu Fiyatları ve Modelleri', heading)
            self.assertEqual([c['source_url'] for c in parse_category_document(html, url)[0]], [PDP])
            self.assertEqual(parse_category_document(html.replace(heading, 'Other Phone'), url)[0], [])
        for bad in [iphone.replace('60005202', '60005201'),
                    a35.replace('60005201', '60005202'), a35.replace('samsung-', 'colezium-'),
                    iphone + '?q=iphone', iphone.replace('-t12', '-t012')]:
            self.assertIsNone(category_identity(bad))

    def test_real_protocol_relative_rel_next_follows_only_the_same_immediate_category_page(self):
        html = document() + '<link rel="next" href="//www.hepsiburada.com/cep-telefonlari-c-371965?sayfa=2">'
        self.assertEqual(parse_category_document(html, CATEGORY_URL)[1], CATEGORY_URL + '?sayfa=2')
        for changed in [html.replace('sayfa=2', 'sayfa=3'),
                        html.replace('//www.hepsiburada.com/', '//evil.test/'),
                        html.replace('sayfa=2', 'sayfa=2&amp;filtreler=satici:x')]:
            diagnostic = {}
            self.assertIsNone(parse_category_document(changed, CATEGORY_URL, diagnostic)[1])
            self.assertEqual(diagnostic['status'], 'continuation_unrecognized')
        self.assertEqual(parse_category_document(html.replace('sayfa=2', 'sayfa=3'),
                         CATEGORY_URL + '?sayfa=2')[1], CATEGORY_URL + '?sayfa=3')
        actual_second = html.replace('Cep Telefonu Fiyatları ve Modelleri', 'Cep Telefonu').replace('sayfa=2', 'sayfa=3')
        self.assertEqual(parse_category_document(actual_second, CATEGORY_URL + '?sayfa=2')[1], CATEGORY_URL + '?sayfa=3')
        self.assertEqual(parse_category_document(actual_second, CATEGORY_URL)[0], [])

    def test_separate_category_budget_and_robots_denial(self):
        t = PriorityRetailerTransport(get=lambda url, **_: Response(url,
            'User-agent: *\nAllow: /\n' if url.endswith('robots.txt') else document()),
            sleep=lambda _: None, maximum_per_host=1)
        self.assertEqual(t.read_hepsiburada_category(CATEGORY_URL)[1], 'document_read')
        self.assertEqual(t.read_document(PDP)[1], 'document_read')
        self.assertEqual(t.report()['Hepsiburada']['category_requests'], 1)
        denied = PriorityRetailerTransport(get=lambda url, **_: Response(url,
            'User-agent: *\nDisallow: /cep-telefonlari\n'), sleep=lambda _: None)
        self.assertEqual(denied.read_hepsiburada_category(CATEGORY_URL)[1], 'robots_disallowed')

    def test_one_changed_brand_heading_does_not_hide_other_observed_category_pages(self):
        from sweep_priority_catalog import sweep
        from test_priority_candidates import Client
        client = Client()
        client.product.update(name='Samsung Galaxy S26', attributes={'ram_gb': 12, 'storage_gb': 256},
                              offer_targets=[])
        client.get_public_price_priority = lambda: {}
        visited = []
        class Transport:
            def read_hepsiburada_category(self, url):
                visited.append(url)
                if url == SAMSUNG:
                    html = '<h1>Changed category heading</h1>'
                elif url == CATEGORY_URL:
                    html = document() + '<link rel="next" href="//www.hepsiburada.com/cep-telefonlari-c-371965?sayfa=2">'
                else:
                    html = document().replace('Cep Telefonu Fiyatları ve Modelleri', 'Cep Telefonu')
                return html, 'document_read', 200
            def report(self):
                return {}
        result = sweep(client=client, transport=Transport(), discover_hepsiburada=True)
        self.assertEqual(visited, [CATEGORY_URL, SAMSUNG, CATEGORY_URL + '?sayfa=2'])
        discovery = result['hepsiburada_category_discovery']
        self.assertEqual(discovery['incomplete_pages'], [SAMSUNG])
        self.assertFalse(discovery['branch_queue_complete'])
        self.assertFalse(result['writes'])

    def test_sweep_follows_observed_branch_and_reverifies_only_fresh_product_pages(self):
        from sweep_priority_catalog import sweep
        from test_priority_candidates import Client
        from test_priority_catalog_sweep import sweep_reader
        client = Client()
        products = [deepcopy(PRODUCT), {'id': 2, 'slug': 'samsung-galaxy-s26', 'name': 'Samsung Galaxy S26',
                     'attributes': {'ram_gb': 12, 'storage_gb': 256}, 'offer_targets': []}]
        client.get_published_products = lambda: products
        client.get_public_price_priority = lambda: {p['id']: (p['slug'], False) for p in products}
        visited = []
        class Transport:
            def read_hepsiburada_category(self, url):
                visited.append(url)
                html = document(url == SAMSUNG)
                if url == CATEGORY_URL:
                    html += '<link rel="next" href="//www.hepsiburada.com/cep-telefonlari-c-371965?sayfa=2">'
                elif url == CATEGORY_URL + '?sayfa=2':
                    html = html.replace('Cep Telefonu Fiyatları ve Modelleri', 'Cep Telefonu')
                return html, 'document_read', 200
            def report(self):
                return {}
        row = {'slug': PRODUCT['slug'], 'retailer': 'Hepsiburada', 'url': PDP, 'observed_url': PDP,
               'reason': 'document_read', 'http_status': 200, 'prepared_offer': {'url': PDP}}
        with patch('sweep_priority_catalog.inspect_target', return_value=row) as inspect, \
                patch('sweep_priority_catalog.refresh_candidates', return_value={
                    'status': 'completed', 'writes': True, 'ingestion': {'accepted': 1}}) as publish:
            result = sweep(client=client, publish=True, reader=sweep_reader(), transport=Transport(),
                           discover_hepsiburada=True)
        self.assertEqual(visited, [CATEGORY_URL, SAMSUNG, CATEGORY_URL + '?sayfa=2'])
        inspect.assert_called_once()
        publish.assert_called_once()
        self.assertEqual(result['accepted'], 1)
        self.assertTrue(result['hepsiburada_category_discovery']['branch_queue_complete'])
        self.assertFalse(result['hepsiburada_category_discovery']['pagination_complete'])
