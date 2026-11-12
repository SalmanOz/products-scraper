import json
import unittest

from comparison_phone_offers import supported_phone_url
from competitor_catalog_gaps import category_url, comparison_cards, retailer_destination
from pttavm_offer_source import BASE, HOST, ptt_seller_identity, selected_ptt_offer
from tws_refresh import selected_host


URL = BASE + '/anker-soundcore-liberty-5-anc-tws-pembe-kulaklik-p-1445536859'
SHOP = BASE + '/magaza/teknopoint'
TITLE = 'Anker Soundcore Liberty 5 ANC TWS Pembe Kulaklık'
SELLER = 'Teknopoint'
COMPANY = 'Teknopoint Bilişim Ticaret Limited Şirketi'


def hydration(route, key, data):
    state = {'loaderData': {route: {'dehydratedState': {'queries': [{
        'queryKey': key, 'state': {'data': {'data': data, 'success': True}},
    }]}}}}
    return '<script>window.__staticRouterHydrationData = JSON.parse(' + json.dumps(
        json.dumps(state, ensure_ascii=False), ensure_ascii=False) + ');</script>'


def product_document(barcode='194644289546', stock=2, basket=False, listed_price='5.696,51 TL',
                     catalog_barcode=None, item_condition=None):
    product = {'@type': 'Product', 'name': TITLE, 'url': URL, 'sku': '1445536859',
               'brand': {'@type': 'Brand', 'name': 'Anker'}, 'offers': {
                   '@type': 'Offer', 'price': 5696.51, 'priceCurrency': 'TRY',
                   'availability': 'https://schema.org/InStock',
                   'seller': {'@type': 'Organization', 'name': SELLER}}}
    if item_condition:
        product['offers']['itemCondition'] = item_condition
    company = {'@type': 'Organization', 'name': SELLER, 'url': BASE + '/teknopoint'}
    state = hydration('slug-page', ['productDetail', '1445536859', False, None, None], {
        'success': True, 'info': {'id': 1445536859, 'item_group_id': 1445536859, 'url': URL.removeprefix(BASE + '/'),
            'name': TITLE, 'category': {'category_slug': 'bluetooth-kulaklik'}, 'stock': stock,
            'isPurchasable': True, 'isVariantPurchasable': True, 'brand': 'Anker',
            'externalSource': {'barcode': barcode, 'brand': 'Anker', 'catalogBarcode': catalog_barcode,
                               'isVariant': False},
            'merchant': {'id': '141502', 'name': SELLER}},
        'cart': {'addToCart': {'text': 'Sepete Ekle'}, 'price': {
            'currency': 'TL', 'originalPrice': 5696.51, 'regularPrice': 5696.51,
            'discountedPrice': 5696.51, 'originalPriceText': '5.696,51 TL', 'isPriceDiscounted': basket}}})
    return f'''<meta property="og:url" content="{URL}">
        <script type="application/ld+json">{json.dumps(product)}</script>
        <script type="application/ld+json">{json.dumps(company)}</script>{state}
        <div class="item__column"><h1 class="productName__title">{TITLE}</h1>
          <div><div class="priceRow__amount"><span class="price__selected">{listed_price}</span></div></div>
          <button class="addToCartButton__selected">Sepete Ekle</button></div>
        <a class="merchantName__selected" href="{SHOP}">{SELLER}</a>'''


def seller_document(seller_id=141502, visible_company=COMPANY):
    shop = {'url': 'teknopoint', 'id': seller_id, 'active': True, 'on_sale': True,
            'shop_name': SELLER, 'name': COMPANY, 'is_verified': 1, 'taxnumber': '-', 'mersis': ''}
    state = hydration('shop-page', ['merchantTag', 'teknopoint'], shop)
    fields = [('Ticaret Ünvanı:', visible_company), ('VKN:', '-'), ('MERSİS No:', '-')]
    return state + ''.join(f'''<div class="shopInfoItem__row">
      <div class="shopInfoLabel__label">{label}</div><div class="shopInfoValue__value">{text}</div></div>'''
                           for label, text in fields)


class PttavmOfferTests(unittest.TestCase):
    def test_selected_cash_offer_and_seller_are_bound_to_the_public_listing(self):
        offer, reason = selected_ptt_offer(product_document(), URL, 'tws')
        self.assertIsNotNone(offer, reason)
        self.assertEqual((offer['gtin13'], offer['price'], offer['merchant'], offer['seller_url']),
                         ('0194644289546', 5696.51, SELLER, SHOP))
        self.assertTrue(ptt_seller_identity(seller_document(), offer))
        self.assertIsNone(ptt_seller_identity(seller_document(seller_id=12345), offer))
        self.assertIsNone(ptt_seller_identity(seller_document(visible_company='Başka Satıcı'), offer))

    def test_selected_cash_reader_rejects_store_codes_and_conflicting_sku_stock_price(self):
        for kwargs in ({'barcode': 'HBCV00009HSBJ7'}, {'catalog_barcode': '0194644197421'},
                       {'stock': 0}, {'basket': True}, {'listed_price': '5.696,52 TL'},
                       {'item_condition': 'https://schema.org/UsedCondition'}):
            with self.subTest(kwargs=kwargs):
                self.assertIsNone(selected_ptt_offer(product_document(**kwargs), URL, 'tws')[0])
        self.assertIsNone(selected_ptt_offer(product_document(), URL, 'phone')[0])

    def test_category_cards_discover_selected_urls_without_card_prices(self):
        category = '/bluetooth-kulaklik-c-315'
        html = f'''<article class="compactArticle__grid"><a class="compactCard__product" href="{URL}">
          <h2 class="compactName__title">{TITLE} (Anker Türkiye Garantili)</h2>
          <span>1 TL</span></a></article>
          <a href="{category}?page=2">2</a>'''
        cards, links = comparison_cards(html, HOST, BASE + category)
        self.assertEqual(list(cards), [{'id': '1445536859', 'title': TITLE, 'url': URL, 'retailerLeads': [URL]}])
        self.assertIn(category_url(HOST, category, 2), links)

    def test_routing_accepts_only_bounded_canonical_public_pdp(self):
        self.assertEqual(retailer_destination(URL), URL)
        self.assertEqual(selected_host(URL), 'www.pttavm.com')
        self.assertTrue(supported_phone_url(URL, expanded=True))
        for url in (SHOP, URL + '?seller=teknopoint', URL + '#prices', URL.replace('/anker', '/yenilenmis-anker')):
            self.assertIsNone(retailer_destination(url))
            self.assertIsNone(selected_host(url))
            self.assertFalse(supported_phone_url(url, expanded=True))
