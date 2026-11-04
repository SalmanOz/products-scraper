import unittest

from priority_retailer_transport import PriorityRetailerTransport
from trendyol_catalog_discovery import CATEGORY_URL, category_page_number, parse_category_document
from teknosa_catalog_discovery import match_catalog_destinations
from test_pasaj_source import Response


TITLE = 'Xiaomi REDMI A7 Pro 4GB+128GB Siyah (Xiaomi Türkiye Garantili)'
URL = 'https://www.trendyol.com/xiaomi/redmi-a7-pro-4gb-128gb-siyah-xiaomi-turkiye-garantili-p-1175413438'


def card(title=TITLE, url=URL):
    return (f'<a class="product-card" href="{url}?boutiqueId=689770&amp;merchantId=968">'
            f'<h2 class="title">{title}</h2><b>1 TL</b><button>Sepete Ekle</button></a>')


def document(body=None, page=1):
    return ('<h1 class="title">Cep Telefonu</h1>' + (card() if body is None else body) +
            (f'<a href="/cep-telefonu-x-c103498?pi={page+1}">{page+1}</a>' if page < 2 else
             '<a href="/cep-telefonu-x-c103498">1</a>'))


class TrendyolDiscoveryTests(unittest.TestCase):
    def test_observed_g_pair_category_card_is_a_raw_destination_hint_not_selected_memory_or_price(self):
        product = {'id': 1, 'name': 'Xiaomi 17', 'slug': 'xiaomi-17',
                   'attributes': {'ram_gb': 12, 'storage_gb': 256}}
        title = 'Xiaomi 17 12G+256G Siyah'
        url = 'https://www.trendyol.com/xiaomi/17-12g-256g-siyah-p-1110127495'
        rows, _ = parse_category_document(document(card(title, url)), CATEGORY_URL)
        before = [dict(r) for r in rows]
        self.assertEqual(match_catalog_destinations(rows, [product])[1], rows)
        self.assertEqual(rows, before)
        self.assertEqual(rows[0]['discovery_title'], title)
        for forbidden in ['price', 'ram_gb', 'storage_gb', 'source_variant_evidence']:
            self.assertNotIn(forbidden, rows[0])
        for changed in ['Xiaomi 17 8G+256G Siyah', 'Xiaomi 17 12G+512G Siyah',
                        'Xiaomi 17 Plus 12G+256G Siyah', 'Xiaomi 17 Pro 12G+256G Siyah',
                        'Xiaomi 17 12G+256G / 12G+512G Siyah']:
            other, _ = parse_category_document(document(card(changed, url)), CATEGORY_URL)
            self.assertEqual(match_catalog_destinations(other, [product]), {}, changed)

    def test_real_card_shape_discovers_only_canonical_destinations_not_card_amounts(self):
        rows, next_url = parse_category_document(document(), CATEGORY_URL)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source_url'], URL)
        self.assertEqual(next_url, CATEGORY_URL + '?pi=2')
        self.assertEqual(rows[0]['discovery_title'], TITLE)
        self.assertNotIn('price', rows[0])
        product = {'id': 1, 'name': 'Xiaomi Redmi A7 Pro', 'slug': 'redmi-a7-pro',
                   'attributes': {'ram_gb': 4, 'storage_gb': 128}}
        self.assertEqual(match_catalog_destinations(rows, [product])[1], rows)
        self.assertEqual(match_catalog_destinations(rows, [{**product, 'attributes': {'ram_gb': 6, 'storage_gb': 128}}]), {})

    def test_only_exact_category_pages_and_unambiguous_public_product_cards_are_accepted(self):
        for bad in [CATEGORY_URL + '?pi=0', CATEGORY_URL + '?pi=01', CATEGORY_URL + '?pi=2&token=x',
                    CATEGORY_URL + '#fragment', CATEGORY_URL.replace('https:', 'http:'),
                    CATEGORY_URL.replace('.com/', '.com.evil.test/')]:
            self.assertIsNone(category_page_number(bad))
        for changed in [document().replace('product-card', 'seller-store-product-card'),
                        document().replace('www.trendyol.com/xiaomi', 'evil.test/xiaomi'),
                        document().replace('merchantId=968', 'token=private'),
                        document().replace('h2 class="title"', 'h2'),
                        document().replace('Cep Telefonu</h1>', 'Telefon Aksesuarları</h1>')]:
            self.assertEqual(parse_category_document(changed, CATEGORY_URL)[0], [])
        rows, _ = parse_category_document(document(card() + card()), CATEGORY_URL)
        self.assertEqual(len(rows), 1)

    def test_observed_sequential_pagination_and_missing_pagination_are_reported_honestly(self):
        diagnostic = {}
        self.assertEqual(parse_category_document(document(), CATEGORY_URL, diagnostic)[1], CATEGORY_URL + '?pi=2')
        self.assertEqual(diagnostic['status'], 'parsed')
        self.assertIsNone(parse_category_document(document(page=2), CATEGORY_URL + '?pi=2', diagnostic)[1])
        self.assertEqual(diagnostic['status'], 'parsed')
        broken = document().replace('?pi=2', '?pi=84')
        self.assertIsNone(parse_category_document(broken, CATEGORY_URL, diagnostic)[1])
        self.assertEqual(diagnostic['status'], 'pagination_unrecognized')
        parse_category_document('<h1 class="title">Cep Telefonu</h1>' + card(), CATEGORY_URL, diagnostic)
        self.assertEqual(diagnostic['status'], 'pagination_unrecognized')

    def test_category_reads_obey_robots_and_reserve_a_separate_bounded_document_budget(self):
        calls = []
        def get(url, **_):
            calls.append(url)
            return Response(url, 'User-agent: *\nAllow: /\n' if url.endswith('robots.txt') else document())
        transport = PriorityRetailerTransport(get=get, sleep=lambda _: None, maximum_per_host=1)
        self.assertEqual(transport.read_trendyol_category(CATEGORY_URL)[1], 'document_read')
        self.assertEqual(transport.read_trendyol_category(CATEGORY_URL + '?pi=2')[1], 'document_read')
        self.assertEqual(transport.read_document(URL)[1], 'document_read')
        self.assertEqual(transport.read_document(URL + '1')[1], 'pdp_budget_exhausted')
        self.assertEqual(transport.report()['Trendyol']['category_requests'], 2)
        self.assertEqual(transport.report()['Trendyol']['pdp_requests'], 1)
        self.assertEqual(transport.read_trendyol_category(CATEGORY_URL + '?pi=101')[1], 'invalid_catalog_category')
        denied = PriorityRetailerTransport(get=lambda url, **_: Response(url,
            'User-agent: *\nDisallow: /cep-telefonu-x-c103498\n'), sleep=lambda _: None)
        self.assertEqual(denied.read_trendyol_category(CATEGORY_URL)[1], 'robots_disallowed')

    def test_full_catalog_discovery_has_room_for_three_pdp_destinations_per_110_models(self):
        transport = PriorityRetailerTransport(get=lambda url, **_: Response(url,
            'User-agent: *\nAllow: /\n' if url.endswith('robots.txt') else '<h1>Phone</h1>'), sleep=lambda _: None)
        transport.configure_catalog_budget(350)
        for number in range(330):
            url = URL.replace('1175413438', str(1175413438 + number))
            self.assertEqual(transport.read_document(url)[1], 'document_read')
        self.assertEqual(transport.report()['Trendyol']['pdp_requests'], 330)
        with self.assertRaises(ValueError):
            PriorityRetailerTransport(maximum_per_host=401)

    def test_discovery_reads_live_pdp_again_before_one_atomic_write_and_commercial_readback(self):
        from sweep_priority_catalog import sweep
        from test_priority_candidates import Client
        from test_trendyol_catalog_source import document as pdp, URL as target, NAME, SPECS
        from retailer_offer_refresh import RetailerOfferRefresher
        from trendyol_catalog_source import TrendyolCatalogSource
        canonical = target.split('?')[0]
        class ScopedClient(Client):
            def ingest_listing_offers(self, records, *, require_all_accepted=True):
                return {**super().ingest_offers(records, require_all_accepted=require_all_accepted),
                        'offer_identity_contract': 'seller-source-url-v1'}
        client = ScopedClient()
        client.product.update(name=NAME, attributes=SPECS, offer_targets=[])
        client.get_public_price_priority = lambda: {}
        calls = []
        class Transport:
            def configure_catalog_budget(self, count):
                pass
            def read_trendyol_category(self, url):
                calls.append(('category', url))
                return document(card(NAME + ' 8GB RAM 128GB', canonical), page=1 if '?' not in url else 2), 'document_read', 200
            def read_document(self, url):
                calls.append(('pdp', url))
                return pdp(), 'document_read', 200
            def resolved_url(self, url):
                return url
            def report(self):
                return {}
        transport = Transport()
        reader = RetailerOfferRefresher(trendyol_catalog_source=TrendyolCatalogSource(transport=transport))
        result = sweep(client=client, publish=True, transport=transport, reader=reader, discover_trendyol=True)
        self.assertEqual(result['accepted'], 1)
        self.assertEqual(len(client.writes), 1)
        self.assertEqual(client.writes[0][0]['price'], 34997)
        self.assertEqual(client.writes[0][0]['source_url'], canonical)
        self.assertEqual(client.writes[0][0]['source_variant_evidence']['ram_gb'], 8)
        self.assertEqual([kind for kind, _ in calls], ['category', 'category', 'pdp', 'pdp'])
        self.assertTrue(result['rows'][0]['publication']['commercial_readback_verified'])
        self.assertTrue(result['trendyol_category_discovery']['pagination_complete'])


if __name__ == '__main__':
    unittest.main()
