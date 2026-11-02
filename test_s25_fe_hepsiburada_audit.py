"""Exact selected SKU, first-party merchant and ordinary cash price contracts."""

import json
import unittest
from io import BytesIO
from unittest.mock import Mock

import requests

from hepsiburada_s25_fe_source import (
    HepsiburadaS25FeSource, PRODUCT_ID, ROBOTS_URL, SELLER_URL, SKU, URL,
    parse_exact_buy_box,
)


def fixture(*, seller='Hepsiburada', event_seller='Hepsiburada', price=36999,
            schema_price=None, storage=256, cart=True, conditional=False,
            schema_seller='Samsung'):
    title = f'Samsung Galaxy S25 Fe {storage} GB 8 GB Ram (Samsung Türkiye Garantili) Gece Siyahı'
    offer = {'@type': 'Offer', 'url': URL, 'availability': 'https://schema.org/InStock',
             'itemCondition': 'https://schema.org/NewCondition',
             'priceCurrency': 'TRY', 'price': str(schema_price or price),
             'seller': {'@type': 'Organization', 'name': schema_seller}}
    schema = {'@type': 'WebPage', 'url': URL, '@graph': [{
        '@type': 'Product', 'name': title, 'sku': SKU, 'color': 'Gece Siyahı',
        'offers': offer}]}
    event = {'data': {'page_type': 'pdp', 'placement': 'buybox',
                      'merchant_name': event_seller, 'product_id': PRODUCT_ID,
                      'page_value': SKU, 'price': str(price)}}
    cash = f'{price:,.2f}'.replace(',', '_').replace('.', ',').replace('_', '.')
    return f'''<html><head><title>{title}</title>
      <script type="application/ld+json">{json.dumps(schema, ensure_ascii=False)}</script>
      </head><body><h1 data-test-id="title">{title}</h1>
      <div><span>Satıcı: </span><a href="{SELLER_URL}" title="{seller}">{seller}</a></div>
      <button data-test-id="variant-box-Kapasite-256 GB" aria-label="Seçili Kapasite 256 GB">256 GB</button>
      <button data-test-id="variant-box-Renk-Gece Siyahı" aria-label="Seçili Renk Gece Siyahı">Siyah</button>
      <div data-test-id="default-price"><div><span>{cash} TL</span></div>
      <div data-hbus='{json.dumps(event, ensure_ascii=False)}'></div></div>
      <button data-test-id="addToCart" {'disabled' if not cart else ''}>Sepete ekle</button>
      {'<p>Sepette ' + cash + ' TL</p>' if conditional else ''}
      </body></html>'''


def response(url, body, status=200):
    result = requests.Response()
    result.status_code = status
    result.url = url
    result._content = body.encode() if isinstance(body, str) else body
    result.raw = BytesIO(result._content)
    result.headers['Content-Length'] = str(len(result._content))
    return result


class HepsiburadaS25FeAuditTests(unittest.TestCase):
    def test_exact_first_party_selected_buy_box(self):
        offer, reason = parse_exact_buy_box(fixture())
        self.assertEqual(reason, 'observed_exact_first_party_cash_offer')
        self.assertEqual((offer['merchant'], offer['price'], offer['availability'], offer['url']),
                         ('Hepsiburada', 36999, 'in_stock', URL))

    def test_marketplace_or_wrong_variant_cannot_be_labeled_hepsiburada(self):
        for changed, expected in (
            ({'seller': 'Another Store'}, 'selected_visible_seller_mismatch'),
            ({'event_seller': 'Another Store'}, 'selected_buy_box_merchant_mismatch'),
            ({'schema_seller': 'Another Store'}, 'unexpected_schema_seller'),
            ({'storage': 128}, 'selected_variant_mismatch'),
            ({'cart': False}, 'selected_cart_unavailable'),
        ):
            with self.subTest(changed=changed):
                offer, reason = parse_exact_buy_box(fixture(**changed))
                self.assertIsNone(offer)
                self.assertEqual(reason, expected)

    def test_conditional_or_conflicting_price_rejected(self):
        for changed, expected in (
            ({'schema_price': 38000}, 'visible_cash_price_mismatch'),
            ({'conditional': True}, 'conditional_price'),
        ):
            with self.subTest(changed=changed):
                self.assertEqual(parse_exact_buy_box(fixture(**changed)), (None, expected))

    def test_only_robots_allowed_exact_pdp_is_requested(self):
        calls = []
        def get(url, **_):
            calls.append(url)
            return response(url, 'User-agent: *\nDisallow: /product/\n'
                            if url == ROBOTS_URL else fixture())
        offer, reason, status = HepsiburadaS25FeSource(get=get).read_offer()
        self.assertEqual((reason, status, offer['price']),
                         ('observed_exact_first_party_cash_offer', 200, 36999))
        self.assertEqual(calls, [ROBOTS_URL, URL])
        blocked = []
        def deny(url, **_):
            blocked.append(url)
            return response(url, 'User-agent: *\nDisallow: /\n')
        self.assertEqual(HepsiburadaS25FeSource(get=deny).read_offer(),
                         (None, 'robots_disallowed', None))
        self.assertEqual(blocked, [ROBOTS_URL])

    def test_403_does_not_trigger_unreviewed_browser_fallback(self):
        calls = []
        def get(url, **_):
            calls.append(url)
            return response(url, 'User-agent: *\nAllow: /\n'
                            if url == ROBOTS_URL else 'denied',
                            200 if url == ROBOTS_URL else 403)
        self.assertEqual(HepsiburadaS25FeSource(get=get).read_offer(),
                         (None, 'http_403', 403))
        self.assertEqual(calls, [ROBOTS_URL, URL])


if __name__ == '__main__':
    unittest.main()
