import json
import unittest
from datetime import datetime, timezone

from audit_mediamarkt_unpriced_coverage import (
    CATALOG_URL, MAX_AUDIT_MODELS, _catalog, run_audit,
    selected_rejection_evidence,
)
from mediamarkt_source import MediaMarktSource, ROBOTS_URL
from test_mediamarkt_source import URL as READER_URL, ROBOTS, Fetch, html


URL = ('https://www.mediamarkt.com.tr/tr/product/'
       '_apple-iphone-16-pro-128-gb-akilli-telefon-siyah-146317261.html')


class Response:
    def __init__(self, body, *, status=200, url=CATALOG_URL):
        self.data = body
        self.status_code = status
        self.url = url
        self.headers = {'Content-Length': str(len(body))}

    def iter_content(self, chunk_size):
        for start in range(0, len(self.data), chunk_size):
            yield self.data[start:start + chunk_size]

    def close(self):
        pass


def product(id, name, slug, price):
    return {'id': id, 'name': name, 'slug': slug,
            'attributes': {'ram_gb': 8, 'storage_gb': 128},
            'fresh_lowest_price': price, 'fresh_offer_count': int(bool(price))}


class Source:
    def __init__(self, names):
        self.names = names
        self.calls = []

    def discover(self, name, specs):
        self.calls.append((name, specs))
        return [{'source_url': URL}] if name in self.names else []

    def report(self):
        return {'sitemap_status': 'ready', 'eligible_urls': 100,
                'browser_requests': 1, 'browser_html_responses': 1}


class Refresher:
    def __init__(self, offer=None):
        self.offer = offer
        self.calls = []

    def refresh_existing_offer_targets(self, name, targets, specs):
        self.calls.append((name, targets, specs))
        return [self.offer] if self.offer else []

    def target_report(self, name, targets, specs):
        return [{'source_url': URL, 'merchant': 'MediaMarkt',
                 'reason': 'observed_exact_offer', 'http_status': 200}]


class UnpricedCoverageAuditTests(unittest.TestCase):
    def test_same_selected_page_classifies_marketplace_stock_and_condition(self):
        marketplace = selected_rejection_evidence(
            html(seller={'name': 'CepHane Teknoloji'}, marketplace='true'), READER_URL)
        self.assertEqual(marketplace, {
            'category': 'marketplace_seller', 'seller': 'CepHane Teknoloji',
            'stock': 'InStock', 'condition': 'NewCondition',
            'marketplace': True, 'out_of_stock': False,
        })
        out = selected_rejection_evidence(
            html().replace(b'https://schema.org/InStock',
                           b'https://schema.org/OutOfStock'), READER_URL)
        self.assertEqual(out['category'], 'out_of_stock')
        self.assertTrue(out['out_of_stock'])
        marketplace_out = selected_rejection_evidence(
            html(seller={'name': 'CepHane Teknoloji'}, marketplace='true').replace(
                b'https://schema.org/InStock', b'https://schema.org/OutOfStock'),
            READER_URL)
        self.assertEqual(marketplace_out['category'], 'marketplace_seller')
        self.assertTrue(marketplace_out['out_of_stock'])
        used = selected_rejection_evidence(
            html(condition='RefurbishedCondition'), READER_URL)
        self.assertEqual(used['category'], 'non_new_condition')
        self.assertEqual(selected_rejection_evidence(
            html(seller={'name': 'CepHane'}, marketplace='true'),
            READER_URL + '?tracking=1')['category'], 'unverified_document')

    def test_rejection_observer_reuses_the_strict_readers_existing_pdp_get(self):
        fetch = Fetch({ROBOTS_URL: ROBOTS,
                       READER_URL: html(seller={'name': 'CepHane Teknoloji'},
                                        marketplace='true')})
        rejected = {}
        source = MediaMarktSource(
            get=fetch, sleep=lambda _: None,
            rejected_offer_observer=lambda url, body: rejected.__setitem__(
                url, selected_rejection_evidence(body, url)))
        offer, reason, status = source.read_offer(READER_URL)
        self.assertIsNone(offer)
        self.assertEqual((reason, status), ('no_exact_product_offer', 200))
        self.assertEqual(rejected[READER_URL]['category'], 'marketplace_seller')
        self.assertEqual(fetch.calls, [READER_URL])

    def test_public_catalog_rejects_redirects_and_oversized_payloads(self):
        rows = [product(1, 'Apple iPhone 16 Pro', 'apple-iphone-16-pro', None)]
        raw = json.dumps(rows).encode()
        self.assertEqual(_catalog(lambda *_args, **_kwargs: Response(raw)), rows)
        with self.assertRaisesRegex(ValueError, 'catalog_unavailable_or_redirected'):
            _catalog(lambda *_args, **_kwargs: Response(raw, url='https://example.com/'))
        with self.assertRaisesRegex(ValueError, 'catalog_oversized'):
            _catalog(lambda *_args, **_kwargs: Response(b'x' * (2 * 1024 * 1024 + 1)))

    def test_only_unpriced_sitemap_candidate_is_read_and_strict_offer_verified(self):
        rows = [product(2, 'Apple iPhone 15 Pro', 'apple-iphone-15-pro', 90000),
                product(1, 'Apple iPhone 16 Pro', 'apple-iphone-16-pro', None)]
        source = Source({row['name'] for row in rows})
        offer = {'merchant': 'MediaMarkt', 'price': 79999, 'currency': 'TRY',
                 'availability': 'in_stock',
                 'source_title': 'APPLE iPhone 16 Pro 128 GB Akıllı Telefon Siyah',
                 'url': URL, 'observed_at': datetime.now(timezone.utc).isoformat()}
        refresher = Refresher(offer)
        report = run_audit(catalog_get=lambda *_args, **_kwargs:
                           Response(json.dumps(rows).encode()),
                           source=source, refresher=refresher)
        self.assertEqual(report['status'], 'completed')
        self.assertEqual((report['catalog_products'], report['priced_products'],
                          report['sitemap_matched_priced'],
                          report['sitemap_matched_unpriced'],
                          report['verified_unpriced_products']), (2, 1, 1, 1, 1))
        self.assertEqual([call[0] for call in refresher.calls], ['Apple iPhone 16 Pro'])
        self.assertEqual(report['products'][0]['verified_offers'][0]['price'], 79999)
        self.assertFalse(report['writes'])

    def test_candidate_limit_fails_before_any_pdp_read(self):
        rows = [product(i, f'Apple iPhone {i} Pro', f'apple-iphone-{i}-pro', None)
                for i in range(1, MAX_AUDIT_MODELS + 2)]
        refresher = Refresher()
        report = run_audit(catalog_get=lambda *_args, **_kwargs:
                           Response(json.dumps(rows).encode()),
                           source=Source({row['name'] for row in rows}),
                           refresher=refresher)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['error'], 'candidate_budget_exceeded')
        self.assertEqual(refresher.calls, [])


if __name__ == '__main__':
    unittest.main()
