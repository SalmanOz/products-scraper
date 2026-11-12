import json
import unittest
from decimal import Decimal

from bs4 import BeautifulSoup, Script

from competitor_catalog_gaps import comparison_cards, phone_family, retailer_destination
from metto_phone_source import (METTO_BASE, METTO_CATEGORY, METTO_HOST, METTO_TERMS, metto_offer,
                                verified_product_selection)


URL = METTO_BASE + '/oppo-a6-pro-4g-8/256gb-akilli-telefon-gul-kizili'
TITLE = 'OPPO A6 Pro 4G 8/256GB Akıllı Telefon Gül Kızılı'
GTIN = '6932169383806'
SELLER = 'BERLİNG GROUP TEKNOLOJİ LİMİTED ŞİRKETİ'
TERMS = f'<link rel="canonical" href="{METTO_TERMS}"><p>Ünvan: {SELLER}</p>'
SPECS = {'ram_gb': 8, 'storage_gb': 256}


def product_document():
    offer = {'@type': 'Offer', 'url': URL, 'price': '20999.00', 'priceCurrency': 'TRY',
             'availability': 'https://schema.org/InStock', 'itemCondition': 'https://schema.org/NewCondition',
             'seller': {'@type': 'Organization', 'name': SELLER}}
    schema = {'@type': 'Product', 'name': TITLE, 'sku': '1250448', 'mpn': '1250448', 'gtin': GTIN, 'offers': offer}
    variant = {'in_stock': True, 'quantity': '20', 'barcode': GTIN,
               'price_list': {'ID': '256 GB_4G (LTE)', 'fiyat_format_0': '20999.00', 'fiyat': '20.999,00 TL',
                              'indirimli_fiyatlar': {'sabit_indirim': {'aktif': False}, 'promosyon': {'aktif': False}}}}
    variants = {'count': 2, 'summary': {'256 GB': {'4G (LTE)': variant,
                                                 '5G': {**variant, 'barcode': '6932169383844'}}}}
    return f'''<link rel="canonical" href="{URL}">
      <div class="product-profile-1" data-product-id="13396">
        <h1 class="title">{TITLE}</h1><ul class="product-profile-info">
          <li>Ürün Kodu: <span class="value">1250448</span></li>
          <li>Barkod: <span class="value">{GTIN}</span></li></ul>
        <div class="product-price-group"><div class="variant-box">
          <select data-variant-name="Depolama"><option value="256 GB" selected>256 GB</option></select>
          <select data-variant-name="Mobil Telefon Standardı">
            <option value="4G (LTE)">4G (LTE)</option><option value="5G">5G</option>
          </select></div><div class="price-group">
            <div class="sale-variant-price">20.999,00 TL</div>
            <button class="btn-cart" onclick="addCart(13396, 'product', {{}})">Sepete Ekle</button>
        </div></div>
      </div><script type="application/ld+json">{json.dumps(schema, ensure_ascii=False)}</script>
      <script>const PRODUCT_PAGE_DATA = {{price: '20.999,00 TL', variants: {json.dumps(variants, ensure_ascii=False)}}};</script>'''


class MettoPhoneTests(unittest.TestCase):
    def test_offer_reconciles_exact_radio_barcode_merchant_and_cash(self):
        def parse(page, legal=TERMS, specs=SPECS):
            return metto_offer(page, legal, url=URL, name='OPPO A6 Pro 4G', specs=specs)

        html = product_document()
        offer = parse(html)
        self.assertEqual((offer['gtin13'], offer['price'], offer['merchant']), (GTIN, 20999, 'Metto Market'))
        for original, changed in (
            ('value="5G"', 'value="5G" selected'), ('value="4G (LTE)"', 'value="4G (LTE)" disabled'),
            ('"quantity": "20"', '"quantity": "0"'), ('"in_stock": true', '"in_stock": false'),
            ('20.999,00 TL</div>', '20.998,00 TL</div>'), ('"fiyat_format_0": "20999.00"', '"fiyat_format_0": "20998.00"'),
            ('"promosyon": {"aktif": false}', '"promosyon": {"aktif": true}'), ('addCart(13396', 'addCart(13395'),
            (f'"barcode": "{GTIN}"', '"barcode": "6932169383844"'),
            ("price: '20.999,00 TL'", "price: '20.998,00 TL'"),
        ):
            self.assertIsNone(parse(html.replace(original, changed, 1)), original)
        self.assertIsNone(parse(html, TERMS.replace(SELLER, 'Başka Mağaza')))
        self.assertIsNone(parse(html, specs={**SPECS, 'network_generation': '5g'}))

    def test_same_barcode_color_selection_and_single_sku_do_not_accept_hidden_choices(self):
        html = product_document().replace('Mobil Telefon Standardı', 'Renk').replace('4G (LTE)', 'Kızılı')
        soup = BeautifulSoup(html, 'html.parser')
        boxes = soup.select('.variant-box select')
        boxes[0].insert_before(boxes[1].extract())
        script = soup.find('script', string=lambda body: body and 'const PRODUCT_PAGE_DATA' in body)
        variants = json.JSONDecoder().raw_decode(script.string.split('variants: ', 1)[1])[0]
        entry = variants['summary']['256 GB']['Kızılı']
        entry['price_list']['ID'] = 'Kızılı_256 GB'
        variants['summary'] = {'Kızılı': {'256 GB': entry}}
        script.string = Script("const PRODUCT_PAGE_DATA = {price: '20.999,00 TL', variants: " + json.dumps(variants) + '};')
        price_box = soup.select_one('.product-price-group')
        cart = price_box.select_one('button')
        price = Decimal('20999')
        self.assertTrue(verified_product_selection(soup, price_box, cart, TITLE, SPECS, GTIN, price))
        self.assertFalse(verified_product_selection(soup, price_box, cart, TITLE.replace('Kızılı', 'Siyah'), SPECS, GTIN, price))
        price_box.select_one('.variant-box').decompose()
        self.assertFalse(verified_product_selection(soup, price_box, cart, TITLE, SPECS, GTIN, price))
        script.string = Script("const PRODUCT_PAGE_DATA = {price: '20.999,00 TL'};")
        self.assertTrue(verified_product_selection(soup, price_box, cart, TITLE, SPECS, GTIN, price))

    def test_category_keeps_canonical_product_link_and_capacity_folds_into_family(self):
        html = f'''<div class="card-product-inner"><a class="c-p-i-link" href="{URL}" title="{TITLE}">
          <span class="title">{TITLE}</span></a></div>'''
        cards, _ = comparison_cards(html, METTO_HOST, METTO_BASE + METTO_CATEGORY)
        self.assertEqual([row['retailerLeads'] for row in cards], [[URL]])
        self.assertEqual(retailer_destination(URL), URL)
        self.assertIsNone(retailer_destination(URL + '?radio=5g'))
        self.assertEqual(phone_family(TITLE), 'oppo a6 pro 4g')
