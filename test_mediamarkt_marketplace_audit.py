"""Negative evidence tests for the isolated, read-only marketplace parser."""

import json
import unittest
from copy import deepcopy
from html import escape

from mediamarkt_marketplace_audit import catalog_variant_matches, selected_marketplace_offer


URL = ('https://www.mediamarkt.com.tr/tr/product/'
       '_samsung-galaxy-s25-ultra-5g-12-gb-256-gb-akilli-telefon-titanyum-mavi-166438395.html')
TITLE = 'SAMSUNG Galaxy S25 Ultra 12 GB 256 GB Akıllı Telefon Titanyum Mavi'
SKU = '166438395'
OFFER = '1647130524'
SELLER = 'CepHane Teknoloji'
SELLER_ID = '11289'
PRICE = 78899


def fixture():
    """Compact graph shape captured from the live S25 Ultra selected offer."""
    base = 'Media:tr:' + SKU
    selected = base + '[' + OFFER + ']'
    a = {
        'ROOT_QUERY': {
            'cofrProductAggregate({"productId":"' + SKU + '"})': {
                '__typename': 'CofrProductAggregate', 'productId': SKU, 'offerId': None,
                'cofrCoreFeature': {'__ref': 'CofrCoreFeature:' + base},
                'cofrPriceFeature': {'__ref': 'CofrPriceFeature:base'},
                'cofrOnlineStatusFeature': {'__ref': 'CofrOnlineStatusFeature:' + base},
            },
        },
        'CofrCoreFeature:' + base: {
            '__typename': 'CofrCoreFeature', 'id': base,
            'defaultOfferId': OFFER, 'relevantOfferId': OFFER,
            'isProductOfTypeMarketplace': True, 'productName': TITLE,
        },
        'CofrPriceFeature:base': {
            '__typename': 'CofrPriceFeature', 'id': base,
            'currency': 'TRY', 'isProductOfTypeMarketplace': True,
            'marketplaceSeller': {'sellerId': SELLER_ID, 'sellerName': SELLER},
            'price': {'amount': PRICE},
        },
        'CofrOnlineStatusFeature:' + base: {
            '__typename': 'CofrOnlineStatusFeature', 'id': base,
            'isAvailableAndBuyable': True, 'isAvailableForDelivery': True,
            'onlineStatus': 'MP_OFFER',
        },
        'GraphqlProductAggregate:Media:tr-TR:' + SKU: {
            '__typename': 'GraphqlProductAggregate',
            'bestMarketplaceOfferV3': {'__ref': 'GraphqlMarketplaceOfferV3:' + OFFER},
            'product': {'__ref': 'GraphqlProduct:Media:tr-TR:' + SKU},
        },
        'GraphqlProduct:Media:tr-TR:' + SKU: {
            '__typename': 'GraphqlProduct', 'id': SKU,
            'featureGroupsWithProductId': {'__ref': 'GraphqlProductFeatureGroups:' + SKU},
        },
        'GraphqlProductFeatureGroups:' + SKU: {
            '__typename': 'GraphqlProductFeatureGroups', 'productId': SKU,
            'featureGroups': [{'features': [
                {'__ref': 'GraphqlProductFeature:RAM'},
                {'__ref': 'GraphqlProductFeature:Storage'},
            ]}],
        },
        'GraphqlProductFeature:RAM': {
            '__typename': 'GraphqlProductFeature', 'name': 'RAM Bellek Boyutu', 'value': '12',
        },
        'GraphqlProductFeature:Storage': {
            '__typename': 'GraphqlProductFeature', 'name': 'Bellek Kapasitesi', 'value': '256',
        },
        'GraphqlMarketplaceOfferV3:' + OFFER: {
            '__typename': 'GraphqlMarketplaceOfferV3', 'offerId': int(OFFER),
            'productId': SKU, 'currencyIsoCode': 'TRY', 'price': PRICE,
            'sellerId': int(SELLER_ID), 'sellerName': SELLER,
            'offerCondition': 'NEW', 'isBestOffer': True,
            'cofrProductAggregate({})': {
                'cofrCoreFeature': {'__ref': 'CofrCoreFeature:' + selected},
                'cofrPriceFeature': {'__ref': 'CofrPriceFeature:selected'},
                'cofrOnlineStatusFeature': {'__ref': 'CofrOnlineStatusFeature:' + selected},
                'cofrRefurbishedGoodsFeature': {'__ref': 'CofrRefurbishedGoodsFeature:' + selected},
            },
        },
        'CofrCoreFeature:' + selected: {
            '__typename': 'CofrCoreFeature', 'id': selected,
            'defaultOfferId': OFFER, 'relevantOfferId': OFFER,
            'isProductOfTypeMarketplace': True, 'productName': TITLE,
        },
        'CofrPriceFeature:selected': {
            '__typename': 'CofrPriceFeature', 'id': selected,
            'currency': 'TRY', 'isProductOfTypeMarketplace': True,
            'marketplaceSeller': {'sellerId': SELLER_ID, 'sellerName': SELLER},
            'price': {'amount': PRICE},
        },
        'CofrOnlineStatusFeature:' + selected: {
            '__typename': 'CofrOnlineStatusFeature', 'id': selected,
            'isAvailableAndBuyable': True, 'isAvailableForDelivery': True,
            'onlineStatus': 'MP_OFFER',
        },
        'CofrRefurbishedGoodsFeature:' + selected: {
            '__typename': 'CofrRefurbishedGoodsFeature', 'id': selected,
            'offer': {'conditionType': 'NEW', 'offerId': OFFER,
                      'priceAmount': PRICE, 'priceCurrency': 'TRY'},
        },
    }
    product = {
        '@type': 'ProductGroup', 'sku': SKU, 'url': URL, 'name': TITLE,
        'brand': {'@type': 'Brand', 'name': 'SAMSUNG'},
        'offers': [{
            '@type': 'Offer', 'url': URL, 'price': PRICE,
            'priceCurrency': 'TRY', 'itemCondition': 'https://schema.org/NewCondition',
            'availability': 'https://schema.org/InStock',
            'seller': {'@type': 'Organization', 'name': SELLER, 'id': SELLER_ID},
        }],
        # A cheaper related color/variant cannot become the selected offer.
        'hasVariant': [{'@type': 'Product', 'sku': '1245472',
                        'offers': {'@type': 'Offer', 'price': 64197}}],
    }
    return {'apollo': a, 'action': {'@type': 'BuyAction', 'object': product},
            'canonical': URL, 'h1': TITLE, 'visible_price': '₺ 78.899,',
            'visible_seller': SELLER, 'seller_href':
                'https://www.mediamarkt.com.tr/tr/marketplace/seller/' + SELLER_ID,
            'stock': 'Adrese teslimata uygun.', 'stock_test': 'mms-cofr-delivery_AVAILABLE',
            'button_label': 'Sepete Ekle ' + TITLE, 'button_disabled': 'false'}


