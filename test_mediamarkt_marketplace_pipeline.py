"""Production boundary tests for exact marketplace seller ingestion."""

import asyncio
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from audit_mediamarkt_marketplace_two import run_audit
from mediamarkt_marketplace_source import (MediaMarktMarketplaceSource, PROFILES,
                                           reviewed_marketplace_offer)
from mediamarkt_marketplace_preflight import (no_merchant_collision,
                                              validated_offers)
from retailer_offer_refresh import RetailerOfferRefresher
from reviewed_offer_targets import daily_offer_targets, with_reviewed_radio
from test_mediamarkt_marketplace_audit import URL, fixture, render
from update_prices import PriceUpdater
from verify_mediamarkt_marketplace_two import validate_scoped_refresh


TITLES = {
    'samsung-galaxy-s25-ultra': 'SAMSUNG Galaxy S25 Ultra 12 GB 256 GB Akıllı Telefon Titanyum Mavi',
    'samsung-galaxy-s24-ultra': 'SAMSUNG Galaxy S24 Ultra 5G 12 GB 256 GB Akıllı Telefon Titanyum Mor',
}


def candidate(url, now=None):
    p = PROFILES[url]
    stamp = (now or datetime.now(timezone.utc)).isoformat().replace('+00:00', 'Z')
    return {'platform': 'MediaMarkt', 'merchant': p['seller_name'],
            'seller_name': p['seller_name'], 'seller_id': p['seller_id'],
            'sku': p['sku'], 'offer_id': '1647130524', 'condition': 'new',
            'ram_gb': p['ram_gb'], 'storage_gb': p['storage_gb'],
            'network_generation': '5g' if '-5g-' in url else None,
            'source_title': TITLES[p['slug']], 'url': url, 'price': 78899.0,
            'currency': 'TRY', 'availability': 'in_stock', 'observed_at': stamp}


def catalog_product(url):
    p = PROFILES[url]
    return {'id': p['id'], 'slug': p['slug'], 'name': p['name'],
            'attributes': {'ram_gb': p['ram_gb'], 'storage_gb': p['storage_gb']},
            'offer_targets': []}


class Transport:
    _rules = None
    def __init__(self, body):
        self.body = body
        self._rules = self
        self._robots_status = 'ready'
    def _load_robots(self):
        pass
    def can_fetch(self, bot, url):
        return True
    def _direct(self, url, limit, accept):
        return self.body, 'ready', 200
    def report(self):
        return {'robots_status': self._robots_status}


class Reader:
    def __init__(self):
        self.mediamarkt_source = Transport(b'')
    def refresh_existing_offer_targets(self, name, targets, specs):
        return [candidate(targets[0]['source_url'])]
    def target_report(self, name, targets, specs):
        url = targets[0]['source_url']
        return [{'source_url': url, 'merchant': PROFILES[url]['seller_name'],
                 'reason': 'observed_exact_new_marketplace_offer', 'http_status': 200}]


class CatalogClient:
    def get_published_products(self):
        return [catalog_product(url) for url in PROFILES]


