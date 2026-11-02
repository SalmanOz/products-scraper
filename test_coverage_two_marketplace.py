"""Read-only two-PDP audit boundaries; no ingestion path is exercised."""

import unittest
from unittest.mock import patch

from coverage_two_marketplace import PROFILES, audit


PRICES = (30499.0, 39999.0)


def offers():
    return {
        url: {'platform': 'MediaMarkt', 'seller_name': profile['seller_name'],
              'seller_id': profile['seller_id'], 'sku': profile['sku'],
              'gtin13': profile['gtin13'], 'source_title': profile['source_title'],
              'url': url, 'ram_gb': profile['ram_gb'],
              'storage_gb': profile['storage_gb'],
              'network_generation': profile['network_generation'],
              'offer_id': str(1830000000 + index), 'condition': 'new',
              'availability': 'in_stock', 'currency': 'TRY',
              'price': PRICES[index]}
        for index, (url, profile) in enumerate(PROFILES.items())}


class Client:
    def __init__(self):
        self.rows = [
            {'id': profile['id'], 'slug': profile['slug'], 'name': profile['name'],
             'attributes': {'ram_gb': profile['ram_gb'],
                            'storage_gb': profile['storage_gb']},
             'offer_targets': []}
            for profile in PROFILES.values()]
        self.prices = {profile['id']: (profile['slug'], False)
                       for profile in PROFILES.values()}
        self.writes = []

    def get_published_products(self):
        return self.rows

    def get_public_price_priority(self):
        return self.prices

    def ingest_offers(self, rows):
        self.writes.append(rows)
        raise AssertionError('read-only audit must never ingest')


class Source:
    def __init__(self, status='ready'):
        self._rules = self
        self.status = status
        self.direct = []
        self.browser = []
        self.direct_reason = 'ready'
        self.changed_browser_url = False

    def _load_robots(self):
        pass

    def can_fetch(self, bot, url):
        return self.status == 'ready'

    def _direct(self, url, limit, accept):
        self.direct.append(url)
        if self.direct_reason == 'http_403':
            return None, 'http_403', 403
        return b'<html></html>', 'ready', 200

    def _browser_get(self, url):
        self.browser.append(url)
        final = url + '?changed=1' if self.changed_browser_url else url
        return b'<html></html>', final, 'ready'

    def report(self):
        return {'robots_status': self.status, 'direct_requests': len(self.direct),
                'browser_requests': len(self.browser)}


class CoverageTwoMarketplaceTests(unittest.TestCase):
    def audited(self, client=None, source=None, candidates=None):
        client = client or Client()
        source = source or Source()
        candidates = candidates or offers()
        with patch('coverage_two_marketplace.selected_marketplace_offer',
                   side_effect=lambda body, url: candidates[url]):
            report = audit(client=client, source=source)
        return report, client, source

    def test_exact_two_pdp_reads_and_zero_writes(self):
        report, client, source = self.audited()
        self.assertEqual(report['status'], 'passed', report.get('error'))
        self.assertEqual(source.direct, list(PROFILES))
        self.assertEqual(source.browser, [])
        self.assertEqual(client.writes, [])
        self.assertIs(report['writes'], False)
        self.assertEqual({row['verified_offer']['sku'] for row in report['rows']},
                         {profile['sku'] for profile in PROFILES.values()})

    def test_selected_identity_seller_cash_and_stock_drift_fail_closed(self):
        url = list(PROFILES)[1]
        for field, value in (('gtin13', '6941812757995'),
                             ('seller_id', '999'),
                             ('source_title', 'POCO X6 Pro 12 GB 256 GB'),
                             ('ram_gb', 12), ('availability', 'out_of_stock'),
                             ('condition', 'refurbished'), ('price', -1),
                             ('price', True)):
            with self.subTest(field=field, value=value):
                candidates = offers()
                candidates[url][field] = value
                report, client, source = self.audited(candidates=candidates)
                self.assertEqual(report['status'], 'failed')
                self.assertIsNone(report['rows'][-1]['verified_offer'])
                self.assertEqual(source.direct, list(PROFILES))
                self.assertEqual(client.writes, [])

    def test_priced_catalog_stops_before_pdp_but_collision_still_reads_both(self):
        client = Client()
        first_url = list(PROFILES)[0]
        first = PROFILES[first_url]
        client.prices[first['id']] = (first['slug'], True)
        report, _, source = self.audited(client=client)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(source.direct, [])
        client.prices[first['id']] = (first['slug'], False)
        client.rows[0]['offer_targets'] = [
            {'merchant_name': first['seller_name'],
             'source_url': 'https://www.hepsiburada.com/other'}]
        report, _, source = self.audited(client=client)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(source.direct, list(PROFILES))
        self.assertTrue(all(row['verified_offer'] for row in report['rows']))
        collision = report['catalog_collisions'][first['slug']]
        self.assertEqual(collision['target_count'], 1)
        self.assertEqual(collision['targets'][0]['source_url'],
                         'https://www.hepsiburada.com/other')
        self.assertIn('seller_name_on_other_url_or_platform', collision['reasons'])
        self.assertEqual(client.writes, [])

    def test_full_private_target_page_is_reported_after_both_live_reads(self):
        client = Client()
        first = list(PROFILES.values())[0]
        client.rows[0]['offer_targets'] = [
            {'merchant_name': 'MediaMarkt',
             'source_url': f'https://www.mediamarkt.com.tr/tr/product/_other-{i}.html'}
            for i in range(12)]
        report, client, source = self.audited(client=client)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(source.direct, list(PROFILES))
        self.assertTrue(all(row['verified_offer'] for row in report['rows']))
        collision = report['catalog_collisions'][first['slug']]
        self.assertEqual(collision['target_count'], 12)
        self.assertEqual(len(collision['targets']), 12)
        self.assertEqual(collision['reasons'], ['target_page_full'])
        self.assertEqual(client.writes, [])

    def test_robots_and_changed_browser_url_never_verify_offer(self):
        report, client, source = self.audited(source=Source('unavailable'))
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(source.direct, [])
        self.assertEqual(client.writes, [])
        source = Source()
        source.direct_reason = 'http_403'
        source.changed_browser_url = True
        report, client, source = self.audited(source=source)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(source.direct, list(PROFILES))
        self.assertEqual(source.browser, list(PROFILES))
        self.assertEqual(client.writes, [])

    def test_exact_403_browser_retry_stays_within_two_urls(self):
        source = Source()
        source.direct_reason = 'http_403'
        report, client, source = self.audited(source=source)
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(source.direct, list(PROFILES))
        self.assertEqual(source.browser, list(PROFILES))
        self.assertEqual(client.writes, [])


if __name__ == '__main__':
    unittest.main()
