"""Offline two-retailer Fold8 audit and exact single-SKU write contracts."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from audit_fold8_ultra_two_merchants import run_audit
from fold8_ultra_preflight import PROFILE, URLS, validated_offers
from retailer_offer_refresh import RetailerOfferRefresher
from tr_price_scraper import TRPriceScraper
from update_prices import PriceUpdater
from verify_fold8_ultra_scoped_refresh import validate_scoped_refresh


class CatalogResponse:
    def __init__(self, product):
        self.url = 'https://teknoskor.com/api/products?limit=200'
        self.status_code = 200
        self.body = json.dumps([product]).encode()
        self.headers = {'Content-Type': 'application/json',
                        'Content-Length': str(len(self.body))}

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]

    def close(self):
        pass


def catalog_product():
    return {'id': PROFILE[0], 'slug': PROFILE[1], 'name': PROFILE[2],
            'attributes': {'ram_gb': PROFILE[3], 'storage_gb': PROFILE[4]}}


def offers():
    now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    titles = ('SAMSUNG GALAXY Z FOLD8 ULTRA 12/256GB GKREM',
              'Samsung Galaxy Z Fold8 Ultra 12GB 256GB')
    return [{
        'merchant': merchant, 'price': 151999.0, 'currency': 'TRY',
        'availability': 'in_stock', 'source_title': title,
        'url': url, 'observed_at': now,
    } for (url, merchant), title in zip(URLS.items(), titles)]


class SourceStatus:
    def __init__(self, tt=False):
        self.tt = tt

    def report(self):
        return ({'status': 'ready', 'robots_status': 'allowed', 'pdp_requests': 1}
                if self.tt else {'robots_status': 'allowed', 'pdp_requests': 1})

    def discover(self, name, specs):
        return [{'source_url': next(url for url, merchant in URLS.items()
                               if merchant == 'Türk Telekom eMağaza')}] if self.tt else []


class Reader:
    def __init__(self, rows=None):
        self.rows = offers() if rows is None else rows
        self.calls = []
        self.turk_telekom_source = SourceStatus(tt=True)
        self.pasaj_source = SourceStatus()

    def refresh_existing_offer_targets(self, name, targets, specs):
        self.calls.append((name, targets, specs))
        return deepcopy(self.rows)

    def target_report(self, name, targets, specs):
        return [{'source_url': url, 'merchant': merchant,
                 'reason': 'observed_exact_cash_offer', 'http_status': 200}
                for url, merchant in URLS.items()]


class Ingestion:
    def __init__(self):
        self.submissions = []

    def get_published_products(self):
        return [catalog_product()]

    def ingest_offers(self, rows):
        self.submissions.append(deepcopy(rows))
        return {'accepted': len(rows), 'stale_ignored': 0, 'variant_rejected': 0,
                'variant_rejections': [], 'changed_paths': [], 'committed': True}


class Fold8PreflightTests(unittest.TestCase):
    def audit(self):
        product = catalog_product()
        report = run_audit(Reader(), lambda *_args, **_kwargs: CatalogResponse(product))
        self.assertEqual((report['status'], len(report['verified_offers'])), ('passed', 2))
        self.assertFalse(report['writes'])
        return report

    def preflight_file(self, report):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'audit.json'
        path.write_text(json.dumps(report))
        return path

    def test_both_public_offers_and_exact_catalog_are_required(self):
        audit = self.audit()
        self.assertEqual(set(validated_offers(audit)), set(URLS))
        for mutate in (
            lambda a: a['product'].update(id=294),
            lambda a: a['verified_offers'][0].update(price=0),
            lambda a: a['verified_offers'][0].update(source_title='Samsung Galaxy Z Fold8 Ultra 12/512GB'),
            lambda a: a['verified_offers'][1].update(merchant='Marketplace'),
            lambda a: a['source_attempts'][1].update(reason='no_selected_cash_offer'),
            lambda a: a.update(checked_at=(datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()),
        ):
            changed = deepcopy(audit)
            mutate(changed)
            with self.subTest(change=changed):
                self.assertIsNone(validated_offers(changed))

    def test_bare_unit_pair_accepts_one_physical_variant_only(self):
        matcher = TRPriceScraper()
        specs = {'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g'}
        url = next(url for url, seller in URLS.items() if seller == 'Pasaj')
        def evidence(title):
            return matcher.has_observed_offer_evidence(
                PROFILE[2], {'source_title': title, 'url': url}, specs)
        self.assertTrue(evidence('Samsung Galaxy Z Fold8 Ultra 12GB 256GB'))
        for title in ('Samsung Galaxy Z Fold8 Ultra 16GB 256GB',
                      'Samsung Galaxy Z Fold8 Ultra 12GB 512GB',
                      'Samsung Galaxy Z Fold8 Ultra 12GB 256GB veya 512GB',
                      'Samsung Galaxy Z Fold8 Ultra 12GB 256GB 16GB RAM',
                      'Samsung Galaxy Z Fold8 Ultra 12GB 256GB + 12GB virtual RAM'):
            with self.subTest(title=title):
                self.assertFalse(evidence(title))

    def test_changed_catalog_capacity_stops_before_retailer_reads(self):
        product = catalog_product()
        product['attributes']['storage_gb'] = 512
        reader = Reader()
        report = run_audit(reader, lambda *_args, **_kwargs: CatalogResponse(product))
        self.assertEqual(report['catalog_reason'], 'not_checked')
        self.assertEqual(report['error'], 'catalog_identity_or_variant_changed')
        self.assertEqual(reader.calls, [])

    def test_same_run_handoff_uses_two_observed_offers_without_retailer_network(self):
        audit = self.audit()
        path = self.preflight_file(audit)
        network = Mock(side_effect=AssertionError('no retailer refetch'))
        with patch.dict(os.environ, {'FOLD8_ULTRA_VERIFIED_AUDIT_FILE': str(path)}):
            refresher = RetailerOfferRefresher(fetch=network)
            rows = refresher.refresh_existing_offer_targets(
                PROFILE[2], [{'source_url': url} for url in URLS],
                {'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g'})
            attempts = refresher.target_report(PROFILE[2],
                [{'source_url': url} for url in URLS],
                {'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g'})
        self.assertEqual({row['url'] for row in rows}, set(URLS))
        self.assertEqual({row['observed_at'] for row in rows},
                         {row['observed_at'] for row in audit['verified_offers']})
        self.assertTrue(all(row['reason'] == 'observed_exact_cash_offer_from_preflight'
                            for row in attempts))
        network.assert_not_called()

    def test_writer_prepares_exactly_two_and_requires_accepted_two(self):
        audit = self.audit()
        path = self.preflight_file(audit)
        ingestion = Ingestion()
        network = Mock(side_effect=AssertionError('no retailer refetch'))
        with patch.dict(os.environ, {'FOLD8_ULTRA_VERIFIED_AUDIT_FILE': str(path)}):
            refresher = RetailerOfferRefresher(fetch=network)
            updater = PriceUpdater(ingestion_client=ingestion, offer_refresher=refresher)
            asyncio.run(updater.run_update(
                product_slugs=[PROFILE[1]], known_pdp_only=True))
            report = updater.last_report
        self.assertEqual(len(ingestion.submissions), 1)
        self.assertEqual(len(ingestion.submissions[0]), 2)
        self.assertEqual({row['seller'] for row in ingestion.submissions[0]}, set(URLS.values()))
        self.assertEqual({row['observed_at'] for row in ingestion.submissions[0]},
                         {row['observed_at'] for row in audit['verified_offers']})
        self.assertEqual(validate_scoped_refresh(audit, report),
                         (True, 'both exact first-party offers accepted for one Fold8 SKU'))
        network.assert_not_called()
        changed = deepcopy(report)
        changed['ingestion']['accepted'] = 1
        changed['ingestion']['stale_ignored'] = 1
        self.assertFalse(validate_scoped_refresh(audit, changed)[0])
        changed = deepcopy(report)
        changed['products'][0]['prepared_offers'][1]['seller'] = 'Marketplace'
        self.assertFalse(validate_scoped_refresh(audit, changed)[0])

    def test_writer_scope_and_stale_handoff_fail_before_ingestion(self):
        audit = self.audit()
        path = self.preflight_file(audit)
        ingestion = Ingestion()
        with patch.dict(os.environ, {'FOLD8_ULTRA_VERIFIED_AUDIT_FILE': str(path)}):
            updater = PriceUpdater(ingestion_client=ingestion)
            with self.assertRaisesRegex(RuntimeError, 'exact reviewed SKU'):
                asyncio.run(updater.run_update(product_slugs=['xiaomi-17t'], known_pdp_only=True))
        audit['checked_at'] = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        path.write_text(json.dumps(audit))
        with patch.dict(os.environ, {'FOLD8_ULTRA_VERIFIED_AUDIT_FILE': str(path)}):
            updater = PriceUpdater(ingestion_client=ingestion)
            with self.assertRaisesRegex(RuntimeError, 'invalid or stale'):
                asyncio.run(updater.run_update(product_slugs=[PROFILE[1]], known_pdp_only=True))
        self.assertEqual(ingestion.submissions, [])

    def test_partial_server_acceptance_fails_the_scoped_writer(self):
        audit = self.audit()
        path = self.preflight_file(audit)
        class PartialIngestion(Ingestion):
            def ingest_offers(self, rows):
                self.submissions.append(deepcopy(rows))
                return {'accepted': 1, 'stale_ignored': 1, 'variant_rejected': 0,
                        'variant_rejections': [], 'changed_paths': [], 'committed': True}
        ingestion = PartialIngestion()
        with patch.dict(os.environ, {'FOLD8_ULTRA_VERIFIED_AUDIT_FILE': str(path)}):
            updater = PriceUpdater(ingestion_client=ingestion,
                offer_refresher=RetailerOfferRefresher(
                    fetch=Mock(side_effect=AssertionError('no retailer refetch'))))
            with self.assertRaisesRegex(RuntimeError, 'did not accept both exact offers'):
                asyncio.run(updater.run_update(product_slugs=[PROFILE[1]], known_pdp_only=True))
        self.assertEqual(len(ingestion.submissions[0]), 2)
        self.assertEqual(updater.last_report['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
