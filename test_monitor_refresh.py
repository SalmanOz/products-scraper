from datetime import datetime, timedelta, timezone
from unittest import TestCase

from monitor_refresh import matches_model, refresh, targets_for_catalog


class MonitorRefreshTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 8, 20, tzinfo=timezone.utc)
        self.facts = {'brand': 'ASUS', 'size': 15.6, 'resolution': '1920 × 1080',
                      'refresh': 60, 'panel': 'IPS'}
        self.n11 = {'platform': 'n11', 'merchant': 'Firma A', 'price': 6000,
                    'url': 'https://www.n11.com/urun/mb166c-11111?magaza=firma-a',
                    'observed_at': (self.now - timedelta(hours=1)).isoformat(), 'basket': False}
        self.vatan = {**self.n11, 'platform': 'Vatan Bilgisayar', 'merchant': 'Vatan Bilgisayar',
                      'url': 'https://www.vatanbilgisayar.com/asus-mb166c.html', 'price': 7000}
        self.model = {'gtin': '4711081273981', 'slug': 'asus-mb166c', **self.facts,
                      'offers': [self.n11, self.vatan]}
        self.observation = {**self.n11, 'gtin13': self.model['gtin'], 'code': 'mb166c',
                            'facts': self.facts, 'price': 5900, '_legal_name': 'firma a',
                            'observed_at': self.now.isoformat()}

    def test_retains_missing_source_timestamp_only_while_current(self):
        groups = {('mb166c', self.model['gtin']): [self.observation]}
        updated, fresh, pairs = refresh({'models': [self.model]}, groups, self.now)
        self.assertEqual((fresh, pairs), (1, 0))
        self.assertEqual(updated['models'][0]['offers'][0]['price'], 5900)
        self.assertEqual(updated['models'][0]['offers'][1], self.vatan)
        updated, _, _ = refresh({'models': [self.model]}, groups, self.now + timedelta(hours=73))
        self.assertEqual(len(updated['models'][0]['offers']), 1)

    def test_does_not_combine_new_seller_with_historical_observation(self):
        observation = {**self.observation, 'merchant': 'Firma B'}
        updated, _, _ = refresh({'models': [self.model]}, {('mb166c', self.model['gtin']): [observation]}, self.now)
        self.assertEqual([o['merchant'] for o in updated['models'][0]['offers']], ['Firma B'])

    def test_rejects_other_barcode_and_incompatible_panel(self):
        self.assertFalse(matches_model({**self.observation, 'gtin13': '6977295259083'}, self.model))
        self.assertFalse(matches_model({**self.observation, 'facts': {**self.facts, 'panel': 'VA'}}, self.model))

    def test_automatically_discovered_targets_survive_even_without_published_prices(self):
        model = {**self.model, 'mpn': 'MB166C', 'offers': []}
        first = {'gtin': model['gtin'], 'code': 'mb166c', 'url': self.n11['url']}
        second = {**first, 'url': self.vatan['url']}
        snapshot = {'models': [model], 'discoveryTargets': [first, second]}
        self.assertEqual(targets_for_catalog(snapshot, [first]), [first, second])
        with self.assertRaisesRegex(ValueError, 'does not identify'):
            targets_for_catalog({**snapshot, 'discoveryTargets': [{**first, 'code': 'other'}]}, [])
