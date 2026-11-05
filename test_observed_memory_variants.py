from copy import deepcopy
import unittest
from unittest.mock import patch

from audit_priority_retailers import inspect_target
from observed_memory_variants import (inspect_trendyol_memory_variant, inspect_hepsiburada_memory_variant,
                                     inspect_n11_memory_variant, inspect_teknosa_memory_variant)
from sweep_priority_catalog import sweep, coverage_readback
from test_trendyol_catalog_source import document, URL, NAME, SPECS


ALT_URL = URL.replace('128-gb-8-gb', '256-gb-8-gb')
ALT_TITLE = NAME + ' 256 GB 8 GB RAM Grafit'


def alternate_document(changes=None):
    base = {('title',): ALT_TITLE, ('state', 'product', 'name'): ALT_TITLE.removeprefix('Samsung '),
            ('schema', 'name'): ALT_TITLE,
            ('state', 'product', 'attributes', 1, 'value', 'name'): '256 GB'}
    return document({**base, **(changes or {})}).replace(URL.split('?')[0], ALT_URL.split('?')[0])


class ObservedMemoryVariantTests(unittest.TestCase):
    def test_trendyol_other_memory_diagnostics_explain_stock_radio_and_later_proof_rejections(self):
        parent = {'ram_gb': 8, 'storage_gb': 128, 'network_generation': '5g'}
        original = deepcopy(parent)
        product = {'name': NAME, 'slug': 'samsung-galaxy-a54-5g', 'attributes': parent}
        for changes, expected_status, expected_reason in [
            ({('state', 'product', 'inStock'): False}, 'selected_product_unavailable', None),
            ({('state', 'product', 'attributes', 2, 'value', 'name'): '4.5G'}, 'selected_radio_mismatch', None),
            ({('schema', 'offers', 'price'): '1.00'}, 'selected_variant_parse_rejected',
             'schema_selected_offer_mismatch'),
        ]:
            with self.subTest(stage=expected_status):
                html = alternate_document({**changes, ('state', 'unrelated_private_account'): 'must-not-appear'})
                diagnostic = {}
                self.assertIsNone(inspect_trendyol_memory_variant(html, ALT_URL, NAME, parent, diagnostic))
                self.assertEqual(diagnostic['status'], expected_status)
                self.assertFalse(diagnostic['writes'])
                self.assertEqual(diagnostic['scope'], 'diagnostic_only_not_offer_evidence')
                self.assertEqual(diagnostic.get('reader_reason'), expected_reason)
                self.assertNotIn('must-not-appear', str(diagnostic))
                if expected_status != 'selected_radio_mismatch':
                    class Source:
                        def read_document(self, url): return html, 'document_read', 200
                        def resolved_url(self, url): return url
                    row = inspect_target(Source(), ALT_URL, product)
                    self.assertIsNone(row['prepared_offer'])
                    self.assertNotIn('other_memory_variant', row)
                    self.assertEqual(row['other_memory_variant_diagnostic'], diagnostic)
        diagnostic = {}
        preview = inspect_trendyol_memory_variant(alternate_document(), ALT_URL, NAME, parent, diagnostic)
        self.assertEqual(diagnostic['status'], 'selected_variant_verified')
        self.assertEqual(diagnostic['observed_specs'], {**parent, 'storage_gb': 256})
        self.assertFalse(preview['writes'])
        self.assertEqual(parent, original)

    def test_native_unknown_ram_field_discovers_explicit_other_physical_ram_without_changing_parent(self):
        from pathlib import Path
        html = Path('fixtures/trendyol-a16-6gb-selected-unknown-ram-property.html').read_text()
        url = 'https://www.trendyol.com/samsung/galaxy-a16-128-gb-6-gb-ram-gri-p-879171111'
        parent = {'ram_gb': 4, 'storage_gb': 128, 'network_generation': '4g'}
        before = deepcopy(parent)
        result = inspect_trendyol_memory_variant(html, url, 'Samsung Galaxy A16 4G', parent)
        self.assertEqual(result['observed_specs'], {**parent, 'ram_gb': 6})
        self.assertEqual(result['catalog_memory'], {'ram_gb': 4, 'storage_gb': 128})
        self.assertFalse(result['writes'])
        self.assertEqual(parent, before)
        self.assertIsNone(inspect_trendyol_memory_variant(html, url, 'Samsung Galaxy A16 4G', {**parent, 'ram_gb': 6}))
        for changed in [html.replace('6 GB Ram', '6+6 GB Sanal RAM'), html.replace('6 GB Ram', ''),
                        html.replace('"name": "Yok"', '"name": "Belirtilmemiş"')]:
            self.assertIsNone(inspect_trendyol_memory_variant(changed, url, 'Samsung Galaxy A16 4G', parent))

    def test_native_tb_variant_preview_proves_its_own_capacity_and_keeps_parent_unchanged(self):
        from pathlib import Path
        html = Path('fixtures/trendyol-iphone17pro-native-tb-selected.html').read_text()
        url = 'https://www.trendyol.com/apple/iphone-17-pro-1tb-abis-p-985256817'
        parent = {'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g'}
        before = deepcopy(parent)
        result = inspect_trendyol_memory_variant(html, url, 'Apple iPhone 17 Pro', parent)
        self.assertFalse(result['writes'])
        self.assertEqual(result['observed_specs'], {**parent, 'storage_gb': 1024})
        self.assertEqual(result['catalog_memory'], {'ram_gb': 12, 'storage_gb': 256})
        self.assertEqual(parent, before)
        self.assertIsNone(inspect_trendyol_memory_variant(html, url, 'Apple iPhone 17 Pro',
                          {**parent, 'storage_gb': 1024}))
        self.assertIsNone(inspect_trendyol_memory_variant(html, url, 'Apple iPhone 17 Pro',
                          {**parent, 'network_generation': '4g'}))
        samsung = Path('fixtures/trendyol-s25ultra-native-tb-selected.html').read_text()
        self.assertIsNone(inspect_trendyol_memory_variant(samsung,
            'https://www.trendyol.com/samsung/galaxy-s25-ultra-5g-12gb-1tb-akilli-telefon-titanyum-gumus-p-889950716',
            'Samsung Galaxy S25 Ultra', parent))

    def test_observed_missing_first_g_cards_are_only_other_capacity_routing_hints(self):
        from pathlib import Path
        from bs4 import BeautifulSoup
        from automatic_memory_variants import memory_variant_destinations
        from trendyol_catalog_discovery import parse_category_document
        from selected_sku_evidence import selected_capacity_title
        url='https://www.trendyol.com/xiaomi-android-cep-telefonu-x-b101939-c164461'
        html=Path('fixtures/trendyol-xiaomi-public-second-page-routing.html').read_text()
        cards,_=parse_category_document(html,url+'?pi=2')
        parent={'id':270,'slug':'xiaomi-17t-pro','name':'Xiaomi 17T Pro',
                'attributes':{'ram_gb':12,'storage_gb':512,'Connectivity':{'5G':True}}}
        hints=memory_variant_destinations(cards,[parent])
        self.assertEqual(len(hints),1)
        self.assertIn('12+256G',hints[0][1]['discovery_title'])
        self.assertNotIn('price',hints[0][1])
        self.assertNotIn('source_variant_evidence',hints[0][1])
        raw='Xiaomi 17T Pro 12+256G Mor (Xiaomi Türkiye Garantili)'
        self.assertEqual(selected_capacity_title(raw,None),raw)
        self.assertEqual(selected_capacity_title(raw,{'ram_gb':8,'storage_gb':256}),raw)
        self.assertEqual(selected_capacity_title(raw,{'ram_gb':12,'storage_gb':512}),raw)
        self.assertIn('RAM 12 GB; ROM 256 GB',selected_capacity_title(raw,{'ram_gb':12,'storage_gb':256}))
        for bad in ['Xiaomi 17T Pro RAM 12+256G','Xiaomi 17T Pro 12+256G RAM',
                    'Xiaomi 17T Pro 12+256G sanal RAM','Xiaomi 17T Pro 12+256G 8G RAM',
                    'Xiaomi 17T Pro 12+256G 12+512G']:
            self.assertEqual(selected_capacity_title(bad,{'ram_gb':12,'storage_gb':256}),bad)

    def test_n11_rejected_other_memory_reports_only_bounded_source_stage_without_authorizing_an_offer(self):
        from test_n11_catalog_source import document as n11_document, URL as n11_url, NAME as n11_name
        import json
        parent={'ram_gb':6,'storage_gb':128,'network_generation':'5g'}
        def changed(state,soup):
            state['unrelated_private_account']='must-not-appear'
            node=soup.select_one('script[type="application/ld+json"]')
            schema=json.loads(node.get_text());schema['offers']['price']='1.00';node.string=json.dumps(schema)
        diagnostic={}
        self.assertIsNone(inspect_n11_memory_variant(n11_document(changed),n11_url,n11_name,parent,diagnostic))
        self.assertEqual(diagnostic,{'status':'selected_variant_parse_rejected','writes':False,
            'scope':'diagnostic_only_not_offer_evidence','observed_specs':{'ram_gb':8,'storage_gb':256,'network_generation':'5g'},
            'reader_reason':'schema_selected_cash_offer_mismatch'})
        product={'name':n11_name,'slug':'samsung-galaxy-a57','attributes':parent}
        class Source:
            def read_document(self,url): return n11_document(changed),'document_read',200
            def resolved_url(self,url): return url
        row=inspect_target(Source(),n11_url,product)
        self.assertIsNone(row['prepared_offer'])
        self.assertNotIn('other_memory_variant',row)
        self.assertEqual(row['other_memory_variant_diagnostic'],diagnostic)
        self.assertNotIn('must-not-appear',str(row))

    def test_teknosa_omitted_heading_ram_uses_selected_table_for_separate_memory_discovery(self):
        from pathlib import Path
        from automatic_memory_variants import observed_variant_candidates, variant_selected_evidence
        html = Path(__file__).with_name('fixtures').joinpath('teknosa-selected-s25-ultra-512-physical-table.html').read_text()
        url = 'https://www.teknosa.com/samsung-galaxy-s25-ultra-5g-512gb-titanyum-gumus-akilli-telefon-p-125079845'
        parent = {'id': 99, 'slug': 'samsung-galaxy-s25-ultra', 'name': 'Samsung Galaxy S25 Ultra',
                  'attributes': {'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g'}}
        before = deepcopy(parent)
        class Transport:
            def read_document(self, target):
                assert target == url
                return html, 'document_read', 200
        row = inspect_target(Transport(), url, parent)
        self.assertIsNone(row['prepared_offer'])
        self.assertEqual(row['reader_reason'], 'catalog_variant_mismatch')
        preview = row['other_memory_variant']
        self.assertFalse(preview['writes'])
        self.assertEqual(preview['observed_specs'], {'ram_gb': 12, 'storage_gb': 512, 'network_generation': '5g'})
        self.assertEqual(preview['offer']['source_title'],
                         'Samsung Galaxy S25 Ultra 5G 512GB Titanyum Gümüş Akıllı Telefon')
        proof = variant_selected_evidence(preview['offer'], parent['name'], preview['observed_specs'])
        self.assertEqual((proof['sku'], proof['merchant_name'], proof['storage_gb']),
                         ('125079845', 'hepsi10line', 512))
        for fields in ({'selected_teknosa_physical_specs_verified': False},
                       {'selected_teknosa_variant_verified': False}, {'product_id': None},
                       {'ram_gb': 8}, {'storage_gb': 256}, {'network_generation': '4g'},
                       {'merchant_id': '1001'}, {'gtin13': '8806097015155'}):
            self.assertIsNone(variant_selected_evidence({**preview['offer'], **fields},
                parent['name'], preview['observed_specs']), fields)
        candidates = observed_variant_candidates({'rows': [row]}, [parent])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['targets'], [url])
        self.assertEqual(parent, before)
        for name, specs in [('Samsung Galaxy S25', parent['attributes']),
                            (parent['name'], {**parent['attributes'], 'network_generation': '4g'}),
                            (parent['name'], {**parent['attributes'], 'storage_gb': 512}),
                            (parent['name'], {'ram_gb': 12, 'storage_gb': 256})]:
            self.assertIsNone(inspect_teknosa_memory_variant(html, url, name, specs))

    def test_teknosa_table_memory_discovery_requires_complete_selected_and_commercial_proof(self):
        from pathlib import Path
        from bs4 import BeautifulSoup
        html = Path(__file__).with_name('fixtures').joinpath('teknosa-selected-s25-ultra-512-physical-table.html').read_text()
        url = 'https://www.teknosa.com/samsung-galaxy-s25-ultra-5g-512gb-titanyum-gumus-akilli-telefon-p-125079845'
        specs = {'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g'}
        for label, mutate in [
            ('missing table', lambda s: s.select_one('#pdp-technical').decompose()),
            ('duplicate table', lambda s: s.select_one('#pdp-technical').insert_after(deepcopy(s.select_one('#pdp-technical')))),
            ('unrelated table', lambda s: s.body.append(s.select_one('#pdp-technical').extract())),
            ('wrong selected seller', lambda s: s.select_one('#addToCartButton').__setitem__('data-shop-name', 'Other')),
            ('invalid barcode', lambda s: s.select_one('#addToCartButton').__setitem__('data-product-ean', '8806097015155')),
            ('unavailable', lambda s: s.select_one('#addToCartButton').__setitem__('aria-disabled', 'true')),
            ('cash disagreement', lambda s: setattr(s.select_one('.prc-third'), 'string', '1 TL')),
        ]:
            with self.subTest(label=label):
                soup = BeautifulSoup(html, 'html.parser'); mutate(soup)
                self.assertIsNone(inspect_teknosa_memory_variant(str(soup), url, 'Samsung Galaxy S25 Ultra', specs))
        for title in ('Samsung Galaxy S25 Ultra 5G 8 GB RAM 512 GB',
                      'Samsung Galaxy S25 Ultra 4G 512GB',
                      'Samsung Galaxy S25 Ultra 5G 12+12 GB RAM 512 GB',
                      'Samsung Galaxy S25 Ultra 5G 256GB'):
            soup = BeautifulSoup(html, 'html.parser')
            soup.select_one('#addToCartButton')['data-product-name'] = title
            self.assertIsNone(inspect_teknosa_memory_variant(str(soup), url, 'Samsung Galaxy S25 Ultra', specs), title)

    def test_teknosa_selected_first_party_unitless_pair_never_prices_the_original_memory(self):
        from pathlib import Path
        from bs4 import BeautifulSoup
        from teknosa_reviewed_source import parse_exact_first_party_offer
        from automatic_memory_variants import variant_selected_evidence
        html = Path(__file__).with_name('fixtures').joinpath('teknosa-selected-unitless-memory-first-party.html').read_text()
        url = 'https://www.teknosa.com/oppo-reno15-pro-5g-12512-alacakaranlik-kahvesi-akilli-telefon-p-100000060715'
        model = 'OPPO Reno15 Pro 5G'
        original = {'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g'}
        before = deepcopy(original)
        self.assertIsNone(parse_exact_first_party_offer(html, url, {'name': model, 'specs': original})[0])
        preview = inspect_teknosa_memory_variant(html, url, model, original)
        self.assertIsNotNone(preview)
        self.assertEqual(preview['observed_specs'], {**original, 'storage_gb': 512})
        self.assertFalse(preview['writes'])
        self.assertEqual(original, before)
        self.assertEqual((preview['offer']['merchant'], preview['offer']['merchant_id'],
                          preview['offer']['gtin13']), ('Teknosa', '1001', '6932169396011'))
        proof = variant_selected_evidence(preview['offer'], model, preview['observed_specs'])
        self.assertEqual((proof['ram_gb'], proof['storage_gb']), (12, 512))
        for key, value in [('data-product-ean', '6932169396012'), ('data-shop-name', 'Other Seller'),
                           ('data-product-stock', 'N'), ('data-product-price', '1')]:
            bad = BeautifulSoup(html, 'html.parser')
            bad.select_one('button#addToCartButton')[key] = value
            self.assertIsNone(inspect_teknosa_memory_variant(str(bad), url, model, original), key)
        for changed in [url.replace('12512', '12256'), url + '?shopId=1001',
                        url.replace('www.teknosa.com', 'evil.example')]:
            self.assertIsNone(inspect_teknosa_memory_variant(html, changed, model, original), changed)
        self.assertIsNone(variant_selected_evidence({**preview['offer'],
            'selected_teknosa_first_party_verified': False}, model, preview['observed_specs']))

    def test_unitless_teknosa_pair_requires_the_complete_model_before_it_and_no_capacity_choices(self):
        from tr_price_scraper import TRPriceScraper
        matcher = TRPriceScraper()
        url = 'https://www.teknosa.com/oppo-reno15-pro-5g-12512-akilli-telefon-p-100000060715'
        title = 'Oppo Reno15 Pro 5G 12/512 Akıllı Telefon'
        self.assertEqual(matcher.teknosa_shorthand_capacities('OPPO Reno15 Pro 5G', title, url), (12, 512))
        for model, raw in [('OPPO Reno15 Pro 5G', title.replace('12/512', value)) for value in (
            '12/512/1024', '8/256 12/512', '8+12/512', '12/512+8', '12/512 64GB',
            '12/512 sanal RAM', '12/512 256', '12/512 veya', '512', '12/512GB')]:
            self.assertIsNone(matcher.teknosa_shorthand_capacities(model, raw, url), raw)
        self.assertIsNone(matcher.teknosa_shorthand_capacities('Honor 12', 'Honor 12/512 Akıllı Telefon',
            'https://www.teknosa.com/honor-12512-akilli-telefon-p-100000060715'))
        self.assertIsNone(matcher.teknosa_shorthand_capacities('OPPO Reno15 Pro 5G', title.replace('Pro', ''), url))

    def test_teknosa_real_selected_cart_recovers_memory_without_copying_parent_facts(self):
        from pathlib import Path
        from bs4 import BeautifulSoup
        html = Path(__file__).with_name('fixtures').joinpath('teknosa-selected-brand-rendering.html').read_text()
        url = ('https://www.teknosa.com/poco-x6-pro-5g-8-gb-ram-256-gb-siyah-cep-telefonu-'
               'poco-turkiye-garantili-p-780010584')
        parent = {'ram_gb': 12, 'storage_gb': 512, 'network_generation': '5g'}
        before = deepcopy(parent)
        preview = inspect_teknosa_memory_variant(html, url, 'POCO X6 Pro 12GB 512GB', parent)
        self.assertIsNotNone(preview)
        self.assertEqual(preview['observed_specs'], {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'})
        self.assertEqual((preview['offer']['merchant'], preview['offer']['price'], preview['offer']['gtin13']),
                         ('BAŞARAN İLETİŞİM', 39999, '6941812757994'))
        self.assertFalse(preview['writes'])
        self.assertEqual(parent, before)
        from automatic_memory_variants import observed_variant_candidates, variant_selected_evidence
        proof = variant_selected_evidence(preview['offer'], 'POCO X6 Pro', preview['observed_specs'])
        self.assertEqual((proof['platform'], proof['merchant_name'], proof['ram_gb'], proof['storage_gb']),
                         ('Teknosa', 'BAŞARAN İLETİŞİM', 8, 256))
        product = {'id': 999, 'slug': 'poco-x6-pro', 'name': 'POCO X6 Pro', 'attributes': parent}
        candidates = observed_variant_candidates({'rows': [{'slug': product['slug'], 'other_memory_variant': preview}]}, [product])
        self.assertEqual(candidates[0]['targets'], [url])
        for change in ({'merchant_id': '1001'}, {'listing_id': None}, {'gtin13': '6941812757995'},
                       {'selected_teknosa_variant_verified': False}, {'url': url + '?shopId=1234'}):
            self.assertIsNone(variant_selected_evidence({**preview['offer'], **change}, 'POCO X6 Pro', preview['observed_specs']))
        for value in ('8+8 GB RAM 256 GB', 'RAM 6/8 GB 256 GB', '8 GB RAM 128/256 GB',
                      '256 GB', '8 GB RAM', '8 GB RAM 256 GB 4G 5G'):
            soup = BeautifulSoup(html, 'html.parser')
            soup.select_one('#pdp-main button#addToCartButton')['data-product-name'] = 'Poco X6 Pro ' + value
            self.assertIsNone(inspect_teknosa_memory_variant(str(soup), url, 'POCO X6 Pro', parent), value)
        for selector, attribute, value in [
            ('button#addToCartButton', 'data-product-ean', '6941812757995'),
            ('button#addToCartButton', 'data-shop-name', 'Other seller'),
            ('button#addToCartButton', 'aria-disabled', 'true'),
        ]:
            soup = BeautifulSoup(html, 'html.parser')
            soup.select_one('#pdp-main ' + selector)[attribute] = value
            self.assertIsNone(inspect_teknosa_memory_variant(str(soup), url, 'POCO X6 Pro', parent))
        for name, specs in [('POCO X6', parent), ('POCO X6 Pro', dict(parent, network_generation='4g')),
                            ('POCO X6 Pro', dict(parent, ram_gb=8, storage_gb=256)),
                            ('POCO X6 Pro', {'ram_gb': 12, 'storage_gb': 512})]:
            self.assertIsNone(inspect_teknosa_memory_variant(html, url, name, specs))
        self.assertIsNone(inspect_teknosa_memory_variant(html + html, url, 'POCO X6 Pro', parent))

    def test_teknosa_first_party_preview_is_separate_from_original_sku_offer(self):
        from test_teknosa_catalog_source import document as tk_document, TARGET, PROFILE
        parent = {'ram_gb': 6, 'storage_gb': 128, 'network_generation': '5g'}
        class Transport:
            def read_document(self, url):
                return '<div id="pdp-main">' + tk_document() + '</div>', 'document_read', 200
        product = {'name': PROFILE['name'], 'slug': 'samsung-galaxy-a36-5g', 'attributes': parent}
        row = inspect_target(Transport(), TARGET, product)
        self.assertIsNone(row['prepared_offer'])
        self.assertFalse(row['other_memory_variant']['writes'])
        self.assertEqual(row['other_memory_variant']['observed_specs'], dict(parent, ram_gb=8))
        self.assertEqual(row['other_memory_variant']['offer']['merchant'], 'Teknosa')
        from automatic_memory_variants import variant_selected_evidence
        preview = row['other_memory_variant']
        proof = variant_selected_evidence(preview['offer'], PROFILE['name'], preview['observed_specs'])
        self.assertEqual((proof['product_id'], proof['sku'], proof['listing_id'], proof['merchant_id']),
                         ('100000123456', '100000123456', '100000123456', '1001'))
        for change in ({'merchant': 'Other shop'}, {'merchant_id': '9999'}, {'listing_id': '9999'},
                       {'selected_teknosa_first_party_verified': False}, {'availability': 'out_of_stock'}):
            self.assertIsNone(variant_selected_evidence({**preview['offer'], **change}, PROFILE['name'], preview['observed_specs']))

    def test_n11_other_memory_requires_selected_sku_stock_gtin_seller_and_radio(self):
        from test_n11_catalog_source import document as n11_document, URL as n11_url, NAME as n11_name
        parent = {'ram_gb': 6, 'storage_gb': 128, 'network_generation': '5g'}
        before = deepcopy(parent)
        preview = inspect_n11_memory_variant(n11_document(), n11_url, n11_name, parent)
        self.assertIsNotNone(preview)
        self.assertEqual(preview['observed_specs'], dict(parent, ram_gb=8, storage_gb=256))
        self.assertEqual(parent, before)
        self.assertFalse(preview['writes'])
        for mutate in [lambda state, _s: state['product'].update(availableToBuy=False),
                       lambda state, _s: state['product'].update(defaultGtin='8806094821438'),
                       lambda state, _s: state['product']['seller'].update(nickName='Different seller'),
                       lambda state, _s: state['product'].update(defaultSkuId=123)]:
            self.assertIsNone(inspect_n11_memory_variant(n11_document(mutate), n11_url, n11_name, parent))
        self.assertIsNone(inspect_n11_memory_variant(n11_document(), n11_url, n11_name,
            dict(parent, network_generation='4g')))
        self.assertIsNone(inspect_n11_memory_variant(n11_document(), n11_url, n11_name,
            dict(parent, ram_gb=8, storage_gb=256)))

    def test_hb_other_ram_requires_the_full_ordinary_selected_buy_box_proof(self):
        from test_hepsiburada_catalog_source import document as hb_document, NAME as hb_name, TARGET
        parent = {'ram_gb': 6, 'storage_gb': 128, 'network_generation': '5g'}
        before = deepcopy(parent)
        preview = inspect_hepsiburada_memory_variant(hb_document(), TARGET, hb_name, parent)
        self.assertIsNotNone(preview)
        self.assertEqual(preview['observed_specs'], dict(parent, ram_gb=8))
        self.assertFalse(preview['writes'])
        self.assertEqual(parent, before)
        for old, new in [('RAM Kapasitesi <span>8 GB RAM', 'RAM Kapasitesi <span>8+8 GB RAM'),
                         ('Dahili Hafıza <span>128 GB', 'Dahili Hafıza <span>Var'),
                         ('Mobil Bağlantı Hızı <span>5G', 'Mobil Bağlantı Hızı <span>4G'),
                         ('RAM Kapasitesi <span>8 GB RAM', 'RAM Kapasitesi <span>8 GB RAM Kapasitesi 8 GB RAM')]:
            html = hb_document()
            self.assertIn(old, html)
            self.assertIsNone(inspect_hepsiburada_memory_variant(html.replace(old, new), TARGET, hb_name, parent))
        self.assertIsNone(inspect_hepsiburada_memory_variant(hb_document(), TARGET, hb_name.replace('A36', 'A35'), parent))
        self.assertIsNone(inspect_hepsiburada_memory_variant(hb_document(), TARGET, hb_name, dict(parent, ram_gb=8)))

    def test_other_capacity_requires_complete_selected_proof_and_never_changes_parent_specs(self):
        before = deepcopy(SPECS)
        result = inspect_trendyol_memory_variant(alternate_document(), ALT_URL, NAME, SPECS)
        self.assertEqual(result['scope'], 'other_memory_variant_preview_only')
        self.assertFalse(result['writes'])
        self.assertEqual(result['catalog_memory'], {'ram_gb': 8, 'storage_gb': 128})
        self.assertEqual(result['observed_specs'], dict(SPECS, storage_gb=256))
        self.assertEqual(result['offer']['source_title'], ALT_TITLE)
        self.assertEqual(SPECS, before)
        self.assertIsNone(inspect_trendyol_memory_variant(document(), URL, NAME, SPECS))
        self.assertIsNotNone(inspect_trendyol_memory_variant(alternate_document(), ALT_URL, NAME + ' 128GB', SPECS))

    def test_ambiguous_radio_memory_model_seller_stock_and_gtin_never_produce_a_variant_preview(self):
        p = ('state', 'product')
        winner = p + ('merchantListing', 'winnerVariant')
        for changes in [
            {p + ('attributes', 0, 'value', 'name'): 'Yok',
             ('title',): NAME + ' 256GB Grafit', ('schema', 'name'): NAME + ' 256GB Grafit',
             p + ('name',): (NAME + ' 256GB Grafit').removeprefix('Samsung ')},
            {p + ('attributes', 0, 'value', 'name'): '8+8 GB'},
            {p + ('attributes', 1, 'value', 'name'): '128/256 GB'},
            {p + ('attributes', 2, 'value', 'name'): '4.5G'},
            {p + ('attributes', 2, 'value', 'name'): 'Yok'},
            {p + ('id',): 123}, {p + ('inStock',): False},
            {winner + ('barcode',): '8806094821438'},
            {winner + ('inStock',): False},
            {p + ('merchantListing', 'merchant', 'id'): 123},
            {('visible_price',): '1 TL'}, {('cart_attributes',): 'disabled'},
            {('title',): NAME + ' 8GB Sanal RAM 256GB'},
            {('schema', 'name'): NAME + ' Plus 256GB'},
        ]:
            self.assertIsNone(inspect_trendyol_memory_variant(alternate_document(changes), ALT_URL, NAME, SPECS), changes)
        self.assertIsNone(inspect_trendyol_memory_variant(alternate_document(), ALT_URL, 'Samsung Galaxy A54',
            {'ram_gb': 8, 'storage_gb': 128}))  # The parent radio cannot be guessed.

    def test_publishing_sweep_keeps_the_other_memory_quote_in_preview_and_parent_unpriced(self):
        product = {'id': 999, 'name': NAME, 'slug': 'samsung-galaxy-a54-5g', 'attributes': dict(SPECS),
                   'offer_targets': [{'source_url': ALT_URL}], 'offer_targets_truncated': False}
        class Client:
            def get_published_products(self):
                return [deepcopy(product)]
            def get_public_price_priority(self):
                return {999: (product['slug'], False)}
        calls = []
        class Transport:
            def read_document(self, url):
                calls.append(url)
                return alternate_document(), 'document_read', 200
            def resolved_url(self, url):
                return url
            def report(self):
                return {}
        with patch('sweep_priority_catalog.refresh_candidates') as writer:
            result = sweep(client=Client(), transport=Transport(), publish=True)
        self.assertEqual(calls, [ALT_URL.split('?')[0]])
        self.assertIsNone(result['rows'][0]['prepared_offer'])
        self.assertEqual(result['rows'][0]['reader_reason'], 'selected_memory_mismatch')
        self.assertEqual(result['rows'][0]['other_memory_variant']['observed_specs']['storage_gb'], 256)
        self.assertEqual((result['accepted'], result['writes']), (0, False))
        self.assertEqual(coverage_readback(result, {999: (product['slug'], False)})['priced'], 0)
        writer.assert_not_called()


if __name__ == '__main__':
    unittest.main()
