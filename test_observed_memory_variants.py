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
            {p + ('attributes', 0, 'value', 'name'): 'Yok'},
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
