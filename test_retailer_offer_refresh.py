"""Offline-only known-PDP regression and request-boundary tests."""

from copy import deepcopy
import json
import socket
from threading import Lock
import time
import unittest
from unittest.mock import patch

import requests

from retailer_offer_refresh import ALLOWED_HOSTS, RetailerOfferRefresher


URL = 'https://www.idefix.com/samsung-galaxy-s25-12gb-128gb-p-1'
TITLE = 'Samsung Galaxy S25 12GB RAM 128GB Siyah'
SPECS = {'ram_gb': 12, 'storage_gb': 128}


def product_schema(url=URL, title=TITLE):
    return {'@type': 'Product', '@id': url + '#product', 'url': url, 'name': title,
            'offers': {'@type': 'Offer', 'url': url, 'price': '29999.90',
                       'priceCurrency': 'TRY', 'availability': 'https://schema.org/InStock'}}


def page(data, heading=TITLE):
    return '<html><h1>' + heading + '</h1><script type="application/ld+json">' + json.dumps(data) + '</script></html>'


class Response:
    def __init__(self, html='', status=200, url=URL, headers=None):
        self.text, self.status_code, self.url = html, status, url
        self.headers = headers or {}


class RetailerRefreshTests(unittest.TestCase):
    def setUp(self):
        # Any forgotten injection or accidental browser/HTTP fallback fails.
        self.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('Live network forbidden'))
        self.http = patch.object(requests.sessions.Session, 'request', side_effect=AssertionError('Unexpected HTTP'))
        self.network.start()
        self.http.start()
        self.addCleanup(self.network.stop)
        self.addCleanup(self.http.stop)

    def refresh(self, schema=None, *, url=URL, name='Samsung Galaxy S25', specs=None, response=None):
        calls = []
        def fetch(target, **kwargs):
            calls.append((target, kwargs))
            return response or Response(page(schema if schema is not None else product_schema(url)), url=url)
        refresher = RetailerOfferRefresher(fetch=fetch)
        offers = refresher.refresh_existing_offer_targets(name, [{'source_url': url}], specs or SPECS)
        return offers, calls, refresher

    def test_exact_product_offer_title_price_currency_stock(self):
        offers, calls, _ = self.refresh()
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]['source_title'], TITLE)
        self.assertEqual(offers[0]['price'], 29999.90)
        self.assertEqual(offers[0]['merchant'], 'idefix')
        self.assertEqual(offers[0]['currency'], 'TRY')
        self.assertEqual(offers[0]['availability'], 'in_stock')
        self.assertTrue(offers[0]['observed_at'].endswith('Z'))
        self.assertEqual(calls[0][1]['timeout'], (5, 15))
        self.assertIs(calls[0][1]['allow_redirects'], False)

    def test_graph_product_own_offer_reference(self):
        product = product_schema()
        offer = product.pop('offers')
        offer['@id'] = URL + '#offer'
        product['offers'] = {'@id': offer['@id']}
        offers, _, _ = self.refresh({'@graph': [product, offer, {'@type': 'Organization', 'name': 'Unrelated'}]})
        self.assertEqual(len(offers), 1)

    def test_aggregate_prices_multiple_offers_and_unrelated_product_fail_closed(self):
        cases = []
        aggregate = product_schema()
        aggregate['offers'] = {'@type': 'AggregateOffer', 'lowPrice': '100', 'offers': [product_schema()['offers']]}
        cases.append(aggregate)
        multiple = product_schema()
        multiple['offers'] = [deepcopy(multiple['offers']), deepcopy(multiple['offers'])]
        cases.append(multiple)
        unrelated = product_schema('https://www.idefix.com/different-phone-p-2')
        cases.append(unrelated)
        variant_group = product_schema()
        variant_group['hasVariant'] = [product_schema()]
        cases.append(variant_group)
        unlinked = [product_schema(), product_schema()]
        for p in unlinked:
            p.pop('url'); p.pop('@id'); p['offers'].pop('url')
        cases.append(unlinked)
        for schema in cases:
            with self.subTest(schema=schema):
                self.assertEqual(self.refresh(schema)[0], [])

    def test_recommendation_price_is_not_used_when_main_product_has_no_offer(self):
        main = product_schema()
        main.pop('offers')
        recommendation = product_schema('https://www.idefix.com/other-sku-p-2')
        self.assertEqual(self.refresh({'@graph': [main, recommendation]})[0], [])

    def test_price_specification_does_not_replace_exact_offer_price(self):
        schema = product_schema()
        schema['offers']['priceSpecification'] = {'@type': 'UnitPriceSpecification', 'price': '1.00'}
        self.assertEqual(self.refresh(schema)[0][0]['price'], 29999.90)
        for kind in ('missing', 'monthly', 'offer_name', 'currency', 'stock', 'invalid_price'):
            schema = product_schema()
            if kind == 'missing':
                schema['offers'].pop('price')
                schema['offers']['priceSpecification'] = {'price': '29999.90'}
            elif kind == 'monthly':
                schema['offers']['priceSpecification'] = {'price': '2499.99', 'billingDuration': 'P1M'}
            elif kind == 'offer_name':
                schema['offers']['name'] = '12 ay taksit'
            elif kind == 'currency':
                schema['offers']['priceCurrency'] = 'USD'
            elif kind == 'stock':
                schema['offers'].pop('availability')
            else:
                schema['offers']['price'] = True
            with self.subTest(kind=kind):
                self.assertEqual(self.refresh(schema)[0], [])

    def test_out_of_stock_is_observed_honestly_not_invented_as_in_stock(self):
        schema = product_schema()
        schema['offers']['availability'] = 'https://schema.org/OutOfStock'
        offers, _, _ = self.refresh(schema)
        self.assertEqual(offers[0]['availability'], 'out_of_stock')

    def test_conditional_basket_or_member_price_is_never_unconditional_cash(self):
        schema = product_schema()
        schema['offers']['price'] = '15470'
        html = page(schema) + '<main>17.000 TL <strong>Sepete Özel</strong>15.470 TL</main>'
        self.assertEqual(self.refresh(schema, response=Response(html))[0], [])
        schema['offers']['price'] = '17000'
        self.assertEqual(self.refresh(schema, response=Response(page(schema) + '<main>Sepette 15.470 TL</main>'))[0][0]['price'], 17000)
        for condition in [{'name': 'Kuponla fiyat'}, {'validForMemberTier': 'Premium'},
                          {'eligibleCustomerType': 'Member'}, {'eligibleTransactionVolume': {'price': '1000'}}]:
            row = product_schema()
            row['offers'].update(condition)
            self.assertEqual(self.refresh(row)[0], [])

    def test_denied_host_is_reported_without_fallback_or_timestamp_renewal(self):
        calls = []
        def fetch(url, **kwargs):
            calls.append(url)
            return Response(status=403, url=url)
        reader = RetailerOfferRefresher(fetch=fetch)
        targets = [URL]
        self.assertEqual(reader.refresh_existing_offer_targets('Samsung Galaxy S25', targets, SPECS), [])
        report = reader.target_report('Samsung Galaxy S25', targets, SPECS)
        self.assertEqual(report[0]['reason'], 'access_denied')
        self.assertEqual(report[0]['http_status'], 403)
        self.assertNotIn('observed_at', report[0])
        self.assertEqual(reader.refresh_existing_offer_targets('Samsung Galaxy S25', targets, SPECS), [])
        self.assertEqual(calls, [URL])

    def test_exact_host_boundary_rejects_credentials_ips_ports_and_suffix_tricks_before_fetch(self):
        urls = ['http://www.idefix.com/a', 'https://www.idefix.com:444/a',
                'https://user:pass@www.idefix.com/a', 'https://127.0.0.1/a',
                'https://[::1]/a', 'https://localhost/a', 'https://idefix.com.attacker.test/a',
                'https://attackeridefix.com/a', 'https://www.idefix.com./a',
                'https://www.idefix.com\\@evil.test/a', 'https://www.idefix.com/a\nInjected']
        for url in urls:
            with self.subTest(url=url):
                offers, calls, _ = self.refresh(url=url)
                self.assertEqual((offers, calls), ([], []))
        self.assertNotIn('evil.idefix.com', ALLOWED_HOSTS)

    def test_secret_bearing_query_keys_fail_before_fetch_including_redirect_targets(self):
        for key in ['access_token', 'auth', 'password', 'client_secret', 'signature',
                    'sessionId', 'email', 'apiKey', 'access_key', 'credential']:
            url = URL + '?' + key + '=never-log-or-fetch-this'
            offers, calls, _ = self.refresh(url=url)
            self.assertEqual((offers, calls), ([], []), key)
        calls = []
        def fetch(url, **kwargs):
            calls.append(url)
            return Response(status=302, url=url, headers={'Location': URL + '?token=forbidden'})
        refresher = RetailerOfferRefresher(fetch=fetch)
        self.assertEqual(refresher.refresh_existing_offer_targets('Samsung Galaxy S25', [URL], SPECS), [])
        self.assertEqual(len(calls), 1)

    def test_distinct_or_unestablished_selected_variant_query_is_never_the_same_offer(self):
        for key in ['variant', 'sku', 'variation', 'storage', 'memory', 'ram',
                    'capacity', 'selected_variant', 'dahili-hafiza']:
            target = URL + '?' + key + '=128'
            for offered in [URL, URL + '?' + key + '=512']:
                schema = product_schema(target)
                schema['offers']['url'] = offered
                self.assertEqual(self.refresh(schema, url=target)[0], [], (target, offered))
        ambiguous = URL + '?variant=128&variant=512'
        self.assertEqual(self.refresh(url=ambiguous)[1], [])
        # A selected-variant target whose whole schema forgets that choice is
        # not established by a matching path alone.
        self.assertEqual(self.refresh(product_schema(), url=URL + '?variant=128')[0], [])

    def test_equal_selected_variants_and_tracking_differences_can_use_the_observed_offer_url(self):
        target = URL + '?variant=128&utm_source=discovery'
        schema = product_schema(URL + '?variant=128')
        offers, _, _ = self.refresh(schema, url=target)
        self.assertEqual(offers[0]['url'], URL + '?variant=128')
        schema = product_schema()
        schema['offers']['url'] = URL + '?merchantId=1'
        offers, _, _ = self.refresh(schema, url=URL + '?merchantId=2&ref=tracking&tag=affiliate')
        self.assertEqual(offers[0]['url'], URL + '?merchantId=1')

    def test_relative_schema_references_are_absolute_safe_and_keep_selected_variant(self):
        target = URL + '?variant=128&utm_source=discovery'
        relative = '/samsung-galaxy-s25-12gb-128gb-p-1?variant=128'
        schema = product_schema(URL + '?variant=128')
        schema['url'] = relative
        schema['offers']['url'] = relative
        offers, _, _ = self.refresh(schema, url=target)
        self.assertEqual(offers[0]['url'], 'https://www.idefix.com' + relative)
        schema['offers']['url'] = relative.replace('variant=128', 'variant=512')
        self.assertEqual(self.refresh(schema, url=target)[0], [])
        schema['offers']['url'] = '//evil.test/samsung-galaxy-s25-12gb-128gb-p-1?variant=128'
        self.assertEqual(self.refresh(schema, url=target)[0], [])

    def test_redirect_cannot_discard_or_change_requested_variant_selection(self):
        for location in [URL, URL + '?variant=512']:
            calls = []
            def fetch(url, **kwargs):
                calls.append(url)
                return Response(status=302, url=url, headers={'Location': location})
            refresher = RetailerOfferRefresher(fetch=fetch)
            self.assertEqual(refresher.refresh_existing_offer_targets(
                'Samsung Galaxy S25', [URL + '?variant=128'], SPECS), [])
            self.assertEqual(len(calls), 1)

    def test_pttavm_and_official_xiaomi_host_names_use_the_manifest_merchant_identity(self):
        ptt = 'https://www.pttavm.com/samsung-galaxy-s25-12gb-128gb-p-1'
        self.assertEqual(self.refresh(product_schema(ptt), url=ptt)[0][0]['merchant'], 'PttAVM')
        self.assertEqual(ALLOWED_HOSTS['mi.com'], 'Xiaomi')

    def test_redirects_are_manual_bounded_and_validate_every_destination(self):
        unsafe = ['http://www.idefix.com/a', 'https://evil.test/a', 'https://127.0.0.1/a',
                  'https://x:y@www.idefix.com/a', 'https://www.idefix.com:8443/a']
        for location in unsafe:
            calls = []
            def fetch(url, **kwargs):
                calls.append(url)
                return Response(status=302, url=url, headers={'Location': location})
            refresher = RetailerOfferRefresher(fetch=fetch)
            self.assertEqual(refresher.refresh_existing_offer_targets('Samsung Galaxy S25', [URL], SPECS), [])
            self.assertEqual(len(calls), 1)
        for limit, expected_calls in [(3, 4), (4, 4)]:
            calls = []
            def fetch(url, **kwargs):
                calls.append(url)
                if len(calls) <= limit:
                    return Response(status=302, url=url, headers={'Location': f'/redirect-{len(calls)}'})
                return Response(page(product_schema(url)), url=url)
            result = RetailerOfferRefresher(fetch=fetch).refresh_existing_offer_targets('Samsung Galaxy S25', [URL], SPECS)
            self.assertEqual(len(calls), expected_calls)
            self.assertEqual(bool(result), limit == 3)

    def test_external_offer_url_or_auto_redirect_cannot_reuse_a_trusted_title(self):
        schema = product_schema()
        schema['offers']['url'] = 'https://evil.test/samsung-galaxy-s25-12gb-128gb'
        self.assertEqual(self.refresh(schema)[0], [])
        self.assertEqual(self.refresh(response=Response(page(product_schema()), url='https://evil.test/p'))[0], [])
        for item in [{'@id': URL + '#other-product'}, {'name': 'Samsung Galaxy S24 12GB RAM 128GB'}]:
            schema = product_schema()
            schema['offers']['itemOffered'] = item
            self.assertEqual(self.refresh(schema)[0], [])

    def test_network_timeouts_fail_without_retry_or_fabricated_observation(self):
        calls = []
        def timeout(url, **kwargs):
            calls.append(url)
            raise requests.Timeout('Fixture timeout')
        refresher = RetailerOfferRefresher(fetch=timeout)
        self.assertEqual(refresher.refresh_existing_offer_targets('Samsung Galaxy S25', [URL], SPECS), [])
        self.assertEqual(len(calls), 1)

    def test_repeated_transient_failures_stop_the_host_for_later_products(self):
        for mode in ['503', 'timeout']:
            calls = []
            def fetch(url, **kwargs):
                calls.append(url)
                if mode == 'timeout':
                    raise requests.Timeout('Fixture timeout')
                return Response(status=503, url=url)
            refresher = RetailerOfferRefresher(fetch=fetch)
            for index in range(3):
                self.assertEqual(refresher.refresh_existing_offer_targets(
                    'Samsung Galaxy S25', [URL + f'?tracking={index}'], SPECS), [])
            self.assertEqual(len(calls), 2)

    def test_blocks_and_interstitials_trip_a_run_wide_host_breaker(self):
        challenges = [Response(status=403), Response(status=429),
                      Response('<title>Robot Check</title>'),
                      Response('<form action="/errors/validateCaptcha"><input id="captchacharacters"></form>'),
                      Response('Alışverişe devam etmek için aşağıdaki düğmeye tıklayın')]
        for response in challenges:
            offers, calls, refresher = self.refresh(response=response)
            self.assertEqual(offers, [])
            self.assertEqual(refresher.refresh_existing_offer_targets('Samsung Galaxy S25', [URL + '?second=1'], SPECS), [])
            self.assertEqual(len(calls), 1)

    def test_exact_url_response_cache_rechecks_requested_product_and_keeps_original_observation(self):
        base = 'https://www.idefix.com/samsung-galaxy-s26-12gb-256gb-p-2'
        schema = product_schema(base, 'Samsung Galaxy S26 12GB RAM 256GB')
        specs = {'ram_gb': 12, 'storage_gb': 256}
        offers, calls, refresher = self.refresh(schema, url=base, name='Samsung Galaxy S26', specs=specs)
        self.assertEqual(len(offers), 1)
        self.assertEqual(refresher.refresh_existing_offer_targets('Samsung Galaxy S26+', [base], specs), [])
        again = refresher.refresh_existing_offer_targets('Samsung Galaxy S26', [base], specs)
        self.assertEqual(len(calls), 1)
        self.assertEqual(again[0]['observed_at'], offers[0]['observed_at'])
        again[0]['price'] = 1
        self.assertEqual(refresher.refresh_existing_offer_targets('Samsung Galaxy S26', [base], specs)[0]['price'], 29999.90)

    def test_existing_positive_evidence_rules_preserve_ram_storage_radio_and_accessory_rejection(self):
        for title in ['Samsung Galaxy S25 8GB RAM 128GB', 'Samsung Galaxy S25 12GB RAM 256GB',
                      'Samsung Galaxy S25 128GB', 'Samsung Galaxy S25 12GB RAM 128GB Kılıf',
                      'Samsung Galaxy S24 12GB RAM 128GB']:
            self.assertEqual(self.refresh(product_schema(title=title))[0], [], title)
        for title in ['Samsung Galaxy A16 4GB RAM 128GB', 'Samsung Galaxy A16 5G 4GB RAM 128GB']:
            self.assertEqual(self.refresh(product_schema(title=title), name='Samsung Galaxy A16 4G',
                                          specs={'ram_gb': 4, 'storage_gb': 128, 'network_generation': '4g'})[0], [])

    def test_at_most_four_workers_and_twelve_unique_targets(self):
        state = {'active': 0, 'max': 0, 'calls': 0}
        lock = Lock()
        def fetch(url, **kwargs):
            with lock:
                state['active'] += 1; state['calls'] += 1
                state['max'] = max(state['max'], state['active'])
            time.sleep(0.005)
            with lock:
                state['active'] -= 1
            return Response(page(product_schema(url)), url=url)
        targets = [f'https://www.idefix.com/samsung-galaxy-s25-12gb-128gb-p-{i}' for i in range(20)]
        offers = RetailerOfferRefresher(fetch=fetch).refresh_existing_offer_targets('Samsung Galaxy S25', targets, SPECS)
        self.assertEqual(len(offers), 12)
        self.assertEqual(state['calls'], 12)
        self.assertLessEqual(state['max'], 4)


if __name__ == '__main__':
    unittest.main()
