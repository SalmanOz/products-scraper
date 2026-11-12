import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from comparison_accessory_candidates import candidate_targets, monitor_codes, stage_category


N11 = 'https://www.n11.com/urun/asus-proart-pa278cv-75hz-monitor-53683691?magaza=n11'
TRENDYOL = 'https://www.trendyol.com/asus/proart-pa278cv-75hz-monitor-p-1223819436'


class ComparisonAccessoryTests(unittest.TestCase):
    def test_shared_model_is_scheduled_on_two_hosts_when_oldest_urls_differ(self):
        other_n11 = N11.replace('pa278cv', 'pa279cv').replace('53683691', '53683692')
        other_trendyol = TRENDYOL.replace('pa278cv', 'pa279cv').replace('1223819436', '1223819437')
        state = {'products': {
            'first': {'category': 'monitor', 'title': 'ASUS PA278CV', 'retailerLeads': [N11, TRENDYOL]},
            'second': {'category': 'monitor', 'title': 'ASUS PA279CV', 'retailerLeads': [other_n11, other_trendyol]},
        }, 'accessoryLeadAudits': {
            f'monitor/{other_n11}': '2026-11-12T06:00:00Z',
            f'monitor/{TRENDYOL}': '2026-11-12T05:00:00Z',
        }}
        diagnostics = {}
        groups = candidate_targets(state, ['monitor'], 1, diagnostics)
        self.assertEqual(dict(groups), {'www.n11.com': [('monitor', N11, 'pa278cv')],
                                        'www.trendyol.com': [('monitor', TRENDYOL, 'pa278cv')]})
        self.assertEqual(diagnostics['monitor'], {'modelGroupsQueued': 1, 'modelGroupsQueuedOnTwoHosts': 1})
        groups = candidate_targets(state, ['monitor'], 1, existing_codes={'monitor': {'pa278cv'}})
        self.assertEqual(dict(groups), {'www.n11.com': [('monitor', other_n11, 'pa279cv')],
                                        'www.trendyol.com': [('monitor', other_trendyol, 'pa279cv')]})

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

    def test_monitor_discovery_keeps_msi_variant_suffix(self):
        n11 = 'https://www.n11.com/urun/msi-pro-mp242-e14a-144hz-monitor-127487692'
        trendyol = 'https://www.trendyol.com/msi/pro-mp242-e14a-144hz-monitor-p-1144662158'
        state = {'products': {'a': {'category': 'monitor', 'title': 'MSI PRO MP242 E14A 144 Hz',
                                    'retailerLeads': [n11, trendyol]}}}
        self.assertEqual(dict(candidate_targets(state, ['monitor'], 5)), {
            'www.n11.com': [('monitor', n11, 'mp242e14a')],
            'www.trendyol.com': [('monitor', trendyol, 'mp242e14a')],
        })

    def test_monitor_discovery_keeps_full_msi_mag_code(self):
        for title in ('MSI MAG 274QPF X30MV 27 inç 300Hz', 'MSI MAG-274QPF-X30MV Monitör'):
            self.assertEqual(monitor_codes(title), {'274qpfx30mv'})
        self.assertEqual(monitor_codes('MSI MAG 274QPF Monitör'), {'274qpf'})

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
