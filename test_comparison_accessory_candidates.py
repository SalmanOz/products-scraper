import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from comparison_accessory_candidates import candidate_targets, stage_category


N11 = 'https://www.n11.com/urun/asus-proart-pa278cv-75hz-monitor-53683691?magaza=n11'
TRENDYOL = 'https://www.trendyol.com/asus/proart-pa278cv-75hz-monitor-p-1223819436'


class ComparisonAccessoryTests(unittest.TestCase):
    def test_monitor_joins_the_same_model_code_across_comparison_pages(self):
        first = {'category': 'monitor', 'title': 'ASUS PA278CV 75Hz DisplayPort Monitör', 'retailerLeads': [N11]}
        second = {'category': 'monitor', 'title': 'ASUS PA278CV Monitör', 'retailerLeads': [TRENDYOL]}
        state = {'products': {'akakce': first, 'cimri': second}}
        groups = candidate_targets(state, ['monitor'], 5)
        self.assertEqual(groups['www.n11.com'], [('monitor', N11, 'pa278cv')])
        self.assertEqual(groups['www.trendyol.com'], [('monitor', TRENDYOL, 'pa278cv')])
        second['title'] = 'ASUS PA278QV Monitör'
        self.assertFalse(candidate_targets(state, ['monitor'], 5))
        second['title'] = 'ASUS PA278CV Monitör'
        second['retailerLeads'] = [N11.replace('magaza=n11', 'magaza=other')]
        self.assertFalse(candidate_targets(state, ['monitor'], 5))

    def test_only_supported_original_products_enter_the_queue(self):
        state = {'products': {'a': {'category': 'monitor', 'title': 'ASUS PA278CV', 'retailerLeads': [
            N11, TRENDYOL, N11.replace('magaza=n11', 'token=secret'), 'https://other.example/pa278cv',
        ]}}}
        self.assertEqual(dict(candidate_targets(state, ['monitor'], 5)), {
            'www.n11.com': [('monitor', N11, 'pa278cv')],
            'www.trendyol.com': [('monitor', TRENDYOL, 'pa278cv')],
        })

    def test_single_platform_or_existing_barcode_cannot_stage_an_addition(self):
        barcode = '4711081273981'
        other = '0198155418945'
        catalog = {'models': [{'mpn': 'EXIST123', 'gtin': barcode, 'slug': 'known'}]}
        observations = {('different', barcode): [{'platform': 'Trendyol'}, {'platform': 'n11'}],
                        ('another', other): [{'platform': 'n11'}, {'platform': 'n11'}]}
        with TemporaryDirectory() as tmp, patch('comparison_accessory_candidates.materialize_candidate') as build:
            rows, rejected = stage_category('monitor', catalog, observations, Path(tmp), 6)
        self.assertEqual(rows, [])
        self.assertEqual(rejected, {'already_in_catalog': 1, 'same_barcode_second_platform_missing': 1})
        build.assert_not_called()
