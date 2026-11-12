import json
import unittest

from competitor_catalog_gaps import comparison_cards, retailer_destination
from serinal_phone_source import SERINAL_BASE, SERINAL_CATEGORY, SERINAL_HOST, SERINAL_TERMS, serinal_offer


URL = SERINAL_BASE + '/samsung-galaxy-z-fold8-ultra-512gb-grafit-gri'
TITLE = 'Samsung Galaxy Z Fold8 Ultra 512 GB 12 GB RAM Grafit Gri'
SPECS = {'ram_gb': 12, 'storage_gb': 512, 'network_generation': '5g'}
TERMS = (f'<link rel="canonical" href="{SERINAL_TERMS}"><div>AD- SOYAD: Serinal iletişim hiz. '
         've iklimlendirme sist. san tic ltd şti</div>')


def product():
    data = {'@type': 'Product', 'name': TITLE, 'sku': 'SM-F976BZKCTUR', 'mpn': 'SM-F976BZKCTUR',
            'gtin': '8806099223236', 'brand': {'name': 'Samsung'},
            'offers': {'@type': 'Offer', 'url': URL, 'priceCurrency': 'TRY', 'price': '142900.00',
                       'availability': 'https://schema.org/InStock', 'itemCondition': 'https://schema.org/NewCondition',
                       'seller': {'name': 'SERİNAL İLEŞİM HİZ. VE İKLİMLENDİRME SİST. SAN TİC LDT ŞTİ'}}}
    return f'''<link rel="canonical" href="{URL}">
      <script type="application/ld+json">{json.dumps(data, ensure_ascii=False)}</script>
      <div class="product-profile-1" data-product-id="1649"><h1 class="title">{TITLE}</h1>
      <ul class="product-profile-info"><li>Ürün Kodu: <span class="value">SM-F976BZKCTUR</span></li>
      <li>Barkod: <span class="value">8806099223236</span></li>
      <li><a href="{SERINAL_BASE}{SERINAL_CATEGORY}">Akıllı Telefonlar</a></li></ul>
      <div class="product-price-group"><div class="sale-variant-price">142.900,00 TL</div></div>
      <button class="btn-cart" onclick="addCart(1649, 'product', {{}})">Sepete Ekle</button></div>'''


class SerinalPhoneTests(unittest.TestCase):
    def test_selected_offer_needs_matching_stock_cash_barcode_and_seller(self):
        html = product()
        parse = lambda h, terms=TERMS: serinal_offer(h, terms, url=URL, name='Samsung Galaxy Z Fold8 Ultra', specs=SPECS)
        offer = parse(html)
        self.assertEqual((offer['merchant'], offer['gtin13'], offer['price']), ('Serinal', '8806099223236', 142900))
        for old, new in [('InStock', 'OutOfStock'), ('NewCondition', 'UsedCondition'),
                         ('142.900,00 TL', '142.800,00 TL'), ('142.900,00 TL', 'Sepette 142.900,00 TL'),
                         ('addCart(1649', 'addCart(1650'), ('btn-cart"', 'btn-cart" disabled'),
                         ('>8806099223236</span>', '>8806099223601</span>'),
                         ('SERİNAL İLEŞİM', 'FARKLI SATICI')]:
            with self.subTest(change=new):
                self.assertIsNone(parse(html.replace(old, new)))
        self.assertIsNone(parse(html, TERMS.replace('Serinal iletişim', 'Başka mağaza')))

    def test_category_discovers_product_url_without_carrying_the_card_price(self):
        html = (f'<div class="card-product-inner"><a class="c-p-i-link" title="{TITLE}" href="{URL}">'
                f'<div class="title">{TITLE}</div><div class="price-group">1 TL</div></a></div>')
        rows, _ = comparison_cards(html, SERINAL_HOST, SERINAL_BASE + SERINAL_CATEGORY)
        self.assertEqual(list(rows), [{'id': URL.split('samsung-galaxy-')[1], 'title': TITLE,
                                      'url': URL, 'retailerLeads': [URL]}])
        self.assertEqual(retailer_destination(URL), URL)
        self.assertIsNone(retailer_destination(URL + '?seller=abc'))
        self.assertIsNone(retailer_destination(SERINAL_BASE + SERINAL_CATEGORY))
