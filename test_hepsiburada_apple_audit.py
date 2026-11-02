"""Contracts for three reviewed Apple buy boxes; no network or writes."""

import json
import unittest
from io import BytesIO
from pathlib import Path

import requests

from audit_hepsiburada_apple import run_audit
from hepsiburada_apple_source import (
    BY_URL, REVIEWED_PDPS, HepsiburadaAppleSource, parse_exact_buy_box,
)
from hepsiburada_s25_fe_source import ROBOTS_URL, SELLER_URL, SOLVER_URL
from retailer_offer_refresh import RetailerOfferRefresher


def fixture(target, *, seller='Hepsiburada', event_seller='Hepsiburada',
            schema_seller='Apple', price=66229, schema_price=None,
            event_price=None, title_storage=256, details_storage=256,
            sku=None, cart=True, conditional=False, stock=True):
    title = f'{target.name} {title_storage} GB {target.color}'
    cash = f'{price:,.2f}'.replace(',', '_').replace('.', ',').replace('_', '.')
    schema = {'@type': 'WebPage', 'url': target.schema_url, '@graph': [{
        '@type': 'Product', 'name': f'{target.name} 256 GB', 'sku': sku or target.sku,
        'color': target.color, 'offers': {
            '@type': 'Offer', 'url': target.schema_url,
            'availability': ('https://schema.org/InStock' if stock else
                             'https://schema.org/OutOfStock'),
            'itemCondition': 'https://schema.org/NewCondition',
            'priceCurrency': 'TRY', 'price': str(schema_price if schema_price is not None else price),
            'seller': {'@type': 'Organization', 'name': schema_seller},
        },
    }]}
    events = []
    for component in ('variant', 'Key Features'):
        event = {'data': {
            'component': component, 'page_type': 'pdp', 'placement': 'buybox',
            'merchant_name': event_seller,
            'merchant_id': 'e750130c-d0ff-469f-8767-66a63af8ea0c',
            'product_id': target.product_id, 'page_value': target.sku,
            'price': str(event_price if event_price is not None else price),
        }}
        events.append(f"<div data-hbus='{json.dumps(event)}'></div>")
    return f'''<html><head><title>{title}</title>
      <script type="application/ld+json">{json.dumps(schema)}</script>
      </head><body><h1 data-test-id="title">{title}</h1>
      <div><span>Satıcı: </span><a href="{SELLER_URL}" title="{seller}">{seller}</a></div>
      <button data-test-id="variant-box-Kapasite-256 GB" aria-label="Seçili Kapasite 256 GB">256 GB</button>
      <button data-test-id="variant-box-Renk-{target.color}" aria-label="Seçili Renk {target.color}">{target.color}</button>
      <div id="techSpecs" data-test-id="KeyFeaturesTable" class="generated-class">
        <div><div>Dahili Hafıza</div><div><span>{details_storage} GB</span></div></div>
        <div><div>RAM  Kapasitesi</div><div><span>{target.ram_gb} GB RAM</span></div></div>
        <div><div>Mobil Bağlantı Hızı</div><div><span>5G</span></div></div>
      </div>
      <div data-test-id="default-price"><div><span>{cash} TL</span></div></div>
      {''.join(events)}
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


class HepsiburadaAppleAuditTests(unittest.TestCase):
    def test_reviewed_batch_matches_only_the_three_reader_urls(self):
        path = Path(__file__).parent / 'offer_batches/hepsiburada-apple-exact-2026-11-02.json'
        rows = json.loads(path.read_text())['products']
        self.assertEqual([row['id'] for row in rows], [248, 296, 297])
        for row, target in zip(rows, REVIEWED_PDPS):
            self.assertEqual((row['slug'], row['name'], row['ram_gb'],
                              row['storage_gb'], row['network_generation'], row['targets']),
                             (target.slug, target.name, target.ram_gb, 256, '5g', [target.url]))
            self.assertNotIn('price', row)

    def test_three_exact_first_party_selected_buy_boxes(self):
        self.assertEqual(len(REVIEWED_PDPS), 3)
        prices = [66229, 137999, 149999.02]
        for target, price in zip(REVIEWED_PDPS, prices):
            with self.subTest(target=target.slug):
                offer, reason = parse_exact_buy_box(fixture(target, price=price), target.url)
                self.assertEqual(reason, 'observed_exact_first_party_cash_offer')
                self.assertEqual((offer['merchant'], offer['price'], offer['url']),
                                 ('Hepsiburada', price, target.url))

    def test_marketplace_wrong_sku_or_conditional_price_is_rejected(self):
        target = REVIEWED_PDPS[0]
        for changed, expected in (
            ({'seller': 'Third Party'}, 'selected_visible_seller_mismatch'),
            ({'event_seller': 'Third Party'}, 'selected_buy_box_merchant_mismatch'),
            ({'schema_seller': 'Third Party'}, 'unexpected_schema_seller'),
            ({'title_storage': 512}, 'selected_variant_mismatch'),
            ({'details_storage': 512}, 'technical_variant_mismatch'),
            ({'sku': 'HBCV0000OTHER'}, 'schema_identity_missing'),
            ({'cart': False}, 'selected_cart_unavailable'),
            ({'conditional': True}, 'conditional_price'),
            ({'stock': False}, 'schema_exact_offer_missing'),
            ({'schema_price': 67000}, 'visible_cash_price_mismatch'),
            ({'event_price': 67000}, 'selected_buy_box_merchant_mismatch'),
        ):
            with self.subTest(changed=changed):
                self.assertEqual(parse_exact_buy_box(fixture(target, **changed), target.url),
                                 (None, expected))

    def test_17e_seller_query_must_be_exact_and_rechecked(self):
        target = REVIEWED_PDPS[0]
        self.assertIn('?magaza=Hepsiburada&', target.url)
        self.assertEqual(parse_exact_buy_box(fixture(target), target.schema_url),
                         (None, 'unreviewed_or_oversized_pdp'))
        self.assertEqual(parse_exact_buy_box(fixture(target), target.url,
                                             final_url=target.schema_url),
                         (None, 'unreviewed_or_oversized_pdp'))
        self.assertEqual(parse_exact_buy_box(fixture(target, seller='OptimumBar'), target.url),
                         (None, 'selected_visible_seller_mismatch'))

    def test_robots_denial_stops_before_product(self):
        calls = []
        def get(url, **_):
            calls.append(url)
            return response(url, 'User-agent: *\nDisallow: /\n')
        reader = HepsiburadaAppleSource(get=get)
        self.assertEqual(reader.read_offer(REVIEWED_PDPS[0].url),
                         (None, 'robots_disallowed', None))
        self.assertEqual(calls, [ROBOTS_URL])
        self.assertEqual(reader.read_offer('https://www.hepsiburada.com/unreviewed'),
                         (None, 'unreviewed_apple_pdp', None))
        self.assertEqual(calls, [ROBOTS_URL])

    def test_bounded_browser_fallback_uses_exact_query_url(self):
        target = REVIEWED_PDPS[0]
        calls = []
        def get(url, **_):
            calls.append(('direct', url))
            return response(url, 'denied', 403)
        def post(_url, **kwargs):
            url = kwargs['json']['url']
            calls.append(('browser', url))
            return solver_result(url, '<html><pre>User-agent: *\nAllow: /\n</pre></html>'
                                 if url == ROBOTS_URL else fixture(target))
        offer, reason, status = HepsiburadaAppleSource(get=get, post=post).read_offer(target.url)
        self.assertEqual((reason, status, offer['price']),
                         ('observed_exact_first_party_cash_offer', 200, 66229))
        self.assertEqual(calls, [
            ('direct', ROBOTS_URL), ('browser', ROBOTS_URL),
            ('direct', target.url), ('browser', target.url),
        ])

    def test_audit_only_reports_three_reviewed_urls_without_writes(self):
        class FakeSource:
            def read_offer(self, url):
                target = BY_URL[url]
                offer, reason = parse_exact_buy_box(fixture(target), url)
                return offer, reason, 200
        report = run_audit(FakeSource())
        self.assertTrue(report['verified'])
        self.assertIs(report['writes'], False)
        self.assertEqual([row['id'] for row in report['products']], [248, 296, 297])
        self.assertEqual([row['url'] for row in report['products']],
                         [target.url for target in REVIEWED_PDPS])

    def test_only_exact_url_routes_to_reader_and_catalog_variant_is_rechecked(self):
        class FakeSource:
            def __init__(self):
                self.calls = []

            def read_offer(self, url):
                self.calls.append(url)
                offer, reason = parse_exact_buy_box(fixture(BY_URL[url]), url)
                return offer, reason, 200

        dedicated = FakeSource()
        generic_calls = []
        def generic_get(url, **_):
            generic_calls.append(url)
            return response(url, 'denied', 403)
        reader = RetailerOfferRefresher(fetch=generic_get,
                                        hepsiburada_apple_source=dedicated)
        for target in REVIEWED_PDPS:
            targets = [{'source_url': target.url}]
            offers = reader.refresh_existing_offer_targets(
                target.name, targets, target.specs)
            self.assertEqual([(row['merchant'], row['url']) for row in offers],
                             [('Hepsiburada', target.url)])
            self.assertEqual(reader.target_report(
                target.name, targets, target.specs)[0]['reason'],
                'observed_exact_first_party_cash_offer')
        self.assertEqual(dedicated.calls, [target.url for target in REVIEWED_PDPS])
        self.assertEqual(generic_calls, [])

        target = REVIEWED_PDPS[0]
        self.assertEqual(reader.refresh_existing_offer_targets(
            target.name, [{'source_url': target.url}],
            {**target.specs, 'storage_gb': 128}), [])
        self.assertEqual(dedicated.calls, [item.url for item in REVIEWED_PDPS])
        self.assertEqual(reader.refresh_existing_offer_targets(
            target.name, [{'source_url': target.schema_url}], target.specs), [])
        self.assertEqual(generic_calls, [target.schema_url])


if __name__ == '__main__':
    unittest.main()
