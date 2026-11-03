import json
import unittest
from xml.sax.saxutils import escape

import requests

from mediamarkt_source import (
    MediaMarktSource, MAX_BROWSER_REQUESTS, MAX_DIRECT_REQUESTS,
    ROBOTS_URL, SITEMAP_URLS, REVIEWED_COMPACT_PHONE_URLS,
    _selected_offer, product_url,
)
from retailer_offer_refresh import RetailerOfferRefresher


URL = ('https://www.mediamarkt.com.tr/tr/product/'
       '_apple-mg6k4tua-iphone-17-256gb-akilli-telefon-beyaz-1249222.html')
OLD_URL = ('https://www.mediamarkt.com.tr/tr/product/'
           '_apple-iphone-17-256-gb-akilli-telefon-beyaz-166347558.html')
ROBOTS = ('User-agent: *\nAllow: /tr/product/\n'
          + ''.join(f'Sitemap: {url}\n' for url in SITEMAP_URLS)).encode()


def sitemap(*urls):
    return ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + ''.join(f'<url><loc>{escape(url)}</loc></url>' for url in urls)
            + '</urlset>').encode()


def html(*, seller=None, condition='NewCondition', price=89999,
         visible='₺ 89.999,', disabled='false', marketplace='false', title=None,
         url=URL, sku='1249222'):
    title = title or 'APPLE iPhone 17 256 GB Akıllı Telefon Beyaz MG6K4TU/A'
    product = {'@type': 'ProductGroup', 'name': title, 'url': url,
               'sku': sku, 'brand': {'@type': 'Brand', 'name': 'APPLE'},
               'offers': {'@type': 'Offer', 'url': url, 'price': price,
                          'priceCurrency': 'TRY',
                          'itemCondition': 'https://schema.org/' + condition,
                          'availability': 'https://schema.org/InStock', 'seller': seller}}
    state = ('window.__PRELOADED_STATE__ = '
             '{"__typename":"CofrOnlineStatusFeature","id":"Media:tr:' + sku + '",'
             '"isAvailableAndBuyable":true,"onlineStatus":"AVAILABLE"},'
             '{"__typename":"CofrPriceFeature","id":"Media:tr:' + sku + '",'
             '"currency":"TRY","isProductOfTypeMarketplace":' + marketplace
             + ',"marketplaceSeller":null};')
    return (f'<html><head><link rel="canonical" href="{url}">'
            f'<script type="application/ld+json">{json.dumps({"@type":"BuyAction","object":product})}</script>'
            f'<script>{state}</script></head><body><h1>{title}</h1>'
            f'<div data-test="cofr-price mms-branded-price">'
            f'<span data-test="branded-price-whole-value">{visible}</span></div>'
            f'<nav data-test="mms-anchor-navigation-bar"><button data-test="cofr-add-to-basket-button a2c-Button" aria-disabled="{disabled}">Sepete Ekle</button></nav>'
            '</body></html>').encode()


class Response:
    def __init__(self, url, body=b'', status=200, location=None):
        self.url, self.data, self.status_code = url, body, status
        self.headers = {'Content-Length': str(len(body))}
        if location:
            self.headers['Location'] = location

    def iter_content(self, chunk_size):
        for index in range(0, len(self.data), chunk_size):
            yield self.data[index:index + chunk_size]

    def close(self):
        pass


class Fetch:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def __call__(self, url, **_kwargs):
        self.calls.append(url)
        row = self.pages[url]
        return row if isinstance(row, Response) else Response(url, row)


