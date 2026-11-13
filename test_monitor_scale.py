from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from monitor_scale import expand_plan, make_plan, stage_catalog, verify_plan
from monitor_scale_discovery import match_cards


class MonitorScaleTests(TestCase):
    n11 = 'www.n11.com'
    trendyol = 'www.trendyol.com'
    first = {'gtin': '8806097322344', 'code': 'ls32fm700uuxuf'}
    second = {'gtin': '4711081273981', 'code': 'mb166c'}

    def seeds(self):
        return [
            {**self.first, 'url': f'https://{self.n11}/urun/samsung-ls32fm700uuxuf-102483666?magaza=mxshop'},
            {**self.first, 'url': f'https://{self.trendyol}/samsung/ls32fm700uuxuf-p-991494581'},
            {**self.second, 'url': f'https://{self.n11}/urun/asus-mb166c-3778457?magaza=goldsist'},
        ]

    def test_all_models_receive_a_budget_slot_before_second_stores(self):
        seeds = self.seeds()
        archived = {
            'gtin13': self.second['gtin'], 'source_title': 'ASUS MB166C Monitör',
            'platform': 'n11', 'url': seeds[2]['url'], 'price': 1, '_legal_name': 'Private evidence',
        }
        plan = make_plan(seeds, [archived], [], model_count=2, store_budget=1)
        selected = {model['gtin']: model['targets'] for model in plan['candidates']}
        self.assertEqual(selected, {self.first['gtin']: [seeds[1]], self.second['gtin']: [seeds[2]]})
        self.assertEqual(plan['discovery']['rejected'], {'duplicate_url': 1})

    def test_verify_keeps_source_stops_out_of_rejections(self):
        seeds = self.seeds()
        plan = make_plan(seeds, [], [], model_count=2, store_budget=2)

        def read(host, rows, *, retry_transient, audit):
            if host == self.n11:
                audit['attempted'] = 1
                audit['outcomes'] = {rows[0]['url']: 'http_403', rows[1]['url']: 'not_attempted_source_stopped'}
                return [], {'http_403': 1}
            audit['attempted'] = 1
            audit['outcomes'] = {rows[0]['url']: 'verified'}
            return [{'gtin13': rows[0]['gtin'], 'code': rows[0]['code'], 'price': 14000}], {}

        with patch('monitor_scale.read_targets', side_effect=read):
            report, artifact = verify_plan(plan)
        self.assertEqual(report['pdp_planned'], 3)
        self.assertEqual(report['pdp_attempted'], 2)
        self.assertEqual(report['pdp_not_attempted'], 1)
        self.assertEqual(report['rejected'], {'http_403': 1})
        self.assertEqual(report['offer_coverage'], .5)
        self.assertEqual(report['pair_coverage'], 0)
        self.assertEqual(len(artifact['groups']), 1)

    def test_expansion_keeps_cohort_and_prioritizes_new_platforms(self):
        base = make_plan(self.seeds(), [], [], model_count=2, store_budget=3)
        second_trendyol = {**self.second, 'url': f'https://{self.trendyol}/asus/mb166c-p-981444581'}
        alternate_n11 = {**self.second, 'url': f'https://{self.n11}/urun/asus-mb166c-3778458'}
        outside = {'gtin': '4711081951506', 'code': 'pa279crv',
                   'url': f'https://{self.n11}/urun/asus-pa279crv-3778459'}
        plan = expand_plan(base, [alternate_n11, second_trendyol, outside], {'models': [], 'stores': {}},
                           store_budget=3)
        self.assertEqual([row['gtin'] for row in plan['candidates']], [row['gtin'] for row in base['candidates']])
        second = next(row for row in plan['candidates'] if row['gtin'] == self.second['gtin'])
        self.assertEqual(second['available_platforms'], 2)
        self.assertIn(second_trendyol, second['targets'][:2])

    def test_card_titles_emit_only_exact_unambiguous_price_free_hints(self):
        url = f'https://{self.trendyol}/asus/tasinabilir-monitor-p-981444581'
        self.assertEqual(match_cards([self.second], [(url, 'ASUS MB166C Monitör 6999 TL')]),
                         [{**self.second, 'url': url}])
        self.assertEqual(match_cards([self.second], [(url, 'ASUS MB166C2 Monitör')]), [])
        self.assertEqual(match_cards([self.second], [(url, 'ASUS MB166C Monitör askı aparatı')]), [])
        self.assertEqual(match_cards([self.second, {**self.second, 'gtin': self.first['gtin']}],
                                     [(url, 'ASUS MB166C Monitör')]), [])

    def test_discovery_keeps_two_viewsonic_models_with_the_same_base_code_separate(self):
        url = f'https://{self.trendyol}/viewsonic/monitor-p-32608779'
        models = [{'code': 'vx3276', 'gtin': gtin} for gtin in ('0766907002959', '0766907002966')]
        self.assertEqual(match_cards(models, [(url, 'ViewSonic VX3276-4K-MHD Monitör')]),
                         [{**models[1], 'url': url}])
        self.assertEqual(match_cards(models, [(url, 'ViewSonic VX3276 Monitör')]), [])

    def test_stage_checks_age_and_pair_gate_before_creating_a_bundle(self):
        now = datetime(2026, 11, 9, 14, tzinfo=timezone.utc)
        artifact = {'model_count': 2, 'checked_at': now.isoformat(), 'groups': [[{
            'gtin13': self.first['gtin'], 'code': self.first['code'],
            'observed_at': (now - timedelta(hours=2)).isoformat(),
        }]]}
        with TemporaryDirectory() as work, patch('monitor_scale.export_verified_pilot') as export:
            destination = Path(work) / 'staged'
            decision = stage_catalog(artifact, destination, now)
            self.assertEqual(decision['status'], 'blocked')
            self.assertEqual(decision['eligible_models'], 0)
            self.assertEqual(decision['required_pairs'], 2)
            self.assertFalse(destination.exists())
            export.assert_not_called()

    def test_stage_exports_a_complete_local_bundle_from_verified_offers(self):
        now = datetime(2026, 11, 9, 14, tzinfo=timezone.utc)
        seeds = self.seeds()
        offers = [{
            'gtin13': self.first['gtin'], 'code': self.first['code'], 'url': row['url'],
            'observed_at': now.isoformat(), 'price': 14000 + index,
            'platform': platform, 'merchant': merchant, '_legal_name': merchant.lower(),
            'facts': {'brand': 'Samsung', 'size': 32, 'resolution': '3840 × 2160',
                      'refresh': 60, 'panel': 'VA'},
            '_image': 'https://cdn.dsmcdn.com/monitor.jpg',
        } for index, (row, platform, merchant) in enumerate(zip(seeds, ('n11', 'Trendyol'), ('Seller A', 'Seller B')))]
        artifact = {'model_count': 1, 'checked_at': now.isoformat(), 'groups': [offers]}
        with TemporaryDirectory() as work, patch('monitor_pilot._image', return_value=b'photo'):
            destination = Path(work) / 'staged'
            decision = stage_catalog(artifact, destination, now)
            self.assertEqual(decision['status'], 'staged')
            model, = json.loads((destination / 'monitorPilot.json').read_text())['models']
            self.assertEqual(len(model['offers']), 2)
            self.assertEqual((destination / 'images' / (model['slug'] + '.webp')).read_bytes(), b'photo')
