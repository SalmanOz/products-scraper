from copy import deepcopy
import json
from pathlib import Path
import unittest

from bs4 import BeautifulSoup

from n11_catalog_source import N11CatalogSource, catalog_offer_matches, discover_catalog_targets, parse_catalog_listing
from retailer_offer_refresh import RetailerOfferRefresher
from update_prices import PriceUpdater

URL = 'https://www.n11.com/urun/samsung-galaxy-a57-8-gb-256-gb-samsung-turkiye-garantili-123382026'
NAME = 'Samsung Galaxy A57'
SPECS = {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'}


def document(change=None):
    soup = BeautifulSoup(Path(__file__).with_name('fixtures').joinpath('n11-selected-ordinary-cash.html').read_text(), 'html.parser')
    script = next(n for n in soup.select('script') if n.get_text().startswith('window.model = '))
    state = json.loads(script.get_text()[len('window.model = '):].rstrip(';'))
    if change:
        change(state, soup)
    script.string = 'window.model = ' + json.dumps(state, ensure_ascii=False) + ';'
    return str(soup)


class N11CatalogTests(unittest.TestCase):
    def test_real_server_html_placeholder_keeps_selected_sku_cash_and_stock_proof(self):
        html = Path(__file__).with_name('fixtures').joinpath('n11-selected-ssr-cash.html').read_text()
        offer, reason = parse_catalog_listing(html, URL, NAME, SPECS)
        self.assertEqual(reason, 'selected_n11_cash_offer')
        self.assertEqual((offer['merchant'], offer['price'], offer['source_render_state']),
                         ('n11', 29499, 'ssr_placeholder'))
        for before, after in (
            ('Seçim Yap', '128 GB, Gri'), ('"availableToBuy": true', '"availableToBuy": false'),
            ('"isProductDetailPage": true', '"isProductDetailPage": false'),
            ('disabled="disabled"', ''), ('"outOfStock": false', '"outOfStock": true'),
            ('<ins data-v-01cd5d8e=""></ins>', '<ins data-v-01cd5d8e="">1 TL</ins>'),
        ):
            with self.subTest(before=before):
                self.assertIn(before, html)
                self.assertIsNone(parse_catalog_listing(html.replace(before, after), URL, NAME, SPECS)[0])

    def test_public_basket_price_keeps_its_condition_and_selected_physical_variant(self):
        html = Path(__file__).with_name('fixtures').joinpath('n11-selected-ssr-basket.html').read_text()
        url = 'https://www.n11.com/urun/samsung-galaxy-s23-fe-8-gb-128-gb-samsung-turkiye-garantili-44089317'
        name, specs = 'Samsung Galaxy S23 FE', {'ram_gb': 8, 'storage_gb': 128, 'network_generation': '5g'}
        offer, reason = parse_catalog_listing(html, url, name, specs)
        self.assertEqual(reason, 'selected_n11_basket_offer')
        self.assertEqual((offer['price'], offer['listed_price'], offer['price_kind']), (37439.04, 38999, 'basket'))
        self.assertTrue(catalog_offer_matches(offer, name, specs))
        updater = PriceUpdater(ingestion_client=object())
        record = updater.prepare_product_offers({'id': 999, 'slug': 'samsung-galaxy-s23-fe'},
            [offer], updater.utc_timestamp(), updater.utc_timestamp())[0]
        proof = record['source_variant_evidence']
        self.assertEqual((proof['price_kind'], proof['listed_price'], proof['payable_price']),
                         ('basket', 38999, 37439.04))
        self.assertEqual(record['source_title'], offer['source_title'])
        # A missing heading RAM declaration can be established by actual selected
        # technical attributes, never by the expected catalog values.
        no_ram = html.replace('S23 FE 8 GB 128 GB', 'S23 FE 128 GB')
        self.assertIsNotNone(parse_catalog_listing(no_ram, url, name, specs)[0])
        for before, after in (
            ('"sellerHasMinimumCartAmount": false', '"sellerHasMinimumCartAmount": true'),
            ('"mobileDiscountExist": false', '"mobileDiscountExist": true'),
            ('"isPrivateProduct": false', '"isPrivateProduct": true'),
            ('"hasInstantDiscount": false', '"hasInstantDiscount": true'),
            ('"isShowPrivateProductPrice": false', '"isShowPrivateProductPrice": true'),
            ('"value": "8 GB"', '"value": "12 GB"'),
            ('"key": "5G Desteği"', '"key": "Unknown attribute"'),
            ('"priceFloat": 37439.04', '"priceFloat": 37438.04'),
        ):
            with self.subTest(before=before):
                self.assertIn(before, html)
                self.assertIsNone(parse_catalog_listing(html.replace(before, after), url, name, specs)[0])

    def test_search_discovers_only_matching_new_phone_destinations_and_never_card_prices(self):
        title = 'Samsung Galaxy A57 8 GB 256 GB (Samsung Türkiye Garantili)'
        def card(url, name=title):
            return f'<a href="{url}"><h2 class="product-item-title">{name}</h2><h3>1 TL</h3></a>'
        html = (card(URL + '?magaza=mediamarkt&renk=gri') + card(URL)
                + card(URL.replace('123382026', '123382027'), title.replace('A57', 'A56'))
                + card(URL, 'Yenilenmiş ' + title) + card(URL, title + ' Kılıf')
                + card(URL, title.replace('256', '128')) + card(URL + '?token=secret')
                + card(URL.replace('www.n11.com', 'attacker.example')))
        result = discover_catalog_targets(html, NAME, SPECS)
        self.assertEqual(result, [{'source_url': URL, 'discovery_title': title}])
        self.assertNotIn('price', result[0])

    def test_live_ordinary_sale_uses_selected_shop_and_cash_not_old_price(self):
        offer, reason = parse_catalog_listing(document(), URL, NAME, SPECS)
        self.assertEqual(reason, 'selected_n11_cash_offer')
        self.assertEqual((offer['merchant'], offer['merchant_id'], offer['price']), ('MEDIAMARKT', 4640050, 29499))
        self.assertEqual((offer['sku'], offer['gtin13']), ('127369474277', '8806099044916'))
        self.assertTrue(catalog_offer_matches(offer, NAME, SPECS))
        self.assertNotIn('buyer', json.dumps(offer))
        self.assertNotIn('accessToken', json.dumps(offer))

    def test_public_reader_writer_and_complete_seller_proof_are_integrated(self):
        calls = []
        class Transport:
            def read_document(self, url):
                calls.append(url)
                return document(), 'document_read', 200
        source = N11CatalogSource(transport=Transport())
        reader = RetailerOfferRefresher(n11_catalog_source=source)
        offers = reader.refresh_existing_offer_targets(NAME,
            [{'source_url': URL + '?magaza=stale&renk=krem'}], SPECS)
        self.assertEqual(calls, [URL])
        self.assertEqual(len(offers), 1)
        updater = PriceUpdater(ingestion_client=object(), offer_refresher=reader)
        product = {'id': 999, 'slug': 'samsung-galaxy-a57', 'name': NAME,
                   'offer_targets': [{'source_url': URL, 'merchant_name': 'n11'}],
                   'offer_targets_truncated': False}
        self.assertTrue(updater.live_offer_is_valid(product, offers[0], SPECS))
        records = updater.prepare_product_offers(product, offers, updater.utc_timestamp(), updater.utc_timestamp())
        self.assertEqual(records[0]['seller'], 'MEDIAMARKT')
        product['offer_targets'].append({'merchant_name': 'MediaMarkt',
                                         'source_url': 'https://www.mediamarkt.com.tr/tr/product/_other-123456.html'})
        self.assertFalse(updater.live_offer_is_valid(product, offers[0], SPECS))

    def test_stock_private_member_app_and_basket_prices_never_become_ordinary_cash(self):
        changes = [('availableToBuy', False), ('outOfStock', True), ('stock', 0), ('secondHand', True),
                   ('preSale', True), ('privateProduct', True), ('isPrivateProduct', True),
                   ('mobileDiscountExist', True), ('hasInstantDiscount', True),
                   ('sellerHasMinimumCartAmount', True), ('displayPriceFloat', 1),
                   ('defaultGtin', '8806099044917'), ('groupId', 123382027), ('defaultSkuId', 'wrong')]
        for key, value in changes:
            with self.subTest(key=key):
                self.assertIsNone(parse_catalog_listing(document(lambda d, s: d['product'].update({key: value})), URL, NAME, SPECS)[0])
        # n11's separate basket layout has hasDiscount=false and mismatched
        # cash/display values, even though its JSON-LD advertises the lower one.
        self.assertEqual(parse_catalog_listing(document(lambda d, s: d['product'].update(hasDiscount=False)),
                         URL, NAME, SPECS)[1], 'conditional_or_inconsistent_cash_price')

    def test_visible_cart_seller_variant_and_schema_are_independent_required_proofs(self):
        changes = [
            lambda d, s: s.select_one('#addToBasket').attrs.update(disabled=''),
            lambda d, s: setattr(s.select_one('.sku-attributes-text'), 'string', '128 GB, Gri'),
            lambda d, s: setattr(s.select_one('.newPrice ins'), 'string', '1 TL'),
            lambda d, s: setattr(s.select_one('.sidebarSellerArea-top-name'), 'string', 'Other Shop'),
            lambda d, s: d['productMeta'].update(sellerId='1'),
            lambda d, s: d['product'].update(seller={'id': 123, 'nickName': 'Other Shop'}),
            lambda d, s: setattr(s.select_one('script[type="application/ld+json"]'), 'string', '{}'),
        ]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                self.assertIsNone(parse_catalog_listing(document(change), URL, NAME, SPECS)[0])
        for suffix in ('?magaza=other', '?renk=siyah', '?dahili-hafiza=128-gb', '?token=private', '#price'):
            self.assertIsNone(parse_catalog_listing(document(), URL + suffix, NAME, SPECS)[0])
        for key, value in (('ram_gb', 12), ('storage_gb', 128)):
            self.assertIsNone(parse_catalog_listing(document(), URL, NAME, {**SPECS, key: value})[0])


if __name__ == '__main__':
    unittest.main()
