import unittest
from unittest.mock import patch

from pazarama_offer_source import pazarama_selected

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


if __name__ == '__main__':
    unittest.main()
