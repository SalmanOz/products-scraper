"""Offline gates for the two exact unpriced Vatan category/PDP candidates."""

from io import BytesIO
import unittest

import requests
from bs4 import BeautifulSoup

from audit_vatan_category_unpriced import PROFILES, run_audit
from retailer_offer_refresh import RetailerOfferRefresher, _schema_nodes
from test_retailer_offer_refresh import Response, vatan_variant_page, vatan_variant_schema
from tr_price_scraper import TRPriceScraper
from vatan_category_source import (OPPO_CATEGORY, VIVO_CATEGORY, VatanCategorySource,
                                   parse_category_cards)
from vatan_sitemap_source import ROBOTS_URL


TITLES = (
    'OPPO Find X9 Pro 16/512 Akıllı Telefon Titanyum Antrasit',
    'Vivo X300 16/512GB Akıllı Telefon Pembe',
)
ROBOTS = 'User-agent: *\nAllow: /\nDisallow: /*?page=*\nDisallow: /arama/\n'


def response(url, body, status=200):
    result = requests.Response()
    result.status_code = status
    result.url = url
    result._content = body.encode() if isinstance(body, str) else body
    result.raw = BytesIO(result._content)
    result.headers['Content-Length'] = str(len(result._content))
    return result


def category_html(url, title, extra=''):
    return (f'<html><div id="productsLoad"><a class="product-list-link" href="{url}">'
            f'<div class="product-list__product-name"><h3>{title}</h3></div>'
            '<span class="price">1 TL</span></a>' + extra + '</div></html>')


class Categories:
    def __init__(self, titles=TITLES):
        self.titles = titles

    def read_category(self, url):
        index = 0 if url == OPPO_CATEGORY else 1
        return [{'title': self.titles[index], 'source_url': PROFILES[index]['url']}]

    def report(self):
        return {'robots_status': 'ready', 'categories': {}}


class Reader:
    def __init__(self, offers):
        self.offers = offers
        self.vatan_reviewed_source = self

    def refresh_existing_offer_targets(self, name, targets, specs):
        index = next(i for i, profile in enumerate(PROFILES) if profile['name'] == name)
        assert targets == [{'source_url': PROFILES[index]['url']}]
        assert specs == {'ram_gb': 16, 'storage_gb': 512, 'network_generation': '5g'}
        return [self.offers[index]] if self.offers[index] else []

    def target_report(self, *_args):
        return [{'reason': 'observed_exact_offer', 'http_status': 200}]

    def report(self):
        return {'robots_status': 'ready'}


def offers():
    return [
        {'merchant': 'Vatan Bilgisayar', 'url': profile['url'],
         'source_title': TITLES[index], 'price': price, 'currency': 'TRY',
         'availability': 'in_stock', 'observed_at': '2026-11-02T12:00:00Z'}
        for index, (profile, price) in enumerate(zip(PROFILES, (104999, 75999)))
    ]


class Catalog:
    def __init__(self, products=None):
        self.products = products if products is not None else [
            {'id': profile['id'], 'slug': profile['slug'], 'name': profile['name'],
             'attributes': {'ram_gb': 16, 'storage_gb': 512,
                            'Connectivity': {'5G': True}}}
            for profile in PROFILES
        ]

    def get_published_products(self):
        return self.products


