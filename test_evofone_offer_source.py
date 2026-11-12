import json
import unittest

from evofone_offer_source import BASE, COMPANY, TERMS, product_url, selected_offer, seller_identity


URL = BASE + '/nothing-ear-3-siyah'
TITLE = 'Nothing Ear (3) Siyah'
BARCODE = '6974434226784'


def listing(*, checkout_changes=None, sku_changes=None, schema_changes=None, button='Sepete Ekle',
            display_price='₺8.999,00', campaign='display:none'):
    sku = {'id': 2671, 'urunKartiId': 1399, 'urunAdi': TITLE, 'stokKodu': '3000001107',
           'barkod': BARCODE, 'stokAdedi': 3, 'aktif': True, 'anaUrun': True}
    sku.update(sku_changes or {})
    checkout = {'productId': 1399, 'mainVariantId': 2671, 'productName': TITLE, 'brandName': 'NOTHING (R)',
                'stockCode': '3000001107', 'productIsAsorti': False, 'isPackageProduct': False, 'productType': 'Urun',
                'productCategoryId': 123, 'productActive': True, 'totalStockAmount': 3, 'productCurrency': 'TRY',
                'productPriceKDVIncluded': 8999.00004, 'fiyatGoster': True, 'products': [sku], 'product': sku}
    checkout.update(checkout_changes or {})
    offer = {'@type': 'Offer', 'url': URL, 'availability': 'InStock', 'itemCondition': 'NewCondition',
             'priceCurrency': 'TRY', 'price': '8999.00', 'seller': {'name': 'EVOFONE TEKNOLOJİ A.Ş.'}}
    offer.update(schema_changes or {})
    product = {'@context': 'https://schema.org', '@type': 'Product', 'name': TITLE,
               'brand': {'name': 'NOTHING (R)'}, 'offers': offer}
    return f'''
      <link rel="canonical" href="{URL}">
      <script type="application/ld+json">{json.dumps(product)}</script>
      <script>var productDetailModel = {json.dumps(checkout)};</script>
      <div class="RightDetail">
        <div class="ProductName"><h1>{TITLE}</h1></div>
        <div id="divUrunKampanyaliFiyat" style="{campaign}">Sepetteki Son Fiyat</div>
        <div id="divFiyatAlanlari"><div><span class="spanFiyat">{display_price}</span></div></div>
        <div id="divSatinAl"><input class="btnAddBasketOnDetail" value="{button}"></div>
      </div>
    '''


class EvofoneOfferSourceTest(unittest.TestCase):
    def test_selects_only_current_checkout_barcode_and_cash_price(self):
        offer, status = selected_offer(listing(), URL, COMPANY, kind='tws')
        self.assertEqual(status, 'verified')
        self.assertEqual((offer['gtin13'], offer['price'], offer['_legal_name'], offer['brand']),
                         (BARCODE, 8999, COMPANY, 'Nothing'))
        self.assertIsNone(selected_offer(listing(), URL, COMPANY, kind='phone')[0])

    def test_rejects_ambiguous_identity_and_outlet_urls(self):
        for changes in ({'products': []}, {'mainVariantId': 2672}, {'productIsAsorti': True}):
            self.assertIsNone(selected_offer(listing(checkout_changes=changes), URL, COMPANY, kind='tws')[0])
        self.assertIsNone(selected_offer(listing(sku_changes={'barkod': '0'}), URL, COMPANY, kind='tws')[0])
        for url in (URL + '-outlet', URL + '?variant=1', URL.replace('evofone.com', 'evofone.com.invalid')):
            self.assertFalse(product_url(url))

    def test_rejects_unavailable_or_conditional_offers(self):
        for document in (listing(sku_changes={'stokAdedi': 0}), listing(button='Gelince Haber Ver'),
                         listing(schema_changes={'availability': 'OutOfStock'}), listing(display_price='₺8.499,00'),
                         listing(campaign='display:block')):
            self.assertIsNone(selected_offer(document, URL, COMPANY, kind='tws')[0])

    def test_legal_terms_require_the_same_seller(self):
        def terms(name):
            return f'<link rel="canonical" href="{TERMS}"><div class="html-context"><p><strong>Ünvanı</strong><span>{name}</span></p></div>'
        self.assertEqual(seller_identity(terms('EVOFONE TEKNOLOJİ ANONİM ŞİRKETİ')), COMPANY)
        self.assertIsNone(seller_identity(terms('Başka bir mağaza')))
        self.assertIsNone(selected_offer(listing(), URL, None, kind='tws')[0])
