import json
import asyncio
import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from audit_xiaomi_17t_first_party import REVIEWED, run_audit
from mediamarkt_source import MediaMarktSource
from test_xiaomi_17t_first_party_audit import Reader, catalog_get
from verify_xiaomi_17t_scoped_refresh import validate_scoped_refresh
from xiaomi_17t_preflight import PROFILES, validated_offers
from update_prices import PriceUpdater


class XiaomiPreflightHandoffTests(unittest.TestCase):
    def audit(self):
        report = run_audit(Reader(), catalog_get)
        self.assertEqual(report['verified_products'], 2)
        self.assertEqual(set(PROFILES), {row[6] for row in REVIEWED})
        return report

    def source(self, audit):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'xiaomi-audit.json'
        path.write_text(json.dumps(audit))
        forbidden_network = Mock(side_effect=AssertionError('preflight must not refetch'))
        return MediaMarktSource(get=forbidden_network, post=forbidden_network,
                               preflight_file=path, now=lambda: datetime.now(timezone.utc)), forbidden_network

    def test_both_exact_offers_reuse_same_run_observation_without_network(self):
        audit = self.audit()
        source, network = self.source(audit)
        for row in audit['products']:
            url = row['source_url']
            offer, reason, status = source.read_offer(url)
            self.assertEqual((reason, status), ('observed_exact_offer_from_preflight', 200))
            self.assertEqual(offer, row['verified_offer'])
        network.assert_not_called()

    def test_stale_or_tampered_audit_fails_closed_without_network(self):
        baseline = self.audit()
        old = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        cases = []
        for change in (
            lambda r: r.update(checked_at=old),
            lambda r: r['products'][0]['verified_offer'].update(observed_at=old),
            lambda r: r['products'][0]['verified_offer'].update(merchant='Başaran Ticaret'),
            lambda r: r['products'][0]['verified_offer'].update(price=0),
            lambda r: r['products'][0]['verified_offer'].update(url=REVIEWED[1][6]),
            lambda r: r['products'][0]['verified_offer'].update(availability='out_of_stock'),
            lambda r: r['products'][0]['verified_offer'].update(source_title='Xiaomi 17T Pro 12/512GB Siyah'),
            lambda r: r['products'][0].update(id=271),
            lambda r: r['products'][0].update(catalog_reason='catalog_identity_or_variant_changed'),
            lambda r: r['products'][0]['source_attempts'][0].update(http_status=500),
            lambda r: r['mediamarkt_source'].update(robots_status='unavailable'),
            lambda r: r['products'].pop(),
        ):
            case = deepcopy(baseline)
            change(case)
            cases.append(case)
        for case in cases:
            with self.subTest(case=case):
                source, network = self.source(case)
                self.assertEqual(source.read_offer(REVIEWED[0][6]),
                                 (None, 'preflight_invalid_or_stale', None))
                network.assert_not_called()

    def test_scoped_gate_requires_two_prepared_and_accepted_media_offers(self):
        audit = self.audit()
        self.assertEqual(len(validated_offers(audit)), 2)
        products = []
        for row in audit['products']:
            offer = row['verified_offer']
            products.append({'slug': row['slug'],
                             'source_attempts': [{'source_url': row['source_url'],
                                                  'reason': 'observed_exact_offer_from_preflight',
                                                  'http_status': 200}],
                             'prepared_offers': [{
                                 'product_id': row['id'], 'product_slug': row['slug'],
                                 'seller': 'MediaMarkt', 'price': offer['price'],
                                 'currency': 'TRY', 'availability': offer['availability'],
                                 'source_title': offer['source_title'],
                                 'source_url': offer['url'],
                                 'observed_at': offer['observed_at'],
                                 'checked_at': offer['observed_at'],
                             }]})
        writer = {'status': 'completed', 'mode': 'apply', 'known_pdp_only': True,
                  'products': products,
                  'ingestion': {'committed': True, 'accepted': 2,
                                'stale_ignored': 0, 'variant_rejected': 0}}
        self.assertEqual(validate_scoped_refresh(audit, writer),
                         (True, 'both exact MediaMarkt offers committed'))
        cases = []
        for change in (
            lambda r: r['products'][0]['source_attempts'][0].update(reason='http_500'),
            lambda r: r['products'][0]['prepared_offers'][0].update(price=1),
            lambda r: r['products'][1]['prepared_offers'].clear(),
            lambda r: r['ingestion'].update(accepted=1, stale_ignored=1),
            lambda r: r['ingestion'].update(variant_rejected=1),
            lambda r: r['ingestion'].update(committed=False),
        ):
            case = deepcopy(writer)
            change(case)
            cases.append(case)
        for case in cases:
            with self.subTest(case=case):
                self.assertFalse(validate_scoped_refresh(audit, case)[0])

    def test_scoped_handoff_reads_only_two_pinned_media_urls(self):
        class Catalog:
            def get_published_products(self):
                return [{'id': p[0], 'slug': p[1], 'name': p[2],
                         'attributes': {'ram_gb': p[3], 'storage_gb': p[4],
                                        'Connectivity': {'5G': 'n1 (2100)'}}}
                        for p in REVIEWED]

        class Refresher:
            def __init__(self):
                self.calls = []

            def refresh_existing_offer_targets(self, name, targets, specs):
                self.calls.append((name, targets, specs))
                return []

        async def no_delay(_seconds):
            return None

        refresher = Refresher()
        updater = PriceUpdater(ingestion_client=Catalog(), offer_refresher=refresher)
        with patch.dict(os.environ, {'XIAOMI_17T_VERIFIED_AUDIT_FILE': '/private/tmp/reviewed.json'}), \
             patch('update_prices.asyncio.sleep', new=no_delay):
            asyncio.run(updater.run_update(dry_run=True, known_pdp_only=True,
                                           product_slugs=['xiaomi-17t-pro', 'xiaomi-17t']))
            with self.assertRaisesRegex(RuntimeError, 'two reviewed slugs'):
                asyncio.run(updater.run_update(dry_run=True, known_pdp_only=True,
                                               product_slugs=['xiaomi-17t-pro']))
        self.assertEqual({targets[0]['source_url'] for _name, targets, _specs in refresher.calls},
                         set(PROFILES))
        self.assertTrue(all(len(targets) == 1 for _name, targets, _specs in refresher.calls))


if __name__ == '__main__':
    unittest.main()
