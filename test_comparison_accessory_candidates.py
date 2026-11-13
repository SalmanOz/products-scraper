import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from comparison_accessory_candidates import candidate_targets, monitor_codes, search_accessory_retailer, stage_category


N11 = 'https://www.n11.com/urun/asus-proart-pa278cv-75hz-monitor-53683691?magaza=n11'
TRENDYOL = 'https://www.trendyol.com/asus/proart-pa278cv-75hz-monitor-p-1223819436'


class ComparisonAccessoryTests(unittest.TestCase):
    def test_native_search_pairs_comparison_candidates_without_any_retailer_urls(self):
        hb = 'https://www.hepsiburada.com/asus-proart-pa278cv-monitor-p-HBCV00000ABC1'
        state = {'products': {
            'first': {'category': 'monitor', 'title': 'ASUS PA278CV Monitor', 'source': 'akakce'},
            'second': {'category': 'monitor', 'title': 'ASUS PA278CV 75Hz', 'source': 'epey'},
            'one_source': {'category': 'monitor', 'title': 'ASUS PA279CV', 'source': 'epey'},
            'already_listed': {'category': 'monitor', 'title': 'ASUS PA280CV', 'source': 'cimri',
                               'retailerLeads': [TRENDYOL.replace('pa278cv', 'pa280cv')]},
        }}

        def search(host, models):
            self.assertEqual(models, [('monitor', 'pa278cv')])
            return {models[0]: [hb if host == 'www.hepsiburada.com' else N11]}, [
                {'category': 'monitor', 'code': 'pa278cv', 'httpStatus': 200, 'parser': 'parsed'}]

        with patch('comparison_accessory_candidates.search_accessory_retailer', side_effect=search) as reader:
            kwargs = {'existing_codes': {'monitor': {'pa280cv'}}, 'native_search': 2}
            groups = candidate_targets(state, ['monitor'], 2, **kwargs)
            self.assertEqual(dict(groups), {'www.hepsiburada.com': [('monitor', hb, 'pa278cv')],
                                            'www.n11.com': [('monitor', N11, 'pa278cv')]})
            self.assertEqual(dict(candidate_targets(state, ['monitor'], 2, **kwargs)), dict(groups))
            self.assertEqual(reader.call_count, 2)
        self.assertEqual(set(state['accessoryNativeLeads']['monitor/pa278cv']), {N11, hb})

    def test_native_cards_require_matching_code_in_title_and_original_retailer_url(self):
        with patch('comparison_accessory_candidates.PriorityRetailerTransport') as transport, \
                patch('comparison_accessory_candidates.public_search_cards') as cards:
            transport.return_value.read_n11_search.return_value = ('public document', 'document_read', 200)
            cards.return_value = [
                {'source_url': N11, 'discovery_title': 'ASUS PA278CV Monitor'},
                {'source_url': TRENDYOL, 'discovery_title': 'ASUS PA278CV Monitor'},
                {'source_url': N11.replace('pa278cv', 'pa278qv'), 'discovery_title': 'ASUS PA278CV Monitor'},
                {'source_url': N11, 'discovery_title': 'ASUS PA278QV Monitor'},
            ]
            links, report = search_accessory_retailer('www.n11.com', [('monitor', 'pa278cv')])
        self.assertEqual(links, {('monitor', 'pa278cv'): [N11]})
        self.assertEqual(report[0]['destinations'], 1)

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

    def test_feed_barcode_joins_a_retailer_url_without_the_model_code(self):
        barcode = '4711081273981'
        gaming = 'https://www.gaming.gen.tr/urun/10000/asus-pa278cv-monitor/'
        sinerji = 'https://www.sinerji.gen.tr/asus-monitor-p-52859'
        state = {'products': {
            'gaming': {'category': 'monitor', 'title': 'Asus PA278CV Monitör', 'source': 'www.gaming.gen.tr',
                       'url': gaming, 'retailerLeads': [gaming], 'feedGtin': barcode},
            'sinerji': {'category': 'monitor', 'title': 'Asus PA278CV Monitör', 'source': 'www.sinerji.gen.tr',
                        'url': sinerji, 'retailerLeads': [sinerji], 'feedGtin': barcode},
        }}
        self.assertEqual(dict(candidate_targets(state, ['monitor'], 5, allow_single=True)), {
            'www.gaming.gen.tr': [('monitor', gaming, 'pa278cv', barcode)],
            'www.sinerji.gen.tr': [('monitor', sinerji, 'pa278cv', barcode)],
        })
        self.assertFalse(candidate_targets(state, ['monitor'], 5, allow_single=True,
                                           existing_barcodes={'monitor': {barcode}}))

    def test_feed_signal_stays_bound_to_its_own_store(self):
        bogus = {'category': 'monitor', 'title': 'ASUS PA278CV', 'source': 'www.gaming.gen.tr',
                 'url': N11, 'retailerLeads': [N11], 'feedGtin': '4711081273981'}
        self.assertFalse(candidate_targets({'products': {'bad': bogus}}, ['monitor'], 5, allow_single=True))

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
