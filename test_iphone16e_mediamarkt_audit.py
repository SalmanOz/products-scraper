import unittest

from audit_iphone16e_mediamarkt import PROFILE, run_audit, summary


class FakeReader:
    def __init__(self, offers):
        self.offers = offers
        self.mediamarkt_source = self

    def refresh_existing_offer_targets(self, name, targets, specs):
        assert name == PROFILE['name']
        assert targets == [{'source_url': PROFILE['targets'][0]}]
        assert specs == {key: PROFILE[key] for key in
                         ('ram_gb', 'storage_gb', 'network_generation')}
        return self.offers

    def target_report(self, *_args):
        return [{'reason': 'observed_exact_offer', 'http_status': 200}]

    def report(self):
        return {'robots_status': 'ready'}


class IPhone16eMediaMarktAuditTests(unittest.TestCase):
    def test_only_exact_purchasable_first_party_cash_offer_is_verified(self):
        offer = {'merchant': 'MediaMarkt', 'url': PROFILE['targets'][0],
                 'price': 54999, 'currency': 'TRY', 'availability': 'in_stock',
                 'source_title': 'APPLE iPhone 16e 8/128 GB Akıllı Telefon Beyaz MD1R4TU/A'}
        report = run_audit(FakeReader([offer]))
        self.assertTrue(report['verified'])
        self.assertFalse(report['writes'])
        self.assertEqual(report['offer']['price'], 54999)
        self.assertIn('selected cash TRY 54999', summary(report))
        for changed in ({'merchant': 'Marketplace Seller'},
                        {'url': PROFILE['targets'][0] + '?variant=256'},
                        {'currency': 'USD'}, {'availability': 'out_of_stock'},
                        {'price': 0}):
            with self.subTest(changed=changed):
                self.assertFalse(run_audit(FakeReader([{**offer, **changed}]))['verified'])


if __name__ == '__main__':
    unittest.main()
