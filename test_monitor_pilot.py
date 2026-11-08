from unittest import TestCase
from unittest.mock import patch

from monitor_pilot import independent_pair, materialize
from monitor_sources import monitor_facts


class MonitorPilotTests(TestCase):
    def setUp(self):
        self.facts = monitor_facts('Samsung LS32FM700UUXUF 32" IPS 4K 60Hz Monitör',
                                   'Samsung', 'ls32fm700uuxuf')
        self.first = {
            'code': 'ls32fm700uuxuf', 'gtin13': '8806097322344', 'facts': self.facts,
            'platform': 'n11', 'merchant': 'Satıcı A', '_legal_name': 'satici a limited sirketi',
            '_tax_key': 'same-company', 'price': 10000, '_image': 'https://example.com/monitor.png',
            'observed_at': '2026-11-08T21:00:00+00:00', 'url': 'https://www.n11.com/monitor',
        }
        self.second = {
            **self.first, 'platform': 'Trendyol', 'merchant': 'Satıcı B', '_legal_name': 'satici b',
            '_tax_key': 'other-company',
        }

    def test_pair_requires_a_distinct_platform_and_company(self):
        self.assertTrue(independent_pair(self.first, self.second))
        for patch_fields in ({'platform': 'n11'}, {'_tax_key': 'same-company'},
                             {'_legal_name': 'satici a anonim sirketi'}):
            self.assertFalse(independent_pair(self.first, {**self.second, **patch_fields}))

    def test_pair_rejects_conflicting_variant_facts_and_barcode(self):
        self.assertFalse(independent_pair(self.first, {**self.second, 'gtin13': '8806097931393'}))
        self.assertFalse(independent_pair(self.first, {**self.second, 'facts': {**self.facts, 'refresh': 120}}))

    def test_manufacturer_spec_has_precedence_for_the_exact_gtin(self):
        with patch('monitor_pilot._image', return_value=b'photo'):
            model, _ = materialize((self.first,), [self.first])
        panel = next(spec for spec in model['specs'] if spec['label'] == 'Panel tipi')
        self.assertEqual(model['panel'], 'VA')
        self.assertEqual(panel['source']['name'], 'Samsung')
        self.assertEqual(model['offers'][0]['price'], self.first['price'])
