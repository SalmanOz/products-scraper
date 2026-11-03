import copy
import json
import unittest

from trendyol_catalog_source import TrendyolCatalogSource, parse_catalog_listing
from retailer_offer_refresh import RetailerOfferRefresher
from update_prices import PriceUpdater

URL = 'https://www.trendyol.com/samsung/galaxy-a54-5g-128-gb-8-gb-ram-grafit-p-676507062?merchantId=657799&v=tek-ebat'
NAME = 'Samsung Galaxy A54 5G'
TITLE = NAME + ' 128 GB 8 GB RAM Grafit'
SPECS = {'ram_gb': 8, 'storage_gb': 128, 'network_generation': '5g'}


def evidence():
    price = {'value': 34997, 'text': '34.997 TL'}
    variant = {'isSelected': True, 'inStock': True, 'itemNumber': 894997311,
               'barcode': '8806094821437', 'price': dict(price)}
    product = {'id': 676507062, 'inStock': True, 'brand': {'name': 'Samsung'},
               'name': TITLE.removeprefix('Samsung '), 'attributes': [
                   {'key': {'name': k}, 'value': {'name': v}} for k, v in (
                       ('RAM Kapasitesi', '8 GB'), ('Dahili Hafıza', '128 GB'),
                       ('Mobil Bağlantı Hızı', '5G'))], 'variants': [copy.deepcopy(variant)],
               'merchantListing': {'merchant': {'id': 657799, 'name': 'Test Satıcı'},
                   'variants': [copy.deepcopy(variant)], 'winnerVariant': {
                       'listingId': 'c470a9f5a0d9e00d22786c5f724277c3',
                       'itemNumber': 894997311, 'barcode': '8806094821437',
                       'inStock': True, 'sellable': True, 'stockStatus': 1,
                       'price': {'currency': 'TRY', **{k: dict(price) for k in (
                           'sellingPrice', 'discountedPrice', 'couponApplicablePrice',
                           'tyPlusCouponApplicablePrice', 'discountedPriceAfterNoLimitPromotions')}}}}}
    schema = {'@type': 'Product', '@id': URL.split('?')[0], 'name': TITLE, 'sku': '676507062',
              'offers': {'@type': 'Offer', 'url': URL.split('?')[0], 'price': '34997.00',
                         'priceCurrency': 'TRY', 'itemCondition': 'https://schema.org/NewCondition',
                         'availability': 'https://schema.org/InStock'}}
    return {'state': {'product': product}, 'schema': schema, 'title': TITLE,
            'visible_price': '34.997 TL', 'cart_attributes': '', 'extra': ''}


def document(changes=None):
    data = evidence()
    for path, value in (changes or {}).items():
        node = data
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
    return f'''<script>window["__envoy__SHARED_PROPS"]={json.dumps(data['state'])};</script>
      <script type="application/ld+json">{json.dumps(data['schema'])}</script>
      <h1 data-testid="product-title">{data['title']}</h1>
      <div data-testid="normal-price"><span class="discounted">{data['visible_price']}</span></div>
      <button data-testid="buy-now-button" {data['cart_attributes']}>Şimdi Al</button>{data['extra']}'''


