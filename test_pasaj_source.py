"""Selected Pasaj cash evidence and read-only transport boundaries."""

import json
import unittest

from audit_pasaj_source import PROFILES as AUDIT_PROFILES, run_audit
from pasaj_source import A26_6_128_URL, FOLD8_ULTRA_12_256_URL, IPHONE16_128_URL, IPHONE17_256_URL, ROBOTS_URL, PasajSource, _selected_offer
from retailer_offer_refresh import RetailerOfferRefresher


def page(*, cash='86.999', seller='Turkcell Satış A.Ş.', price=86999,
         availability='https://schema.org/InStock', ram='8 GB', storage='256 GB',
         button='Sepete Ekle', disabled='', canonical=None,
         url=IPHONE17_256_URL, title='iPhone 17 256 GB', sku=1175749,
         ram_feature=True, faq_ram=None, brand='Apple', network=None,
         color_stock='true', color_seller='Turkcell Satış A.Ş.',
         color_price='19.499', color_uid='1159284-1-1004412',
         color_at_basket='false'):
    product = {'@type': 'Product', 'name': title,
               'url': url, 'sku': sku,
               'brand': {'@type': 'Brand', 'name': brand},
               'offers': {'@type': 'AggregateOffer', 'lowPrice': price,
                          'highPrice': price, 'priceCurrency': 'TRY',
                          'availability': availability, 'offerCount': 1,
                          'offers': [{'@type': 'Offer', 'price': price,
                                      'priceCurrency': 'TRY',
                                      'offeredBy': {'@type': 'Organization',
                                                    'name': seller}}]}}
    ram_html = (f'<div class="m-product-detail-features__wrap"><div class="m-product-detail-features__title">Ram (bellek) Kapasitesi</div><div class="m-product-detail-features__text">{ram}</div></div>'
                if ram_feature else '')
    faq_html = (f'<section id="service-faq"><div class="m-accordion"><div class="m-accordion__container"><div class="m-accordion__head"><span itemprop="name">iPhone 16 128 GB Özellikleri Nelerdir?</span></div><div class="m-accordion__body" itemprop="acceptedAnswer"><ul><li>{faq_ram}</li></ul></div></div></div></section>'
                if faq_ram is not None else '')
    network_html = (f'<div class="m-product-detail-features__wrap"><div class="m-product-detail-features__title">Hücresel Plan</div><div class="m-product-detail-features__text">{network}</div></div>'
                    if network is not None else '')
    color_html = (f'<select class="m-select-color"><option selected '
                  f'data-stock-available="{color_stock}" data-at-basket="{color_at_basket}" '
                  f'data-seller-name="{color_seller}" '
                  f'data-price="{color_price}" data-device-product-uid="{color_uid}">'
                  f'Beyaz</option></select>' if url in (A26_6_128_URL, FOLD8_ULTRA_12_256_URL) else '')
    return f'''<html><head><title>{title}</title><link rel="canonical" href="{canonical or url}">
    <script type="application/ld+json">{json.dumps(product)}</script></head><body>
    <h1>{title}</h1>
    {ram_html}{faq_html}{network_html}
    <div class="m-product-detail-features__wrap"><div class="m-product-detail-features__title">Dahili Hafıza</div><div class="m-product-detail-features__text">{storage}</div></div>
    <form class="m-form">{color_html}<div class="price-radio-cash">
    <input class="product-basket-type" type="radio" data-type="cash" name="price" checked>
    <div class="m-card-offer__seller-container"><span class="a-price-radio-b__title">{seller}</span></div>
    <div class="a-price-radio-b__price-box"><span class="a-price-val">{cash}</span><span class="a-price__currency">TL</span></div></div>
    <div class="price-radio-contracted"><span class="a-price-val">36.784,31</span></div>
    <button id="p-{sku}" class="device-available add-to-basket-non-login" {disabled}>{button}</button></form>
    <div class="recommendation"><span class="a-price-val">60.000</span></div></body></html>'''


def iphone16_page(**changes):
    values = {'cash': '72.899', 'price': 72899, 'storage': '128 GB',
              'url': IPHONE16_128_URL, 'title': 'iPhone 16 128 GB', 'sku': 1131622,
              'ram_feature': False, 'faq_ram': '8 GB LPDDR5 RAM ile donatılmıştır.'}
    values.update(changes)
    return page(**values)


def a26_page(**changes):
    values = {'cash': '19.499', 'price': 19499, 'url': A26_6_128_URL,
              'title': 'Samsung Galaxy A26 5G 6 GB Ram 128 GB', 'brand': 'Samsung',
              'sku': 1159284, 'ram': '6 GB', 'storage': '128 GB', 'network': '5G'}
    values.update(changes)
    return page(**values)


