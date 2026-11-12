import json
from unittest import TestCase

from nethouse_offer_source import BASE, LEGAL_NAME, selected_nethouse_offer
from tws_offer_pilot import independent_sellers, model_name


class NethouseOfferTest(TestCase):
    def test_requires_selected_stock_barcode_and_cash_price(self):
        url = BASE + '/baseus-inspire-xp1-siyah'
        title = 'Baseus Inspire XP1 Bluetooth Kulaklık - Siyah'
        name = 'Baseus Inspire XP1'
        gtin = '6953156299948'
        product = {'@type': 'Product', 'name': title, 'sku': gtin, 'brand': {'name': 'Baseus'},
                   'offers': {'@type': 'Offer', 'url': url, 'seller': {'name': 'Nethouse'},
                              'priceCurrency': 'try', 'price': '4999', 'availability': 'InStock',
                              'itemCondition': 'NewCondition'}}
        native = {'productId': 100, 'mainVariantId': 123, 'productName': title, 'brandName': 'Baseus',
                  'productIsAsorti': False, 'productVariantData': None, 'productActive': True,
                  'stockCode': gtin, 'totalStockAmount': 2, 'productCurrency': 'TRY', 'productPriceStr': '4999',
                  'product': {'id': 123, 'urunKartiId': 100, 'barkod': gtin, 'stokKodu': gtin, 'stokAdedi': 2}}
        html = f'''
        <link rel="canonical" href="{url}">
        <script>var productDetailModel = {json.dumps(native)}; globalModel.pageType = 'productdetail';</script>
        <script type="application/ld+json">{json.dumps(product)}</script>
        <h1><span>{title}</span><span id="divUrunKodu">{gtin}</span></h1>
        <span id="spnBarkod">{gtin}</span>
        <div id="divSatinAl"><input class="Addtobasket" value="Sepete Ekle"></div>
        <div id="divIndirimliFiyat"><span id="indirimliFiyat"><span class="spanFiyat">₺4.999,00</span></span></div>
        '''
        offer, reason = selected_nethouse_offer(html, url, name, model_name)
        self.assertEqual(reason, 'verified')
        self.assertEqual((offer['gtin13'], offer['price']), (gtin, 4999.0))
        self.assertFalse(independent_sellers(offer, {'_legal_name': LEGAL_NAME, 'platform': 'n11'}))
        for old, new, reason in (
            (f'id="spnBarkod">{gtin}', 'id="spnBarkod">6953156299924', 'nethouse_selected_gtin_unverified'),
            ('"stokAdedi": 2', '"stokAdedi": 0', 'nethouse_selected_stock_unverified'),
            ('₺4.999,00', '₺4.000,00', 'nethouse_selected_cash_unverified'),
        ):
            with self.subTest(reason=reason):
                self.assertEqual(selected_nethouse_offer(html.replace(old, new), url, name, model_name),
                                 (None, reason))
