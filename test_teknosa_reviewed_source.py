import json
import unittest
from unittest.mock import Mock

from bs4 import BeautifulSoup

from retailer_offer_refresh import _product_offer
from teknosa_reviewed_source import (
    EAN, PRODUCT_ID, ROBOTS_URL, URL, TeknosaReviewedSource,
    parse_exact_first_party_offer,
)


TITLE = 'Apple iPhone 17e 256GB Beyaz Akıllı Telefon'


def page(*, seller='TEKNOSA', price='68999.00', visible='68.999 TL',
         title=TITLE, stock='Y', sku=PRODUCT_ID, cart_seller=None):
    schema = {
        '@type': 'WebPage', 'url': URL,
        '@graph': {'@type': 'Product', 'name': title, 'sku': sku,
                   'offers': {'@type': 'Offer', 'url': URL,
                              'priceCurrency': 'TRY', 'price': price,
                              'availability': 'https://schema.org/InStock',
                              'itemCondition': 'https://schema.org/NewCondition',
                              'seller': {'name': seller}}},
    }
    return f'''<html><head><title>{title}</title>
      <script type="application/ld+json">{json.dumps(schema)}</script></head>
      <body><h1 class="pdp-title">{title}</h1>
      <div class="pdp-prices"><div class="pdp-prc2"><span class="prc">{visible}</span></div></div>
      <div class="pdp-seller-info"><span>Satıcı:</span><b>{seller}</b></div>
      <button id="addToCartButton" data-product-id="{PRODUCT_ID}"
        data-product-ean="{EAN}" data-product-name="{title}"
        data-product-url="/{URL.split('/', 3)[3]}"
        data-shop-name="{cart_seller or seller}" data-product-shop-id="1001"
        data-product-stock="{stock}" data-product-stock-status="Satışta"
        data-product-price="{price}" data-product-discounted-price="{price}">Sepete Ekle</button>
      </body></html>'''


class Response:
    def __init__(self, url, status, body):
        self.url = url
        self.status_code = status
        self.headers = {'Content-Length': str(len(body))}
        self._body = body

    def iter_content(self, chunk_size):
        yield self._body

    def close(self):
        pass


class TeknosaReviewedSourceTests(unittest.TestCase):
    def test_exact_first_party_cash_offer(self):
        offer, reason = parse_exact_first_party_offer(page())
        self.assertEqual(reason, 'observed_exact_first_party_cash_offer')
        self.assertEqual((offer['merchant'], offer['price']), ('Teknosa', 68999.0))

    def test_rejects_marketplace_selected_seller_even_on_same_product_url(self):
        for html in (page(seller='Kamer Online'),
                     page(cart_seller='Kamer Online')):
            with self.subTest(html=html[-230:]):
                offer, reason = parse_exact_first_party_offer(html)
                self.assertIsNone(offer)
                self.assertIn(reason, {'schema_first_party_offer_missing',
                                       'selected_cart_offer_mismatch'})
        self.assertIsNone(_product_offer(BeautifulSoup(page(seller='Kamer Online'),
                                                      'html.parser'), URL))

    def test_rejects_variant_price_stock_and_destination_drift(self):
        cases = [
            (page(title='Apple iPhone 17 256GB Beyaz Akıllı Telefon'),
             'selected_variant_mismatch'),
            (page(title='Apple iPhone 17e 128GB Beyaz Akıllı Telefon'),
             'selected_variant_mismatch'),
            (page(title='Apple iPhone 17e 256GB Beyaz OUTLET'),
             'selected_variant_mismatch'),
            (page(sku='WRONG'), 'schema_identity_missing'),
            (page(visible='72.999 TL'), 'visible_cash_price_mismatch'),
            (page(stock='N'), 'selected_cart_offer_mismatch'),
        ]
        for html, expected in cases:
            with self.subTest(reason=expected):
                self.assertEqual(parse_exact_first_party_offer(html)[1], expected)
        self.assertEqual(parse_exact_first_party_offer(page(), URL + '?shopId=teknosa')[1],
                         'wrong_or_oversized_pdp')

    def test_robots_denial_blocks_pdp_request(self):
        calls = []

        def get(url, **kwargs):
            calls.append(url)
            return Response(url, 200, b'User-agent: *\nDisallow: /\n')

        offer, reason, status = TeknosaReviewedSource(get=get).read_offer()
        self.assertIsNone(offer)
        self.assertEqual((reason, status), ('robots_disallowed', 200))
        self.assertEqual(calls, [ROBOTS_URL])

    def test_bounded_403_browser_fallback_reads_only_robots_and_reviewed_pdp(self):
        requests = []
        def get(url, **kwargs):
            requests.append(('get', url))
            return Response(url, 403, b'blocked')

        def post(url, *, json, **kwargs):
            target = json['url']
            requests.append(('browser', target))
            body = ('User-agent: *\nDisallow: /sepet\n' if target == ROBOTS_URL
                    else page())
            payload = {'status': 'ok', 'solution':
                       {'status': 200, 'url': target, 'response': body}}
            return Response(url, 200, __import__('json').dumps(payload).encode())

        reader = TeknosaReviewedSource(get=get, post=post, sleep=Mock())
        offer, reason, status = reader.read_offer()
        self.assertEqual((offer['price'], reason, status),
                         (68999.0, 'observed_exact_first_party_cash_offer', 200))
        self.assertEqual(requests, [('get', ROBOTS_URL), ('browser', ROBOTS_URL),
                                    ('get', URL), ('browser', URL)])
        self.assertEqual(reader.read_offer(URL + '?shopId=teknosa')[1], 'unreviewed_pdp')


if __name__ == '__main__':
    unittest.main()
