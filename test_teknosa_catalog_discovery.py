from copy import deepcopy
import unittest
from unittest.mock import patch

from priority_retailer_transport import PriorityRetailerTransport
from teknosa_catalog_discovery import (CATEGORY_URL, category_page_number,
                                       parse_category_document, match_catalog_destinations)
from test_pasaj_source import Response


TITLE = 'Apple iPhone 15 128GB Siyah Akıllı Telefon'
URL = 'https://www.teknosa.com/apple-iphone-15-128gb-siyah-akilli-telefon-p-125079197'


def card(title=TITLE, url=URL, sku='125079197', stock='Y'):
    # Whitelisted public fields observed on the phone category, Nov 4 2026.
    return (f'<div data-product-url="{url}" data-product-name="{title}" '
            f'data-product-id="{sku}" data-product-ean="0195949036040" '
            f'data-product-page-type="CATEGORY" data-product-stock="{stock}">'
            f'<h3 class="prd-title">{title}</h3></div>')


def document(body='', next_page=1):
    return ('<h1 class="plp-title">Cep Telefonu Fiyatları ve Modelleri</h1>' + body +
            (f'<a href="/cep-telefonu-c-100001?page={next_page}">'
             f'Daha Fazla Ürün Gör ({next_page} / 35)</a>' if next_page else ''))


PRODUCT = {'id': 1, 'slug': 'apple-iphone-15', 'name': 'Apple iPhone 15',
           'attributes': {'ram_gb': 6, 'storage_gb': 128}, 'offer_targets': []}


