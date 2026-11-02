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
VATAN_URL = 'https://www.vatanbilgisayar.com/iphone-17-256-gb-akilli-telefon-beyaz.html'
VATAN_TITLE = 'iPhone 17 256 GB Akıllı Telefon Beyaz'


def product_schema(url=URL, title=TITLE):
    return {'@type': 'Product', '@id': url + '#product', 'url': url, 'name': title,
            'offers': {'@type': 'Offer', 'url': url, 'price': '29999.90',
                       'priceCurrency': 'TRY', 'availability': 'https://schema.org/InStock'}}


def page(data, heading=TITLE):
    return '<html><h1>' + heading + '</h1><script type="application/ld+json">' + json.dumps(data) + '</script></html>'


def vatan_variant_schema(url=VATAN_URL, title=VATAN_TITLE, sku='153480', price='89999'):
    return {'@type': 'Product', 'name': title, 'sku': sku,
            'hasVariant': [{'@type': 'Product', 'sku': '153481',
                            'url': 'https://www.vatanbilgisayar.com/other-color.html'}],
            'offers': {'@type': 'Offer', 'url': url, 'price': price,
                       'priceCurrency': 'TRY', 'availability': 'https://schema.org/InStock',
                       'itemCondition': 'https://schema.org/NewCondition',
                       'seller': {'@type': 'Organization', 'name': 'Vatan Bilgisayar',
                                  'url': 'https://www.vatanbilgisayar.com'}}}