class MediaMarktSourceTests(unittest.TestCase):
    def test_full_catalog_can_read_a_verified_offer_after_seed_batch_quota(self):
        fetch = Fetch({ROBOTS_URL: ROBOTS, URL: Response(URL, status=403)})
        calls = []
        def browser(url, **kwargs):
            calls.append(kwargs['json']['url'])
            return Response(url, json.dumps({'status': 'ok', 'solution': {
                'status': 200, 'url': URL, 'response': html().decode()}}).encode())
        source = MediaMarktSource(get=fetch, post=browser, sleep=lambda _: None)
        source.configure_catalog_budget(109)
        source._browser_count = MAX_BROWSER_REQUESTS
        offer, reason, status = source.read_offer(URL)
        self.assertEqual((offer['price'], reason, status), (89999, 'observed_exact_offer', 200))
        self.assertEqual(calls, [URL])
        self.assertEqual(source.report()['browser_request_limit'], 160)
        source._browser_count = 160
        self.assertEqual(source.read_offer(URL), (None, 'browser_budget_exhausted', None))
        self.assertEqual(calls, [URL])
        with self.assertRaisesRegex(RuntimeError, 'before source requests'):
            source.configure_catalog_budget(109)

    def test_iphone_16e_exact_first_party_cash_offer_reaches_product_reader(self):
        url = ('https://www.mediamarkt.com.tr/tr/product/'
               '_apple-md1r4tua-iphone-16e-128gb-akilli-telefon-beyaz-1244800.html')
        title = 'APPLE iPhone 16e 8/128 GB Akıllı Telefon Beyaz MD1R4TU/A'
        body = html(url=url, sku='1244800', title=title, price=54999,
                    visible='₺ 54.999,')
        fetch = Fetch({ROBOTS_URL: ROBOTS, url: body})
        reader = RetailerOfferRefresher(fetch=fetch)
        specs = {'ram_gb': 8, 'storage_gb': 128, 'network_generation': '5g'}
        targets = [{'source_url': url}]
        offers = reader.refresh_existing_offer_targets('Apple iPhone16e', targets, specs)
        self.assertEqual([(offer['merchant'], offer['price'], offer['url'])
                          for offer in offers], [('MediaMarkt', 54999, url)])
        self.assertEqual(reader.target_report('Apple iPhone16e', targets, specs)[0]['reason'],
                         'observed_exact_offer')
        for name, variant in (('Apple iPhone 16', specs),
                              ('Apple iPhone16e', {**specs, 'storage_gb': 256}),
                              ('Apple iPhone16e', {**specs, 'ram_gb': 6})):
            self.assertEqual(reader.refresh_existing_offer_targets(name, targets, variant), [])

    def test_first_party_cash_offer_and_negative_evidence(self):
        offer = _selected_offer(html(), URL)
        self.assertEqual(offer['price'], 89999)
        self.assertEqual(offer['availability'], 'in_stock')
        self.assertEqual(offer['url'], URL)
        cases = (html(seller={'name': 'Bittibitiyor'}),
                 html(condition='RefurbishedCondition'),
                 html(price=88000),
                 html(disabled='true'),
                 html(marketplace='true'))
        for body in cases:
            with self.subTest(body=body[:80]):
                self.assertIsNone(_selected_offer(body, URL))

    def test_only_clean_exact_product_urls(self):
        self.assertTrue(product_url(URL))
        self.assertEqual(len(REVIEWED_COMPACT_PHONE_URLS), 2)
        self.assertTrue(all(product_url(url) for url in REVIEWED_COMPACT_PHONE_URLS))
        compact = next(url for url in REVIEWED_COMPACT_PHONE_URLS if '1249062' in url)
        self.assertFalse(product_url(compact.replace('8256gb', '8128gb')))
        self.assertFalse(product_url(compact.replace('1249062', '1249063')))
        for url in (URL.replace('https:', 'http:'), URL + '?sku=1',
                    URL.replace('www.mediamarkt.com.tr', 'attacker.example'),
                    URL.replace('iphone-17', 'outlet-iphone-17'),
                    'https://www.mediamarkt.com.tr/tr/category/iphone-17.html'):
            with self.subTest(url=url):
                self.assertFalse(product_url(url))

    def test_robots_and_sitemap_are_cached_and_only_variant_hints_return(self):
        wrong_storage = URL.replace('256gb', '512gb')
        other = URL.replace('www.mediamarkt.com.tr', 'attacker.example')
        fetch = Fetch({ROBOTS_URL: ROBOTS,
                       SITEMAP_URLS[0]: sitemap(URL, wrong_storage, other),
                       SITEMAP_URLS[1]: sitemap()})
        source = MediaMarktSource(get=fetch, sleep=lambda _: None)
        specs = {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'}
        self.assertEqual(source.discover('Apple iPhone 17', specs), [{'source_url': URL}])
        self.assertEqual(source.discover('Apple iPhone 17', specs), [{'source_url': URL}])
        self.assertEqual(fetch.calls, [ROBOTS_URL, *SITEMAP_URLS])
        self.assertEqual(source.report()['sitemap_status'], 'ready')

    def test_runner_403_browser_get_must_select_the_final_first_party_offer(self):
        fetch = Fetch({ROBOTS_URL: ROBOTS, OLD_URL: Response(OLD_URL, status=403)})
        browser_calls = []

        def browser(_url, **kwargs):
            browser_calls.append(kwargs['json']['url'])
            solution = {'status': 'ok', 'solution': {'status': 200,
                         'url': URL, 'response': html().decode()}}
            return Response(_url, json.dumps(solution).encode())

        source = MediaMarktSource(get=fetch, post=browser, sleep=lambda _: None)
        offer, reason, status = source.read_offer(OLD_URL)
        self.assertEqual((offer['price'], reason, status), (89999, 'observed_exact_offer', 200))
        self.assertEqual(browser_calls, [OLD_URL])
        self.assertEqual(source.report()['browser_html_responses'], 1)

    def test_browser_quota_does_not_poison_later_direct_cash_offer(self):
        direct_title = 'APPLE iPhone 17 256 GB Akıllı Telefon Beyaz MG6K4TU/A'
        fetch = Fetch({ROBOTS_URL: ROBOTS,
                       URL: Response(URL, status=403),
                       OLD_URL: html(url=OLD_URL, sku='166347558', title=direct_title)})
        source = MediaMarktSource(get=fetch, sleep=lambda _: None,
                                 post=lambda *_args, **_kwargs: self.fail('Browser quota must not be exceeded'))
        source._browser_count = MAX_BROWSER_REQUESTS

        self.assertEqual(source.read_offer(URL), (None, 'browser_budget_exhausted', None))
        self.assertFalse(source._blocked)
        self.assertEqual(source._browser_failures, 0)
        offer, reason, status = source.read_offer(OLD_URL)
        self.assertEqual((offer['price'], reason, status), (89999, 'observed_exact_offer', 200))
        self.assertEqual(fetch.calls, [ROBOTS_URL, URL, OLD_URL])
        self.assertEqual(source.report()['browser_requests'], MAX_BROWSER_REQUESTS)

    def test_direct_quota_is_reported_without_transport_or_network_request(self):
        fetch = Fetch({ROBOTS_URL: ROBOTS})
        source = MediaMarktSource(get=fetch, sleep=lambda _: None)
        source._load_robots()
        source._direct_count = MAX_DIRECT_REQUESTS
        self.assertEqual(source.read_offer(URL), (None, 'direct_budget_exhausted', None))
        self.assertEqual(fetch.calls, [ROBOTS_URL])
        self.assertEqual(source._browser_failures, 0)
        self.assertFalse(source._blocked)

    def test_real_transport_failures_still_open_source_circuit(self):
        calls = []

        def fetch(url, **_kwargs):
            calls.append(url)
            if url == ROBOTS_URL:
                return Response(url, ROBOTS)
            raise requests.RequestException('simulated connection failure')

        source = MediaMarktSource(get=fetch, sleep=lambda _: None)
        self.assertEqual(source.read_offer(URL), (None, 'transport_error', None))
        self.assertEqual(source.read_offer(OLD_URL), (None, 'transport_error', None))
        self.assertEqual(source.read_offer(URL), (None, 'source_circuit_open', None))
        self.assertEqual(calls, [ROBOTS_URL, URL, OLD_URL])
        self.assertTrue(source._blocked)

    def test_missing_robots_or_marketplace_offer_never_publish(self):
        blocked = Fetch({ROBOTS_URL: Response(ROBOTS_URL, b'denied', status=403)})
        source = MediaMarktSource(get=blocked, sleep=lambda _: None)
        self.assertEqual(source.read_offer(URL)[0], None)
        self.assertEqual(blocked.calls, [ROBOTS_URL])
        first = Fetch({ROBOTS_URL: ROBOTS, URL: html(seller={'name': 'Bittibitiyor'})})
        source = MediaMarktSource(get=first, sleep=lambda _: None)
        self.assertEqual(source.read_offer(URL)[0], None)

    def test_unreviewed_mediamarkt_query_never_uses_generic_reader(self):
        def unexpected_fetch(*_args, **_kwargs):
            raise AssertionError('Generic GET must not run')

        reader = RetailerOfferRefresher(fetch=unexpected_fetch)
        url = URL + '?utm_source=card'
        self.assertEqual(reader._fetch_target(url), None)
        self.assertEqual(reader.target_report('Apple iPhone 17', [url], {}), [{
            'source_url': url, 'merchant': 'MediaMarkt',
            'reason': 'invalid_mediamarkt_pdp_url', 'http_status': None,
        }])


if __name__ == '__main__':
    unittest.main()