class VatanCategoryTests(unittest.TestCase):
    @staticmethod
    def vivo_page(*, seller='Vatan Bilgisayar', visible_price='75.999 TL',
                  cart=True, has_variant=True):
        profile = PROFILES[1]
        title = TITLES[1]
        schema = vatan_variant_schema(profile['url'], title, '154791', '75999')
        schema['name'] = title.replace('Akıllı', 'Ak&#x131;ll&#x131;')
        schema['hasVariant'] = ([{'@type': 'Product', 'sku': '154790',
                                 'url': 'https://www.vatanbilgisayar.com/'
                                        'vivo-x300-16-512gb-akilli-telefon-siyah.html'}]
                                if has_variant else [])
        schema['offers']['seller']['name'] = seller
        schema['review'] = [{'@type': 'Review',
                             'reviewBody': 'Telefon gerçekten iyi,\nbaşka yönden üstün.'}]
        html = vatan_variant_page(schema, title=title, code='5671588 / 154791',
                                  visible_price=visible_price, web_price=False,
                                  cart=cart)
        return html.replace('iyi,\\nb', 'iyi,\nb')

    def test_vivo_pdp_repairs_only_review_body_raw_newline_and_proves_selected_offer(self):
        profile = PROFILES[1]
        html = self.vivo_page()
        self.assertIn('iyi,\nbaşka', html)
        soup = BeautifulSoup(html, 'html.parser')
        self.assertEqual([node.get('sku') for node in _schema_nodes(soup, profile['url'])
                          if node.get('@type') == 'Product'], ['154791'])
        self.assertEqual([node for node in _schema_nodes(soup, 'https://www.amazon.com.tr/dp/B123456789')
                          if node.get('@type') == 'Product'], [])
        class NoReviewedVatan:
            @staticmethod
            def is_reviewed(_url):
                return False
        reader = RetailerOfferRefresher(
            fetch=lambda _url, **_kw: Response(html, url=profile['url']),
            vatan_source=NoReviewedVatan())
        found = reader.refresh_existing_offer_targets(profile['name'],
            [{'source_url': profile['url']}],
            {'ram_gb': 16, 'storage_gb': 512, 'network_generation': '5g'})
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['source_title'], TITLES[1])
        self.assertEqual(found[0]['price'], 75999)
        self.assertEqual(found[0]['merchant'], 'Vatan Bilgisayar')

    def test_vivo_repaired_schema_still_requires_first_party_selected_variant(self):
        profile = PROFILES[1]
        for options in ({'seller': 'Other'}, {'visible_price': '74.999 TL'},
                        {'cart': False}, {'has_variant': False}):
            with self.subTest(options=options):
                html = self.vivo_page(**options)
                class NoReviewedVatan:
                    @staticmethod
                    def is_reviewed(_url):
                        return False
                reader = RetailerOfferRefresher(
                    fetch=lambda _url, **_kw: Response(html, url=profile['url']),
                    vatan_source=NoReviewedVatan())
                found = reader.refresh_existing_offer_targets(profile['name'],
                    [{'source_url': profile['url']}],
                    {'ram_gb': 16, 'storage_gb': 512, 'network_generation': '5g'})
                self.assertEqual(found, [])

    def test_bare_16_512_needs_real_title_and_corresponding_vatan_pdp_slug(self):
        matcher = TRPriceScraper()
        profile = PROFILES[0]
        specs = {'ram_gb': 16, 'storage_gb': 512, 'network_generation': '5g'}
        exact = {'source_title': TITLES[0], 'url': profile['url']}
        self.assertTrue(matcher.has_observed_offer_evidence(profile['name'], exact, specs))
        for bad in (
            {**exact, 'source_title': TITLES[0].replace('16/512', '8/256')},
            {**exact, 'source_title': TITLES[0] + ' veya 8/256'},
            {**exact, 'url': profile['url'].replace('16-512', '8-256')},
            {**exact, 'url': 'https://other.example/oppo-find-x9-pro-16-512-akilli-telefon.html'},
        ):
            self.assertFalse(matcher.has_observed_offer_evidence(profile['name'], bad, specs), bad)
        self.assertFalse(matcher.has_observed_offer_evidence(
            'Apple iPhone 16', {'source_title': 'Apple iPhone 16/512 Akıllı Telefon',
                                'url': 'https://www.vatanbilgisayar.com/iphone-16-512-akilli-telefon.html'},
            specs))

    def test_category_cards_are_exact_urls_only_and_never_return_price(self):
        url = PROFILES[0]['url']
        extra = (f'<a class="product-list-link" href="{url}?page=2"><h3>other</h3></a>'
                 '<a class="product-list-link" href="https://other.example/phone.html">'
                 '<h3>other</h3></a>')
        self.assertEqual(parse_category_cards(category_html(url, TITLES[0], extra),
                                              OPPO_CATEGORY),
                         [{'title': TITLES[0], 'source_url': url}])
        self.assertEqual(parse_category_cards(category_html(url, TITLES[0]),
                                              OPPO_CATEGORY + '?page=2'), [])

    def test_direct_category_get_is_bounded_and_403_stops(self):
        calls = []
        def fetch(url, **_kwargs):
            calls.append(url)
            return response(url, ROBOTS if url == ROBOTS_URL else 'denied',
                            200 if url == ROBOTS_URL else 403)
        source = VatanCategorySource(fetch=fetch, sleep=lambda _: None)
        self.assertEqual(source.read_category(OPPO_CATEGORY), [])
        self.assertEqual(source.read_category(OPPO_CATEGORY), [])
        self.assertEqual(source.read_category(OPPO_CATEGORY + '?page=2'), [])
        self.assertEqual(calls, [OPPO_CATEGORY])
        self.assertEqual(source.report()['categories'][OPPO_CATEGORY]['status'], 'http_403')
    def test_selected_pdp_parser_keeps_exact_real_h1_and_visible_cash(self):
        for profile, title, sku, price, visible in zip(
                PROFILES, TITLES, ('155165', '154791'), ('104999', '75999'),
                ('104.999 TL', '75.999 TL')):
            with self.subTest(profile=profile['slug']):
                schema = vatan_variant_schema(profile['url'], title, sku, price)
                html = vatan_variant_page(schema, title=title, code='MODEL / ' + sku,
                                          visible_price=visible, web_price=False)
                class NoReviewedVatan:
                    @staticmethod
                    def is_reviewed(_url):
                        return False
                reader = RetailerOfferRefresher(
                    fetch=lambda _url, **_kw: Response(html, url=profile['url']),
                    vatan_source=NoReviewedVatan())
                specs = {'ram_gb': 16, 'storage_gb': 512, 'network_generation': '5g'}
                found = reader.refresh_existing_offer_targets(
                    profile['name'], [{'source_url': profile['url']}], specs)
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]['source_title'], title)
                self.assertEqual(found[0]['price'], float(price))
                self.assertEqual(found[0]['availability'], 'in_stock')
                self.assertTrue(found[0]['selected_vatan_variant_verified'])

    def test_read_only_audit_requires_both_exact_category_and_pdp_offers(self):
        good = offers()
        report = run_audit(Categories(), Reader(good), Catalog())
        self.assertEqual(report['status'], 'passed')
        self.assertFalse(report['writes'])
        self.assertTrue(all(row['catalog_confirmed'] for row in report['products']))
        for bad in ({**good[0], 'url': good[0]['url'].replace('16-512', '8-256')},
                    {**good[0], 'availability': 'out_of_stock'},
                    {**good[0], 'merchant': 'Other'}):
            self.assertEqual(run_audit(Categories(), Reader([bad, good[1]]), Catalog())['status'],
                             'partial')
        self.assertEqual(run_audit(Categories(('wrong', TITLES[1])), Reader(good), Catalog())['status'],
                         'partial')
        for changed in ({'id': 290, 'slug': 'other'},
                        {'id': 290, 'attributes': {'ram_gb': 8, 'storage_gb': 512,
                                                  'Connectivity': {'5G': True}}},
                        {'id': 290, 'attributes': {'ram_gb': 16, 'storage_gb': 512}}):
            products = Catalog().products
            products[0] = {**products[0], **changed}
            self.assertEqual(run_audit(Categories(), Reader(good), Catalog(products))['status'],
                             'partial')


if __name__ == '__main__':
    unittest.main()
