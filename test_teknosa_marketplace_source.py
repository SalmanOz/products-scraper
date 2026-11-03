from copy import deepcopy
from pathlib import Path
import unittest

from teknosa_marketplace_source import parse_selected_marketplace
from teknosa_reviewed_source import TeknosaCatalogSource
from retailer_offer_refresh import RetailerOfferRefresher
from update_prices import PriceUpdater

URL = 'https://www.teknosa.com/samsung-galaxy-a26-6128gb-akilli-telefon-beyaz-p-125070166'
NAME = 'Samsung Galaxy A26 5G'
SPECS = {'ram_gb': 6, 'storage_gb': 128, 'network_generation': '5g'}
PROFILE = {'name': NAME, 'specs': SPECS}


def document():
    return Path(__file__).with_name('fixtures').joinpath('teknosa-marketplace-selected.html').read_text()


class TeknosaMarketplaceTests(unittest.TestCase):
    def test_observed_marketplace_layout_retains_actual_shop_cash_and_raw_heading(self):
        offer, reason = parse_selected_marketplace(document(), URL, PROFILE)
        self.assertEqual(reason, 'selected_teknosa_marketplace_cash_offer')
        self.assertEqual((offer['merchant'], offer['merchant_id'], offer['price']),
                         ('BittiBitiyor', '2014', 19099))
        self.assertEqual(offer['source_title'], 'SAMSUNG GALAXY A26_ 6_128GB AKILLI TELEFON BEYAZ')
        self.assertEqual((offer['gtin13'], offer['listing_id']), ('8806097100676', '1978045'))

    def test_live_reader_complete_collision_gate_and_writer_use_the_observed_shop(self):
        class Transport:
            def read_document(self, url):
                return document(), 'document_read', 200
        reader = RetailerOfferRefresher(teknosa_catalog_source=TeknosaCatalogSource(transport=Transport()))
        offers = reader.refresh_existing_offer_targets(NAME, [{'source_url': URL}], SPECS)
        self.assertEqual(len(offers), 1)
        updater = PriceUpdater(ingestion_client=object(), offer_refresher=reader)
        product = {'id': 999, 'slug': 'samsung-galaxy-a26-5g', 'name': NAME,
                   'offer_targets': [{'source_url': URL, 'merchant_name': 'Teknosa'}],
                   'offer_targets_truncated': False}
        self.assertTrue(updater.live_offer_is_valid(product, offers[0], SPECS))
        rows = updater.prepare_product_offers(product, offers, updater.utc_timestamp(), updater.utc_timestamp())
        self.assertEqual(rows[0]['seller'], 'BittiBitiyor')
        product['offer_targets'].append({'merchant_name': 'BittiBitiyor',
                                         'source_url': 'https://www.mediamarkt.com.tr/tr/product/_other-123456.html'})
        self.assertFalse(updater.live_offer_is_valid(product, offers[0], SPECS))
        product['offer_targets'] = []
        product['offer_targets_truncated'] = True
        self.assertFalse(updater.live_offer_is_valid(product, offers[0], SPECS))

    def test_each_independent_shop_price_stock_identity_signal_fails_closed(self):
        changes = [
            ('data-product-shop-id="2014"', 'data-product-shop-id="9999"'),
            ('data-shop-name="BittiBitiyor"', 'data-shop-name="Other Shop"'),
            ('data-price-with-discount="19099.00000000"', 'data-price-with-discount="18999"'),
            ('data-product-id="125070166"', 'data-product-id="125070167"'),
            ('data-product-ean="8806097100676"', 'data-product-ean="8806097100677"'),
            ('"unit_sale_price":19099.00000000', '"unit_sale_price":1'),
            ('"availability": "https://schema.org/InStock"', '"availability": "https://schema.org/OutOfStock"'),
            ('"name": "BittiBitiyor"', '"name": "Other Shop"'),
            ('iş günü içinde kargoda', 'stok tükendi'),
            ('id="addToCartButton"', 'disabled id="addToCartButton"'),
            ('SAMSUNG GALAXY A26_ 6_128GB', 'SAMSUNG GALAXY A26_ 8_256GB'),
        ]
        for before, after in changes:
            with self.subTest(before=before):
                self.assertIn(before, document())
                self.assertIsNone(parse_selected_marketplace(document().replace(before, after), URL, PROFILE)[0])
        self.assertIsNone(parse_selected_marketplace(document(), URL + '?shopId=2014', PROFILE)[0])
        for key, value in (('ram_gb', 8), ('storage_gb', 256)):
            profile = deepcopy(PROFILE)
            profile['specs'][key] = value
            self.assertIsNone(parse_selected_marketplace(document(), URL, profile)[0])


if __name__ == '__main__':
    unittest.main()
