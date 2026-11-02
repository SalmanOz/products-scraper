"""Selected Pasaj cash evidence and read-only transport boundaries."""

import json
import unittest

from pasaj_source import IPHONE17_256_URL, ROBOTS_URL, PasajSource, _selected_offer
from retailer_offer_refresh import RetailerOfferRefresher


def page(*, cash='86.999', seller='Turkcell Satış A.Ş.', price=86999,
         availability='https://schema.org/InStock', ram='8 GB', storage='256 GB',
         button='Sepete Ekle', disabled='', canonical=IPHONE17_256_URL):
    product = {'@type': 'Product', 'name': 'iPhone 17 256 GB',
               'url': IPHONE17_256_URL, 'sku': 1175749,
               'brand': {'@type': 'Brand', 'name': 'Apple'},
               'offers': {'@type': 'AggregateOffer', 'lowPrice': price,
                          'highPrice': price, 'priceCurrency': 'TRY',
                          'availability': availability, 'offerCount': 1,
                          'offers': [{'@type': 'Offer', 'price': price,
                                      'priceCurrency': 'TRY',
                                      'offeredBy': {'@type': 'Organization',
                                                    'name': seller}}]}}
    return f'''<html><head><title>iPhone 17</title><link rel="canonical" href="{canonical}">
    <script type="application/ld+json">{json.dumps(product)}</script></head><body>
    <h1>iPhone 17 256 GB</h1>
    <div class="m-product-detail-features__wrap"><div class="m-product-detail-features__title">Ram (bellek) Kapasitesi</div><div class="m-product-detail-features__text">{ram}</div></div>
    <div class="m-product-detail-features__wrap"><div class="m-product-detail-features__title">Dahili Hafıza</div><div class="m-product-detail-features__text">{storage}</div></div>
    <form class="m-form"><div class="price-radio-cash">
    <input class="product-basket-type" type="radio" data-type="cash" name="price" checked>
    <div class="m-card-offer__seller-container"><span class="a-price-radio-b__title">{seller}</span></div>
    <div class="a-price-radio-b__price-box"><span class="a-price-val">{cash}</span><span class="a-price__currency">TL</span></div></div>
    <div class="price-radio-contracted"><span class="a-price-val">36.784,31</span></div>
    <button id="p-1175749" class="device-available add-to-basket-non-login" {disabled}>{button}</button></form>
    <div class="recommendation"><span class="a-price-val">60.000</span></div></body></html>'''


class Response:
    def __init__(self, url, body, status=200, content_type='text/html'):
        self.url = url
        self.status_code = status
        self.headers = {'Content-Type': content_type}
        self.body = body.encode()

    def iter_content(self, chunk_size=65536):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]

    def close(self):
        pass


class PasajSourceTest(unittest.TestCase):
    def test_exact_cash_offer(self):
        offer = _selected_offer(page(), IPHONE17_256_URL)
        self.assertEqual((offer['merchant'], offer['price'], offer['currency'],
                          offer['availability']), ('Pasaj', 86999, 'TRY', 'in_stock'))

    def test_rejects_mismatched_or_conditional_evidence(self):
        cases = (
            {'cash': '36.784,31'}, {'seller': 'Marketplace Mağazası'},
            {'availability': 'https://schema.org/OutOfStock'},
            {'ram': '6 GB'}, {'storage': '128 GB'},
            {'button': 'Stokta Yok'}, {'disabled': 'disabled'},
            {'canonical': 'https://www.turkcell.com.tr/pasaj/c/ios-telefonlar'},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertIsNone(_selected_offer(page(**changes), IPHONE17_256_URL))

    def test_rejects_unreviewed_url_and_offer_count_tampering(self):
        self.assertIsNone(_selected_offer(page(), IPHONE17_256_URL + '?seller=x'))
        self.assertIsNone(_selected_offer(page().replace('"offerCount": 1', '"offerCount": 2'),
                                           IPHONE17_256_URL))
        self.assertIsNone(_selected_offer(page().replace('"lowPrice": 86999', '"lowPrice": 67999'),
                                           IPHONE17_256_URL))
        self.assertIsNone(_selected_offer(page().replace('"price": 86999', '"price": 67999', 1),
                                           IPHONE17_256_URL))

    def test_robots_and_exact_pdp_transport(self):
        calls = []
        def get(url, **kwargs):
            calls.append((url, kwargs))
            if url == ROBOTS_URL:
                return Response(url, 'User-agent: *\nDisallow: /account\n',
                                content_type='text/plain')
            return Response(url, page())
        source = PasajSource(get=get, sleep=lambda _: None)
        offer, reason, status = source.read_offer(IPHONE17_256_URL)
        self.assertEqual((reason, status, offer['price']), ('observed_exact_cash_offer', 200, 86999))
        self.assertEqual([url for url, _ in calls], [ROBOTS_URL, IPHONE17_256_URL])
        self.assertTrue(all(not kw['allow_redirects'] and kw['stream'] for _, kw in calls))

    def test_robots_disallow_and_redirect_fail_closed(self):
        def disallowed(url, **kwargs):
            return Response(url, 'User-agent: *\nDisallow: /pasaj\n', content_type='text/plain')
        source = PasajSource(get=disallowed, sleep=lambda _: None)
        self.assertEqual(source.read_offer(IPHONE17_256_URL)[1], 'robots_disallowed')
        self.assertEqual(source.report()['pdp_requests'], 0)
        def redirected(url, **kwargs):
            if url == ROBOTS_URL:
                return Response(url, 'User-agent: *\nAllow: /\n', content_type='text/plain')
            return Response('https://www.turkcell.com.tr/pasaj/c/ios-telefonlar', page(), 302)
        source = PasajSource(get=redirected, sleep=lambda _: None)
        self.assertEqual(source.read_offer(IPHONE17_256_URL)[1], 'unexpected_final_url')

    def test_refresher_identity_and_unreviewed_routing(self):
        class FakeSource:
            def read_offer(self, url):
                return _selected_offer(page(), url), 'observed_exact_cash_offer', 200
        refresher = RetailerOfferRefresher(pasaj_source=FakeSource())
        specs = {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'}
        offers = refresher.refresh_existing_offer_targets('Apple iPhone 17',
                         [{'source_url': IPHONE17_256_URL}], specs)
        self.assertEqual(len(offers), 1)
        other = IPHONE17_256_URL + '?seller=other'
        self.assertEqual(refresher.refresh_existing_offer_targets('Apple iPhone 17',
                         [{'source_url': other}], specs), [])
        self.assertEqual(refresher.target_report('Apple iPhone 17', [other], specs)[0]['reason'],
                         'unreviewed_pasaj_pdp')


if __name__ == '__main__':
    unittest.main()
