"""Offline gates for the separate, read-only selected Vivo PDP runner."""

from copy import deepcopy
from datetime import datetime, timezone
import unittest

from audit_vatan_vivo_exact_readonly import (PRODUCT, SELECTED_H1,
                                             VIVO_X300_VATAN_PDP, run_audit)


NOW = datetime(2026, 11, 2, 16, 45, tzinfo=timezone.utc)


class Reader:
    def __init__(self, offer=None, reason='observed_exact_offer', robots='ready'):
        self.offer = offer if offer is not None else {
            'merchant': 'Vatan Bilgisayar', 'source_title': SELECTED_H1,
            'url': VIVO_X300_VATAN_PDP, 'currency': 'TRY',
            'availability': 'in_stock', 'price': 75999,
            'selected_vatan_variant_verified': True,
            'observed_at': '2026-11-02T16:44:00Z'}
        self.reason = reason
        self.robots = robots
        self.vatan_reviewed_source = self

    def refresh_existing_offer_targets(self, name, targets, specs):
        assert name == PRODUCT['name']
        assert targets == [{'source_url': VIVO_X300_VATAN_PDP}]
        assert specs == {'ram_gb': 16, 'storage_gb': 512,
                         'network_generation': '5g'}
        return [self.offer] if self.offer else []

    def target_report(self, *_args):
        return [{'source_url': VIVO_X300_VATAN_PDP,
                 'merchant': 'Vatan Bilgisayar',
                 'reason': self.reason, 'http_status': 200}]

    def report(self):
        return {'robots_status': self.robots}


class Catalog:
    def __init__(self, product=None):
        self.product = product if product is not None else {
            'id': 286, 'slug': 'vivo-x300', 'name': 'vivo X300',
            'attributes': {'ram_gb': 16, 'storage_gb': 512,
                           'Connectivity': {'5G': True}}}

    def get_published_products(self):
        return [self.product]


class VivoExactReadonlyTests(unittest.TestCase):
    def test_exact_selected_offer_is_reported_without_writes(self):
        report = run_audit(Reader(), Catalog(), NOW)
        self.assertEqual(report['status'], 'passed')
        self.assertTrue(report['catalog_confirmed'])
        self.assertTrue(report['attempt_confirmed'])
        self.assertTrue(report['offer_confirmed'])
        self.assertTrue(report['offer']['selected_vatan_variant_verified'])
        self.assertFalse(report['writes'])

    def test_live_observation_is_checked_after_the_pdp_read(self):
        row = {**Reader().offer,
               'observed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}
        self.assertEqual(run_audit(Reader(row), Catalog())['status'], 'passed')

    def test_same_run_failure_stale_offer_and_catalog_drift_fail_closed(self):
        for changes in ({'merchant': 'Marketplace'}, {'source_title': 'Vivo X300 8/256GB'},
                        {'selected_vatan_variant_verified': False},
                        {'url': VIVO_X300_VATAN_PDP + '?seller=other'},
                        {'availability': 'out_of_stock'},
                        {'observed_at': '2026-11-02T16:39:59Z'},
                        {'observed_at': '2026-11-02T16:46:00Z'}):
            with self.subTest(changes=changes):
                row = {**Reader().offer, **changes}
                self.assertEqual(run_audit(Reader(row), Catalog(), NOW)['status'], 'partial')
        self.assertEqual(run_audit(Reader(reason='no_exact_product_offer'),
                                   Catalog(), NOW)['status'], 'partial')
        self.assertEqual(run_audit(Reader(robots='unavailable'), Catalog(), NOW)['status'],
                         'partial')
        for changes in ({'slug': 'other'}, {'name': 'vivo X300 Pro'},
                        {'attributes': {'ram_gb': 8, 'storage_gb': 512,
                                        'Connectivity': {'5G': True}}},
                        {'attributes': {'ram_gb': 16, 'storage_gb': 512}}):
            with self.subTest(changes=changes):
                product = {**Catalog().product, **deepcopy(changes)}
                self.assertEqual(run_audit(Reader(), Catalog(product), NOW)['status'],
                                 'partial')


if __name__ == '__main__':
    unittest.main()
