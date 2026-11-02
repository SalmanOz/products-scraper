"""Exact GTIN radio provenance, source and one-SKU writer boundary tests."""

import asyncio
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from audit_mediamarkt_s24fe_gtin import run_audit
from mediamarkt_marketplace_audit import selected_marketplace_offer
from mediamarkt_marketplace_source import (
    MediaMarktMarketplaceSource, S24FE_PROFILES, S24FE_URL,
    reviewed_marketplace_offer,
)
from mediamarkt_s24fe_preflight import validated_offer
from retailer_offer_refresh import RetailerOfferRefresher
from reviewed_offer_targets import daily_offer_targets, with_reviewed_radio
from test_mediamarkt_marketplace_audit import (
    URL as OLD_URL, TITLE as OLD_TITLE, SKU as OLD_SKU,
    OFFER as OLD_OFFER, SELLER as OLD_SELLER,
    SELLER_ID as OLD_SELLER_ID, fixture, render,
)
from update_prices import PriceUpdater
from verify_mediamarkt_s24fe_gtin import validate_scoped_refresh


PROFILE = S24FE_PROFILES[S24FE_URL]
EAN = PROFILE['gtin13']
TITLE = 'SAMSUNG Galaxy S24 FE 8 GB 128 GB Akıllı Telefon Mavi'


def selected_fixture():
    raw = json.dumps(fixture(), ensure_ascii=False)
    for old, new in (
        (OLD_URL, S24FE_URL), (OLD_TITLE, TITLE),
        (OLD_SKU, PROFILE['sku']), (OLD_OFFER, '1868775190'),
        (OLD_SELLER_ID, PROFILE['seller_id']),
        (OLD_SELLER, PROFILE['seller_name']),
        ('78899', '41399'), ('78.899', '41.399'),
        ('"value": "12"', '"value": "8"'),
        ('"value": "256"', '"value": "128"'),
    ):
        raw = raw.replace(old, new)
    value = json.loads(raw)
    product = value['action']['object']
    product['gtin13'] = EAN
    product['hasVariant'].append({'@type': 'Product', 'sku': PROFILE['sku'],
                                  'gtin13': EAN})
    return value


def catalog_product():
    return {'id': PROFILE['id'], 'slug': PROFILE['slug'], 'name': PROFILE['name'],
            'attributes': {'ram_gb': 8, 'storage_gb': 128,
                           'Connectivity': {'5G': 'n78 (3500)'}},
            'offer_targets': []}


class Transport:
    def __init__(self, body):
        self.body = body
        self._rules = self
    def _load_robots(self):
        pass
    def can_fetch(self, bot, url):
        return True
    def _direct(self, url, limit, accept):
        return self.body, 'ready', 200
    def report(self):
        return {'robots_status': 'ready'}


class CatalogClient:
    def get_published_products(self):
        return [catalog_product()]


