import unittest

from test_trendyol_catalog_source import document
from tws_offer_pilot import MODEL_NAMES, model_name, selected_tws_offer


class TwsOfferPilotTests(unittest.TestCase):
    def test_selected_tws_proof_requires_expected_model_category_barcode_and_merchant(self):
        canonical = 'https://www.trendyol.com/samsung/galaxy-buds-core-siyah-p-676507062'
        url = canonical + '?merchantId=657799'
        title = 'Samsung Galaxy Buds Core Bluetooth Kulak İçi Kulaklık Siyah'
        changes = {
            ('title',): title,
            ('state', 'product', 'name'): title.removeprefix('Samsung '),
            ('state', 'product', 'category'): {'id': 1058, 'name': 'Kulak içi TWS Bluetooth Kulaklık'},
            ('schema', '@id'): canonical,
            ('schema', 'offers', 'url'): canonical,
            ('schema', 'name'): title,
        }
        model = 'Samsung Galaxy Buds Core'
        offer, _ = selected_tws_offer(document(changes), url, model)
        self.assertEqual((offer['gtin13'], offer['item_number'], offer['merchant_id'], offer['merchant']),
                         ('8806094821437', 894997311, 657799, 'Test Satıcı'))
        for mutation in (
            {('state', 'product', 'category'): {'id': 200, 'name': 'Kulak içi TWS Bluetooth Kulaklık'}},
            {('state', 'product', 'merchantListing', 'winnerVariant', 'barcode'): '8806094821438'},
            {('state', 'product', 'merchantListing', 'merchant', 'id'): 657798},
            {('schema', 'offers', 'itemCondition'): 'https://schema.org/RefurbishedCondition'},
        ):
            with self.subTest(mutation=mutation):
                self.assertIsNone(selected_tws_offer(document({**changes, **mutation}), url, model)[0])
        self.assertIsNone(selected_tws_offer(document(changes), url, 'Samsung Galaxy Buds4 Pro')[0])

    def test_distinct_models_and_airpods_variants(self):
        self.assertEqual(len(MODEL_NAMES), len(set(MODEL_NAMES)))
        self.assertEqual(len(MODEL_NAMES), 25)
        self.assertEqual(model_name('Xiaomi Redmi Buds 8 Lite Siyah', 'Xiaomi'), 'Xiaomi Redmi Buds 8 Lite')
        self.assertEqual(model_name('Apple AirPods 4 Aktif Gürültü Engelleme MXP93TU/A', 'Apple'), 'Apple AirPods 4 ANC')
        self.assertIsNone(model_name('Apple AirPods 4. Nesil (MXP63TU/A) Yenilenmiş', 'Apple'))
        self.assertIsNone(model_name('AirPods 4. Nesil (MXP63TU/A)', 'TARÇIN'))


if __name__ == '__main__':
    unittest.main()
