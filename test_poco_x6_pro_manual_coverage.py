"""Offline exact-one write boundary tests; all ingestion is a fake client."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from coverage_two_marketplace import PROFILES
from poco_x6_pro_manual_coverage import (URL, PROFILE, audit, publish,
                                         verified_offer)
from test_coverage_two_marketplace import Client, Source, offers


class RecordingClient(Client):
    def __init__(self):
        super().__init__()
        poco = next(row for row in self.rows if row['id'] == PROFILE['id'])
        poco['attributes']['Connectivity'] = {'5G': True}
        poco['offer_targets_truncated'] = False
        poco['offer_targets'] = [
            {'merchant_name': 'MediaMarkt', 'seller_name': 'Other Seller',
             'source_url': 'https://www.mediamarkt.com.tr/tr/product/_other.html'},
        ]
        self.ack = {'committed': True, 'accepted': 1,
                    'stale_ignored': 0, 'variant_rejected': 0}
        self.readback_seller = PROFILE['seller_name']

    def ingest_offers(self, rows):
        self.writes.append(deepcopy(rows))
        return self.ack

    def get_offer_identity_readiness(self):
        row = self.writes[-1][0]
        return {
            'evidenceVersion': 'product-offer-match-v1',
            'mode': 'shadow_audit_only',
            'auditedAt': datetime.now(timezone.utc).isoformat(),
            'productCoverage': [{
                'slug': PROFILE['slug'],
                'matchedOffers': [{
                    'merchant_name': self.readback_seller,
                    'source_url': URL, 'price': row['price'],
                }],
            }],
        }


class PocoManualCoverageTests(unittest.TestCase):
    def audited(self, client=None, source=None):
        client = client or RecordingClient()
        source = source or Source()
        with patch('coverage_two_marketplace.selected_marketplace_offer',
                   return_value=offers()[URL]):
            report = audit(client=client, source=source)
        return report, client, source

    def test_read_only_audit_rechecks_only_poco_and_one_exact_observation(self):
        report, client, source = self.audited()
        self.assertEqual(report['status'], 'passed', report.get('catalog_error'))
        self.assertEqual(source.direct, [URL])
        self.assertIs(report['writes'], False)
        self.assertEqual(client.writes, [])
        self.assertEqual(report['verified_offer']['gtin13'], PROFILE['gtin13'])

    def test_exact_one_record_requires_accepted_one_and_authenticated_readback(self):
        report, client, _ = self.audited()
        result = publish(report, client=client)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['accepted'], 1)
        self.assertEqual(len(client.writes), 1)
        self.assertEqual(len(client.writes[0]), 1)
        record = client.writes[0][0]
        self.assertEqual((record['product_id'], record['product_slug'],
                          record['seller'], record['source_url'], record['price']),
                         (PROFILE['id'], PROFILE['slug'], PROFILE['seller_name'],
                          URL, 39999.0))

    def test_failed_or_stale_or_tampered_audit_never_writes(self):
        report, client, _ = self.audited()
        changes = [
            lambda data: data.update(status='failed'),
            lambda data: data.update(checked_at=(datetime.now(timezone.utc)
                - timedelta(minutes=6)).isoformat()),
            lambda data: data['verified_offer'].update(gtin13='6941812757995'),
            lambda data: data['catalog_collision'].update(offer_targets_truncated=True),
            lambda data: data['source_attempt'].update(http_status=403),
        ]
        for change in changes:
            with self.subTest(change=change):
                candidate = deepcopy(report)
                change(candidate)
                self.assertIsNone(verified_offer(candidate))
                with self.assertRaises(ValueError):
                    publish(candidate, client=client)
                self.assertEqual(client.writes, [])

    def test_current_catalog_drift_never_writes(self):
        report, client, _ = self.audited()
        client.prices[PROFILE['id']] = (PROFILE['slug'], True)
        with self.assertRaises(ValueError):
            publish(report, client=client)
        self.assertEqual(client.writes, [])
        client.prices[PROFILE['id']] = (PROFILE['slug'], False)
        poco = next(row for row in client.rows if row['id'] == PROFILE['id'])
        poco['offer_targets'] = [{
            'merchant_name': 'MediaMarkt', 'seller_name': PROFILE['seller_name'],
            'source_url': 'https://www.hepsiburada.com/other'}]
        with self.assertRaises(ValueError):
            publish(report, client=client)
        self.assertEqual(client.writes, [])

    def test_bad_commit_ack_or_readback_never_reports_success(self):
        report, client, _ = self.audited()
        client.ack = {'committed': True, 'accepted': 0,
                      'stale_ignored': 1, 'variant_rejected': 0}
        with self.assertRaisesRegex(RuntimeError, 'not_accepted'):
            publish(report, client=client)
        client.writes.clear()
        client.ack = {'committed': True, 'accepted': 1,
                      'stale_ignored': 0, 'variant_rejected': 0}
        client.readback_seller = 'Other Seller'
        with self.assertRaisesRegex(RuntimeError, 'readback_missing'):
            publish(report, client=client)


if __name__ == '__main__':
    unittest.main()
