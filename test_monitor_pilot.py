from unittest import TestCase
from unittest.mock import patch

from monitor_pilot import independent_pair, materialize, read_targets
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

    def test_verified_monitor_category_can_supply_a_truncated_title(self):
        title = 'ASUS VG249QM5A 23.8 inç 240Hz Fast IPS Gaming Monitö'
        self.assertIsNone(monitor_facts(title, 'ASUS', 'vg249qm5a'))
        facts = monitor_facts(title, 'ASUS', 'vg249qm5a', category_verified=True)
        self.assertEqual(facts['refresh'], 240)
        self.assertIsNone(monitor_facts(title + ' askı aparatı', 'ASUS', 'vg249qm5a', category_verified=True))

    def test_redirected_product_is_skipped_but_denial_stops_source(self):
        host = 'www.mediamarkt.com.tr'
        rows = [{'url': f'https://{host}/tr/product/_monitor-{sku}.html',
                 'gtin': self.first['gtin13'], 'code': self.first['code']} for sku in (123456, 123457)]
        for reason, expected_reads in [('redirect', 2), ('http_403', 1)]:
            with self.subTest(reason=reason), patch('monitor_pilot.Reader') as factory:
                reader = factory.return_value
                reader.stop = False

                def unavailable(url):
                    reader.last_reason = reason
                    reader.stop = True
                    return None, None

                reader.read.side_effect = unavailable
                offers, rejected = read_targets(host, rows)
                self.assertEqual(offers, [])
                self.assertEqual(rejected, {reason: expected_reads})
                self.assertEqual(reader.read.call_count, expected_reads)

    def test_transport_failure_retries_only_the_unfinished_rows_once(self):
        host = 'www.mediamarkt.com.tr'
        rows = [{'url': f'https://{host}/tr/product/_monitor-{sku}.html',
                 'gtin': self.first['gtin13'], 'code': self.first['code']} for sku in (123456, 123457)]
        with patch('monitor_pilot.Reader') as factory, patch('monitor_pilot.sleep') as wait:
            reader = factory.return_value

            def fail(url):
                reader.last_reason = 'transport_error'
                reader.stop = True
                return None, None

            reader.read.side_effect = fail
            factory.side_effect = lambda *args, **kwargs: self.reset_reader(reader)
            _, rejected = read_targets(host, rows)
            self.assertEqual([call.args[0] for call in reader.read.call_args_list], [rows[0]['url']] * 2)
            self.assertEqual(rejected, {'transport_error': 2})
            wait.assert_called_once_with(8)

    @staticmethod
    def reset_reader(reader):
        reader.stop = False
        return reader

    def test_manufacturer_spec_has_precedence_for_the_exact_gtin(self):
        with patch('monitor_pilot._image', return_value=b'photo'):
            model, _ = materialize((self.first,), [self.first])
        panel = next(spec for spec in model['specs'] if spec['label'] == 'Panel tipi')
        self.assertEqual(model['panel'], 'VA')
        self.assertEqual(panel['source']['name'], 'Samsung')
        self.assertEqual(model['offers'][0]['price'], self.first['price'])
