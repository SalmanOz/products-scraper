"""Adversarial contracts for the read-only selected marketplace buy box."""

import html as html_library
import json
import unittest
from io import BytesIO

import requests

from audit_hepsiburada_iphone16pm_readonly import (
    COLOR, GTIN13, LISTING_ID, MERCHANT_ID, PRODUCT_ID, SCHEMA_URL,
    SELLER, SELLER_URL, SKU, URL, HepsiburadaIphone16PmAuditSource,
    parse_exact_buy_box, run_audit,
)
from hepsiburada_s25_fe_source import ROBOTS_URL, SOLVER_URL


def evidence():
    title = f'Apple iPhone 16 Pro Max 256GB {COLOR}'
    variant = {
        'sku': SKU, 'isDefault': True, 'isInStock': True,
        'isPreOrder': False, 'name': title, 'price': 129999,
        'barcode': 'LumoShop87d936ae-f11119',
        'barcodes': ['LumoShop87d936ae-f11119', '8697750216635221547', GTIN13],
        'properties': [
            {'name': 'Renk', 'valueObject': {'actualValue': COLOR}},
            {'name': 'Kapasite', 'valueObject': {'actualValue': '256 GB'}},
        ],
    }
    product = {
        'productId': PRODUCT_ID, 'sku': SKU, 'listingId': LISTING_ID,
        'merchantName': SELLER, 'merchantId': MERCHANT_ID,
        'merchant': {'id': MERCHANT_ID, 'name': SELLER,
                     'urlPostfix': 'bsz-elektronik'},
        'isInStock': True, 'stockInformation': {'isInStock': True},
        'isProductLive': True, 'isClosedProduct': False, 'isPreOrder': False,
        'prices': [{'value': 129999, 'formattedPrice': '129.999,00'}],
        'variants': [variant],
    }
    schema = {'@type': 'WebPage', 'url': SCHEMA_URL, '@graph': [{
        '@type': 'Product', 'sku': SKU, 'color': COLOR, 'name': title,
        # These two JSON-LD fields are misleading in the actual PDP.
        'gtin': 'LumoShop87d936ae-f11119',
        'offers': {'@type': 'Offer', 'url': SCHEMA_URL,
                   'priceCurrency': 'TRY', 'price': '129999.00',
                   'availability': 'https://schema.org/InStock',
                   'itemCondition': 'https://schema.org/NewCondition',
                   'seller': {'@type': 'Organization', 'name': 'Apple'}},
    }]}
    events = []
    for component in ('variant', 'Key Features'):
        events.append({'data': {
            'component': component, 'placement': 'buybox',
            'page_type': 'pdp', 'page_value': SKU,
            'product_id': PRODUCT_ID, 'listing_id': LISTING_ID,
            'merchant_id': MERCHANT_ID, 'merchant_name': SELLER,
            'price': '129999',
        }})
    return {'title': title, 'redux': {'productState': {'product': product}},
            'schema': schema, 'events': events, 'visible_seller': SELLER,
            'visible_seller_url': SELLER_URL, 'visible_price': '129.999,00 TL',
            'visible_ram': '8 GB RAM', 'visible_storage': '256 GB',
            'selected_color': COLOR, 'selected_capacity': '256 GB',
            'cart_disabled': False, 'extra_html': ''}


def fixture(changes=None):
    data = evidence()
    for path, value in (changes or {}).items():
        node = data
        for part in path[:-1]:
            node = node[part]
        node[path[-1]] = value
    title = html_library.escape(data['title'])
    seller = html_library.escape(data['visible_seller'])
    seller_url = html_library.escape(data['visible_seller_url'], quote=True)
    color = html_library.escape(data['selected_color'], quote=True)
    capacity = html_library.escape(data['selected_capacity'], quote=True)
    price = html_library.escape(data['visible_price'])
    events = ''.join('<div data-hbus="' + html_library.escape(
        json.dumps(event, ensure_ascii=False), quote=True) + '"></div>'
        for event in data['events'])
    return f'''<html><head><title>{title}</title>
      <script type="application/ld+json">{json.dumps(data['schema'], ensure_ascii=False)}</script>
      <script id="reduxStore" type="application/json">{json.dumps(data['redux'], ensure_ascii=False)}</script>
      </head><body><h1 data-test-id="title">{title}</h1>
      <div><span>Satıcı: </span><a href="{seller_url}" title="{seller}">{seller}</a></div>
      <button data-test-id="variant-box-Renk-{color}" aria-label="Seçili Renk {color}">{color}</button>
      <button data-test-id="variant-box-Kapasite-{capacity}" aria-label="Seçili Kapasite {capacity}">{capacity}</button>
      <div id="techSpecs" data-test-id="KeyFeaturesTable">
        <div>Dahili Hafıza <span>{data['visible_storage']}</span></div>
        <div>RAM Kapasitesi <span>{data['visible_ram']}</span></div>
        <div>Mobil Bağlantı Hızı <span>5G</span></div>
        <div>Stok Kodu <span>{SKU}</span></div>
      </div>
      <div data-test-id="default-price"><span>{price}</span></div>
      {events}
      <button data-test-id="addToCart" {'disabled' if data['cart_disabled'] else ''}>Sepete ekle</button>
      {data['extra_html']}
      </body></html>'''


