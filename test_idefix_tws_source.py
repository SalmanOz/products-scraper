import json
from unittest import TestCase

from idefix_tws_source import (BASE, idefix_other_seller_urls, idefix_selected_seller, idefix_seller_identity,
                               selected_idefix_offer)
from tws_offer_pilot import model_name


class IdefixTwsTest(TestCase):
    def test_selected_offer_and_seller_need_the_same_barcode_price_id_and_vendor(self):
        path = '/baseus-inspire-xp1-siyah-p-18308408'
        canonical, url = BASE + path, BASE + path + '?vendorId=33'
        title = 'Baseus Inspire XP1 Bluetooth Kulaklık - Siyah'
        name, gtin = 'Baseus Inspire XP1', '6953156299948'
        native = {'id': 1234, 'inventoryQuantity': 3, 'price': 7999, 'effectivePrice': 7999,
                  'effectivePriceType': 'price', 'isZoneSale': False}
        selected = {'id': 18308408, 'url': canonical, 'slug': path + '?vendorId=33', 'state': 'published',
                    'pageState': 'available', 'barcode': gtin, 'title': title, 'brandName': 'Baseus',
                    'currentPrice': native}
        state = {'page': '/[slug]', 'query': {'slug': path[1:], 'vendorId': '33'}, 'props': {'pageProps': {
            'productDetail': selected, 'initialState': {'pdp': {'slugId': '18308408', 'priceId': 1234}},
        }}}
        schema = {'@type': 'Product', 'name': title, 'gtin13': gtin, 'sku': gtin,
                  'offers': {'@type': 'Offer', 'url': canonical, 'price': 7999, 'priceCurrency': 'TRY',
                             'availability': 'https://schema.org/InStock', 'itemCondition': 'https://schema.org/NewCondition'}}
        html = f'''
        <link rel="canonical" href="{canonical}">
        <script id="__NEXT_DATA__" type="application/json">{json.dumps(state)}</script>
        <script type="application/ld+json">{json.dumps(schema)}</script>
        <h1>{title}</h1><span class="text-title-xl-semibold">7.999,00 TL</span>
        <div aria-label="Add To Basket Section"><button>Sepete Ekle</button></div>
        '''
        context = {}
        offer, reason = selected_idefix_offer(html, url, name, model_name, discovery=context)
        self.assertEqual(reason, 'verified')
        self.assertEqual((offer['gtin13'], offer['price'], offer['price_id']), (gtin, 7999.0, 1234))
        for before, after, reason in (
            ('"priceId": 1234', '"priceId": 2345', 'idefix_selected_stock_unverified'),
            ('"inventoryQuantity": 3', '"inventoryQuantity": 0', 'idefix_selected_stock_unverified'),
            ('"sku": "6953156299948"', '"sku": "6953156299924"', 'idefix_selected_variant_unverified'),
            ('"effectivePriceType": "price"', '"effectivePriceType": "discountedPrice"', 'idefix_selected_cash_unverified'),
        ):
            with self.subTest(reason=reason):
                self.assertEqual(selected_idefix_offer(html.replace(before, after), url, name, model_name),
                                 (None, reason))
        vendor = {'vendorId': 33, 'vendorName': 'D&R', 'vendorSlug': '/satici/dr-33', 'vendorBadges': {'isVerified': True}}
        seller = idefix_selected_seller(json.dumps(vendor), offer)
        self.assertEqual(seller, (33, 'D&R', '/satici/dr-33'))
        self.assertIsNone(idefix_selected_seller(json.dumps(vendor), {**offer, 'requested_vendor_id': 347}))
        profile = {'props': {'pageProps': {'vendorId': '33', 'vendorDetail': {
            'id': 33, 'title': 'D&R', 'legalTitle': 'T*****', 'isVerified': True, 'isActive': True,
        }}}}
        profile_html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(profile)}</script>'
        self.assertEqual(idefix_seller_identity(profile_html, seller)[0].split()[0], 'turkuvaz')
        self.assertIsNone(idefix_seller_identity(profile_html.replace('T*****', 'N*****'), seller))
        alternate = {'vendorId': 347, 'inventoryQuantity': 1, 'slug': path + '?vendorId=347'}
        self.assertEqual(idefix_other_seller_urls(json.dumps([alternate, {**alternate, 'vendorId': 3}]), context),
                         [BASE + alternate['slug']])