def fold8_ultra_page(**changes):
    values = {'cash': '151.999', 'price': 151999,
              'url': FOLD8_ULTRA_12_256_URL,
              'title': 'Samsung Galaxy Z Fold8 Ultra 12GB 256GB',
              'brand': 'Samsung', 'sku': 1181169, 'ram': '12 GB',
              'storage': '256 GB', 'network': '5G',
              'color_price': '151.999', 'color_uid': '1181169-1-1013789'}
    values.update(changes)
    return page(**values)


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
    def test_catalog_variant_without_a_manual_profile_uses_selected_cash_and_memory_features(self):
        from test_pasaj_unreviewed_candidates import selected_page, URL
        from update_prices import PriceUpdater
        specs = {'ram_gb': 8, 'storage_gb': 128, 'network_generation': '5g'}
        html = selected_page().replace('Ram (bellek) Kapasitesi', 'Bellek Ram')
        html = html.replace('"highPrice": 19499, ', '')
        calls = []
        def get(url, **kwargs):
            calls.append(url)
            if url == ROBOTS_URL:
                return Response(url, 'User-agent: *\nAllow: /pasaj\n', content_type='text/plain')
            return Response(url, html)
        source = PasajSource(get=get, sleep=lambda _: None)
        source.configure_catalog_budget(110)
        source._pdp_requests = 6
        reader = RetailerOfferRefresher(fetch=get, pasaj_source=source)
        observed = reader.refresh_existing_offer_targets('Samsung Galaxy A54 5G',
                                                       [{'source_url': URL}], specs)
        self.assertEqual([(o['price'], o['merchant']) for o in observed], [(19499, 'Pasaj')])
        updater = PriceUpdater(ingestion_client=object(), offer_refresher=reader)
        product = {'id': 999, 'slug': 'samsung-galaxy-a54-5g', 'name': 'Samsung Galaxy A54 5G'}
        self.assertTrue(updater.live_offer_is_valid(product, observed[0], specs))
        self.assertEqual(len(updater.prepare_product_offers(product, observed,
                             updater.utc_timestamp(), updater.utc_timestamp())), 1)
        self.assertEqual(calls, [URL])
        self.assertEqual(source.report()['pdp_request_limit'], 60)
        self.assertEqual(reader.refresh_existing_offer_targets('Samsung Galaxy A54 5G',
                         [{'source_url': URL}], {**specs, 'storage_gb': 256}), [])

    def test_automatic_catalog_reader_rejects_variant_stock_seller_and_cash_conflicts(self):
        from test_pasaj_unreviewed_candidates import selected_page, URL
        specs = {'ram_gb': 8, 'storage_gb': 128, 'network_generation': '5g'}
        source = PasajSource()
        source.register_catalog_targets('Samsung Galaxy A54 5G', [{'source_url': URL}], specs)
        profile = source._catalog_profiles[URL]
        for html in (selected_page(ram='6 GB'), selected_page(storage='256 GB'),
                     selected_page(network='4G'), selected_page(seller='Other'),
                     selected_page(availability='https://schema.org/OutOfStock'),
                     selected_page(cash='18.499'), selected_page(button='Stokta Yok'),
                     selected_page().replace('data-stock-available="true"', 'data-stock-available="false"'),
                     selected_page().replace('"priceCurrency": "TRY"', '"priceCurrency": "USD"'),
                     selected_page().replace('"@type": "Offer",', '"@type": "Offer", "itemCondition": "https://schema.org/RefurbishedCondition",')):
            self.assertIsNone(_selected_offer(html, URL, profile))
        for url in (URL+'?seller=other', URL.replace('https:', 'http:'),
                    URL.replace('www.turkcell.com.tr', 'attacker.example'),
                    URL.replace('128-gb', '256-gb')):
            source.register_catalog_targets('Samsung Galaxy A54 5G', [{'source_url': url}], specs)
            self.assertFalse(source.is_catalog_target(url))

    def test_reversed_memory_url_reaches_the_same_selected_offer_parser(self):
        from test_pasaj_unreviewed_candidates import selected_page, URL
        url = URL.replace('8gb-128-gb', '128gb-8gb')
        specs = {'ram_gb': 8, 'storage_gb': 128, 'network_generation': '5g'}
        source = PasajSource()
        source.register_catalog_targets('Samsung Galaxy A54 5G', [{'source_url': url}], specs)
        self.assertEqual(source.parse_offer(selected_page(url=url), url)['price'], 19499)
        no_ram_heading = selected_page(url=url, title='Samsung Galaxy A54 5G 128 GB')
        offer = source.parse_offer(no_ram_heading, url)
        self.assertEqual(offer['selected_pasaj_ram_gb'], 8)
        self.assertEqual(offer['source_title'], 'Samsung Galaxy A54 5G 128 GB')
        from pasaj_source import catalog_offer_matches
        self.assertTrue(catalog_offer_matches(offer, 'Samsung Galaxy A54 5G', specs))
        for changes in ({'selected_pasaj_ram_gb': 6}, {'selected_pasaj_storage_gb': 256},
                        {'selected_pasaj_sku': ''}, {'selected_pasaj_seller': 'Other'},
                        {'selected_pasaj_network_generation': '4g'}):
            self.assertFalse(catalog_offer_matches({**offer, **changes}, 'Samsung Galaxy A54 5G', specs))

    def test_exact_cash_offer(self):
        offer = _selected_offer(page(), IPHONE17_256_URL)
        self.assertEqual((offer['merchant'], offer['price'], offer['currency'],
                          offer['availability']), ('Pasaj', 86999, 'TRY', 'in_stock'))
        self.assertIsNone(_selected_offer(page(sku=1175750), IPHONE17_256_URL))

    def test_iphone16_exact_faq_ram_cash_and_stock(self):
        offer = _selected_offer(iphone16_page(), IPHONE16_128_URL)
        self.assertEqual((offer['merchant'], offer['source_title'], offer['price']),
                         ('Pasaj', 'iPhone 16 128 GB', 72899))
        for changes in ({'faq_ram': None}, {'faq_ram': '6 GB LPDDR5 RAM ile donatılmıştır.'},
                        {'ram_feature': True, 'ram': '6 GB'},
                        {'storage': '256 GB'}, {'seller': 'Marketplace Mağazası'},
                        {'cash': '26.576'}, {'availability': 'https://schema.org/OutOfStock'},
                        {'button': 'Stokta Yok'}):
            with self.subTest(changes=changes):
                self.assertIsNone(_selected_offer(iphone16_page(**changes), IPHONE16_128_URL))

    def test_a26_exact_direct_cash_offer(self):
        offer = _selected_offer(a26_page(), A26_6_128_URL)
        self.assertEqual((offer['merchant'], offer['price'], offer['currency'],
                          offer['availability']), ('Pasaj', 19499, 'TRY', 'in_stock'))
        html = a26_page()
        class FakeSource:
            def read_offer(self, url):
                return _selected_offer(html, url), 'observed_exact_cash_offer', 200
        refresher = RetailerOfferRefresher(pasaj_source=FakeSource())
        specs = {'ram_gb': 6, 'storage_gb': 128, 'network_generation': '5g'}
        self.assertEqual(len(refresher.refresh_existing_offer_targets(
            'Samsung Galaxy A26 5G', [{'source_url': A26_6_128_URL}], specs)), 1)

    def test_a26_rejects_mismatched_variant_seller_stock_or_price(self):
        cases = (
            {'cash': '18.719'}, {'seller': 'Other'}, {'price': 18719},
            {'availability': 'https://schema.org/OutOfStock'},
            {'ram': '8 GB'}, {'storage': '256 GB'}, {'network': '4G'},
            {'brand': 'Apple'}, {'title': 'Samsung Galaxy A26 5G 8 GB Ram 128 GB'},
            {'color_stock': 'false'}, {'color_seller': 'Other'},
            {'color_at_basket': 'true'},
            {'color_price': '18.719'}, {'color_uid': '1159285-1-1004412'},
            {'button': 'Stokta Yok'}, {'disabled': 'disabled'},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertIsNone(_selected_offer(a26_page(**changes), A26_6_128_URL))

    def test_fold8_ultra_exact_first_party_cash_and_fail_closed_variants(self):
        offer = _selected_offer(fold8_ultra_page(), FOLD8_ULTRA_12_256_URL)
        self.assertEqual((offer['merchant'], offer['price'], offer['availability']),
                         ('Pasaj', 151999, 'in_stock'))
        for changes in ({'ram': '16 GB'}, {'storage': '512 GB'},
                        {'seller': 'Marketplace Mağazası'}, {'color_seller': 'Marketplace Mağazası'},
                        {'color_stock': 'false'}, {'color_price': '141.999'},
                        {'color_uid': '1181170-1-1013789'}, {'cash': '141.999'},
                        {'button': 'Stokta Yok'}, {'disabled': 'disabled'}):
            with self.subTest(changes=changes):
                self.assertIsNone(_selected_offer(fold8_ultra_page(**changes),
                                                   FOLD8_ULTRA_12_256_URL))

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

    def test_exact_pdp_transport(self):
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
        self.assertEqual([url for url, _ in calls], [IPHONE17_256_URL])
        self.assertTrue(all(not kw['allow_redirects'] and kw['stream'] for _, kw in calls))

    def test_redirect_fails_closed(self):
        def redirected(url, **kwargs):
            if url == ROBOTS_URL:
                return Response(url, 'User-agent: *\nAllow: /\n', content_type='text/plain')
            return Response('https://www.turkcell.com.tr/pasaj/c/ios-telefonlar', page(), 302)
        source = PasajSource(get=redirected, sleep=lambda _: None)
        self.assertEqual(source.read_offer(IPHONE17_256_URL)[1], 'unexpected_final_url')

    def test_read_only_audit_covers_all_three_exact_variants(self):
        pages = {IPHONE17_256_URL: page(), IPHONE16_128_URL: iphone16_page(),
                 A26_6_128_URL: a26_page()}
        class FakeSource:
            def read_offer(self, url):
                offer = _selected_offer(pages[url], url)
                return offer, 'observed_exact_cash_offer' if offer else 'no_selected_cash_offer', 200
            def report(self):
                return {'robots_status': 'allowed', 'pdp_requests': len(pages)}
        report = run_audit(FakeSource())
        self.assertEqual({profile['url'] for profile in AUDIT_PROFILES}, set(pages))
        self.assertEqual((report['status'], report['verified_products'],
                          report['required_products']), ('passed', 3, 3))

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