def vatan_variant_page(schema, *, title=VATAN_TITLE, visible_price='89.999 TL', web_price=True,
                       code='MG6K4TU/A / 153480', cart=True, extra=''):
    price = ('<div class="web-ozel-prod"><span>Web\'e Özel</span>'
             '<span class="web-ozel-fiyat">' + visible_price + '</span></div>'
             '<div class="product-detail-price">94.999 TL</div>' if web_price else
             '<div class="product-detail-price">' + visible_price + '</div>')
    button = ('<button id="add-to-cart-button" class="detail-cart-button basketBTN">'
              'Sepete Ekle</button>' if cart else '')
    return ('<html><div class="product-detail"><h1 class="product-list__product-name">'
            + title + '</h1><div class="product-id">' + code + '</div>'
            + price + button + '</div>' + extra + '<script type="application/ld+json">'
            + json.dumps(schema, ensure_ascii=False) + '</script></html>')


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
        # Parser regression cases supply their own PDP HTML. The reviewed
        # Vatan transport and robots gate are covered in dedicated tests.
        class NoReviewedVatan:
            @staticmethod
            def is_reviewed(_url):
                return False
        refresher = RetailerOfferRefresher(fetch=fetch, vatan_source=NoReviewedVatan())
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

    def test_vatan_selected_variant_uses_own_exact_offer_and_visible_cash_price(self):
        schema = vatan_variant_schema()
        for web_price in (True, False):
            with self.subTest(web_price=web_price):
                html = vatan_variant_page(schema, web_price=web_price)
                offers, calls, _ = self.refresh(
                    schema, url=VATAN_URL, name='Apple iPhone 17',
                    specs={'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'},
                    response=Response(html, url=VATAN_URL))
                self.assertEqual(len(calls), 1)
                self.assertEqual(len(offers), 1)
                self.assertEqual(offers[0]['merchant'], 'Vatan Bilgisayar')
                self.assertEqual(offers[0]['price'], 89999)
                self.assertEqual(offers[0]['url'], VATAN_URL)
        encoded = vatan_variant_schema()
        encoded['name'] = VATAN_TITLE.replace('Akıllı', 'Ak&#x131;ll&#x131;')
        offers, _, _ = self.refresh(
            encoded, url=VATAN_URL, name='Apple iPhone 17',
            specs={'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'},
            response=Response(vatan_variant_page(encoded), url=VATAN_URL))
        self.assertEqual(offers[0]['source_title'], VATAN_TITLE)

    def test_vatan_android_selected_variants_require_exact_memory_and_radio(self):
        cases = [
            ('https://www.vatanbilgisayar.com/xiaomi-17t-12-256gb-akilli-telefon-siyah.html',
             'Xiaomi 17T 12/256GB Akıllı Telefon Siyah', 'Xiaomi 17T', '157950', '54999', '54.999 TL'),
            ('https://www.vatanbilgisayar.com/samsung-galaxy-s26-akilli-telefon.html',
             'Samsung Galaxy S26 5G 12/256 Gb Akıllı Telefon Siyah', 'Samsung Galaxy S26',
             '156533', '68999', '68.999 TL'),
        ]
        for url, title, name, sku, price, visible in cases:
            with self.subTest(name=name):
                schema = vatan_variant_schema(url, title, sku, price)
                html = vatan_variant_page(schema, title=title, code='MODEL / ' + sku,
                                          visible_price=visible, web_price=False)
                offers, _, _ = self.refresh(
                    schema, url=url, name=name,
                    specs={'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g'},
                    response=Response(html, url=url))
                self.assertEqual(len(offers), 1)
                self.assertEqual(offers[0]['price'], float(price))
                self.assertEqual(self.refresh(
                    schema, url=url, name=name,
                    specs={'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'},
                    response=Response(html, url=url))[0], [])

    def test_vatan_variant_rejects_ambiguous_or_unpurchasable_selected_sku(self):
        cases = []
        cases.append(('missing_offer', lambda s: s.pop('offers'), {}))
        cases.append(('wrong_offer_url', lambda s: s['offers'].update(
            url='https://www.vatanbilgisayar.com/iphone-17-512-gb-akilli-telefon-beyaz.html'), {}))
        cases.append(('same_variant_url', lambda s: s['hasVariant'][0].update(url=VATAN_URL), {}))
        cases.append(('same_variant_sku', lambda s: s['hasVariant'][0].update(sku='153480'), {}))
        cases.append(('wrong_seller', lambda s: s['offers']['seller'].update(name='Other'), {}))
        cases.append(('wrong_seller_url', lambda s: s['offers']['seller'].update(url='https://www.idefix.com'), {}))
        cases.append(('used_condition', lambda s: s['offers'].update(itemCondition='https://schema.org/UsedCondition'), {}))
        cases.append(('out_of_stock', lambda s: s['offers'].update(availability='https://schema.org/OutOfStock'), {}))
        cases.append(('wrong_visible_price', lambda s: None, {'visible_price': '88.999 TL'}))
        cases.append(('wrong_visible_sku', lambda s: None, {'code': 'MG6K4TU/A / 153481'}))
        cases.append(('missing_main_cart', lambda s: None, {'cart': False}))
        cases.append(('basket_price', lambda s: None, {'extra': '<p>Sepette 89.999 TL</p>'}))
        for label, mutate, page_options in cases:
            with self.subTest(label=label):
                schema = vatan_variant_schema()
                mutate(schema)
                html = vatan_variant_page(schema, **page_options)
                offers, _, _ = self.refresh(
                    schema, url=VATAN_URL, name='Apple iPhone 17',
                    specs={'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'},
                    response=Response(html, url=VATAN_URL))
                self.assertEqual(offers, [])
        schema = vatan_variant_schema()
        disabled = vatan_variant_page(schema).replace(
            '<button id="add-to-cart-button"', '<button disabled id="add-to-cart-button"')
        self.assertEqual(self.refresh(
            schema, url=VATAN_URL, name='Apple iPhone 17',
            specs={'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'},
            response=Response(disabled, url=VATAN_URL))[0], [])

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

    def test_target_budget_reaches_later_distinct_merchants(self):
        calls = []
        lock = Lock()
        def fetch(url, **kwargs):
            with lock:
                calls.append(url)
            return Response(page(product_schema(url)), url=url)
        idefix = [f'https://www.idefix.com/samsung-galaxy-s25-12gb-128gb-p-{i}'
                  for i in range(30)]
        teknosa = 'https://www.teknosa.com/samsung-galaxy-s25-12gb-128gb-p-1'
        n11 = 'https://www.n11.com/samsung-galaxy-s25-12gb-128gb-p-1'
        targets = idefix + [teknosa, n11, idefix[0]]
        reader = RetailerOfferRefresher(fetch=fetch)
        offers = reader.refresh_existing_offer_targets('Samsung Galaxy S25', targets, SPECS)
        self.assertEqual(len(calls), 12)
        self.assertEqual(set(calls), set(idefix[:10]) | {teknosa, n11})
        self.assertEqual({row['merchant'] for row in offers}, {'idefix', 'Teknosa', 'n11'})
        report = reader.target_report('Samsung Galaxy S25', targets, SPECS)
        self.assertIn(n11, [row['source_url'] for row in report])
        self.assertIn(teknosa, [row['source_url'] for row in report])
        self.assertEqual(next(row['reason'] for row in report if row['source_url'] == idefix[10]),
                         'target_budget_skipped')

    def test_access_denial_stops_queued_urls_on_the_same_retailer(self):
        calls = []
        def fetch(url, **kwargs):
            calls.append(url)
            return Response(status=403, url=url)
        targets = [f'https://www.idefix.com/samsung-galaxy-s25-12gb-128gb-p-{i}'
                   for i in range(8)]
        reader = RetailerOfferRefresher(fetch=fetch)
        self.assertEqual(reader.refresh_existing_offer_targets('Samsung Galaxy S25', targets, SPECS), [])
        self.assertEqual(calls, targets[:1])
        reasons = {row['source_url']: row['reason'] for row in reader.target_report(
            'Samsung Galaxy S25', targets, SPECS)}
        self.assertEqual(reasons[targets[0]], 'access_denied')
        self.assertEqual({reasons[url] for url in targets[1:]}, {'host_circuit_open'})

    def test_unreviewed_vatan_denial_does_not_block_reviewed_exact_pdp(self):
        unreviewed = 'https://www.vatanbilgisayar.com/iphone-17-256-gb-akilli-telefon-sis-mavisi.html'
        html = vatan_variant_page(vatan_variant_schema())
        calls = []

        class ReviewedVatan:
            @staticmethod
            def is_reviewed(url):
                return url == VATAN_URL

            @staticmethod
            def prepare(url):
                self.assertEqual(url, VATAN_URL)
                return True, 'ready'

            @staticmethod
            def browser_get(url):
                self.assertEqual(url, VATAN_URL)
                return html, 'browser_html_ready'

        def fetch(url, **_kwargs):
            calls.append(url)
            return Response(status=403, url=url)

        reader = RetailerOfferRefresher(fetch=fetch, vatan_source=ReviewedVatan())
        offers = reader.refresh_existing_offer_targets(
            'Apple iPhone 17', [unreviewed, VATAN_URL],
            {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'},
        )
        self.assertEqual(calls, [unreviewed, VATAN_URL])
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]['merchant'], 'Vatan Bilgisayar')
        self.assertEqual(offers[0]['price'], 89999)
        reasons = {row['source_url']: row['reason'] for row in reader.target_report(
            'Apple iPhone 17', [unreviewed, VATAN_URL],
            {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'},
        )}
        self.assertEqual(reasons[unreviewed], 'access_denied')
        self.assertEqual(reasons[VATAN_URL], 'observed_exact_offer')


if __name__ == '__main__':
    unittest.main()
