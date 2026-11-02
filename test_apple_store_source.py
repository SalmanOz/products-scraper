import json
import unittest

from apple_store_source import (
    AppleStoreSource, PDP_URLS, ROBOTS_URL, _NAMES, reviewed_slug, selected_offer,
)
from tr_price_scraper import TRPriceScraper


SKU = {'apple-iphone-18-pro-max': 'MJXN4TU/A',
       'apple-iphone-18-pro': 'MJRP4TU/A',
       'apple-iphone-17e': 'MHRV4TU/A',
       'apple-iphone-17': 'MG6J4TU/A'}
PRICE = {'apple-iphone-18-pro-max': 149999, 'apple-iphone-18-pro': 137999,
         'apple-iphone-17e': 72999, 'apple-iphone-17': 99999}


def page(slug='apple-iphone-17', *, price=None, visible=None, color='black',
         capacity='256gb', screen=None, disabled=False, seller=None,
         condition=None, schema_url=None, duplicate=False):
    url = PDP_URLS[slug]
    name = _NAMES[slug]
    price = PRICE[slug] if price is None else price
    visible = (f'{PRICE[slug]:,.2f}'.replace(',', '_').replace('.', ',')
               .replace('_', '.') + ' TL') if visible is None else visible
    offer = {'@type': 'Offer', 'priceCurrency': 'TRY', 'price': price,
             'sku': SKU[slug]}
    if seller is not None:
        offer['seller'] = seller
    if condition is not None:
        offer['itemCondition'] = condition
    product = {'@type': 'Product', 'name': name, 'color': 'Siyah',
               'url': schema_url or url, 'mainEntityOfPage': url, 'offers': [offer]}
    scripts = f'<script type="application/ld+json">{json.dumps(product)}</script>'
    if duplicate:
        scripts += scripts
    if screen is None and slug in {'apple-iphone-18-pro-max', 'apple-iphone-18-pro'}:
        screen = '6_9inch' if slug.endswith('-max') else '6_3inch'
    screen_radio = (f'<input type="radio" name="dimensionScreensize" value="{screen}" checked>'
                    if screen else '')
    disabled_attr = ' disabled' if disabled else ''
    return (f'<html><head><title>{name} satın alın - Apple (TR)</title>{scripts}</head>'
            f'<body>{screen_radio}'
            f'<input type="radio" name="dimensionColor" value="{color}" checked>'
            f'<input type="radio" name="dimensionCapacity" value="{capacity}" checked>'
            f'<div class="rf-bfe-summary">'
            f'<div data-autom="summary-productName">{name}</div>'
            f'<div data-autom="full-price">{visible}</div>'
            f'<button data-autom="add-to-cart"{disabled_attr}>Çantaya Ekleyin</button>'
            f'</div></body></html>')


class Response:
    def __init__(self, url, body=b'', status=200):
        self.url, self.data, self.status_code = url, body, status
        self.headers = {'Content-Length': str(len(body))}

    def iter_content(self, chunk_size):
        for index in range(0, len(self.data), chunk_size):
            yield self.data[index:index + chunk_size]

    def close(self):
        pass


class AppleStoreSourceTests(unittest.TestCase):
    def test_four_selected_exact_cash_offers_match_catalog_variants(self):
        matcher = TRPriceScraper()
        for slug, url in PDP_URLS.items():
            with self.subTest(slug=slug):
                offer = selected_offer(page(slug), url)
                self.assertEqual(offer['price'], PRICE[slug])
                self.assertEqual(offer['availability'], 'in_stock')
                name = 'Apple ' + _NAMES[slug].rsplit(' 256 GB Siyah', 1)[0]
                ram = 12 if slug.startswith('apple-iphone-18-pro') else 8
                self.assertTrue(matcher.has_observed_offer_evidence(
                    name, offer, {'ram_gb': ram, 'storage_gb': 256,
                                  'network_generation': '5g'}))

    def test_identity_price_and_purchasability_mismatches_fail_closed(self):
        url = PDP_URLS['apple-iphone-17']
        cases = [
            page(price=89999), page(visible='89.999,00 TL'), page(color='white'),
            page(capacity='512gb'), page(disabled=True), page(seller={'name': 'Other'}),
            page(condition='https://schema.org/RefurbishedCondition'),
            page(schema_url=PDP_URLS['apple-iphone-17e']), page(duplicate=True),
        ]
        for html in cases:
            with self.subTest(case=html[:100]):
                self.assertIsNone(selected_offer(html, url))
        self.assertIsNone(selected_offer(page('apple-iphone-18-pro-max', screen='6_3inch'),
                                         PDP_URLS['apple-iphone-18-pro-max']))

    def test_only_four_clean_reviewed_urls(self):
        url = PDP_URLS['apple-iphone-17']
        self.assertEqual(reviewed_slug(url), 'apple-iphone-17')
        for changed in (url + '?color=black', url.replace('https:', 'http:'),
                        url.replace('www.apple.com', 'apple.com.evil.example'),
                        'https://www.apple.com/tr/shop/buy-iphone/iphone-17'):
            self.assertIsNone(reviewed_slug(changed))

    def test_robots_and_browser_selected_offer_only(self):
        url = PDP_URLS['apple-iphone-17']
        calls = []

        def get(request_url, **_kwargs):
            calls.append(request_url)
            return Response(request_url, b'User-agent: *\nAllow: /tr/shop/buy-iphone/\n')

        def post(request_url, **kwargs):
            calls.append(kwargs['json']['url'])
            body = {'status': 'ok', 'solution': {'status': 200, 'url': url,
                                               'response': page()}}
            return Response(request_url, json.dumps(body).encode())

        source = AppleStoreSource(get=get, post=post, sleep=lambda _: None)
        offer, reason, status = source.read_offer(url)
        self.assertEqual((offer['price'], reason, status),
                         (99999, 'observed_exact_offer', 200))
        self.assertEqual(calls, [ROBOTS_URL, url])
        self.assertEqual(source.report()['browser_requests'], 1)

    def test_robots_disallow_never_calls_browser(self):
        def get(url, **_kwargs):
            return Response(url, b'User-agent: *\nDisallow: /tr/shop/buy-iphone/\n')

        def post(*_args, **_kwargs):
            raise AssertionError('Browser must not run when robots disallows')

        source = AppleStoreSource(get=get, post=post)
        self.assertIsNone(source.read_offer(PDP_URLS['apple-iphone-17'])[0])


if __name__ == '__main__':
    unittest.main()
