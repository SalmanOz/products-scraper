"""Offline fixtures matching the reviewed public HTML contracts; no accounts."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch, AsyncMock

import requests
from bs4 import BeautifulSoup

from public_retailer_sources import PublicRetailerSources, public_source
from retailer_offer_refresh import _product_offer, _safe_url, RetailerOfferRefresher
from test_retailer_offer_refresh import Response


PT = 'https://www.pt.com.tr/iphone-16-128gb-deniz-mavisi-myed3tu-a'
TROY = 'https://www.troyestore.com/apple-iphone-16-128gb-deniz-mavisi-myed3tua_216591'
SPECS = {'ram_gb': 8, 'storage_gb': 128}


def fixture(url=PT, stock='in_stock'):
    pt = 'pt.com.tr' in url
    title = 'iPhone 16 128GB Deniz Mavisi MYED3TU/A' if pt else 'Apple iPhone 16 128GB Deniz Mavisi'
    data = {'@type': 'Product', '@id': url + '#richSnippet', 'name': title, 'brand': {'@type': 'Brand', 'name': 'Apple'},
            'sku': 'MYED3TU/A' if pt else '216591',
            'offers': {'@type': 'Offer', 'url': url if pt else url.replace('https:', 'http:'),
                       'price': '83999.00', 'priceCurrency': 'TRY', 'itemCondition': 'NewCondition',
                       'seller': {'name': 'Pozitif Teknoloji' if pt else 'Troyestore'},
                       'availability': 'https://schema.org/' + ('InStock' if stock == 'in_stock' else 'OutOfStock')}}
    html = f'<link rel="canonical" href="{url}"><h1>{title}</h1>'
    if pt:
        html += ('<div class="summary"><div class="pt-price-col"><span class="product-page-price">83.999,00 ₺</span></div>'
                 f'<p class="stock {"in-stock" if stock == "in_stock" else "out-of-stock"}">Stok</p>'
                 '<div class="accessory"><span class="product-page-price">949,00 ₺</span><p class="stock in-stock">Stokta</p></div>'
                 '</div><p>Tüm Kartlar Tek Çekim veya Havale/EFT</p>')
    else:
        html += ('<div class="product-details-area"><div class="p-info"><div class="p-price"><span class="one-price">83.999,00 TL</span></div></div></div>'
                 '<button data-js="btn-add-basket">Sepete Ekle</button>'
                 '<div style="display:none !important"><button disabled>Stok Tükendi</button></div>')
    return html, data


def parse(html, data, url=PT):
    soup = BeautifulSoup(html + '<script type="application/ld+json">' + json.dumps(data) + '</script>', 'html.parser')
    return _product_offer(soup, url)


class PublicSourceContractTests(unittest.TestCase):
    def test_exact_price_and_stock_not_accessories(self):
        for url in (PT, TROY):
            with self.subTest(url=url):
                offer = parse(*fixture(url), url)
                self.assertIsNotNone(offer)
                self.assertEqual(offer['price'], 83999)
                self.assertEqual(offer['availability'], 'in_stock')
                self.assertEqual(offer['url'], url)
        self.assertEqual(parse(*fixture(stock='out_of_stock'))['availability'], 'out_of_stock')

    def test_wrong_price_stock_condition_seller_country_brand_and_sku_fail_closed(self):
        html, data = fixture()
        mutations = [('price', '949.00'), ('itemCondition', 'UsedCondition'),
                     ('seller', {'name': 'Some Other Seller'}), ('availability', 'https://schema.org/OutOfStock')]
        for key, value in mutations:
            changed = deepcopy(data); changed['offers'][key] = value
            self.assertIsNone(parse(html, changed), key)
        for bad_html in (html.replace('83.999,00', '82.999,00'), html.replace('Tüm Kartlar Tek Çekim', 'Sadece Havale'),
                         html.replace('class="stock in-stock"', 'class="stock unknown"')):
            self.assertIsNone(parse(bad_html, data))
        for field, value in [('brand', {'name': 'Other'}), ('name', 'iPhone 16 Pro 128GB'), ('hasVariant', [{'sku': 'other'}])]:
            changed = deepcopy(data); changed[field] = value
            self.assertIsNone(parse(html, changed), field)
        self.assertIsNone(parse(html.replace('MYED3TU/A', 'MYED3LL/A'),
                                {**data, 'name': data['name'].replace('TU/A', 'LL/A'), 'sku': 'MYED3LL/A'},
                                PT.replace('tu-a', 'll-a')))

    def test_troy_http_schema_exception_is_document_only_and_exact(self):
        html, data = fixture(TROY)
        for bad in ('http://evil.test/phone', TROY.replace('https:', 'http:').replace('216591', '216592'),
                    TROY.replace('https:', 'http:') + '?variant=other',
                    TROY.replace('https:', 'http:').replace('.com/', '.com:8080/')):
            changed = deepcopy(data); changed['offers']['url'] = bad
            self.assertIsNone(parse(html, changed, TROY), bad)
        self.assertIsNone(parse(html.replace('rel="canonical"', 'rel="alternate"'), data, TROY))
        html, data = fixture(); data['offers']['url'] = PT.replace('https:', 'http:')
        self.assertIsNone(parse(html, data))
        self.assertIsNone(_safe_url(TROY.replace('https:', 'http:')))

    def test_new_source_urls_cannot_have_cart_queries_credentials_or_encoded_separators(self):
        for url in (PT + '?add-to-cart=123', PT + '?sort=price', PT + '#variant',
                    PT.replace('www.pt.com.tr', 'www.pt.com.tr.evil.test'), PT.replace('https:', 'http:'),
                    PT.replace('https://', 'https://user@'), PT + '%3fadd-to-cart=1'):
            self.assertIsNone(public_source(url), url)
            self.assertIsNone(_safe_url(url), url)


class PublicDiscoveryTests(unittest.TestCase):
    def catalog(self, *, robots='User-agent: *\nAllow: /', status=200, linked=True):
        calls = []
        def fetch(url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith('robots.txt'):
                return Response(robots, status, url)
            if url == 'https://www.pt.com.tr/':
                return Response('<a href="/iphone-16">iPhone</a>' if linked else '', url=url)
            if url == 'https://www.pt.com.tr/iphone-16':
                return Response(''.join(f'<a href="{u}">Phone</a>' for u in (
                    PT, PT, PT.replace('128gb', '256gb'), PT.replace('iphone-16', 'iphone-16-pro'),
                    PT + '?add-to-cart=1', PT + '-teshir')), url=url)
            if url == PT:
                html, data = fixture()
                return Response(html + '<script type="application/ld+json">' + json.dumps(data) + '</script>', url=url)
            return Response('', 403, url)
        catalog = PublicRetailerSources(fetch=fetch, sleep=lambda _: None)
        return catalog, calls, fetch

    def test_discovery_is_live_linked_exact_storage_deduplicated_and_cached(self):
        catalog, calls, _ = self.catalog()
        for _ in range(2):
            self.assertEqual(catalog.discover('Apple iPhone 16', SPECS), [{'source_url': PT}])
        self.assertEqual(sum(u == 'https://www.pt.com.tr/iphone-16' for u, _ in calls), 1)
        self.assertTrue(all(kwargs['allow_redirects'] is False for _, kwargs in calls))
        self.assertTrue(all(url.startswith('https:') and '?' not in url for url, _ in calls))

    def test_missing_live_family_link_and_ineligible_models_do_not_generate_urls(self):
        catalog, calls, _ = self.catalog(linked=False)
        self.assertEqual(catalog.discover('Samsung Galaxy A27 5G', SPECS), [])
        self.assertEqual(catalog.discover('Apple iPhone 16', {}), [])
        self.assertEqual(calls, [])
        self.assertEqual(catalog.discover('Apple iPhone 16', SPECS), [])
        self.assertFalse(any(u == 'https://www.pt.com.tr/iphone-16' for u, _ in calls))

    def test_robots_denial_unavailable_and_host_breaker(self):
        for policy, status in [('User-agent: *\nDisallow: /', 200), ('', 403), ('', 406), ('', 429)]:
            catalog, calls, _ = self.catalog(robots=policy, status=status)
            self.assertEqual(catalog.discover('Apple iPhone 16', SPECS), [])
            self.assertTrue(all(u.endswith('robots.txt') for u, _ in calls))
        catalog, calls, _ = self.catalog()
        catalog.fetch('https://www.pt.com.tr/missing')
        with self.assertRaises(requests.RequestException):
            catalog.fetch(PT)
        self.assertFalse(any(u == PT for u, _ in calls))

    def test_new_pdp_reader_obeys_robots_and_only_fetches_https(self):
        catalog, calls, fetch = self.catalog()
        reader = RetailerOfferRefresher(fetch=fetch)
        reader.public_sources = catalog
        offers = reader.refresh_existing_offer_targets('Apple iPhone 16', [{'source_url': PT}], SPECS)
        self.assertEqual(len(offers), 1)
        self.assertEqual(calls[0][0], 'https://www.pt.com.tr/robots.txt')
        self.assertEqual(reader.refresh_existing_offer_targets('Apple iPhone 16', [{'source_url': PT}], SPECS), offers)
        self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
