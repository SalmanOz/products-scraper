"""Offline gates for the two exact unpriced Vatan category/PDP candidates."""

from io import BytesIO
import unittest
from urllib.robotparser import RobotFileParser

import requests

from audit_vatan_category_unpriced import PROFILES, run_audit
from retailer_offer_refresh import RetailerOfferRefresher
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
        rules = RobotFileParser()
        rules.parse(ROBOTS.splitlines())
        url = PROFILES[0]['url']
        extra = (f'<a class="product-list-link" href="{url}?page=2"><h3>other</h3></a>'
                 '<a class="product-list-link" href="https://other.example/phone.html">'
                 '<h3>other</h3></a>')
        self.assertEqual(parse_category_cards(category_html(url, TITLES[0], extra), rules,
                                              OPPO_CATEGORY),
                         [{'title': TITLES[0], 'source_url': url}])
        self.assertEqual(parse_category_cards(category_html(url, TITLES[0]), rules,
                                              OPPO_CATEGORY + '?page=2'), [])

    def test_direct_category_get_is_robots_bounded_and_403_stops(self):
        calls = []
        def fetch(url, **_kwargs):
            calls.append(url)
            return response(url, ROBOTS if url == ROBOTS_URL else 'denied',
                            200 if url == ROBOTS_URL else 403)
        source = VatanCategorySource(fetch=fetch, sleep=lambda _: None)
        self.assertEqual(source.read_category(OPPO_CATEGORY), [])
        self.assertEqual(source.read_category(OPPO_CATEGORY), [])
        self.assertEqual(source.read_category(OPPO_CATEGORY + '?page=2'), [])
        self.assertEqual(calls, [ROBOTS_URL, OPPO_CATEGORY])
        self.assertEqual(source.report()['categories'][OPPO_CATEGORY]['status'], 'http_403')
        disallowed = VatanCategorySource(fetch=lambda url, **_k: response(
            url, 'User-agent: *\nDisallow: /oppo/\n'), sleep=lambda _: None)
        self.assertEqual(disallowed.read_category(OPPO_CATEGORY), [])
        self.assertEqual(disallowed.report()['categories'][OPPO_CATEGORY]['status'],
                         'robots_disallowed')

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
