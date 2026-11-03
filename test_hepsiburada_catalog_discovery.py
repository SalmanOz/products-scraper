from copy import deepcopy
import unittest
from unittest.mock import patch

from hepsiburada_catalog_discovery import CATEGORY_URL, category_identity, parse_category_document, discovery_links
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
    def test_public_card_and_observed_brand_links_never_supply_prices(self):
        diagnostic = {}
        cards, next_page = parse_category_document(document() + '<b>1 TL</b>', CATEGORY_URL, diagnostic)
        self.assertEqual([c['source_url'] for c in cards], [PDP])
        self.assertNotIn('price', cards[0])
        self.assertNotIn('seller', cards[0])
        self.assertIsNone(next_page)
        self.assertEqual(diagnostic['scope'], 'observed_category_and_brand_first_pages')
        self.assertEqual(discovery_links(document(), CATEGORY_URL, [{'name': 'Samsung Galaxy S26'}]), [SAMSUNG])
        self.assertEqual(discovery_links(document(), CATEGORY_URL, [PRODUCT]), [])
        self.assertEqual(len(parse_category_document(document(True), SAMSUNG)[0]), 1)

    def test_ads_selectors_changed_titles_and_nonphone_branches_are_rejected(self):
        for changed in [document().replace(PDP, 'https://adservice.hepsiburada.com/event/api/v1/track'),
                        document().replace(PDP, PDP + '?magaza=SomeShop'),
                        document().replace('title="Apple', 'title="Samsung'),
                        document().replace('<h1>', '<h3>').replace('</h1>', '</h3>')]:
            self.assertEqual(parse_category_document(changed, CATEGORY_URL)[0], [])
        for url in [CATEGORY_URL + '?sayfa=2', CATEGORY_URL + '#x', SAMSUNG + '?filtreler=satici:x',
                    SAMSUNG.replace('www.hepsiburada.com', 'evil.test'), SAMSUNG.replace('371965', '123456'),
                    SAMSUNG.replace('samsung-', 'colezium-')]:
            self.assertIsNone(category_identity(url))

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
                return document(url == SAMSUNG), 'document_read', 200
            def report(self):
                return {}
        row = {'slug': PRODUCT['slug'], 'retailer': 'Hepsiburada', 'url': PDP, 'observed_url': PDP,
               'reason': 'document_read', 'http_status': 200, 'prepared_offer': {'url': PDP}}
        with patch('sweep_priority_catalog.inspect_target', return_value=row) as inspect, \
                patch('sweep_priority_catalog.refresh_candidates', return_value={
                    'status': 'completed', 'writes': True, 'ingestion': {'accepted': 1}}) as publish:
            result = sweep(client=client, publish=True, reader=sweep_reader(), transport=Transport(),
                           discover_hepsiburada=True)
        self.assertEqual(visited, [CATEGORY_URL, SAMSUNG])
        inspect.assert_called_once()
        publish.assert_called_once()
        self.assertEqual(result['accepted'], 1)
        self.assertTrue(result['hepsiburada_category_discovery']['branch_queue_complete'])
        self.assertFalse(result['hepsiburada_category_discovery']['pagination_complete'])