class ExactGtinTests(unittest.TestCase):
    def test_selected_ean_and_manufacturer_review_bind_5g_only_to_one_sku(self):
        parsed = selected_marketplace_offer(render(selected_fixture()), S24FE_URL)
        self.assertEqual((parsed['sku'], parsed['gtin13'], parsed['seller_name'],
                          parsed['price'], parsed['ram_gb'], parsed['storage_gb'],
                          parsed['network_generation']),
                         (PROFILE['sku'], EAN, 'VintageOnline', 41399, 8, 128, None))
        self.assertEqual(parsed['selected_variant_gtin13'], EAN)
        offer = {**parsed, 'merchant': parsed['seller_name'],
                 'observed_at': datetime.now(timezone.utc).isoformat()}
        specs = {'ram_gb': 8, 'storage_gb': 128, 'network_generation': '5g'}
        self.assertTrue(reviewed_marketplace_offer(offer, PROFILE['name'], specs,
                                                   catalog_product()))
        for changed in ({'gtin13': '8806095750156'},
                        {'selected_variant_gtin13': None},
                        {'network_generation': '4g'},
                        {'seller_name': 'Another'}, {'merchant': 'MediaMarkt'},
                        {'storage_gb': 256}, {'condition': 'refurbished'}):
            self.assertFalse(reviewed_marketplace_offer({**offer, **changed},
                                                        PROFILE['name'], specs,
                                                        catalog_product()))
        self.assertFalse(reviewed_marketplace_offer(offer, PROFILE['name'],
                                                    {**specs, 'network_generation': '4g'},
                                                    catalog_product()))

    def test_ean_checksum_and_selected_variant_must_agree(self):
        base = selected_fixture()
        for change in ('bad_check', 'wrong_variant', 'missing_ean'):
            data = copy.deepcopy(base)
            if change == 'bad_check':
                data['action']['object']['gtin13'] = EAN[:-1] + '4'
            elif change == 'wrong_variant':
                data['action']['object']['hasVariant'][-1]['gtin13'] = '8806095750156'
            else:
                data['action']['object'].pop('gtin13')
            self.assertIsNone(selected_marketplace_offer(render(data), S24FE_URL), change)

    def test_source_refresher_and_daily_seed_are_single_exact_seller(self):
        transport = Transport(render(selected_fixture()))
        source = MediaMarktMarketplaceSource(transport=transport)
        offer, reason, status = source.read_offer(S24FE_URL)
        self.assertEqual((reason, status), ('observed_exact_new_marketplace_offer', 200))
        self.assertEqual(offer['merchant'], 'VintageOnline')
        reader = RetailerOfferRefresher(mediamarkt_source=transport,
                                        mediamarkt_marketplace_source=source)
        product = catalog_product()
        specs = with_reviewed_radio(product, PriceUpdater.get_expected_specs(product['attributes']))
        self.assertEqual(specs['network_generation'], '5g')
        self.assertIn(S24FE_URL, [row['source_url'] for row in daily_offer_targets(product, specs)])
        result = reader.refresh_existing_offer_targets(PROFILE['name'],
            [{'source_url': S24FE_URL}], specs)
        self.assertEqual([row['merchant'] for row in result], ['VintageOnline'])
        updater = PriceUpdater(ingestion_client=object(), offer_refresher=reader)
        self.assertTrue(updater.live_offer_is_valid(product, result[0], specs))

    def test_audit_preflight_and_writer_fail_closed_on_drift(self):
        transport = Transport(render(selected_fixture()))
        reader = RetailerOfferRefresher(mediamarkt_source=transport,
            mediamarkt_marketplace_source=MediaMarktMarketplaceSource(transport=transport))
        audit = run_audit(reader=reader, catalog_client=CatalogClient())
        self.assertEqual(audit['status'], 'passed', audit.get('error'))
        now = datetime.fromisoformat(audit['checked_at'].replace('Z', '+00:00'))
        self.assertEqual(list(validated_offer(audit, now)), [S24FE_URL])
        bad = copy.deepcopy(audit)
        bad['radio_evidence']['gtin13'] = '8806095750156'
        self.assertIsNone(validated_offer(bad, now))
        bad = copy.deepcopy(audit)
        bad['rows'][0]['verified_offer']['gtin13'] = '8806095750156'
        self.assertIsNone(validated_offer(bad, now))
        self.assertIsNone(validated_offer(audit, now + timedelta(minutes=6)))
        offer = audit['rows'][0]['verified_offer']
        report = {'status': 'completed', 'mode': 'apply', 'known_pdp_only': True,
                  'products': [{'slug': PROFILE['slug'],
                    'targets': [{'source_url': S24FE_URL}],
                    'source_attempts': [{'source_url': S24FE_URL,
                        'merchant': PROFILE['seller_name'],
                        'reason': 'observed_exact_new_marketplace_offer_from_preflight',
                        'http_status': 200}],
                    'prepared_offers': [{'product_id': PROFILE['id'],
                        'product_slug': PROFILE['slug'], 'seller': PROFILE['seller_name'],
                        'source_url': S24FE_URL, 'price': offer['price'],
                        'currency': 'TRY', 'availability': 'in_stock',
                        'source_title': offer['source_title'],
                        'observed_at': offer['observed_at'],
                        'checked_at': offer['observed_at']}]}],
                  'ingestion': {'committed': True, 'accepted': 1,
                                'stale_ignored': 0, 'variant_rejected': 0},
                  'coverage': {'products': 1, 'with_purchasable_offer': 1}}
        self.assertTrue(validate_scoped_refresh(audit, report)[0])
        report['ingestion']['accepted'] = 0
        self.assertFalse(validate_scoped_refresh(audit, report)[0])

    def test_same_run_writer_uses_preflight_without_retailer_refetch(self):
        transport = Transport(render(selected_fixture()))
        reader = RetailerOfferRefresher(mediamarkt_source=transport,
            mediamarkt_marketplace_source=MediaMarktMarketplaceSource(transport=transport))
        audit = run_audit(reader=reader, catalog_client=CatalogClient())
        self.assertEqual(audit['status'], 'passed', audit.get('error'))
        class Ingestion(CatalogClient):
            submissions = []
            def ingest_offers(self, rows):
                self.submissions.append(copy.deepcopy(rows))
                return {'accepted': len(rows), 'stale_ignored': 0,
                        'variant_rejected': 0, 'variant_rejections': [],
                        'changed_paths': [], 'committed': True}
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'audit.json'
            path.write_text(json.dumps(audit))
            network = Mock(side_effect=AssertionError('no retailer refetch'))
            ingestion = Ingestion()
            with patch.dict(os.environ, {'MEDIAMARKT_S24FE_VERIFIED_AUDIT_FILE': str(path)}):
                updater = PriceUpdater(ingestion_client=ingestion,
                    offer_refresher=RetailerOfferRefresher(fetch=network))
                asyncio.run(updater.run_update(product_slugs=[PROFILE['slug']],
                                               known_pdp_only=True))
                self.assertTrue(validate_scoped_refresh(audit, updater.last_report)[0])
            self.assertEqual(len(ingestion.submissions), 1)
            self.assertEqual([row['seller'] for row in ingestion.submissions[0]],
                             ['VintageOnline'])
            network.assert_not_called()


if __name__ == '__main__':
    unittest.main()
