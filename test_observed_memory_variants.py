from copy import deepcopy
import unittest
from unittest.mock import patch

from audit_priority_retailers import inspect_target
from observed_memory_variants import inspect_trendyol_memory_variant, inspect_hepsiburada_memory_variant
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
