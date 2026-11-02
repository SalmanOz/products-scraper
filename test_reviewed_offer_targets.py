import json
from unittest.mock import patch
import unittest

from reviewed_offer_targets import URL_BATCH, daily_offer_targets, with_reviewed_radio


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

    def test_missing_radio_uses_only_the_exact_reviewed_sku_profile(self):
        for profile in self.profiles:
            product = {key: profile[key] for key in ('id', 'slug', 'name')}
            memory = {key: profile[key] for key in ('ram_gb', 'storage_gb')}
            specs = with_reviewed_radio(product, memory)
            self.assertEqual(specs['network_generation'], profile['network_generation'])
            self.assertEqual(memory, {key: profile[key] for key in ('ram_gb', 'storage_gb')})
            self.assertTrue(daily_offer_targets(product, specs))
            self.assertNotIn('network_generation', with_reviewed_radio(
                {**product, 'id': 999}, memory))
            self.assertNotIn('network_generation', with_reviewed_radio(
                product, {**memory, 'storage_gb': 512 if memory['storage_gb'] != 512 else 256}))
            opposite = '4g' if profile['network_generation'] == '5g' else '5g'
            self.assertEqual(with_reviewed_radio(product, {**memory, 'network_generation': opposite})[
                'network_generation'], opposite)
            self.assertEqual(daily_offer_targets(product, {**memory, 'network_generation': opposite}), [])

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
