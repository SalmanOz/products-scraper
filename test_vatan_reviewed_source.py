"""Offline gates for reviewed Vatan SKU URLs and bounded browser transport."""

import json
from io import BytesIO
import unittest

import requests

from retailer_offer_refresh import RetailerOfferRefresher
from reviewed_offer_targets import daily_offer_targets, with_reviewed_radio
from test_retailer_offer_refresh import VATAN_URL, vatan_variant_page, vatan_variant_schema
from vatan_reviewed_source import (
    MAX_BROWSER_REQUESTS, SOLVER_URL, VatanReviewedSource,
    reviewed_profiles, S25_FE_256_VATAN_URL,
)


SPECS = {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'}


def response(url, body, status=200):
    result = requests.Response()
    result.status_code = status
    result.url = url
    result._content = body.encode() if isinstance(body, str) else body
    result.raw = BytesIO(result._content)
    result.headers['Content-Length'] = str(len(result._content))
    return result


def solver_result(url=VATAN_URL, html=None, status=200):
    payload = {'status': 'ok', 'solution': {
        'status': status, 'url': url,
        'response': html if html is not None else vatan_variant_page(vatan_variant_schema()),
    }}
    return response(SOLVER_URL, json.dumps(payload))


class VatanReviewedSourceTests(unittest.TestCase):
    def test_enlarged_catalog_budget_stops_after_two_real_browser_denials(self):
        calls = []
        def post(url, **_kwargs):
            calls.append(url)
            return solver_result(status=403)
        source = VatanReviewedSource(
                                     post=post, sleep=lambda _: None)
        source.configure_catalog_budget(110)
        self.assertEqual(source.browser_get(VATAN_URL), (None, 'site_http_403'))
        self.assertEqual(source.browser_get(VATAN_URL), (None, 'site_http_403'))
        self.assertEqual(source.browser_get(VATAN_URL), (None, 'source_circuit_open'))
        self.assertEqual(len(calls), 2)

    def test_unlisted_catalog_variant_gets_live_browser_validation_without_a_profile(self):
        url = VATAN_URL.replace('beyaz', 'siyah')
        self.assertNotEqual(url, VATAN_URL)
        calls = []
        def get(target, **_):
            return response(target, 'denied', 403)
        body = vatan_variant_page(vatan_variant_schema()).replace(VATAN_URL, url)
        def post(_url, **kwargs):
            calls.append(kwargs['json']['url'])
            return solver_result(url=url, html=body)
        source = VatanReviewedSource(get=get, post=post, sleep=lambda _: None)
        self.assertFalse(source.is_reviewed(url))
        source.configure_catalog_budget(110)
        source._browser_requests = MAX_BROWSER_REQUESTS
        reader = RetailerOfferRefresher(fetch=get, vatan_source=source)
        offers = reader.refresh_existing_offer_targets('Apple iPhone 17',
                                                       [{'source_url': url}], SPECS)
        self.assertEqual([(o['price'], o['url']) for o in offers], [(89999, url)])
        self.assertEqual(calls, [url])
        self.assertTrue(source.is_reviewed(url))
        self.assertEqual(source.report()['browser_request_limit'], 80)
        self.assertEqual(reader.refresh_existing_offer_targets('Apple iPhone 17',
                     [{'source_url': url}], {**SPECS, 'storage_gb': 128}), [])
        source.register_catalog_targets('Apple iPhone 17',
                     [{'source_url': url.replace('256', '512')}], SPECS)
        self.assertFalse(source.is_reviewed(url.replace('256', '512')))

    def test_only_exact_new_variant_vatan_url_is_browser_reviewed(self):
        source = VatanReviewedSource()
        self.assertTrue(source.is_reviewed(S25_FE_256_VATAN_URL))
        self.assertFalse(source.is_reviewed(S25_FE_256_VATAN_URL + '?other=1'))
        self.assertFalse(source.is_reviewed(S25_FE_256_VATAN_URL.replace('256', '128')))

    def test_only_exact_catalog_skus_get_reviewed_url_hints(self):
        profiles = reviewed_profiles()
        self.assertEqual(len(profiles), 6)
        for profile in profiles:
            product = {key: profile[key] for key in ('id', 'slug', 'name')}
            specs = {key: profile[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
            self.assertTrue(set(profile['targets']).issubset(
                {row['source_url'] for row in daily_offer_targets(product, specs)}))
            self.assertEqual(with_reviewed_radio(product, {key: specs[key] for key in ('ram_gb', 'storage_gb')}),
                             specs)
            for key, wrong in [('ram_gb', 99), ('storage_gb', 99), ('network_generation', '4g')]:
                self.assertEqual(daily_offer_targets(product, {**specs, key: wrong}), [])
            self.assertEqual(daily_offer_targets({**product, 'id': 999}, specs), [])

    def test_403_browser_html_still_uses_exact_offer_parser_and_matcher(self):
        get_calls, post_calls = [], []

        def get(url, **_):
            get_calls.append(url)
            return response(url, 'denied', 403)

        def post(url, **kwargs):
            post_calls.append((url, kwargs['json']))
            return solver_result()

        source = VatanReviewedSource(get=get, post=post, sleep=lambda _: None)
        reader = RetailerOfferRefresher(fetch=get, vatan_source=source)
        offers = reader.refresh_existing_offer_targets(
            'Apple iPhone 17', [{'source_url': VATAN_URL}], SPECS)
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]['price'], 89999)
        self.assertEqual(offers[0]['availability'], 'in_stock')
        self.assertEqual(get_calls, [VATAN_URL])
        self.assertEqual(post_calls, [(SOLVER_URL, {
            'cmd': 'request.get', 'url': VATAN_URL, 'maxTimeout': 45000})])
        self.assertEqual(source.report()['browser_requests'], 1)

    def test_browser_cannot_turn_wrong_variant_into_offer(self):
        def get(url, **_):
            return response(url, 'denied', 403)
        html = vatan_variant_page(vatan_variant_schema(price='89999'), visible_price='79.999 TL')
        source = VatanReviewedSource(get=get, post=lambda *_args, **_kwargs: solver_result(html=html),
                                     sleep=lambda _: None)
        reader = RetailerOfferRefresher(fetch=get, vatan_source=source)
        self.assertEqual(reader.refresh_existing_offer_targets(
            'Apple iPhone 17', [{'source_url': VATAN_URL}], SPECS), [])
        self.assertEqual(reader.target_report('Apple iPhone 17', [{'source_url': VATAN_URL}], SPECS)[0]['reason'],
                         'no_exact_product_offer')

    def test_exact_url_and_global_browser_budget_fail_closed(self):
        calls = []
        source = VatanReviewedSource(

            post=lambda url, **kwargs: (calls.append(url), solver_result(url=VATAN_URL + '?x=1'))[1],
            sleep=lambda _: None)
        self.assertEqual(source.browser_get('https://www.vatanbilgisayar.com/other.html'),
                         (None, 'not_reviewed'))
        for _ in range(MAX_BROWSER_REQUESTS):
            self.assertEqual(source.browser_get(VATAN_URL), (None, 'unexpected_final_url'))
        self.assertEqual(source.browser_get(VATAN_URL), (None, 'browser_budget_exhausted'))
        self.assertEqual(len(calls), MAX_BROWSER_REQUESTS)

if __name__ == '__main__':
    unittest.main()
