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
    def test_observed_g_shorthand_and_ungrouped_basket_caption_require_all_selected_proof(self):
        from pathlib import Path
        from datetime import datetime, timedelta, timezone
        from bs4 import BeautifulSoup
        from tr_price_scraper import TRPriceScraper
        from selected_sku_evidence import selected_sku_evidence
        soup = BeautifulSoup(Path('fixtures/trendyol-selected-g-shorthand.html').read_text(), 'html.parser')
        state = json.loads(soup.select_one('script').get_text().split('=', 1)[1].rstrip(';'))
        p = state['product']
        # Only advance the expiry clock; the observed quote and identity stay intact.
        future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime('%Y-%m-%dT23:00:00')
        for row in p['merchantListing']['promotions'] + p['merchantListing']['filteredPromotions']:
            row['promotionEndDate'] = future
        soup.select_one('script').string = 'window["__envoy__SHARED_PROPS"]=' + json.dumps(state) + ';'
        url = 'https://www.trendyol.com/xiaomi/17-12g-256g-siyah-p-1110127495'
        specs = {'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g'}
        offer, reason = parse_catalog_listing(str(soup), url, 'Xiaomi 17', specs)
        self.assertEqual(reason, 'selected_trendyol_public_basket_offer')
        self.assertEqual((offer['merchant'], offer['price'], offer['source_title']),
                         ('Xiaomi Resmi Mağazası', 71999, 'Xiaomi 17 12G+256G Siyah'))
        self.assertIsNotNone(selected_sku_evidence(offer))
        from trendyol_basket_price import valid_basket_promotion
        promotion = offer['basket_promotion']
        self.assertTrue(valid_basket_promotion(dict(promotion, name='Sepette 8.000 TL İndirim'), 79999, 71999))
        for label in ['Sepette 8.00 TL İndirim', 'Sepette 80,00 TL İndirim',
                      'Sepette 8,000 TL İndirim', 'Sepette 80000 TL İndirim',
                      'Sepette 8000 TL Kupon', 'Üyelere Sepette 8000 TL İndirim']:
            self.assertFalse(valid_basket_promotion(dict(promotion, name=label), 79999, 71999), label)
        matcher = TRPriceScraper()
        self.assertTrue(matcher.has_observed_offer_evidence('Xiaomi 17', offer, specs))
        for title in ['Xiaomi 17 8G+256G Siyah', 'Xiaomi 17 12G+512G Siyah',
                      'Xiaomi 17 12G+256G RAM', 'Xiaomi 17 RAM 12G+256G',
                      'Xiaomi 17 12G+256G Sanal RAM', 'Xiaomi 17 Plus 12G+256G',
                      'Xiaomi 17 12G+256G 8GB RAM', 'Xiaomi 17 12G+256G 128GB',
                      'Xiaomi 17 12G+256G / 12G+512G', 'Xiaomi 17 12G+256G 4G',
                      'Xiaomi 17 12G+256G 12G+256G']:
            self.assertFalse(matcher.has_observed_offer_evidence('Xiaomi 17', dict(offer, source_title=title), specs), title)
        self.assertFalse(matcher.has_observed_offer_evidence('Xiaomi 17',
            dict(offer, selected_trendyol_variant_verified=False), specs))
        for title in ['Xiaomi 17 Plus 12G+256G Siyah', 'Xiaomi 17 8G+256G Siyah']:
            changed = str(soup).replace('17 12G+256G Siyah', title.removeprefix('Xiaomi '))
            self.assertIsNone(parse_catalog_listing(changed, url, 'Xiaomi 17', specs)[0])

    def test_public_retailer_product_code_is_distinct_from_gtin_and_still_binds_selected_sku(self):
        from selected_sku_evidence import selected_sku_evidence
        from tr_price_scraper import TRPriceScraper
        p = ('state', 'product')
        paths = [p + ('variants', 0, 'barcode'), p + ('merchantListing', 'variants', 0, 'barcode'),
                 p + ('merchantListing', 'winnerVariant', 'barcode')]
        code = 'PMPG24MZTJKBTGVP82'  # Public selected A17 listing, Nov 4 2026.
        changes = {path: code for path in paths}
        offer, reason = parse_catalog_listing(document(changes), URL, NAME, SPECS)
        self.assertIsNotNone(offer, reason)
        self.assertIsNone(offer['gtin13'])
        self.assertEqual(offer['observed_barcode'], code)
        proof = selected_sku_evidence(offer)
        self.assertEqual(proof['version'], 'selected-retailer-sku-v2')
        self.assertEqual(proof['retailer_barcode'], code)
        self.assertNotIn('gtin13', proof)
        self.assertTrue(TRPriceScraper().has_observed_offer_evidence(NAME, offer, SPECS))
        for bad in ['8806094821438', 'PMP', code.lower(), code + 'X', ' ' + code, 'UNKNOWNPRODUCTCODE']:
            self.assertIsNone(parse_catalog_listing(document({path: bad for path in paths}), URL, NAME, SPECS)[0])
        for path in paths:
            self.assertIsNone(parse_catalog_listing(document({**changes, path: 'PMPG24MZTJKBTGVP83'}), URL, NAME, SPECS)[0])
        for patch in [{'barcode_kind': None}, {'observed_barcode': '8806094821438'},
                      {'gtin13': '8806094821438'}, {'listing_id': 'wrong'}, {'merchant': 'Other'}]:
            self.assertIsNone(selected_sku_evidence(dict(offer, **patch)))
        for changed_specs in [dict(SPECS, ram_gb=4), dict(SPECS, storage_gb=256),
                              dict(SPECS, network_generation='4g')]:
            self.assertFalse(TRPriceScraper().has_observed_offer_evidence(NAME, offer, changed_specs))

    def test_checksum_valid_upc_preserves_raw_selected_barcode_and_equivalent_gtin(self):
        from tr_price_scraper import TRPriceScraper
        p = ('state', 'product')
        paths = [p + ('variants', 0, 'barcode'), p + ('merchantListing', 'variants', 0, 'barcode'),
                 p + ('merchantListing', 'winnerVariant', 'barcode')]
        offer, reason = parse_catalog_listing(document({path: '195949018916' for path in paths}), URL, NAME, SPECS)
        self.assertIsNotNone(offer, reason)
        self.assertEqual((offer['gtin13'], offer['observed_barcode']), ('0195949018916', '195949018916'))
        self.assertTrue(TRPriceScraper().has_observed_offer_evidence(NAME, offer, SPECS))
        for barcode in ['195949018917', '19594901891', '00195949018916', ' 195949018916']:
            self.assertIsNone(parse_catalog_listing(document({path: barcode for path in paths}), URL, NAME, SPECS)[0])
        self.assertIsNone(parse_catalog_listing(document({paths[0]: '195949018916'}), URL, NAME, SPECS)[0])

    def test_selected_brand_cannot_be_an_accessory_brand_prefixing_a_phone_name(self):
        for brand in ['Valoris', 'Genel Markalar']:
            title = brand + ' ' + TITLE
            changes = {('state', 'product', 'brand', 'name'): brand,
                       ('state', 'product', 'name'): TITLE, ('title',): title,
                       ('schema', 'name'): title}
            offer, reason = parse_catalog_listing(document(changes), URL, NAME, SPECS)
            self.assertIsNone(offer)
            self.assertEqual(reason, 'selected_brand_mismatch')

    def test_public_lowest_price_template_still_requires_one_visible_cash_price_and_all_selected_proof(self):
        original = '<div data-testid="normal-price"><span class="discounted">34.997 TL</span></div>'
        # Public template observed on S26 Ultra Nov 4 2026. Its struck original
        # price is not the cash price and the historical label is not a coupon.
        alternate = ('<button class="price lowest-price" data-testid="lowest-price">'
                     '<div class="lowest-price-label">Son 10 Günün En Düşük Fiyatı!</div>'
                     '<span class="original">35.997 TL</span><span class="discounted">34.997 TL</span></button>')
        html = document({('state', 'product', 'attributes', 0, 'value', 'name'): 'Yok'}).replace(original, alternate)
        offer, reason = parse_catalog_listing(html, URL, NAME, SPECS)
        self.assertIsNotNone(offer, reason)
        self.assertEqual(offer['price'], 34997)
        for changed in [html.replace('Son 10 Günün En Düşük Fiyatı!', 'Uygulamaya özel fiyat'),
                        html.replace('<span class="discounted">34.997 TL', '<span class="discounted">33.997 TL'),
                        html + original, html.replace('data-testid="lowest-price"', 'data-testid="suggested-price"'),
                        html.replace('>Şimdi Al</button>', ' disabled>Şimdi Al</button>')]:
            self.assertIsNone(parse_catalog_listing(changed, URL, NAME, SPECS)[0])

    def test_unknown_memory_attribute_requires_explicit_unambiguous_selected_heading_capacity(self):
        from tr_price_scraper import TRPriceScraper
        changes = {('state', 'product', 'attributes', 0, 'value', 'name'): 'Yok'}
        offer, reason = parse_catalog_listing(document(changes), URL, NAME, SPECS)
        self.assertIsNotNone(offer, reason)
        self.assertEqual(offer['ram_gb'], 8)
        self.assertTrue(TRPriceScraper().has_observed_offer_evidence(NAME, offer, SPECS))
        for title in [NAME + ' 128GB Grafit', NAME + ' 8+8GB RAM 128GB',
                      NAME + ' 8+128GB Grafit', NAME + ' 12GB RAM 128GB',
                      NAME + ' RAM 6/8GB 128GB', NAME + ' 8GB Sanal RAM 128GB']:
            changed = {**changes, ('title',): title, ('state', 'product', 'name'): title.removeprefix('Samsung '),
                       ('schema', 'name'): title}
            self.assertIsNone(parse_catalog_listing(document(changed), URL, NAME, SPECS)[0], title)
        for value in ['', None, 'Belirtilmemiş', '12 GB']:
            self.assertIsNone(parse_catalog_listing(document({
                ('state', 'product', 'attributes', 0, 'value', 'name'): value}), URL, NAME, SPECS)[0])

    def test_plus_capacity_heading_survives_selected_source_and_ingestion_preparation(self):
        from tr_price_scraper import TRPriceScraper
        for title in [NAME + ' 8+128 GB ROM Grafit', NAME + ' 8GB+128GB Grafit']:
            changes = {('title',): title, ('state', 'product', 'name'): title.removeprefix('Samsung '),
                       ('schema', 'name'): title}
            offer, reason = parse_catalog_listing(document(changes), URL, NAME, SPECS)
            self.assertIsNotNone(offer, reason)
            self.assertEqual(offer['source_title'], title)
            self.assertTrue(TRPriceScraper().has_observed_offer_evidence(NAME, offer, SPECS))
            updater = PriceUpdater(ingestion_client=object())
            record = updater.prepare_product_offers({'id': 999, 'slug': 'samsung-galaxy-a54-5g'},
                [offer], updater.utc_timestamp(), updater.utc_timestamp())[0]
            self.assertEqual(record['source_title'], title)
            self.assertEqual(record['source_variant_evidence']['ram_gb'], 8)

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