class DiscoveryTests(unittest.TestCase):
    def test_explicit_matching_radio_is_not_crowded_out_by_omitted_radio_memory_duplicates(self):
        product = {'id': 262, 'slug': 'samsung-galaxy-a17-5g', 'name': 'Samsung Galaxy A17 5G',
                   'attributes': {'ram_gb': 4, 'storage_gb': 128}}
        rows = [{'source_url': f'https://www.trendyol.com/samsung/{slug}-p-{identity}',
                 'discovery_title': title, 'stock_priority': True}
                for slug, identity, title in [
                    ('galaxy-a17-4-128-gb-gri', 975557871, 'Samsung Galaxy A17 4/128 GB Gri Cep Telefonu'),
                    ('galaxy-a17-4-128-gb-mavi', 1116303517, 'Samsung Galaxy A17 4/128 GB Mavi'),
                    ('galaxy-a17-4gb-ram-128gb-5g-black', 974373405, 'Samsung Galaxy A17, 4GB RAM, 128GB, 5G, Black'),
                    ('galaxy-a17-4g-4gb-128gb', 974373406, 'Samsung Galaxy A17 4GB RAM 128GB')]]
        matched = match_catalog_destinations(rows, [product])[262]
        self.assertEqual(matched[0]['source_url'], rows[2]['source_url'])
        self.assertEqual(len(matched), 2)
        self.assertNotIn(rows[3], matched)
        self.assertNotIn('price', matched[0])

    def test_observed_magic_generation_spacing_keeps_exact_generation_and_qualifiers(self):
        product = {'id': 1, 'name': 'Honor Magic8 Pro',
                   'attributes': {'ram_gb': 12, 'storage_gb': 512}}
        def row(title, i):
            return {'source_url': f'https://www.teknosa.com/honor-phone-p-{125000000+i}',
                    'discovery_title': title, 'stock_priority': True}
        rows = [row('HONOR Magic 8 Pro 5G 512 GB 12 GB Ram (Honor Türkiye Garantili)', 1),
                row('Honor Magic 7 Pro 12GB RAM 512GB', 2), row('Honor Magic 8 12GB RAM 512GB', 3),
                row('Honor Magic 8 Pro 16GB RAM 512GB', 4)]
        self.assertEqual(match_catalog_destinations(rows, [product])[1], rows[:1])

    def test_accessory_brand_destinations_do_not_take_a_phone_discovery_slot(self):
        rows = [{'source_url': f'https://www.trendyol.com/{brand}/iphone-15-128gb-p-{10000+i}',
                 'discovery_title': title, 'stock_priority': True}
                for i, brand, title in [(0, 'valoris', 'Valoris iPhone 15 Telefon Kılıfı'),
                                       (1, 'genel-markalar', 'Genel Markalar Apple iPhone 15 Volet Silikon'),
                                       (2, 'apple', 'Apple iPhone 15 128GB Mavi')]]
        found = match_catalog_destinations(rows, [PRODUCT])[1]
        self.assertEqual([r['source_url'] for r in found], [rows[2]['source_url']])

    def test_explicit_matching_memory_hints_precede_omitted_memory_within_stock_priority(self):
        product = deepcopy(PRODUCT)
        product.update(name='Samsung Galaxy A36', attributes={'ram_gb': 6, 'storage_gb': 128})
        rows = [{'source_url': f'https://www.trendyol.com/samsung/phone-p-{i}',
                 'stock_priority': stock, 'discovery_title': title}
                for i, title, stock in [
                    (1, 'Samsung Galaxy A36 Telefon', True),
                    (2, 'Samsung Galaxy A36 128GB Telefon', True),
                    (3, 'Samsung Galaxy A36 6GB RAM 128GB Telefon', True),
                    (4, 'Samsung Galaxy A36 6GB RAM 128GB Telefon', False),
                    (5, 'Samsung Galaxy A36 8GB RAM 128GB Telefon', True)]]
        found = match_catalog_destinations(rows, [product])[1]
        self.assertEqual([r['source_url'].rsplit('-', 1)[-1] for r in found], ['3', '2'])

    def test_observed_card_identifiers_and_next_link_are_destinations_not_offers(self):
        rows, next_url = parse_category_document(document(card() + '<b>1 TL</b>'), CATEGORY_URL)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source_url'], URL)
        self.assertEqual(next_url, CATEGORY_URL + '?page=1')
        self.assertNotIn('price', rows[0])
        self.assertNotIn('seller', rows[0])
        self.assertEqual(match_catalog_destinations(rows, [PRODUCT])[1], rows)

    def test_changed_sku_title_query_gtin_and_external_destinations_are_not_discovered(self):
        original = card()
        for changed in (
            original.replace('data-product-id="125079197"', 'data-product-id="125079198"'),
            original.replace('data-product-name="Apple', 'data-product-name="Samsung'),
            original.replace(URL, URL + '?shopId=1234'),
            original.replace('0195949036040', '0195949036041'),
            original.replace('www.teknosa.com', 'www.teknosa.com.evil.test'),
            original.replace('CATEGORY', 'RECOMMENDATIONS'),
        ):
            with self.subTest(changed=changed):
                self.assertEqual(parse_category_document(document(changed), CATEGORY_URL)[0], [])
        for bad in (CATEGORY_URL + '?q=phone', CATEGORY_URL + '?page=1&page=2',
                    CATEGORY_URL + '#fragment', CATEGORY_URL.replace('https:', 'http:')):
            self.assertIsNone(category_page_number(bad))

    def test_pagination_follows_only_the_observed_immediate_next_link(self):
        html = document(card(), next_page=3)
        self.assertIsNone(parse_category_document(html, CATEGORY_URL)[1])
        self.assertEqual(parse_category_document(html, CATEGORY_URL + '?page=2')[1],
                         CATEGORY_URL + '?page=3')
        self.assertIsNone(parse_category_document(html.replace('/cep-telefonu',
            'https://evil.test/cep-telefonu'), CATEGORY_URL + '?page=2')[1])
        # A fortieth-page continuation is reported as incomplete by the caller,
        # rather than silently claiming the category ended at our safety bound.
        self.assertEqual(parse_category_document(document(card(), 40), CATEGORY_URL + '?page=39')[1],
                         CATEGORY_URL + '?page=40')

    def test_real_nested_pagination_text_keeps_following_the_entire_category(self):
        html = ('<h1 class="plp-title">Cep Telefonu Fiyatları ve Modelleri</h1>' + card() +
                '<a href="/cep-telefonu-c-100001?page=1" rel="next" class="plp-paging-load-more" '
                'title="Daha Fazla Ürün Gör (1 / 35)"><span>Daha Fazla Ürün Gör ('
                '<span class="plp-paging-current-btn">1</span> / 35) </span></a>')
        self.assertEqual(parse_category_document(html, CATEGORY_URL)[1], CATEGORY_URL + '?page=1')
        self.assertEqual(parse_category_document(html.replace('page=1', 'page=2').replace(
            'current-btn">1', 'current-btn">2'), CATEGORY_URL + '?page=1')[1], CATEGORY_URL + '?page=2')
        self.assertIsNone(parse_category_document(html.replace('current-btn">1', 'current-btn">9'), CATEGORY_URL)[1])
        diagnostic = {}
        parse_category_document(html.replace('current-btn">1', 'current-btn">9'), CATEGORY_URL, diagnostic)
        self.assertEqual(diagnostic['status'], 'continuation_unrecognized')
        parse_category_document(html.replace('h1 class="plp-title"', 'h1'), CATEGORY_URL, diagnostic)
        self.assertEqual(diagnostic['status'], 'category_identity_missing')

    def test_sweep_does_not_claim_complete_pagination_after_an_unrecognized_next_template(self):
        from sweep_priority_catalog import sweep
        from test_priority_candidates import Client
        client = Client()
        client.get_published_products = lambda: [deepcopy(PRODUCT)]
        client.get_public_price_priority = lambda: {}
        class Transport:
            def read_teknosa_category(self, url):
                return document('', None) + '<a rel="next" href="/invalid-next">More</a>', 'document_read', 200
            def report(self):
                return {}
        result = sweep(client=client, transport=Transport(), discover_teknosa=True)
        self.assertFalse(result['category_discovery']['pagination_complete'])
        self.assertEqual(result['category_discovery']['pages'][0]['parser']['status'], 'continuation_unrecognized')

    def test_exact_catalog_variant_unique_owner_and_stock_hint_order(self):
        rows, _ = parse_category_document(document(card(stock='N') + card(
            url=URL.replace('125079197', '125079200'), sku='125079200')), CATEGORY_URL)
        matched = match_catalog_destinations(rows, [PRODUCT])
        self.assertTrue(matched[1][0]['stock_priority'])
        wrong_storage = deepcopy(PRODUCT)
        wrong_storage['attributes']['storage_gb'] = 256
        self.assertEqual(match_catalog_destinations(rows, [wrong_storage]), {})
        duplicate = deepcopy(PRODUCT)
        duplicate['id'] = 2
        self.assertEqual(match_catalog_destinations(rows, [PRODUCT, duplicate]), {})

    def test_category_documents_do_not_consume_the_reserved_pdp_budget_and_obey_robots(self):
        calls = []
        def get(url, **_):
            calls.append(url)
            return Response(url, 'User-agent: *\nAllow: /\n' if url.endswith('robots.txt') else document(card()))
        transport = PriorityRetailerTransport(get=get, sleep=lambda _: None, maximum_per_host=1)
        self.assertEqual(transport.read_teknosa_category(CATEGORY_URL)[1], 'document_read')
        self.assertEqual(transport.read_document(URL)[1], 'document_read')
        self.assertEqual(transport.read_document(URL.replace('197', '198'))[1], 'pdp_budget_exhausted')
        self.assertEqual(transport.report()['Teknosa']['category_requests'], 1)
        self.assertEqual(transport.report()['Teknosa']['pdp_requests'], 1)
        self.assertEqual(transport.report()['Teknosa']['document_requests'], 2)
        self.assertEqual(transport.read_teknosa_category(CATEGORY_URL + '?page=40')[1], 'invalid_catalog_category')

    def test_sweep_discovers_new_destination_but_uses_only_fresh_pdp_publication(self):
        from sweep_priority_catalog import sweep
        from test_priority_candidates import Client
        from test_priority_catalog_sweep import sweep_reader
        client = Client()
        client.get_published_products = lambda: [deepcopy(PRODUCT)]
        client.get_public_price_priority = lambda: {1: (PRODUCT['slug'], False)}
        class Transport:
            def read_teknosa_category(self, url):
                return document(card(), next_page=None), 'document_read', 200
            def report(self):
                return {}
        inspected = {'slug': PRODUCT['slug'], 'retailer': 'Teknosa', 'url': URL,
                     'observed_url': URL, 'reason': 'document_read', 'http_status': 200,
                     'prepared_offer': {'platform': 'Teknosa', 'url': URL}}
        with patch('sweep_priority_catalog.inspect_target', return_value=inspected) as inspect, \
                patch('sweep_priority_catalog.refresh_candidates', return_value={
                    'status': 'completed', 'writes': True, 'ingestion': {'accepted': 1}}) as publisher:
            result = sweep(client=client, publish=True, transport=Transport(), reader=sweep_reader(), discover_teknosa=True)
        inspect.assert_called_once()
        publisher.assert_called_once()
        self.assertEqual(publisher.call_args.args[0]['rows'], [inspected])
        self.assertEqual((result['queued'], result['accepted']), (1, 1))
        self.assertTrue(result['category_discovery']['pagination_complete'])
        self.assertEqual(result['models_without_priority_hints'], [])


if __name__ == '__main__':
    unittest.main()
