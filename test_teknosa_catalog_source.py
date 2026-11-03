import unittest

from retailer_offer_refresh import RetailerOfferRefresher
from teknosa_reviewed_source import TeknosaCatalogSource, parse_exact_first_party_offer
from test_teknosa_reviewed_source import EAN, PRODUCT_ID, URL, page
from update_prices import PriceUpdater


TARGET = 'https://www.teknosa.com/samsung-galaxy-a36-5g-8gb-128gb-akilli-telefon-p-100000123456'
TITLE = 'Samsung Galaxy A36 5G 8 GB RAM 128 GB Akıllı Telefon'
SPECS = {'ram_gb': 8, 'storage_gb': 128, 'network_generation': '5g'}
PROFILE = {'name': 'Samsung Galaxy A36 5G', 'specs': SPECS}


def document(**changes):
    return (page(title=TITLE, **changes).replace(URL, TARGET)
            .replace('/' + URL.split('/', 3)[3], '/' + TARGET.split('/', 3)[3])
            .replace(PRODUCT_ID, '100000123456').replace(EAN, '8806094821437'))


class TeknosaCatalogTests(unittest.TestCase):
    def test_catalog_selected_sku_reaches_writer_preparation_without_a_manual_profile(self):
        class Transport:
            def read_document(self, url):
                self.assert_url = url
                return document(), 'document_read', 200
        transport = Transport()
        source = TeknosaCatalogSource(transport=transport)
        reader = RetailerOfferRefresher(teknosa_catalog_source=source)
        offers = reader.refresh_existing_offer_targets(PROFILE['name'], [{'source_url': TARGET}], SPECS)
        self.assertEqual(transport.assert_url, TARGET)
        self.assertEqual([(o['merchant'], o['price']) for o in offers], [('Teknosa', 68999)])
        updater = PriceUpdater(ingestion_client=object(), offer_refresher=reader)
        product = {'id': 999, 'slug': 'samsung-galaxy-a36-5g', 'name': PROFILE['name']}
        self.assertTrue(updater.live_offer_is_valid(product, offers[0], SPECS))
        self.assertEqual(len(updater.prepare_product_offers(product, offers,
                         updater.utc_timestamp(), updater.utc_timestamp())), 1)
        self.assertEqual(reader.refresh_existing_offer_targets(PROFILE['name'], [{'source_url': TARGET}],
                         {**SPECS, 'storage_gb': 256}), [])

    def test_selected_cart_seller_stock_gtin_sku_and_price_cannot_drift(self):
        self.assertEqual(parse_exact_first_party_offer(document(), TARGET, PROFILE)[1],
                         'observed_exact_first_party_cash_offer')
        cases = [document(seller='Other'), document(cart_seller='Other'), document(stock='N'),
                 document(visible='59.999 TL'), document(sku='WRONG'),
                 document().replace('8806094821437', '8806094821438'),
                 document().replace('data-product-shop-id="1001"', 'data-product-shop-id="1002"'),
                 document().replace('8 GB RAM 128 GB', '12 GB RAM 256 GB'),
                 document().replace('>Sepete Ekle<', '>Stokta Yok<')]
        for html in cases:
            self.assertIsNone(parse_exact_first_party_offer(html, TARGET, PROFILE)[0])
        for url in (TARGET + '?shopId=1001', TARGET.replace('https:', 'http:'),
                    TARGET.replace('www.teknosa.com', 'attacker.example')):
            self.assertIsNone(parse_exact_first_party_offer(document(), url, PROFILE)[0])

    def test_only_matching_catalog_targets_register_and_ambiguous_bindings_are_blocked(self):
        source = TeknosaCatalogSource()
        self.assertEqual(source.read_offer(TARGET)[1], 'unmatched_catalog_pdp')
        source.register_catalog_targets('Samsung Galaxy S26', [{'source_url': TARGET}], SPECS)
        self.assertEqual(source.read_offer(TARGET)[1], 'unmatched_catalog_pdp')
        source.register_catalog_targets(PROFILE['name'], [{'source_url': TARGET}], SPECS)
        source.register_catalog_targets('Samsung Galaxy A36', [{'source_url': TARGET}], SPECS)
        self.assertEqual(source.read_offer(TARGET)[1], 'unmatched_catalog_pdp')


if __name__ == '__main__':
    unittest.main()
