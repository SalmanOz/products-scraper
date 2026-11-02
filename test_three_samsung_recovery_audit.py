"""The recovery pilot is read only and cannot promote marketplace or sold-out prices."""

import unittest
from urllib.parse import urlparse

from audit_three_samsung_recovery import PROFILES, S24_MEDIAMARKT_URL, run_audit, summary
from pasaj_source import A26_6_128_URL
from reviewed_offer_targets import daily_offer_targets


class FakeReader:
    def __init__(self, offers):
        self.offers = offers
        self.mediamarkt_source = self
        self.pasaj_source = self

    def refresh_existing_offer_targets(self, name, targets, specs):
        profile = next(p for p in PROFILES if p['name'] == name)
        assert specs == profile['specs']
        assert targets == [{'source_url': url} for url in profile['targets']]
        return self.offers.get(name, [])

    def target_report(self, name, targets, specs):
        return [{'merchant': 'Pasaj' if url == A26_6_128_URL else 'MediaMarkt',
                 'source_url': url, 'reason': 'no_exact_product_offer', 'http_status': 200}
                for url in (row['source_url'] for row in targets)]

    def report(self):
        return {'robots_status': 'ready'}


class ThreeSamsungRecoveryAuditTests(unittest.TestCase):
    def test_only_direct_pasaj_pdp_is_a_daily_seed(self):
        self.assertEqual({p['slug'] for p in PROFILES}, {
            'samsung-galaxy-a37-5g', 'samsung-galaxy-a26-5g', 'samsung-galaxy-s24'})
        for profile in PROFILES:
            self.assertTrue(all(urlparse(url).hostname in
                {'www.mediamarkt.com.tr', 'www.turkcell.com.tr'}
                for url in profile['targets']))
            seeded = {row['source_url'] for row in daily_offer_targets(
                {key: profile[key] for key in ('id', 'slug', 'name')}, profile['specs'])}
            self.assertEqual(set(profile['targets']).intersection(seeded),
                             {A26_6_128_URL} if profile['slug'] == 'samsung-galaxy-a26-5g'
                             else set())

    def test_only_selected_first_party_purchasable_cash_offer_is_reported(self):
        a37 = PROFILES[0]
        a26 = PROFILES[1]
        s24 = PROFILES[2]
        report = run_audit(FakeReader({
            a37['name']: [{'merchant': 'Güzel Teknoloji', 'url': a37['targets'][0],
                           'price': 23749, 'currency': 'TRY', 'availability': 'in_stock'}],
            a26['name']: [{'merchant': 'Pasaj', 'url': A26_6_128_URL,
                           'price': 19499, 'currency': 'TRY', 'availability': 'out_of_stock'}],
            s24['name']: [{'merchant': 'MediaMarkt', 'url': S24_MEDIAMARKT_URL,
                           'price': 39999, 'currency': 'TRY', 'availability': 'in_stock'}],
        }))
        self.assertFalse(report['writes'])
        self.assertEqual(report['verified_products'], 1)
        self.assertEqual([len(row['verified_cash_offers']) for row in report['products']], [0, 0, 1])
        self.assertIn('no verified in-stock first-party cash offer', summary(report))
        direct = run_audit(FakeReader({a26['name']: [
            {'merchant': 'Pasaj', 'url': A26_6_128_URL,
             'price': 19499, 'currency': 'TRY', 'availability': 'in_stock'}]}))
        self.assertEqual(direct['verified_products'], 1)
        self.assertEqual(direct['products'][1]['verified_cash_offers'][0]['merchant'], 'Pasaj')
        for bad in (
            {'merchant': 'Other', 'price': 39999, 'currency': 'TRY', 'availability': 'in_stock'},
            {'merchant': 'MediaMarkt', 'price': 39999, 'currency': 'TRY', 'availability': 'out_of_stock'},
            {'merchant': 'MediaMarkt', 'price': 39999, 'currency': 'USD', 'availability': 'in_stock'},
            {'merchant': 'MediaMarkt', 'price': 0, 'currency': 'TRY', 'availability': 'in_stock'},
        ):
            attempt = run_audit(FakeReader({s24['name']: [{**bad, 'url': S24_MEDIAMARKT_URL}]}))
            self.assertEqual(attempt['verified_products'], 0)


if __name__ == '__main__':
    unittest.main()
