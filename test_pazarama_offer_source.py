import json
import unittest
from unittest.mock import patch

from pazarama_offer_source import pazarama_selected
from selected_sku_evidence import selected_sku_evidence
from tr_price_scraper import TRPriceScraper

URL = 'https://www.pazarama.com/samsung-galaxy-a27-p-8806099359300'
SKU = 'ed329540-e487-4c66-8d7f-08def1a9b2c9'


class PazaramaSelectedTest(unittest.TestCase):
    def test_native_seller_tokens_must_match_the_selected_product_and_anchor(self):
        document = f'''<div class="product-detail"><a href="/magaza/aksaray-iletisim">Satıcı: Aksaray İletişim</a></div>
          <script>window.__NUXT__=(function(a,b,c){{return {{UI_PRODUCT_DETAIL:{{name:a,displayName:a,sellerName:b,
sellerSeoName:c,code:"8806099359300",stockCount:3,productSellerId:"{SKU}"}}}}}}("Samsung","Aksaray İletişim","aksaray-iletisim"));</script>'''.replace('\n', '')
        product = {'@type': 'Product', 'url': URL, 'sku': SKU, 'offers': {'seller': 'Aksaray İletişim'}}
        with patch('pazarama_offer_source._product_offer', return_value={'availability': 'in_stock'}), \
                patch('pazarama_offer_source._schema_nodes', return_value=[product]):
            offer = pazarama_selected(document, URL)
            self.assertEqual(offer['gtin13'], '8806099359300')
            self.assertEqual(offer['merchant_id'], 'aksaray-iletisim')
            self.assertIsNone(pazarama_selected(document.replace(',"Aksaray İletişim",', ',"Other seller",'), URL))
            self.assertIsNone(pazarama_selected(document.replace('href="/magaza/aksaray-iletisim"', 'href="/magaza/other"'), URL))

    def test_physical_specs_require_both_selected_state_and_visible_fields(self):
        fields = {'Dahili Hafıza': '256 GB', 'Ram': '8 GB', 'Ram Kapasitesi': '8 GB',
                  'Ağ/Bağlantı': '5G', 'Ürün Tipi': 'Akıllı Telefon'}
        attributes = ','.join('{attributeName:' + json.dumps(name) +
                              ',attributeValue:' + json.dumps(value) + ',isVariantable:false}'
                              for name, value in fields.items())
        visible = ''.join(f'<div><span>{name}</span><span>{value}</span></div>' for name, value in fields.items())
        product_id = '507150e8-db62-4038-a0c1-36c319d1c877'
        script = ('window.__NUXT__=(function(a){return {UI_PRODUCT_DETAIL:{name:a,displayName:a,'
                  'sellerName:"Aksaray İletişim",sellerSeoName:"aksaray-iletisim",code:"8806099359300",stockCount:3,'
                  f'attributes:[{attributes}],productId:"{product_id}",productSellerId:"{SKU}"'
                  '}}}("Samsung"));')
        html = ('<div class="product-detail"><div class="feature">' + visible +
                '</div><a href="/magaza/aksaray-iletisim">Satıcı: Aksaray İletişim</a></div>'
                f'<script>{script}</script>')
        product = {'@type': 'Product', 'url': URL, 'sku': SKU, 'offers': {'seller': 'Aksaray İletişim'}}
        cash = {'availability': 'in_stock', 'currency': 'TRY', 'url': URL,
                'source_title': 'SAMSUNG GALAXY A27 256 BLACK'}
        with patch('pazarama_offer_source._product_offer', return_value=cash), \
                patch('pazarama_offer_source._schema_nodes', return_value=[product]):
            offer = pazarama_selected(html, URL, phone=True)
            proof = selected_sku_evidence(offer)
            self.assertEqual((proof['product_id'], proof['sku'], proof['ram_gb'], proof['storage_gb']),
                             (product_id, SKU, 8, 256))
            self.assertTrue(TRPriceScraper().has_observed_offer_evidence(
                'Samsung Galaxy A27 5G', offer, {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'}))
            self.assertIsNone(pazarama_selected(html.replace('<span>256 GB</span>', '<span>128 GB</span>'), URL, phone=True))
            self.assertIsNone(pazarama_selected(html.replace('<span>8 GB</span>', '<span>6 GB</span>', 1), URL, phone=True))
            self.assertIsNone(selected_sku_evidence({**offer, 'url': URL + '?magaza=baska'}))


if __name__ == '__main__':
    unittest.main()