def response(url, body, status=200):
    result = requests.Response()
    result.status_code = status
    result.url = url
    result._content = body.encode() if isinstance(body, str) else body
    result.raw = BytesIO(result._content)
    result.headers['Content-Length'] = str(len(result._content))
    return result


def solver_response(url, body, status=200):
    return response(SOLVER_URL, json.dumps({'status': 'ok', 'solution': {
        'status': status, 'url': url, 'response': body}}))


class HepsiburadaIphone16PmAuditTests(unittest.TestCase):
    def test_synthetic_schema_gtin_and_brand_seller_are_never_offer_identity(self):
        offer, reason = parse_exact_buy_box(fixture())
        self.assertEqual(reason, 'selected_new_cash_offer_observed')
        self.assertEqual((offer['merchant'], offer['gtin13'], offer['sku'],
                          offer['listing_id'], offer['price'], offer['condition']),
                         (SELLER, GTIN13, SKU, LISTING_ID, 129999, 'new'))
        self.assertNotEqual(offer['merchant'], 'Apple')
        self.assertNotEqual(offer['gtin13'], 'LumoShop87d936ae-f11119')
        # An attractive but false JSON-LD GTIN cannot rescue a changed
        # selected variant barcode.
        self.assertEqual(parse_exact_buy_box(fixture({
            ('schema', '@graph', 0, 'gtin'): GTIN13,
            ('redux', 'productState', 'product', 'variants', 0, 'barcodes'):
                ['LumoShop87d936ae-f11119']})),
            (None, 'selected_redux_variant_mismatch'))

    def test_selected_identity_and_retailer_drift_fail_closed(self):
        cases = [
            ({('redux', 'productState', 'product', 'sku'): 'HBCV_OTHER'},
             'selected_redux_offer_mismatch'),
            ({('redux', 'productState', 'product', 'listingId'): 'other-listing'},
             'selected_redux_offer_mismatch'),
            ({('redux', 'productState', 'product', 'merchantId'): 'other-merchant'},
             'selected_redux_offer_mismatch'),
            ({('redux', 'productState', 'product', 'merchant', 'name'): 'Apple'},
             'selected_redux_merchant_mismatch'),
            ({('redux', 'productState', 'product', 'variants', 0, 'properties', 1,
               'valueObject', 'actualValue'): '512 GB'},
             'selected_redux_variant_mismatch'),
            ({('redux', 'productState', 'product', 'variants', 0, 'barcodes'):
                  [GTIN13, '0195949805981']},
             'selected_redux_variant_mismatch'),
            ({('redux', 'productState', 'product', 'variants'):
                  [evidence()['redux']['productState']['product']['variants'][0],
                   {**evidence()['redux']['productState']['product']['variants'][0],
                    'sku': 'OTHER', 'isDefault': True}]},
             'selected_redux_variant_mismatch'),
            ({('visible_seller',): 'İçke İletişim Premium'},
             'selected_visible_seller_mismatch'),
            ({('selected_capacity',): '512 GB'},
             'selected_controls_mismatch'),
            ({('visible_ram',): '12 GB RAM'},
             'technical_variant_mismatch'),
            ({('events', 0, 'data', 'merchant_name'): 'İçke İletişim Premium'},
             'selected_buy_box_event_mismatch'),
            ({('events', 1, 'data', 'listing_id'): 'other-listing'},
             'selected_buy_box_event_mismatch'),
        ]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                self.assertEqual(parse_exact_buy_box(fixture(changes)), (None, expected))

    def test_stock_condition_and_cash_price_are_independent_checks(self):
        cases = [
            ({('redux', 'productState', 'product', 'stockInformation', 'isInStock'): False},
             'selected_redux_offer_mismatch'),
            ({('redux', 'productState', 'product', 'isPreOrder'): True},
             'selected_redux_offer_mismatch'),
            ({('redux', 'productState', 'product', 'variants', 0, 'isInStock'): False},
             'selected_redux_variant_mismatch'),
            ({('title',): 'Yenilenmiş Apple iPhone 16 Pro Max 256GB Çöl Titanyum'},
             'selected_variant_mismatch'),
            ({('schema', '@graph', 0, 'offers', 'itemCondition'):
                  'https://schema.org/RefurbishedCondition'},
             'schema_cash_new_stock_mismatch'),
            ({('schema', '@graph', 0, 'offers', 'price'): '120000.00'},
             'schema_cash_new_stock_mismatch'),
            ({('visible_price',): '99.999,00 TL'},
             'visible_cash_price_mismatch'),
            ({('events', 0, 'data', 'price'): '119999'},
             'selected_buy_box_event_mismatch'),
            ({('cart_disabled',): True}, 'selected_cart_unavailable'),
            ({('extra_html',): '<p>Sepette 129.999,00 TL</p>'},
             'conditional_price'),
        ]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                self.assertEqual(parse_exact_buy_box(fixture(changes)), (None, expected))

    def test_redirect_and_challenge_cannot_be_parsed(self):
        self.assertEqual(parse_exact_buy_box(fixture(), URL + '?magaza=other'),
                         (None, 'wrong_or_oversized_pdp'))
        self.assertEqual(parse_exact_buy_box(
            '<html><title>Access denied</title><body>Captcha</body></html>'),
            (None, 'challenge_page'))

    def test_robots_preflight_and_exact_request_budget(self):
        calls = []

        def get(url, **_):
            calls.append(url)
            return response(url, 'User-agent: *\nAllow: /\n' if url == ROBOTS_URL
                            else fixture())

        report = run_audit(HepsiburadaIphone16PmAuditSource(get=get))
        self.assertTrue(report['verified'])
        self.assertFalse(report['writes'])
        self.assertEqual(calls, [ROBOTS_URL, URL])
        self.assertEqual(report['offer']['merchant'], SELLER)

        calls.clear()

        def deny(url, **_):
            calls.append(url)
            return response(url, 'User-agent: *\nDisallow: /\n')

        self.assertEqual(HepsiburadaIphone16PmAuditSource(get=deny).read_offer(),
                         (None, 'robots_disallowed', None))
        self.assertEqual(calls, [ROBOTS_URL])

    def test_403_browser_fallback_stays_exact_and_429_never_falls_back(self):
        calls = []

        def get(url, **_):
            calls.append(('direct', url))
            return response(url, 'denied', 403)

        def post(_url, **kwargs):
            url = kwargs['json']['url']
            calls.append(('browser', url))
            body = ('<html><pre>User-agent: *\nAllow: /\n</pre></html>'
                    if url == ROBOTS_URL else fixture())
            return solver_response(url, body)

        offer, reason, status = HepsiburadaIphone16PmAuditSource(
            get=get, post=post).read_offer()
        self.assertEqual((reason, status, offer['price']),
                         ('selected_new_cash_offer_observed', 200, 129999))
        self.assertEqual(calls, [
            ('direct', ROBOTS_URL), ('browser', ROBOTS_URL),
            ('direct', URL), ('browser', URL),
        ])

        calls.clear()

        def get_429(url, **_):
            calls.append(('direct', url))
            return response(url, 'User-agent: *\nAllow: /\n' if url == ROBOTS_URL
                            else 'blocked', 200 if url == ROBOTS_URL else 429)

        self.assertEqual(HepsiburadaIphone16PmAuditSource(
            get=get_429, post=post).read_offer(), (None, 'http_429', 429))
        self.assertEqual(calls, [('direct', ROBOTS_URL), ('direct', URL)])

        calls.clear()

        def wrong_final_url(_url, **kwargs):
            url = kwargs['json']['url']
            calls.append(('browser', url))
            return solver_response(url + '?changed', fixture())

        self.assertEqual(HepsiburadaIphone16PmAuditSource(
            get=get, post=wrong_final_url).read_offer(),
            (None, 'robots_browser_changed_url', 200))
        self.assertEqual(calls, [('direct', ROBOTS_URL), ('browser', ROBOTS_URL)])


if __name__ == '__main__':
    unittest.main()