class MarketplacePipelineTests(unittest.TestCase):
    def test_live_reader_returns_actual_seller_not_platform(self):
        source = MediaMarktMarketplaceSource(Transport(render(fixture())))
        offer, reason, status = source.read_offer(URL)
        self.assertEqual((reason, status), ('observed_exact_new_marketplace_offer', 200))
        self.assertEqual(offer['merchant'], 'CepHane Teknoloji')
        self.assertEqual(offer['platform'], 'MediaMarkt')
        profile = PROFILES[URL]
        specs = {k: profile[k] for k in ('ram_gb', 'storage_gb', 'network_generation')}
        self.assertTrue(reviewed_marketplace_offer(offer, profile['name'], specs,
                                                   catalog_product(URL)))
        changed = copy.deepcopy(fixture())
        changed['action']['object']['offers'][0]['seller']['name'] = 'MediaMarkt'
        self.assertEqual(source.read_offer(URL)[0]['merchant'], 'CepHane Teknoloji')
        self.assertIsNone(MediaMarktMarketplaceSource(Transport(render(changed))).read_offer(URL)[0])

    def test_refresher_and_updater_preserve_source_specific_proof(self):
        market = type('Market', (), {'read_offer': lambda _, url:
            (candidate(url), 'observed_exact_new_marketplace_offer', 200)})()
        reader = RetailerOfferRefresher(mediamarkt_marketplace_source=market)
        p = catalog_product(URL)
        specs = {'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g'}
        offers = reader.refresh_existing_offer_targets(p['name'], [{'source_url': URL}], specs)
        self.assertEqual([o['merchant'] for o in offers], ['CepHane Teknoloji'])
        updater = PriceUpdater(ingestion_client=object(), offer_refresher=reader)
        self.assertTrue(updater.live_offer_is_valid(p, offers[0], specs))
        self.assertFalse(updater.live_offer_is_valid({**p, 'id': 999}, offers[0], specs))
        self.assertFalse(updater.live_offer_is_valid(p, {**offers[0], 'merchant': 'MediaMarkt'}, specs))

    def test_collision_guard_rejects_same_seller_on_another_platform(self):
        product = catalog_product(URL)
        self.assertTrue(no_merchant_collision(product, PROFILES[URL], URL))
        product['offer_targets'] = [{'merchant_name': 'CepHane Teknoloji',
                                     'source_url': 'https://www.mediamarkt.com.tr/tr/product/_other.html'}]
        self.assertFalse(no_merchant_collision(product, PROFILES[URL], URL))
        product['offer_targets'][0]['source_url'] = 'https://www.hepsiburada.com/other'
        self.assertFalse(no_merchant_collision(product, PROFILES[URL], URL))
        product['offer_targets'][0]['source_url'] = URL
        self.assertTrue(no_merchant_collision(product, PROFILES[URL], URL))
        product['offer_targets'] = [{'merchant_name': 'Other'}] * 12
        self.assertFalse(no_merchant_collision(product, PROFILES[URL], URL))
        product['offer_targets_truncated'] = False
        self.assertTrue(no_merchant_collision(product, PROFILES[URL], URL))
        product['offer_targets_truncated'] = True
        self.assertFalse(no_merchant_collision(product, PROFILES[URL], URL))

    def test_collision_guard_uses_actual_seller_when_catalog_has_it(self):
        product = catalog_product(URL)
        product['offer_targets_truncated'] = False
        product['offer_targets'] = [{
            'merchant_name': 'MediaMarkt', 'seller_name': 'CepHane Teknoloji',
            'source_url': 'https://www.hepsiburada.com/other'}]
        self.assertFalse(no_merchant_collision(product, PROFILES[URL], URL))
        product['offer_targets'][0]['seller_name'] = 'Başaran Ticaret'
        self.assertTrue(no_merchant_collision(product, PROFILES[URL], URL))
        product['offer_targets'][0]['seller_name'] = None
        self.assertFalse(no_merchant_collision(product, PROFILES[URL], URL))

    def test_daily_refresh_keeps_both_exact_urls_and_seller_drift_fails(self):
        for url, profile in PROFILES.items():
            product = catalog_product(url)
            specs = with_reviewed_radio(product, PriceUpdater.get_expected_specs(product['attributes']))
            self.assertIn(url, [row['source_url'] for row in daily_offer_targets(product, specs)])
            offer = candidate(url)
            self.assertTrue(reviewed_marketplace_offer(offer, profile['name'], specs, product))
            self.assertFalse(reviewed_marketplace_offer(
                {**offer, 'seller_name': 'Another Seller', 'merchant': 'Another Seller'},
                profile['name'], specs, product))

    def test_audit_and_preflight_require_all_two_fresh_exact_offers(self):
        report = run_audit(reader=Reader(), catalog_client=CatalogClient())
        self.assertEqual(report['status'], 'passed')
        now = datetime.fromisoformat(report['checked_at'].replace('Z', '+00:00'))
        self.assertEqual(set(validated_offers(report, now)), set(PROFILES))
        bad = copy.deepcopy(report)
        bad['rows'][0]['verified_offer']['seller_name'] = 'MediaMarkt'
        self.assertIsNone(validated_offers(bad, now))
        stale = now + timedelta(minutes=6)
        self.assertIsNone(validated_offers(report, stale))
        bad = copy.deepcopy(report)
        bad['rows'][0]['merchant_collision'] = True
        self.assertIsNone(validated_offers(bad, now))

    def test_writer_verifier_requires_two_distinct_accepted_sellers(self):
        audit = run_audit(reader=Reader(), catalog_client=CatalogClient())
        products = []
        for url, p in PROFILES.items():
            offer = next(row['verified_offer'] for row in audit['rows'] if row['url'] == url)
            products.append({'slug': p['slug'], 'targets': [{'source_url': url}],
                'source_attempts': [{'source_url': url, 'merchant': p['seller_name'],
                    'reason': 'observed_exact_new_marketplace_offer_from_preflight', 'http_status': 200}],
                'prepared_offers': [{'product_id': p['id'], 'product_slug': p['slug'],
                    'seller': p['seller_name'], 'source_url': url, 'price': offer['price'],
                    'currency': 'TRY', 'availability': 'in_stock',
                    'source_title': offer['source_title'], 'observed_at': offer['observed_at'],
                    'checked_at': offer['observed_at']}]})
        writer = {'status': 'completed', 'mode': 'apply', 'known_pdp_only': True,
                  'products': products, 'ingestion': {'committed': True, 'accepted': 2,
                      'stale_ignored': 0, 'variant_rejected': 0},
                  'coverage': {'products': 2, 'with_purchasable_offer': 2}}
        self.assertTrue(validate_scoped_refresh(audit, writer)[0])
        writer['ingestion']['accepted'] = 1
        self.assertFalse(validate_scoped_refresh(audit, writer)[0])

    def test_same_run_writer_submits_two_real_sellers_without_refetch(self):
        audit = run_audit(reader=Reader(), catalog_client=CatalogClient())
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
            with patch.dict(os.environ, {'MEDIAMARKT_MARKETPLACE_VERIFIED_AUDIT_FILE': str(path)}):
                updater = PriceUpdater(ingestion_client=ingestion,
                    offer_refresher=RetailerOfferRefresher(fetch=network))
                asyncio.run(updater.run_update(product_slugs=[p['slug'] for p in PROFILES.values()],
                                               known_pdp_only=True))
                self.assertTrue(validate_scoped_refresh(audit, updater.last_report)[0])
            self.assertEqual(len(ingestion.submissions), 1)
            self.assertEqual({r['seller'] for r in ingestion.submissions[0]},
                             {p['seller_name'] for p in PROFILES.values()})
            network.assert_not_called()


if __name__ == '__main__':
    unittest.main()
