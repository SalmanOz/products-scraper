import json
from unittest import TestCase
from unittest.mock import Mock

from gaming_catalog_evidence import identity, matching_products, selected_evidence
from retail_gtin_evidence import AMAZON, N11, PAZARAMA, candidates, nuxt_code, selected_pazarama_specs, verify


GTIN = '5903018665788'
OTHER = '0198155448171'


def shop_product(number=34):
    attrs = [('pa_ean', 'EAN', GTIN), ('pa_marka', 'Marka', 'Acme'), ('pa_dpi', 'DPI', '3200 DPI')]
    return {'id': number, 'type': 'simple', 'name': 'Acme Mouse',
            'permalink': f'https://www.gaming.gen.tr/urun/{number}/acme-mouse/', 'variations': [],
            'attributes': [{'name': name, 'taxonomy': key, 'terms': [{'name': value}]} for key, name, value in attrs]}


class EvidenceTest(TestCase):
    def test_store_collection_is_discovery_and_selected_record_is_verified_again(self):
        selected = shop_product()
        self.assertEqual(identity(selected), GTIN)
        target = {'gtin': GTIN, 'kind': 'mouse'}
        data = selected_evidence(selected, target, 34)
        self.assertEqual(data['specs'], {'DPI': '3200 DPI'})
        self.assertEqual(data['source'], selected['permalink'])
        store = Mock()
        store.get.side_effect = [([selected], {'X-WP-TotalPages': '1'}), (shop_product(35), {})]
        self.assertEqual(list(matching_products({GTIN: target}, store)), [])
        store.get.side_effect = [([selected, shop_product(42)], {'X-WP-TotalPages': '1'})]
        self.assertEqual(list(matching_products({GTIN: target}, store)), [])

    def test_store_rejects_other_package_variant_conflict_and_cross_store_permalink(self):
        for changed in ({'type': 'variable'}, {'variations': [7]}, {'permalink': 'https://example.org/urun/34/mouse/'}):
            self.assertIsNone(identity({**shop_product(), **changed}))
        pack = shop_product()
        pack['attributes'][0]['terms'][0]['name'] = '1' + GTIN
        self.assertIsNone(identity(pack))
        conflicting = shop_product()
        conflicting['attributes'][0]['terms'].append({'name': OTHER})
        self.assertIsNone(identity(conflicting))

    def test_pazarama_requires_matching_selected_merchant_code_and_product_title(self):
        url = f'https://{PAZARAMA}/acme-mouse-p-{GTIN}'
        native = 'window.__NUXT__=(function(a,b){return {UI_PRODUCT_DETAIL:{code:b,attributes:[]}}}(void 0,"' + GTIN + '"));'
        item = {'@type': 'Product', 'url': url, 'name': 'Acme Mouse', 'brand': 'Acme'}
        body = (f'<h1>Acme Mouse</h1><script>{native}</script><script type="application/ld+json">{json.dumps(item)}</script>'
                '<div class="feature"><div><span>DPI</span><span>3200 DPI</span></div></div>').encode()
        self.assertEqual(selected_pazarama_specs(body, GTIN)['specs'], {'DPI': '3200 DPI'})
        self.assertIsNone(nuxt_code(native.replace('return', 'b="other";return')))
        self.assertIsNone(selected_pazarama_specs(body.replace(native.encode(), native.replace(GTIN, OTHER).encode()), GTIN))
        self.assertIsNone(selected_pazarama_specs(body.replace(b'<h1>Acme Mouse', b'<h1>Other Mouse'), GTIN))

    def test_search_cards_only_lead_to_selected_product_identity(self):
        amazon = b'<div data-component-type="s-search-result" data-asin="B00EXAMPLE"><h2>Acme Mouse</h2></div>'
        self.assertEqual(candidates(AMAZON, amazon), [f'https://{AMAZON}/dp/B00EXAMPLE'])
        html = b'<a href="/urun/acme-mouse-1234567"><h2 class="product-item-title">Acme Mouse</h2></a>'
        transport = Mock()
        transport.get.side_effect = [(f'https://{N11}/arama?q={GTIN}', html), (f'https://{N11}/urun/acme-mouse-1234567', b'<h1>Acme Mouse</h1>')]
        self.assertIsNone(verify(N11, GTIN, 'mouse', transport))
