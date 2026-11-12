import json
import unittest

from besken_phone_source import BESKEN_BASE, BESKEN_CATEGORY, BESKEN_HOST, BESKEN_TERMS, besken_offer
from competitor_catalog_gaps import category_url, comparison_cards, retailer_destination


URL = BESKEN_BASE + '/samsung-galaxy-s26-ultra-1-tb-16-gb-ram-beyaz-cep-telefonu-4265'
TITLE = 'Samsung Galaxy S26 Ultra 1 TB 16 GB RAM Beyaz Cep Telefonu'
TERMS = (f'<link rel="canonical" href="{BESKEN_TERMS}">'
         '<p>ADI/UNVANI: Beşken Elektronik San.ve Dış Tic.Ltd.Şti.</p>')


def product_document():
    data = {'@type': 'Product', 'name': TITLE, 'sku': 'S26ULTRA(16/1TB)BEYAZ', 'gtin13': '8806097866404',
            'offers': {'@type': 'Offer', 'url': URL, 'priceCurrency': 'TRY', 'price': '124999.00',
                       'availability': 'https://schema.org/InStock', 'itemCondition': 'https://schema.org/NewCondition'}}
    return f'''<link rel="canonical" href="{URL}">
      <script type="application/ld+json">{json.dumps(data, ensure_ascii=False)}</script>
      <div class="productDetails"><h1 class="product-title">{TITLE}</h1>
      <div><span class="product-price-sale"><span class="price">124.999,00 TL</span></span></div>
      <div class="product-button-container">
        <a role="button" class="product-cart-add add-to-cart-btn" title="Sepete Ekle"
            data-productid="8PeBwfTp60BvCVZxKEQEBg==" data-productname="{TITLE}"></a>
      </div></div>
      <div id="collapseProductInformation"><table>
      <tr><th>Stok Kodu</th><td>S26ULTRA(16/1TB)BEYAZ</td></tr>
      <tr><th>Barkod</th><td>8806097866404</td></tr>
      <tr><th>EAN</th><td>8806097866404</td></tr>
      <tr><th>Kullanım Durumu</th><td>Yeni</td></tr>
      <tr><th>Kategoriler</th><td><a href="{BESKEN_BASE}/samsung-cep-telefonlar">Samsung</a></td></tr>
      </table></div>'''


class BeskenPhoneTests(unittest.TestCase):
    def test_selected_offer_requires_same_sku_price_stock_and_seller(self):
        html = product_document()
        offer = besken_offer(html, TERMS, url=URL)
        self.assertEqual((offer['merchant'], offer['gtin13'], offer['price']),
                         ('Beşken Elektronik', '8806097866404', 124999))
        for original, conflict in (
            ('InStock', 'OutOfStock'),
            ('NewCondition', 'RefurbishedCondition'),
            ('>8806097866404</td>', '>8806097866312</td>'),
            ('124.999,00 TL', '124.998,00 TL'),
            ('<div><span class="product-price-sale">', '<div>Sepette <span class="product-price-sale">'),
            ('role="button"', 'role="button" aria-disabled="true"'),
            (f'data-productname="{TITLE}"', 'data-productname="Samsung Galaxy S26"'),
            ('>Yeni<', '>İkinci El<'),
            ('/samsung-cep-telefonlar', '/samsung-aksesuar'),
        ):
            with self.subTest(conflict=conflict):
                self.assertIsNone(besken_offer(html.replace(original, conflict), TERMS, url=URL))
        self.assertIsNone(besken_offer(html, TERMS.replace('Beşken Elektronik', 'Başka Satıcı'), url=URL))

    def test_category_discovers_a_link_without_using_the_card_price(self):
        category = BESKEN_BASE + BESKEN_CATEGORY
        page_two = category + '/sayfa/2'
        html = f'''<div class="product-list-wrapper"><div class="productBox">
            <p class="item-product-name"><a href="{URL}">{TITLE}</a></p>
            <span class="price">9.999 TL</span></div></div>
            <a href="/cep-telefonu/sayfa/2">2</a>'''
        cards, links = comparison_cards(html, BESKEN_HOST, category)
        self.assertEqual(list(cards), [{'id': '4265', 'title': TITLE.removesuffix(' Cep Telefonu'),
                                       'url': URL, 'retailerLeads': [URL]}])
        self.assertIn(page_two, links)
        self.assertEqual(category_url(BESKEN_HOST, BESKEN_CATEGORY, 2), page_two)
        self.assertEqual(retailer_destination(URL), URL)
        self.assertIsNone(retailer_destination(URL + '?seller=abc'))
        self.assertIsNone(retailer_destination(category))