def render(data):
    action = json.dumps(data['action'], ensure_ascii=False)
    state = json.dumps({'apolloState': data['apollo']}, ensure_ascii=False)
    return (f'<html><head><link rel="canonical" href="{escape(data["canonical"])}">'
            f'<script type="application/ld+json">{action}</script>'
            f'<script>window.__PRELOADED_STATE__ = {state};</script></head><body>'
            f'<h1>{escape(data["h1"])}</h1>'
            '<div data-test="cofr-price mms-branded-price">'
            f'<span data-test="branded-price-whole-value">{escape(data["visible_price"])}</span>'
            f'<p>Satış ve sevkiyat <a href="{escape(data["seller_href"])}">'
            f'{escape(data["visible_seller"])}</a></p></div>'
            '<section data-test="mms-pdp-details-availability">'
            f'<div data-test="{escape(data["stock_test"])}">{escape(data["stock"])}</div>'
            '</section><nav data-test="mms-anchor-navigation-bar">'
            f'<button data-test="cofr-add-to-basket-button a2c-Button" '
            f'aria-disabled="{data["button_disabled"]}" '
            f'aria-label="{escape(data["button_label"])}">Sepete Ekle</button>'
            '</nav></body></html>')


class MarketplaceAuditTests(unittest.TestCase):
    def test_selected_offer_ignores_cheaper_related_variant(self):
        result = selected_marketplace_offer(render(fixture()), URL)
        self.assertEqual((result['sku'], result['offer_id'], result['seller_name'],
                          result['price'], result['condition'],
                          result['ram_gb'], result['storage_gb']),
                         (SKU, OFFER, SELLER, 78899, 'new', 12, 256))
        self.assertTrue(catalog_variant_matches(result, 'Samsung Galaxy S25 Ultra',
                                                 {'ram_gb': 12, 'storage_gb': 256}))
        self.assertFalse(catalog_variant_matches(result, 'Samsung Galaxy S25 Ultra',
                                                  {'ram_gb': 12, 'storage_gb': 512}))
        self.assertFalse(catalog_variant_matches(result, 'Samsung Galaxy S25 Ultra',
                                                  {'ram_gb': 8, 'storage_gb': 256}))

    def test_a17_5g_catalog_requires_explicit_radio_evidence(self):
        observation = {
            'source_title': 'SAMSUNG Galaxy A17 4 GB 128 GB Akıllı Telefon Mavi',
            'url': ('https://www.mediamarkt.com.tr/tr/product/'
                    '_samsung-galaxy-a17-4-gb-128-gb-akilli-telefon-mavi-166767190.html'),
            'ram_gb': 4, 'storage_gb': 128, 'network_generation': None,
        }
        self.assertFalse(catalog_variant_matches(observation, 'Samsung Galaxy A17 5G',
                                                  {'ram_gb': 4, 'storage_gb': 128}))
        self.assertTrue(catalog_variant_matches({**observation, 'network_generation': '5g'},
                                                 'Samsung Galaxy A17 5G',
                                                 {'ram_gb': 4, 'storage_gb': 128}))

    def test_document_and_schema_identity_must_be_exact(self):
        cases = [
            lambda d: d.update(canonical=URL.replace(SKU, '1245472')),
            lambda d: d.update(h1=TITLE.replace('Mavi', 'Siyah')),
            lambda d: d['action']['object'].update(sku='1245472'),
            lambda d: d['action']['object'].update(url=URL.replace(SKU, '1245472')),
            lambda d: d['action']['object']['offers'][0].update(url=URL.replace(SKU, '1245472')),
            lambda d: d['action']['object']['offers'].append(deepcopy(d['action']['object']['offers'][0])),
        ]
        for change in cases:
            with self.subTest(change=change):
                data = fixture(); change(data)
                self.assertIsNone(selected_marketplace_offer(render(data), URL))

    def test_selected_seller_price_and_offer_id_must_agree(self):
        cases = [
            lambda d: d['action']['object']['offers'][0]['seller'].update(name='Other Seller'),
            lambda d: d['apollo']['CofrPriceFeature:base']['marketplaceSeller'].update(sellerId='9'),
            lambda d: d['apollo']['GraphqlMarketplaceOfferV3:' + OFFER].update(sellerName='Other Seller'),
            lambda d: d['apollo']['GraphqlMarketplaceOfferV3:' + OFFER].update(productId='1245472'),
            lambda d: d['apollo']['GraphqlProductFeature:RAM'].update(value='8'),
            lambda d: d['apollo']['GraphqlProductFeatureGroups:' + SKU].update(productId='1245472'),
            lambda d: d['apollo']['CofrCoreFeature:Media:tr:' + SKU].update(relevantOfferId='999'),
            lambda d: d['apollo']['GraphqlProductAggregate:Media:tr-TR:' + SKU].update(
                bestMarketplaceOfferV3={'__ref': 'GraphqlMarketplaceOfferV3:999'}),
            lambda d: d['apollo']['CofrPriceFeature:base']['price'].update(amount=64197),
            lambda d: d['apollo']['CofrPriceFeature:selected']['price'].update(amount=64197),
            lambda d: d['apollo']['CofrPriceFeature:selected'].update(currency='USD'),
            lambda d: d.update(visible_price='₺ 64.197,'),
            lambda d: d.update(visible_price='₺ 78.899, sepette kupon'),
            lambda d: d.update(visible_seller='Other Seller'),
            lambda d: d.update(seller_href=d['seller_href'].replace(SELLER_ID, '9')),
        ]
        for change in cases:
            with self.subTest(change=change):
                data = fixture(); change(data)
                self.assertIsNone(selected_marketplace_offer(render(data), URL))

    def test_condition_stock_and_selected_basket_are_required(self):
        cases = [
            lambda d: d['action']['object']['offers'][0].update(
                itemCondition='https://schema.org/RefurbishedCondition'),
            lambda d: d['apollo']['GraphqlMarketplaceOfferV3:' + OFFER].update(offerCondition='VERY_GOOD'),
            lambda d: d['apollo']['CofrRefurbishedGoodsFeature:Media:tr:' + SKU + '[' + OFFER + ']']
                ['offer'].update(conditionType='VERY_GOOD'),
            lambda d: d['action']['object']['offers'][0].update(
                availability='https://schema.org/OutOfStock'),
            lambda d: d['apollo']['CofrOnlineStatusFeature:Media:tr:' + SKU].update(
                isAvailableAndBuyable=False),
            lambda d: d['apollo']['CofrOnlineStatusFeature:Media:tr:' + SKU + '[' + OFFER + ']']
                .update(isAvailableForDelivery=False),
            lambda d: d.update(stock_test='mms-cofr-delivery_UNAVAILABLE'),
            lambda d: d.update(stock='Adrese teslimata uygun değil.'),
            lambda d: d.update(button_disabled='true'),
            lambda d: d.update(button_label='Sepete Ekle başka ürün'),
        ]
        for change in cases:
            with self.subTest(change=change):
                data = fixture(); change(data)
                self.assertIsNone(selected_marketplace_offer(render(data), URL))

    def test_explicit_radio_conflict_between_title_and_url_is_rejected(self):
        data = fixture()
        title = data['h1'].replace('Ultra 12', 'Ultra 4G 12')
        data['h1'] = title
        data['action']['object']['name'] = title
        data['apollo']['CofrCoreFeature:Media:tr:' + SKU]['productName'] = title
        data['apollo']['CofrCoreFeature:Media:tr:' + SKU + '[' + OFFER + ']']['productName'] = title
        data['button_label'] = 'Sepete Ekle ' + title
        self.assertIsNone(selected_marketplace_offer(render(data), URL))


if __name__ == '__main__':
    unittest.main()
