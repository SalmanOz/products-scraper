"""Exact selected SKU, first-party merchant and ordinary cash price contracts."""

import json
import unittest
from io import BytesIO
from unittest.mock import Mock

import requests

from hepsiburada_s25_fe_source import (
    HepsiburadaS25FeSource, NAME, PRODUCT_ID, ROBOTS_URL, SELLER_URL, SKU, SOLVER_URL,
    SPECS, URL,
    parse_exact_buy_box,
)
from retailer_offer_refresh import RetailerOfferRefresher


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


def solver_result(url, body, *, status=200):
    return response(SOLVER_URL, json.dumps({'status': 'ok', 'solution': {
        'status': status, 'url': url, 'response': body,
    }}))


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

    def test_only_exact_pdp_is_requested(self):
        calls = []
        def get(url, **_):
            calls.append(url)
            return response(url, 'User-agent: *\nDisallow: /product/\n'
                            if url == ROBOTS_URL else fixture())
        offer, reason, status = HepsiburadaS25FeSource(get=get).read_offer()
        self.assertEqual((reason, status, offer['price']),
                         ('observed_exact_first_party_cash_offer', 200, 36999))
        self.assertEqual(calls, [URL])
    def test_bounded_browser_reads_product_after_direct_403(self):
        calls = []
        def get(url, **_):
            calls.append(('direct', url))
            return response(url, 'denied', 403)
        def post(_url, **kwargs):
            url = kwargs['json']['url']
            calls.append(('browser', url))
            return solver_result(url, '<html><body><pre>User-agent: *\nAllow: /\n</pre></body></html>'
                                 if url == ROBOTS_URL else fixture())
        offer, reason, status = HepsiburadaS25FeSource(get=get, post=post).read_offer()
        self.assertEqual((reason, status, offer['price']),
                         ('observed_exact_first_party_cash_offer', 200, 36999))
        self.assertEqual(calls, [('direct', URL), ('browser', URL)])

    def test_browser_html_without_selected_offer_is_rejected(self):
        calls = []
        def get(url, **_):
            calls.append(('direct', url))
            return response(url, 'denied', 403)
        def post(_url, **kwargs):
            url = kwargs['json']['url']
            calls.append(('browser', url))
            return solver_result(url, '<html><body>Access denied</body></html>')
        self.assertEqual(HepsiburadaS25FeSource(get=get, post=post).read_offer(),
                         (None, 'selected_title_missing', 200))
        self.assertEqual(calls, [('direct', URL), ('browser', URL)])

    def test_browser_redirect_or_missing_selected_html_fails_closed(self):
        def get(_url, **_):
            return response(URL, 'denied', 403)
        changed = HepsiburadaS25FeSource(
            get=get, post=lambda *_a, **_k: solver_result(URL + '?x=1',
                                                            'User-agent: *\nAllow: /'))
        self.assertEqual(changed.read_offer(), (None, 'browser_changed_url', 200))
        denied = HepsiburadaS25FeSource(
            get=get, post=lambda *_a, **_k: solver_result(URL,
                                                            'User-agent: *\nDisallow: /'))
        self.assertEqual(denied.read_offer(), (None, 'selected_title_missing', 200))

    def test_only_exact_reviewed_url_uses_dedicated_reader_and_8_256_catalog(self):
        class ExactSource:
            calls = 0

            def read_offer(self):
                self.calls += 1
                offer, reason = parse_exact_buy_box(fixture())
                return offer, reason, 200

        dedicated = ExactSource()
        ordinary_requests = []

        def generic_get(url, **_kwargs):
            ordinary_requests.append(url)
            return response(url, 'denied', 403)

        reader = RetailerOfferRefresher(
            fetch=generic_get, hepsiburada_s25_fe_source=dedicated)
        target = {'source_url': URL}
        offers = reader.refresh_existing_offer_targets(NAME, [target], SPECS)
        self.assertEqual([(row['merchant'], row['price']) for row in offers],
                         [('Hepsiburada', 36999)])
        self.assertEqual(reader.target_report(NAME, [target], SPECS)[0]['reason'],
                         'observed_exact_first_party_cash_offer')
        self.assertEqual(dedicated.calls, 1)
        self.assertEqual(ordinary_requests, [])

        # The 8/128 catalog entry cannot reuse the cached 8/256 observation.
        self.assertEqual(reader.refresh_existing_offer_targets(
            'Samsung Galaxy S25 FE', [target],
            {'ram_gb': 8, 'storage_gb': 128, 'network_generation': '5g'}), [])
        self.assertEqual(dedicated.calls, 1)
        altered_url = URL + '?seller=other'
        self.assertEqual(reader.refresh_existing_offer_targets(
            NAME, [{'source_url': altered_url}], SPECS), [])
        # Unknown seller selectors are not routed to a generic JSON-LD reader.
        self.assertEqual(ordinary_requests, [])
        self.assertEqual(reader.target_report(NAME, [{'source_url': altered_url}], SPECS)[0]['reason'],
                         'unmatched_catalog_pdp')
        self.assertEqual(dedicated.calls, 1)


if __name__ == '__main__':
    unittest.main()
