import json
from unittest import TestCase
from unittest.mock import Mock, patch

from gaming_catalog_evidence import identity, matching_products, selected_evidence
from retail_html_catalog import mismatched_spec_table
from retail_gtin_evidence import AMAZON, DIYALOG, N11, OCTO, PAZARAMA, candidates, nuxt_code, selected_diyalog_specs, selected_octo_specs, selected_pazarama_specs, verify


GTIN = '5903018665788'
OTHER = '0198155448171'


def shop_product(number=34):
    attrs = [('pa_ean', 'EAN', GTIN), ('pa_marka', 'Marka', 'Acme'), ('pa_dpi', 'DPI', '3200 DPI')]
    return {'id': number, 'type': 'simple', 'name': 'Acme Mouse',
            'permalink': f'https://www.gaming.gen.tr/urun/{number}/acme-mouse/', 'variations': [],
            'attributes': [{'name': name, 'taxonomy': key, 'terms': [{'name': value}]} for key, name, value in attrs]}


class EvidenceTest(TestCase):
    def test_selected_barcode_rejects_wrong_display_table_in_both_readers(self):
        monitor = shop_product()
        monitor['name'] = 'Acme Studio 75 inç'
        monitor['attributes'].append({'name': 'Ekran Boyutu', 'terms': [{'name': '19 inç'}]})
        self.assertIsNone(selected_evidence(monitor, {'gtin': GTIN, 'kind': 'monitor'}, 34))
        verified = {'gtin': GTIN, 'name': 'Acme Studio', 'specs': {'Screen Size': '19"'}}
        with patch('retail_gtin_evidence.selected_product', return_value=verified):
            self.assertIsNone(verify(N11, GTIN, 'monitor', Mock(), catalog_name='Acme Studio 75"'))
            self.assertIs(verify(N11, GTIN, 'monitor', Mock(), catalog_name='Acme Studio 19.5"'), verified)

    def test_module_memory_generation_and_ambiguous_or_unrelated_claims(self):
        self.assertTrue(mismatched_spec_table('pc-parts', {'RAM Tipi': 'DDR4'}, 'Example DDR5 CUDIMM'))
        self.assertFalse(mismatched_spec_table('pc-parts', {'RAM Tipi': 'DDR4'}, 'Example DDR4/5 RAM'))
        self.assertFalse(mismatched_spec_table('pc-parts', {'RAM Tipi': 'DDR4'}, 'DDR5 RAM compatible board', part_kind='motherboard'))
        self.assertFalse(mismatched_spec_table('monitor', {'Screen Size': '19 inç'}, 'Studio 24–27 inç'))
        self.assertFalse(mismatched_spec_table('monitor', {'Screen Size': '19 inç'}, 'Studio Monitor'))
        self.assertFalse(mismatched_spec_table('monitor', {'Screen Size': '75 cm'}, 'Acme 32"'))
        self.assertFalse(mismatched_spec_table('monitor', {'Tepki Süresi': '8 ms'}, 'Acme 75" 1 ms MPRT'))

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

    def test_diyalog_rejects_a_conflicting_barcode_in_the_selected_table(self):
        url = f'https://{DIYALOG}/bilesenler/prd-ornek'
        graph = {'@graph': [{'@type': 'Product', 'url': url, 'name': 'Acme Fan', 'brand': 'Acme', 'gtin13': GTIN}]}
        page = f'''<link rel="canonical" href="{url}"><script type="application/ld+json">{json.dumps(graph)}</script>
            <div class="product_meta"><span>Barkodu: <span>{GTIN}</span></span></div>
            <div class="woocommerce-Tabs-panel"><table><tr><td>EAN / Barcode</td><td>{GTIN} (Ürün)</td></tr>
            <tr><td>Fan Hızı</td><td>1200 RPM</td></tr></table></div>'''
        self.assertEqual(selected_diyalog_specs(page.encode(), url, GTIN)['specs']['Fan Hızı'], '1200 RPM')
        self.assertIsNone(selected_diyalog_specs(page.replace(f'{GTIN} (Ürün)', OTHER + ' (Ürün)').encode(), url, GTIN))
        self.assertIsNone(selected_diyalog_specs(page.replace('Barkodu:', 'Mağaza Kodu:').encode(), url, GTIN))

    def test_octo_product_microdata_and_legacy_encoding(self):
        url = f'https://{OCTO}/Example-Fan::123.html'
        raw = f'''<html><head><meta charset="iso-8859-15"><link rel="canonical" href="{url}"></head>
            <div itemtype="https://schema.org/Product"><h1 itemprop="name">Beispiel Lüfter</h1>
            <span itemprop="gtin13">{GTIN}</span><span itemprop="brand"><span itemprop="name">Acme</span></span>
            <div><div class="ITSn">Geräuschpegel</div><div class="ITSv">37,6 dBA</div></div></div></html>'''
        data = selected_octo_specs(raw.encode('iso-8859-15'), url, GTIN)
        self.assertEqual(data['specs'], {'Geräuschpegel': '37,6 dBA'})
        self.assertIsNone(selected_octo_specs(raw.encode('iso-8859-15'), url, OTHER))
        self.assertIsNone(selected_octo_specs(raw.encode('iso-8859-15'), url.replace('123', '124'), GTIN))

    def test_search_cards_only_lead_to_selected_product_identity(self):
        amazon = b'<div data-component-type="s-search-result" data-asin="B00EXAMPLE"><h2>Acme Mouse</h2></div>'
        self.assertEqual(candidates(AMAZON, amazon), [f'https://{AMAZON}/dp/B00EXAMPLE'])
        html = b'<a href="/urun/acme-mouse-1234567"><h2 class="product-item-title">Acme Mouse</h2></a>'
        transport = Mock()
        transport.get.side_effect = [(f'https://{N11}/arama?q={GTIN}', html), (f'https://{N11}/urun/acme-mouse-1234567', b'<h1>Acme Mouse</h1>')]
        self.assertIsNone(verify(N11, GTIN, 'mouse', transport))
