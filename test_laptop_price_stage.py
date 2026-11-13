from datetime import datetime, timedelta, timezone
import unittest

from laptop_price_stage import stage, targets_for_catalog


class LaptopPriceStageTest(unittest.TestCase):
    def test_updates_only_same_configuration_on_independent_platforms(self):
        model = {'slug': 'lenovo-82xx00fktx', 'mpn': '82XX00FKTX', 'gtin': '0198156932587',
                 'brand': 'Lenovo', 'cpu': 'AMD Ryzen 5 5625U', 'ram': 16, 'ssd': 512,
                 'discrete': None, 'os': 'Windows 11', 'screen': '14',
                 'offers': [{'url': 'old', 'observed_at': 'original'}]}
        catalog = {'checkedAt': 'old', 'models': [model]}
        key = (model['mpn'], model['gtin'])
        base = {'gtin13': key[1], 'facts': model, 'price': 40000, 'observed_at': 'current'}
        first = {**base, 'url': 'first', 'platform': 'n11', 'merchant': 'Birinci', '_legal_name': 'birinci'}
        second = {**base, 'url': 'second', 'platform': 'Trendyol', 'merchant': 'İkinci', '_legal_name': 'ikinci'}
        for rejected in ({**second, 'platform': 'n11'}, {**second, '_legal_name': 'birinci'},
                         {**second, 'facts': {**model, 'ssd': 256}}):
            result, report = stage(catalog, {key: [first, rejected]})
            self.assertIs(result, catalog)
            self.assertEqual(report['remaining'], {'second_platform_missing': 1})
        result, report = stage(catalog, {key: [first, second]})
        self.assertEqual(report['staged'][0]['platforms'], ['Trendyol', 'n11'])
        self.assertEqual([offer['url'] for offer in result['models'][0]['offers']], ['first', 'second'])
        self.assertEqual(catalog['models'][0]['offers'][0]['observed_at'], 'original')

    def test_refreshes_a_known_single_store_without_fabricating_a_second_offer(self):
        now = datetime.now(timezone.utc)
        prior = {'platform': 'Gaming.Gen.TR', 'merchant': 'Gaming.Gen.TR', 'url': 'listing',
                 'price': 39000, 'observed_at': (now - timedelta(hours=1)).isoformat()}
        model = {'slug': 'lenovo-82xx00fktx', 'mpn': '82XX00FKTX', 'gtin': '0198156932587',
                 'brand': 'Lenovo', 'cpu': 'AMD Ryzen 5 5625U', 'ram': 16, 'ssd': 512,
                 'discrete': None, 'os': 'Windows 11', 'screen': '14', 'offers': [prior]}
        catalog = {'checkedAt': prior['observed_at'], 'models': [model]}
        key = model['mpn'], model['gtin']
        fresh = {**prior, 'price': 40000, 'facts': model, 'gtin13': model['gtin'],
                 'observed_at': now.isoformat()}
        result, _ = stage(catalog, {key: [fresh]}, allow_single=True)
        self.assertEqual(len(result['models'][0]['offers']), 1)
        self.assertEqual(result['models'][0]['offers'][0]['price'], 40000)
        result, _ = stage(catalog, {key: [{**fresh, 'merchant': 'Unexpected'}]}, allow_single=True)
        self.assertIs(result, catalog)

    def test_refuses_a_retained_url_owned_by_another_configuration(self):
        first = {'mpn': '82XX00FKTX', 'gtin': '0198156932587', 'offers': [{'url': 'listing'}]}
        second = {'mpn': 'MHWT4TU/A', 'gtin': '0195951080444', 'offers': []}
        catalog = {'models': [first, second], 'discoveryTargets': [
            {'mpn': second['mpn'], 'gtin': second['gtin'], 'url': 'listing'}]}
        with self.assertRaisesRegex(ValueError, 'multiple configurations'):
            targets_for_catalog(catalog)
