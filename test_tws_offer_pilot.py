import unittest

from test_trendyol_catalog_source import document
from tws_offer_pilot import MODEL_NAMES, PILOT_MODEL_NAMES, independent_sellers, model_name, select_sku_offers, selected_tws_offer


class TwsOfferPilotTests(unittest.TestCase):
    def test_selected_tws_proof_requires_expected_model_category_barcode_and_merchant(self):
        canonical = 'https://www.trendyol.com/samsung/galaxy-buds-core-siyah-p-676507062'
        url = canonical + '?merchantId=657799'
        title = 'Samsung Galaxy Buds Core Bluetooth Kulak İçi Kulaklık Siyah'
        changes = {
            ('title',): title,
            ('state', 'product', 'name'): title.removeprefix('Samsung '),
            ('state', 'product', 'category'): {'id': 1058, 'name': 'Kulak içi TWS Bluetooth Kulaklık'},
            ('state', 'product', 'merchantListing', 'otherMerchants'): [
                {'id': 29, 'url': canonical.removeprefix('https://www.trendyol.com') + '?boutiqueId=61&merchantId=29&sav=true',
                 'variants': [{'itemNumber': 894997311, 'barcode': '8806094821437',
                               'inStock': True, 'sellable': True}]},
                {'id': 30, 'url': canonical.removeprefix('https://www.trendyol.com') + '?merchantId=30',
                 'variants': [{'itemNumber': 894997311, 'barcode': '8806094821438',
                               'inStock': True, 'sellable': True}]},
            ],
            ('schema', '@id'): canonical,
            ('schema', 'offers', 'url'): canonical,
            ('schema', 'name'): title,
        }
        model = 'Samsung Galaxy Buds Core'
        hints = []
        offer, _ = selected_tws_offer(document(changes), url, model, seller_hints=hints)
        self.assertEqual((offer['gtin13'], offer['item_number'], offer['merchant_id'], offer['merchant']),
                         ('8806094821437', 894997311, 657799, 'Test Satıcı'))
        self.assertEqual(hints, [(canonical + '?boutiqueId=61&merchantId=29', 29)])
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
        self.assertEqual(len(PILOT_MODEL_NAMES), 25)
        self.assertGreater(len(MODEL_NAMES), len(PILOT_MODEL_NAMES))
        self.assertEqual(model_name('Xiaomi Redmi Buds 8 Lite Siyah', 'Xiaomi'), 'Xiaomi Redmi Buds 8 Lite')
        self.assertEqual(model_name('Apple AirPods 4 Aktif Gürültü Engelleme MXP93TU/A', 'Apple'), 'Apple AirPods 4 ANC')
        self.assertIsNone(model_name('Apple AirPods 4. Nesil (MXP63TU/A) Yenilenmiş', 'Apple'))
        self.assertIsNone(model_name('AirPods 4. Nesil (MXP63TU/A)', 'TARÇIN'))
        self.assertEqual(model_name('HUAWEI FreeBuds 6 Bluetooth Kulaklık Beyaz', 'Huawei'),
                         'Huawei FreeBuds 6')
        self.assertEqual(model_name('HUAWEI FreeBuds 6i Bluetooth Kulaklık Beyaz', 'Huawei'),
                         'Huawei FreeBuds 6i')
        self.assertEqual(model_name('JBL Wave Flex2 TWS Pembe', 'JBL'), 'JBL Wave Flex 2')
        self.assertEqual(model_name('JBL Wave Buds Bluetooth Siyah', 'JBL'), 'JBL Wave Buds')
        self.assertEqual(model_name('JBL Wave Buds2 Siyah', 'JBL'), 'JBL Wave Buds 2')
        self.assertEqual(model_name('Soundcore P20i Kulak İçi Kulaklık', 'soundcore'), 'Anker Soundcore P20i')
        self.assertIsNone(model_name('JBL Tune Flex 2 NC Ghost Edition Siyah', 'JBL'))
        self.assertIsNone(model_name('JBL Wave Flex 2 Yedek Şarj Kutusu', 'JBL'))

    def test_pair_requires_same_gtin_and_two_legal_identities(self):
        first = {'platform': 'Vatan Bilgisayar', 'gtin13': '6941812791271', 'price': 800,
                 'url': 'https://vatan.example/white', '_legal_name': 'vatan bilgisayar'}
        duplicate = {**first, 'platform': 'Trendyol', 'merchant_id': 10, 'url': 'https://ty.example/white',
                     'price': 790, '_legal_name': 'vatan bilgisayar sanayi ve ticaret anonim sirketi'}
        cheaper = {**duplicate, 'merchant_id': 11, 'gtin13': '6941812791240',
                   'price': 730, '_legal_name': 'ayri satici'}
        matching = {**cheaper, 'platform': 'n11', 'gtin13': first['gtin13'],
                    'price': 810, 'url': 'https://n11.example/white'}
        self.assertFalse(independent_sellers(first, duplicate))
        self.assertEqual(select_sku_offers([first, duplicate, cheaper, matching]), [duplicate, matching])
        self.assertEqual(select_sku_offers([first, duplicate, cheaper]), [cheaper])
        self.assertFalse(independent_sellers({**first, '_tax_key': '1111111111'},
                                             {**matching, '_tax_key': '1111111111'}))
        third = {**matching, 'merchant_id': 27, '_legal_name': 'ucuncu sirket',
                 'price': 900, 'url': 'https://n11.example/third'}
        self.assertEqual(select_sku_offers([first, duplicate, cheaper, matching, third]),
                         [duplicate, matching, third])


if __name__ == '__main__':
    unittest.main()
