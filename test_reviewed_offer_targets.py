import json
from unittest.mock import patch
import unittest

from reviewed_offer_targets import URL_BATCH, daily_offer_targets


class ReviewedDailyTargetTests(unittest.TestCase):
    def setUp(self):
        self.profiles = json.loads(URL_BATCH.read_text())['products']

    def test_all_eight_current_variants_retain_url_hints_without_manual_prices(self):
        for p in self.profiles:
            product = {key: p[key] for key in ('id', 'slug', 'name')}
            specs = {key: p[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
            targets = daily_offer_targets(product, specs)
            self.assertTrue(targets, p['slug'])
            self.assertEqual({r['source_url'] for r in targets}, set(p['targets']))
            for row in targets:
                self.assertEqual(set(row), {'merchant_name', 'source_url'})

    def test_current_sku_drift_never_reuses_reviewed_seeds(self):
        p = self.profiles[0]
        product = {key: p[key] for key in ('id', 'slug', 'name')}
        specs = {key: p[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
        for key, value in [('ram_gb', 12), ('storage_gb', 128), ('network_generation', '4g')]:
            self.assertEqual(daily_offer_targets(product, {**specs, key: value}), [])
        self.assertEqual(daily_offer_targets({**product, 'id': 999}, specs), [])

    def test_only_url_batch_is_opened_never_browser_price_observations(self):
        p = self.profiles[0]
        product = {key: p[key] for key in ('id', 'slug', 'name')}
        specs = {key: p[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
        raw = URL_BATCH.read_text()
        with patch('pathlib.Path.read_text', autospec=True, return_value=raw) as read:
            daily_offer_targets(product, specs)
            self.assertEqual([call.args[0] for call in read.call_args_list], [URL_BATCH])

    def test_distinct_merchants_get_priority_and_unsafe_targets_are_not_forwarded(self):
        product = {'id': 999, 'slug': 'other-phone', 'name': 'Other Phone', 'offer_targets': [
            {'source_url': f'https://www.idefix.com/color-{i}'} for i in range(20)
        ] + [{'source_url': 'https://www.pttavm.com/phone'},
             {'source_url': 'https://www.idefix.com/a?token=do-not-fetch'},
             {'source_url': 'https://evil.test/phone'}]}
        targets = daily_offer_targets(product, {})
        self.assertEqual([r['merchant_name'] for r in targets[:2]], ['idefix', 'PttAVM'])
        self.assertEqual(len(targets), 12)
        self.assertFalse(any('token' in r['source_url'] or 'evil.test' in r['source_url'] for r in targets))


if __name__ == '__main__':
    unittest.main()
