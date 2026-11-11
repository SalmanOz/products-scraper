import unittest
from unittest.mock import patch

from tws_refresh import merge_snapshot
from tws_stage_updates import stage


MODEL = 'Huawei FreeBuds SE 2'
BLACK = '6942103122101'
BLUE = '6942103104046'
TY = 'https://www.trendyol.com/huawei/freebuds-se-2-p-770699647'
N11 = 'https://www.n11.com/urun/huawei-freebuds-se-2-43816686?magaza=gelalbe&renk=mavi'
HB = 'https://www.hepsiburada.com/huawei-freebuds-se-2-p-HBCV00007I7MLV'


def read(host, targets, *, document_reader=None):
    results = []
    for _, target in targets:
        results.append({**target, 'platform': host, '_legal_name': host, 'price': 1300.0,
                        'observed_at': 'new', 'source_title': f'{MODEL} Mavi'})
    return {MODEL: results}, {}


class TwsStageUpdatesTest(unittest.TestCase):
    def setUp(self):
        self.catalog = {'models': [{'name': MODEL}]}
        self.snapshot = {'checked_at': 'before', 'models': [{'name': MODEL, 'gtin': BLACK, 'offers': [
            {'url': HB, 'merchant': 'Hepsiburada', 'price': 1400.0, 'observed_at': 'before'},
        ]}], 'sourceTargets': {MODEL: [{'url': HB, 'merchant': 'Hepsiburada'}]}}

    def test_prices_come_from_live_rereads_and_extend_the_existing_variant(self):
        report = {'verified': [{'name': MODEL, 'observations': [
            {'url': TY, 'merchant': 'Trendyol', 'gtin13': BLACK, 'price': 1.0},
        ]}]}
        with patch('tws_stage_updates.reread', side_effect=read):
            updated, result = stage(self.snapshot, self.catalog, report)
        self.assertEqual(len(updated['models']), 1)
        self.assertEqual([o['price'] for o in updated['models'][0]['offers']], [1300.0, 1300.0])
        self.assertEqual([r['gtin'] for r in result['staged']], [BLACK])
        self.assertEqual(updated['checked_at'], 'new')

    def test_new_barcode_creates_its_own_variant_and_refresh_targets(self):
        report = {'verified': [{'name': MODEL, 'observations': [
            {'url': TY, 'merchant': 'Trendyol', 'gtin13': BLUE},
            {'url': N11, 'merchant': 'gelalbe', 'gtin13': BLUE},
        ]}]}
        with patch('tws_stage_updates.reread', side_effect=read):
            updated, result = stage(self.snapshot, self.catalog, report)
        self.assertEqual(updated['models'][0], self.snapshot['models'][0])
        self.assertEqual(updated['models'][1]['variant'], 'Mavi')
        self.assertEqual(set(updated['sourceTargets']), {f'{MODEL}/{BLACK}', f'{MODEL}/{BLUE}'})
        self.assertEqual([r['gtin'] for r in result['staged']], [BLUE])

    def test_four_verified_retailers_survive_staging_and_daily_refresh(self):
        vatan = 'https://www.vatanbilgisayar.com/huawei-freebuds-se-2-kulaklik-siyah.html'
        report = {'verified': [{'name': MODEL, 'observations': [
            {'url': url, 'merchant': merchant, 'gtin13': BLACK}
            for url, merchant in ((TY, 'Trendyol'), (N11, 'gelalbe'), (vatan, 'Vatan Bilgisayar'))
        ]}]}
        self.catalog['models'][0]['requiresIndependentPair'] = True
        all_offers = []

        def collect(host, targets, *, document_reader=None):
            result, failures = read(host, targets)
            all_offers.extend(result[MODEL])
            return result, failures

        with patch('tws_stage_updates.reread', side_effect=collect):
            updated, _ = stage(self.snapshot, self.catalog, report)
        self.assertEqual(len(updated['models'][0]['offers']), 4)
        refreshed, _ = merge_snapshot(updated, self.catalog, {MODEL: all_offers}, {}, 'later')
        self.assertEqual(len(refreshed['models'][0]['offers']), 4)

    def test_distinct_legal_sellers_can_share_a_marketplace(self):
        other_n11 = N11.replace('gelalbe', 'distinct')
        self.snapshot['models'][0]['offers'] = [{'url': N11, 'merchant': 'gelalbe'}]
        self.snapshot['sourceTargets'][MODEL] = [{'url': N11, 'merchant': 'gelalbe'}]
        report = {'verified': [{'name': MODEL, 'observations': [
            {'url': other_n11, 'merchant': 'distinct', 'gtin13': BLACK},
        ]}]}

        def collect(host, targets, *, document_reader=None):
            offers, failures = read(host, targets)
            for offer in offers[MODEL]:
                offer['_legal_name'] = offer['merchant']
                offer['merchant_id'] = offer['merchant']
            return offers, failures

        with patch('tws_stage_updates.reread', side_effect=collect):
            updated, _ = stage(self.snapshot, self.catalog, report)
        self.assertEqual(len(updated['models'][0]['offers']), 2)

    def test_shared_barcode_or_stale_second_seller_cannot_supply_a_pair(self):
        report = {'verified': [{'name': MODEL, 'observations': [
            {'url': TY, 'merchant': 'Trendyol', 'gtin13': BLACK},
        ]}]}
        with patch('tws_stage_updates.reread', return_value=({}, {})):
            updated, result = stage(self.snapshot, self.catalog, report)
        self.assertIs(updated, self.snapshot)
        self.assertEqual(result['rejections'], {'independent_pair_missing': 1})
        self.snapshot['models'].append({'name': 'Other Model', 'gtin': BLACK, 'offers': []})
        with patch('tws_stage_updates.reread') as fetch:
            updated, result = stage(self.snapshot, self.catalog, report)
        self.assertIs(updated, self.snapshot)
        fetch.assert_not_called()
        self.assertEqual(result['rejections'], {'conflicting_barcode_owner': 1})
