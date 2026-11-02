import json
from unittest.mock import patch
import unittest

from reviewed_offer_targets import (
    APPLE_STORE_BATCH_PATH, MEDIAMARKT_BATCH_PATH, TURK_TELEKOM_BATCH_PATH,
    PASAJ_BATCH_PATH, URL_BATCH, VATAN_BATCH_PATH, S25_FE_256_BATCH_PATH,
    daily_offer_targets, with_reviewed_radio,
)
from pasaj_source import IPHONE16_128_URL, IPHONE17_256_URL


class ReviewedDailyTargetTests(unittest.TestCase):
    def setUp(self):
        self.profiles = json.loads(URL_BATCH.read_text())['products']

    def test_all_eight_current_variants_retain_url_hints_without_manual_prices(self):
        for p in self.profiles:
            product = {key: p[key] for key in ('id', 'slug', 'name')}
            specs = {key: p[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
            targets = daily_offer_targets(product, specs)
            self.assertTrue(targets, p['slug'])
            self.assertTrue(set(p['targets']).issubset({r['source_url'] for r in targets}))
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
            self.assertEqual([call.args[0] for call in read.call_args_list],
                             [URL_BATCH, VATAN_BATCH_PATH, MEDIAMARKT_BATCH_PATH,
                              APPLE_STORE_BATCH_PATH, TURK_TELEKOM_BATCH_PATH,
                              PASAJ_BATCH_PATH,
                              S25_FE_256_BATCH_PATH])

    def test_new_s25_fe_variant_requires_exact_slug_name_memory_and_radio(self):
        profile = json.loads(S25_FE_256_BATCH_PATH.read_text())['products'][0]
        self.assertIsNone(profile['id'])
        product = {'id': 501, 'slug': profile['slug'], 'name': profile['name']}
        specs = {key: profile[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
        targets = daily_offer_targets(product, specs)
        self.assertEqual([row['source_url'] for row in targets], [
            profile['targets'][0], profile['targets'][1], profile['targets'][2],
        ])
        self.assertEqual({row['merchant_name'] for row in targets[:2]},
                         {'MediaMarkt', 'Vatan Bilgisayar'})
        self.assertEqual(with_reviewed_radio(product, {'ram_gb': 8, 'storage_gb': 256}), specs)
        for key, wrong in (('id', 0), ('id', True), ('slug', 'samsung-galaxy-s25-fe'),
                           ('name', 'Samsung Galaxy S25 FE'), ('name', 'Samsung Galaxy S25 FE 512GB')):
            self.assertEqual(daily_offer_targets({**product, key: wrong}, specs), [])
        for key, wrong in (('ram_gb', 6), ('storage_gb', 128), ('network_generation', '4g')):
            self.assertEqual(daily_offer_targets(product, {**specs, key: wrong}), [])

        old_variant = {'id': 234, 'slug': 'samsung-galaxy-s25-fe',
                       'name': 'Samsung Galaxy S25 FE'}
        self.assertEqual(daily_offer_targets(old_variant,
                         {'ram_gb': 8, 'storage_gb': 128, 'network_generation': '5g'}), [])

    def test_same_reviewed_iphone_retains_vatan_and_mediamarkt_targets(self):
        profile = next(p for p in json.loads(VATAN_BATCH_PATH.read_text())['products']
                       if p['slug'] == 'apple-iphone-17')
        product = {key: profile[key] for key in ('id', 'slug', 'name')}
        specs = {key: profile[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
        self.assertEqual({row['merchant_name'] for row in daily_offer_targets(product, specs)},
                         {'Vatan Bilgisayar', 'MediaMarkt', 'Apple Store', 'Pasaj'})

    def test_pasaj_seed_requires_exact_catalog_identity_and_variant(self):
        profiles = json.loads(PASAJ_BATCH_PATH.read_text())['products']
        self.assertEqual({profile['slug']: (profile['id'], profile['ram_gb'],
                                              profile['storage_gb'], profile['targets'])
                          for profile in profiles}, {
            'apple-iphone-17': (235, 8, 256, [IPHONE17_256_URL]),
            'apple-iphone-16': (23, 8, 128, [IPHONE16_128_URL]),
        })

        def pasaj_targets(item, attributes):
            return [row for row in daily_offer_targets(item, attributes)
                    if row['merchant_name'] == 'Pasaj']

        for profile in profiles:
            product = {key: profile[key] for key in ('id', 'slug', 'name')}
            specs = {key: profile[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
            self.assertEqual(pasaj_targets(product, specs),
                             [{'merchant_name': 'Pasaj', 'source_url': profile['targets'][0]}])
            self.assertEqual(with_reviewed_radio(product, {
                'ram_gb': profile['ram_gb'], 'storage_gb': profile['storage_gb'],
            }), specs)
            for key, wrong in (('id', 999), ('slug', 'apple-iphone-17e'),
                               ('name', 'Apple iPhone 17 Pro')):
                self.assertEqual(pasaj_targets({**product, key: wrong}, specs), [])
            for key, wrong in (('ram_gb', 12), ('storage_gb', 512),
                               ('network_generation', '4g')):
                self.assertEqual(pasaj_targets(product, {**specs, key: wrong}), [])

    def test_four_apple_store_seeds_require_exact_catalog_identity(self):
        for profile in json.loads(APPLE_STORE_BATCH_PATH.read_text())['products']:
            product = {key: profile[key] for key in ('id', 'slug', 'name')}
            specs = {key: profile[key] for key in
                     ('ram_gb', 'storage_gb', 'network_generation')}
            urls = [row['source_url'] for row in daily_offer_targets(product, specs)
                    if row['merchant_name'] == 'Apple Store']
            self.assertEqual(urls, profile['targets'])
            for key, wrong in (('id', -1), ('slug', 'wrong'), ('name', 'Other Phone')):
                changed = {**product, key: wrong}
                self.assertFalse(any(row['merchant_name'] == 'Apple Store'
                                     for row in daily_offer_targets(changed, specs)))
            self.assertFalse(any(row['merchant_name'] == 'Apple Store'
                                 for row in daily_offer_targets(
                                     product, {**specs, 'storage_gb': 512})))

    def test_three_runner_verified_turk_telekom_urls_require_exact_catalog_sku(self):
        profiles = json.loads(TURK_TELEKOM_BATCH_PATH.read_text())['products']
        self.assertEqual({p['slug'] for p in profiles}, {
            'samsung-galaxy-a57-5g', 'samsung-galaxy-s26', 'xiaomi-17t',
        })
        for profile in profiles:
            product = {key: profile[key] for key in ('id', 'slug', 'name')}
            specs = {key: profile[key] for key in
                     ('ram_gb', 'storage_gb', 'network_generation')}
            urls = [row['source_url'] for row in daily_offer_targets(product, specs)
                    if row['merchant_name'] == 'Türk Telekom eMağaza']
            self.assertEqual(urls, profile['targets'])
            self.assertEqual(with_reviewed_radio(product, {
                'ram_gb': profile['ram_gb'], 'storage_gb': profile['storage_gb'],
            })['network_generation'], '5g')
            for key, wrong in (('id', -1), ('slug', 'wrong'), ('name', 'Other Phone')):
                self.assertFalse(any(row['merchant_name'] == 'Türk Telekom eMağaza'
                                     for row in daily_offer_targets({**product, key: wrong}, specs)))
            for key, wrong in (('ram_gb', 6), ('storage_gb', 512), ('network_generation', '4g')):
                self.assertFalse(any(row['merchant_name'] == 'Türk Telekom eMağaza'
                                     for row in daily_offer_targets(product, {**specs, key: wrong})))

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
