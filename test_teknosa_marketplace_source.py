from copy import deepcopy
import json
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
    def test_observed_samsung_short_brand_route_keeps_selected_title_shop_and_memory_bound(self):
        from bs4 import BeautifulSoup
        html = Path(__file__).with_name('fixtures').joinpath('teknosa-selected-samsung-brand-route.html').read_text()
        url = 'https://www.teknosa.com/samsung-galaxy-a35-5g-8-gb-ram-128-gb-buz-mavisi-cep-telefonu-samsung-turkiye-garantili-p-780010519'
        profile = {'name': 'Samsung Galaxy A35 5G', 'specs': {'ram_gb': 8, 'storage_gb': 128, 'network_generation': '5g'}}
        offer, reason = parse_selected_marketplace(html, url, profile)
        self.assertEqual(reason, 'selected_teknosa_marketplace_cash_offer')
        self.assertEqual((offer['merchant'], offer['merchant_id'], offer['gtin13'], offer['price']),
                         ('BAŞARAN İLETİŞİM', '3736', '8806095511771', 34999))
        self.assertEqual(offer['source_title'], 'Samsung Galaxy A35 5G 8 GB RAM 128 GB Buz Mavisi Cep Telefonu (Samsung Türkiye Garantili)')
        for path in ['/apple', '/magaza/samsung', '/samsung/', 'https://www.teknosa.com/samsung']:
            soup = BeautifulSoup(html, 'html.parser')
            soup.select_one('h1.pdp-title a.link')['href'] = path
            self.assertIsNone(parse_selected_marketplace(str(soup), url, profile)[0], path)
        for selector, attribute, value in [
            ('input.brandNameReplace', 'value', 'Apple'),
            ('input.productNameReplace', 'value', 'Samsung Galaxy A35 5G 6 GB RAM 128 GB'),
            ('button#addToCartButton', 'data-product-brand', 'Apple'),
            ('button#addToCartButton', 'data-shop-name', 'Other'),
            ('button#addToCartButton', 'data-product-ean', '8806095511772'),
            ('button#addToCartButton', 'data-product-shop-id', '1001'),
        ]:
            soup = BeautifulSoup(html, 'html.parser'); soup.select_one(selector)[attribute] = value
            self.assertIsNone(parse_selected_marketplace(str(soup), url, profile)[0], (selector, attribute))
        for selector in ['input.brandNameReplace', 'input.productNameReplace', 'h1.pdp-title a.link']:
            soup = BeautifulSoup(html, 'html.parser'); soup.select_one(selector).decompose()
            self.assertIsNone(parse_selected_marketplace(str(soup), url, profile)[0], selector)
        soup = BeautifulSoup(html, 'html.parser')
        title = soup.select_one('h1.pdp-title .replaceName')
        title.string = title.get_text().replace('8 GB RAM', '12 GB RAM')
        self.assertIsNone(parse_selected_marketplace(str(soup), url, profile)[0])

    def test_observed_ordinary_sale_binds_list_and_payable_amounts_to_their_own_fields(self):
        from bs4 import BeautifulSoup
        html = Path(__file__).with_name('fixtures').joinpath('teknosa-selected-ordinary-sale.html').read_text()
        url = 'https://www.teknosa.com/xiaomi-17t-12gb-512gb-beyaz-android-telefon-xiaomi-turkiye-garantili-p-780010982'
        profile = {'name': 'Xiaomi 17T', 'specs': {'ram_gb': 12, 'storage_gb': 512, 'network_generation': '5g'}}
        offer, reason = parse_selected_marketplace(html, url, profile)
        self.assertEqual(reason, 'selected_teknosa_marketplace_cash_offer')
        self.assertEqual((offer['merchant'], offer['price'], offer['gtin13'], offer['listing_id']),
                         ('ES VİSİON', 51249, '6932554447908', '1990011'))
        for attribute, value in [('data-price-without-discount', '54998'),
                                 ('data-price-without-discount', '50000'),
                                 ('data-price-with-discount', '51248'),
                                 ('data-price-with-discount', '54999'),
                                 ('data-shop-name', 'Other'),
                                 ('data-product-ean', '6932554447909')]:
            soup = BeautifulSoup(html, 'html.parser')
            soup.select_one('button#addToCartButton')[attribute] = value
            self.assertIsNone(parse_selected_marketplace(str(soup), url, profile)[0], attribute)
        for key, value in [('unit_price', 51249), ('unit_price', 54998), ('unit_sale_price', 54999)]:
            soup = BeautifulSoup(html, 'html.parser')
            script = next(n for n in soup.select('script') if n.get_text().startswith('window.insider_object = '))
            state = json.loads(script.get_text().removeprefix('window.insider_object = ').rstrip(';'))
            state['product'][key] = value
            script.string = 'window.insider_object = ' + json.dumps(state) + ';'
            self.assertIsNone(parse_selected_marketplace(str(soup), url, profile)[0], key)
        for change in ('schema_price', 'schema_member', 'visible_price', 'coupon', 'stock'):
            soup = BeautifulSoup(html, 'html.parser')
            if change in ('schema_price', 'schema_member', 'stock'):
                script = soup.select_one('script[type="application/ld+json"]')
                schema = json.loads(script.get_text())
                field, value = {'schema_price': ('price', 54999), 'schema_member': ('validForMemberTier', 'members'),
                                'stock': ('availability', 'https://schema.org/OutOfStock')}[change]
                schema['@graph']['offers'][field] = value
                script.string = json.dumps(schema)
            elif change == 'visible_price':
                soup.select_one('.prc-third').string = '54.999 TL'
            else:
                soup.select_one('.pdp-prices').append(BeautifulSoup('<span>Kuponla 51.249 TL</span>', 'html.parser'))
            self.assertIsNone(parse_selected_marketplace(str(soup), url, profile)[0], change)

    def test_observed_repeated_brand_rendering_binds_original_heading_cart_schema_and_shop(self):
        from bs4 import BeautifulSoup
        html = Path(__file__).with_name('fixtures').joinpath('teknosa-selected-brand-rendering.html').read_text()
        url = 'https://www.teknosa.com/poco-x6-pro-5g-8-gb-ram-256-gb-siyah-cep-telefonu-poco-turkiye-garantili-p-780010584'
        profile = {'name': 'POCO X6 Pro', 'specs': {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'}}
        offer, why = parse_selected_marketplace(html, url, profile)
        self.assertEqual(why, 'selected_teknosa_marketplace_cash_offer')
        self.assertEqual((offer['merchant'], offer['price'], offer['gtin13']),
                         ('BAŞARAN İLETİŞİM', 39999, '6941812757994'))
        self.assertEqual(offer['source_title'], 'Poco X6 Pro 5G 8 GB RAM 256 GB Siyah Cep Telefonu (Poco Türkiye Garantili)')
        for selector, attribute, value in [
            ('input.productNameReplace', 'value', 'Poco X6 Pro 5G 12 GB RAM 256 GB'),
            ('input.brandNameReplace', 'value', 'Samsung'),
            ('button#addToCartButton', 'data-product-brand', 'Samsung'),
            ('button#addToCartButton', 'data-product-ean', '6941812757995'),
            ('button#addToCartButton', 'data-product-shop-id', '1001'),
            ('button#addToCartButton', 'data-shop-name', 'Other'),
            ('a.link', 'href', '/magaza/poco'),
        ]:
            s = BeautifulSoup(html, 'html.parser');s.select_one(selector)[attribute] = value
            self.assertIsNone(parse_selected_marketplace(str(s), url, profile)[0], (selector, attribute))
        for selector in ['input.productNameReplace', 'input.brandNameReplace', 'a.link']:
            s = BeautifulSoup(html, 'html.parser');s.select_one(selector).decompose()
            self.assertIsNone(parse_selected_marketplace(str(s), url, profile)[0], selector)
        for before, after in [('X6 Pro 5G', 'X6 Pro 4G'), ('8 GB RAM', '12 GB RAM'),
                              ('256 GB', '512 GB'), ('39999', '39998'),
                              ('InStock', 'OutOfStock'), ('NewCondition', 'UsedCondition')]:
            s = BeautifulSoup(html, 'html.parser')
            if before in ['X6 Pro 5G', '8 GB RAM', '256 GB']:
                s.select_one('span.replaceName').string = s.select_one('span.replaceName').get_text().replace(before, after)
                changed = str(s)
            else:
                changed = html.replace(before, after, 1)
            self.assertIsNone(parse_selected_marketplace(changed, url, profile)[0], before)

    def test_observed_marketplace_layout_retains_actual_shop_cash_and_raw_heading(self):
        offer, reason = parse_selected_marketplace(document(), URL, PROFILE)
        self.assertEqual(reason, 'selected_teknosa_marketplace_cash_offer')
        self.assertEqual((offer['merchant'], offer['merchant_id'], offer['price']),
                         ('BittiBitiyor', '2014', 19099))
        self.assertEqual(offer['source_title'], 'SAMSUNG GALAXY A26_ 6_128GB AKILLI TELEFON BEYAZ')
        self.assertEqual((offer['gtin13'], offer['listing_id']), ('8806097100676', '1978045'))

    def test_real_pending_selected_shop_ignores_the_other_inventory_price(self):
        html = Path(__file__).with_name('fixtures').joinpath('teknosa-selected-pending-cart.html').read_text()
        url = 'https://www.teknosa.com/samsung-galaxy-s26-ultra-5g-12256gb-siyah-akilli-telefon-p-100000061482'
        profile = {'name': 'Samsung Galaxy S26 Ultra', 'specs': {'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g'}}
        offer, reason = parse_selected_marketplace(html, url, profile)
        self.assertEqual(reason, 'selected_teknosa_marketplace_cash_offer')
        self.assertEqual((offer['merchant'], offer['price'], offer['source_render_state']),
                         ('hepsi10line', 88648, 'pending_selected_cart'))
        self.assertEqual(offer['source_title'], 'Samsung Galaxy S26 Ultra 5G 12/256GB Siyah Akıllı Telefon')
        self.assertEqual(offer['gtin13'], '8806097866633')
        for before, after in (
            ('js-enable-btn', 'not-a-pending-cart'),
            ('disabled="disabled"', 'disabled="disabled" aria-disabled="true"'),
            ('"unit_sale_price": 88648.0', '"unit_sale_price": 98999.0'),
            ('"availability": "https://schema.org/InStock"', '"availability": "https://schema.org/OutOfStock"'),
            ('iş günü içinde kargoda', 'stok tükendi'),
            ('data-product-shop-id="3973"', 'data-product-shop-id="1001"'),
            ('data-product-name="Samsung', 'data-product-name="Other'),
        ):
            with self.subTest(before=before):
                self.assertIn(before, html)
                self.assertIsNone(parse_selected_marketplace(html.replace(before, after), url, profile)[0])

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
            ('id="addToCartButton"', 'aria-disabled="true" id="addToCartButton"'),
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