class TrendyolCatalogTests(unittest.TestCase):
    def test_selected_seller_reaches_ingestion_preparation_without_manual_phone_profile(self):
        class Transport:
            def read_document(self, url):
                return document(), 'document_read', 200
        source = TrendyolCatalogSource(transport=Transport())
        reader = RetailerOfferRefresher(trendyol_catalog_source=source)
        targets = [{'merchant_name': 'Trendyol', 'source_url': URL}]
        offers = reader.refresh_existing_offer_targets(NAME, targets, SPECS)
        self.assertEqual([(o['merchant'], o['price']) for o in offers], [('Test Satıcı', 34997)])
        product = {'id': 999, 'slug': 'samsung-galaxy-a54-5g', 'name': NAME,
                   'offer_targets': targets, 'offer_targets_truncated': False}
        updater = PriceUpdater(ingestion_client=object(), offer_refresher=reader)
        self.assertTrue(updater.live_offer_is_valid(product, offers[0], SPECS))
        self.assertEqual(len(updater.prepare_product_offers(product, offers,
                         updater.utc_timestamp(), updater.utc_timestamp())), 1)
        targets.append({'merchant_name': 'Test Satıcı', 'source_url': URL.replace('657799', '123')})
        self.assertFalse(updater.live_offer_is_valid(product, offers[0], SPECS))
        self.assertFalse(updater.live_offer_is_valid({'name': NAME}, offers[0], SPECS))

    def test_all_selected_identity_stock_price_and_variant_layers_are_required(self):
        p = ('state', 'product')
        ml = p + ('merchantListing',)
        winner = ml + ('winnerVariant',)
        cases = [
            {p + ('id',): 123}, {p + ('inStock',): False}, {p + ('name',): 'Galaxy A55'},
            {p + ('attributes', 0, 'value', 'name'): '12 GB'},
            {p + ('attributes', 1, 'value', 'name'): '256 GB'},
            {p + ('attributes', 2, 'value', 'name'): '4.5G'},
            {p + ('variants', 0, 'isSelected'): False}, {p + ('variants', 0, 'barcode'): '8806094821438'},
            {p + ('variants', 0, 'price', 'value'): 30000},
            {ml + ('merchant', 'id'): 123}, {ml + ('merchant', 'name'): ''},
            {ml + ('variants', 0, 'itemNumber'): 123}, {winner + ('listingId',): 'bad'},
            {winner + ('inStock',): False}, {winner + ('sellable',): False},
            {winner + ('stockStatus',): 0}, {winner + ('barcode',): '8806094821438'},
            {winner + ('price', 'discountedPrice', 'value'): 30000},
            {winner + ('price', 'couponApplicablePrice', 'value'): 30000},
            {('schema', 'offers', 'price'): '30000'},
            {('schema', 'offers', 'availability'): 'https://schema.org/OutOfStock'},
            {('schema', 'offers', 'itemCondition'): 'https://schema.org/RefurbishedCondition'},
            {('schema', 'offers', 'url'): URL.replace('676507062', '676507063')},
            {('title',): TITLE + ' Yenilenmiş'}, {('visible_price',): '30.000 TL'},
            {('cart_attributes',): 'disabled'}, {('cart_attributes',): 'aria-disabled="true"'},
        ]
        self.assertIsNotNone(parse_catalog_listing(document(), URL, NAME, SPECS)[0])
        for change in cases:
            with self.subTest(change=change):
                self.assertIsNone(parse_catalog_listing(document(change), URL, NAME, SPECS)[0])
        for url in (URL + '#x', URL.replace('https:', 'http:'), URL.replace('657799', '123')):
            self.assertIsNone(parse_catalog_listing(document(), url, NAME, SPECS)[0])
        duplicated = document() + '<script>window["__envoy__SHARED_PROPS"]={};</script>'
        self.assertIsNone(parse_catalog_listing(duplicated, URL, NAME, SPECS)[0])

    def test_unbound_or_ambiguous_targets_are_not_requested(self):
        class Transport:
            def read_document(self, url):
                raise AssertionError('unbound URL')
        source = TrendyolCatalogSource(transport=Transport())
        source.register_catalog_targets('Samsung Galaxy A55', [{'source_url': URL}], SPECS)
        self.assertEqual(source.read_offer(URL)[1], 'unmatched_catalog_pdp')
        source.register_catalog_targets(NAME, [{'source_url': URL}], SPECS)
        source.register_catalog_targets('Samsung Galaxy A54', [{'source_url': URL}], SPECS)
        self.assertEqual(source.read_offer(URL)[1], 'unmatched_catalog_pdp')

    def test_stored_seller_selector_discovers_canonical_pdp_and_new_selected_seller(self):
        calls = []
        class Transport:
            def read_document(self, url):
                calls.append(url)
                return document(), 'document_read', 200
        source = TrendyolCatalogSource(transport=Transport())
        hint = URL.replace('657799', '123')
        source.register_catalog_targets(NAME, [{'source_url': hint}], SPECS)
        offer, reason, status = source.read_offer(hint)
        self.assertEqual(calls, [URL.split('?')[0]])
        self.assertEqual(offer['url'], URL.split('?')[0])
        self.assertEqual(offer['merchant_id'], 657799)
        self.assertEqual((reason, status), ('selected_new_cash_offer_observed', 200))
        self.assertIsNone(parse_catalog_listing(document(), hint, NAME, SPECS)[0])


if __name__ == '__main__':
    unittest.main()
