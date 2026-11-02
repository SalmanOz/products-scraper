"""A37 selected marketplace proof and two-seller writer boundary tests."""

import asyncio
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from audit_mediamarkt_a37_two import run_audit
from mediamarkt_a37_preflight import validated_offers
from mediamarkt_marketplace_audit import selected_marketplace_offer
from mediamarkt_marketplace_source import (
    A37_PROFILES, MediaMarktMarketplaceSource, reviewed_marketplace_offer,
)
from retailer_offer_refresh import RetailerOfferRefresher
from reviewed_offer_targets import daily_offer_targets, with_reviewed_radio
from test_mediamarkt_marketplace_audit import (
    URL as OLD_URL, TITLE as OLD_TITLE, SKU as OLD_SKU,
    OFFER as OLD_OFFER, SELLER as OLD_SELLER,
    SELLER_ID as OLD_SELLER_ID, fixture, render,
)
from update_prices import PriceUpdater
from verify_mediamarkt_a37_two import validate_scoped_refresh


URLS = list(A37_PROFILES)
OFFER_IDS = ['1839996053', '1839175544']
PRICES = [22299, 22500]


def selected_fixture(url):
    index = URLS.index(url)
    profile = A37_PROFILES[url]
    raw = json.dumps(fixture(), ensure_ascii=False)
    for old, new in (
        (OLD_URL, url), (OLD_TITLE, profile['source_title']),
        (OLD_SKU, profile['sku']), (OLD_OFFER, OFFER_IDS[index]),
        (OLD_SELLER_ID, profile['seller_id']),
        (OLD_SELLER, profile['seller_name']),
        ('78899', str(PRICES[index])),
        ('78.899', f'{PRICES[index]:,}'.replace(',', '.')),
        ('"value": "12"', '"value": "6"'),
        ('"value": "256"', '"value": "128"'),
    ):
        raw = raw.replace(old, new)
    value = json.loads(raw)
    product = value['action']['object']
    product['@type'] = 'Product'
    product['gtin13'] = profile['gtin13']
    product.pop('hasVariant', None)
    return value


def catalog_product():
    return {'id': 278, 'slug': 'samsung-galaxy-a37-5g',
            'name': 'Samsung Galaxy A37 5G',
            'attributes': {'ram_gb': 6, 'storage_gb': 128,
                           'Connectivity': {'5G': 'n78 (3500)'}},
            'offer_targets': []}


class Transport:
    def __init__(self):
        self._rules = self
    def _load_robots(self):
        pass
    def can_fetch(self, bot, url):
        return True
    def _direct(self, url, limit, accept):
        return render(selected_fixture(url)), 'ready', 200
    def report(self):
        return {'robots_status': 'ready'}


class CatalogClient:
    def get_published_products(self):
        return [catalog_product()]


def reader():
    transport = Transport()
    return RetailerOfferRefresher(
        mediamarkt_source=transport,
        mediamarkt_marketplace_source=MediaMarktMarketplaceSource(transport=transport))


class A37TwoSellerTests(unittest.TestCase):
    def test_both_exact_gtins_titles_and_selected_sellers_match_one_catalog_variant(self):
        product = catalog_product()
        specs = with_reviewed_radio(product, PriceUpdater.get_expected_specs(product['attributes']))
        self.assertEqual(specs['network_generation'], '5g')
        self.assertTrue(set(URLS).issubset(
            {row['source_url'] for row in daily_offer_targets(product, specs)}))
        for url, price in zip(URLS, PRICES):
            profile = A37_PROFILES[url]
            offer = selected_marketplace_offer(render(selected_fixture(url)), url)
            self.assertIsNotNone(offer)
            self.assertEqual((offer['sku'], offer['gtin13'], offer['source_title'],
                              offer['seller_name'], offer['seller_id'], offer['price'],
                              offer['ram_gb'], offer['storage_gb'], offer['network_generation']),
                             (profile['sku'], profile['gtin13'], profile['source_title'],
                              profile['seller_name'], profile['seller_id'], price, 6, 128, '5g'))
            observed = {**offer, 'merchant': offer['seller_name'],
                        'observed_at': datetime.now(timezone.utc).isoformat()}
            self.assertTrue(reviewed_marketplace_offer(observed, profile['name'], specs, product))
            for drift in ({'gtin13': '8806099054970'},
                          {'source_title': profile['source_title'] + ' 256GB'},
                          {'seller_name': 'Other Seller'}, {'seller_id': '1'},
                          {'price': -1}, {'network_generation': '4g'},
                          {'ram_gb': 8}, {'condition': 'refurbished'}):
                self.assertFalse(reviewed_marketplace_offer({**observed, **drift},
                                                             profile['name'], specs, product))

    def test_selected_parser_rejects_variant_seller_and_stock_drift(self):
        for url in URLS:
            base = selected_fixture(url)
            for kind in ('gtin', 'seller', 'storage', 'stock', 'price'):
                bad = copy.deepcopy(base)
                if kind == 'gtin':
                    bad['action']['object']['gtin13'] = '8806099054970'
                elif kind == 'seller':
                    bad['action']['object']['offers'][0]['seller']['name'] = 'Other Seller'
                elif kind == 'storage':
                    bad['apollo']['GraphqlProductFeature:Storage']['value'] = '256'
                elif kind == 'stock':
                    bad['stock_test'] = 'mms-cofr-delivery_UNAVAILABLE'
                else:
                    bad['apollo']['CofrPriceFeature:base']['price']['amount'] += 1
                self.assertIsNone(selected_marketplace_offer(render(bad), url), (url, kind))

    def test_audit_preflight_and_scoped_writer_reject_drift(self):
        audit = run_audit(reader=reader(), catalog_client=CatalogClient())
        self.assertEqual(audit['status'], 'passed', audit.get('error'))
        self.assertEqual(len(audit['rows']), 2)
        now = datetime.fromisoformat(audit['checked_at'].replace('Z', '+00:00'))
        self.assertEqual(set(validated_offers(audit, now)), set(URLS))
        stale = copy.deepcopy(audit)
        stale['rows'][1]['verified_offer']['seller_name'] = 'Another'
        self.assertIsNone(validated_offers(stale, now))
        self.assertIsNone(validated_offers(audit, now + timedelta(minutes=6)))

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
            with patch.dict(os.environ, {'MEDIAMARKT_A37_VERIFIED_AUDIT_FILE': str(path)}):
                updater = PriceUpdater(ingestion_client=ingestion,
                    offer_refresher=RetailerOfferRefresher(fetch=network))
                asyncio.run(updater.run_update(product_slugs=['samsung-galaxy-a37-5g'],
                                               known_pdp_only=True))
                self.assertTrue(validate_scoped_refresh(audit, updater.last_report)[0])
            self.assertEqual(len(ingestion.submissions), 1)
            self.assertEqual({row['seller'] for row in ingestion.submissions[0]},
                             {'CepHane Teknoloji', 'Birtane Telekom'})
            network.assert_not_called()
        bad_report = copy.deepcopy(updater.last_report)
        bad_report['ingestion']['accepted'] = 1
        self.assertFalse(validate_scoped_refresh(audit, bad_report)[0])


if __name__ == '__main__':
    unittest.main()
