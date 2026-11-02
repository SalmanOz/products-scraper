"""Offline runner-audit reporting checks."""

import unittest

from audit_s25_fe_vatan import NAME, SPECS, URL, run_audit, summary


class Reader:
    def __init__(self, offer=None):
        self.offer = offer
        self.vatan_reviewed_source = self

    def refresh_existing_offer_targets(self, name, targets, specs):
        assert name == NAME and specs == SPECS and targets == [{'source_url': URL}]
        return [self.offer] if self.offer else []

    def target_report(self, *_args):
        return [{'merchant': 'Vatan Bilgisayar', 'source_url': URL,
                 'reason': 'observed_exact_offer' if self.offer else 'access_denied',
                 'http_status': 200 if self.offer else 403}]

    def report(self):
        return {'robots_status': 'allowed'}


class VatanAuditTests(unittest.TestCase):
    def test_only_exact_live_variant_is_verified(self):
        offer = {'merchant': 'Vatan Bilgisayar', 'url': URL, 'price': 38949,
                 'currency': 'TRY', 'availability': 'in_stock',
                 'source_title': 'Samsung Galaxy S25 FE 8/256 GB Buz Mavisi'}
        report = run_audit(Reader(offer))
        self.assertTrue(report['verified'])
        self.assertFalse(report['writes'])
        self.assertIn('exact cash TRY 38949', summary(report))
        for bad in ({**offer, 'url': URL.replace('256', '128')},
                    {**offer, 'availability': 'out_of_stock'},
                    {**offer, 'merchant': 'Other'}):
            self.assertFalse(run_audit(Reader(bad))['verified'])
        self.assertFalse(run_audit(Reader())['verified'])


if __name__ == '__main__':
    unittest.main()
