"""Offline exact-one Redmi writer tests. Every submission is a fake client."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from redmi_note_15_pro_manual_coverage import URL, PROFILE, audit, publish, verified_offer
from test_coverage_two_marketplace import Client, Source, offers


REASONS = {'missing_or_oversize_url': 0, 'malformed_url': 0,
           'non_https_url': 0, 'unsupported_host': 1,
           'credentials_or_port': 0, 'retailer_query_restricted': 0,
           'sensitive_query': 0, 'missing_product_path': 0,
           'seller_identity': 0}
SNAPSHOT = 'a' * 64


class RecordingClient(Client):
    def __init__(self):
        super().__init__()
        redmi = next(row for row in self.rows if row['id'] == PROFILE['id'])
        redmi['attributes']['Connectivity'] = {'5G': True}
        redmi['offer_targets_truncated'] = True
        redmi['offer_targets'] = [
            {'merchant_name': 'MediaMarkt', 'seller_name': 'Other Seller',
             'source_url': f'https://www.mediamarkt.com.tr/tr/product/_other-{i}.html',
             'checked_at': None}
            for i in range(12)
        ]
        self.ack = {'committed': True, 'accepted': 1,
                    'stale_ignored': 0, 'variant_rejected': 0}
        self.readback_seller = PROFILE['seller_name']
        self.reasons = deepcopy(REASONS)
        self.snapshot = SNAPSHOT
        self.raw_other_url_rows = 0

    def get_offer_target_inspection(self, product_id):
        row = next(row for row in self.rows if row['id'] == product_id)
        return {'id': row['id'], 'slug': row['slug'], 'name': row['name'],
                'attributes': deepcopy(row['attributes']),
                'offer_targets': deepcopy(row['offer_targets']),
                'offer_targets_total': 12, 'offer_targets_invalid_count': 1,
                'offer_targets_duplicate_count': 0,
                'offer_targets_invalid_reasons': deepcopy(self.reasons),
                'offer_targets_truncated': True,
                'offer_targets_snapshot': self.snapshot}

    def get_raw_seller_collision_proof(self, product_id, seller_name, source_url):
        return {'purpose': 'read_only_raw_seller_collision_proof',
                'writes': False, 'product_id': product_id,
                'complete': True, 'blocked': bool(self.raw_other_url_rows),
                'reasons': (['proposed_seller_on_other_url_or_platform']
                            if self.raw_other_url_rows else []),
                'stored_rows_checked': 13, 'unknown_seller_rows': 0,
                'invalid_url_rows': 0, 'invalid_url_other_seller_rows': 0,
                'invalid_url_proposed_seller_rows': 0,
                'proposed_seller_other_url_rows': self.raw_other_url_rows,
                'exact_seller_url_rows': 0, 'invalid_row_identity_rows': 0,
                'refresh_targets_total': 12,
                'refresh_targets_invalid_count': 1,
                'refresh_targets_duplicate_count': 0,
                'refresh_targets_invalid_reasons': deepcopy(self.reasons),
                'refresh_targets_snapshot': self.snapshot}

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


class RedmiManualCoverageTests(unittest.TestCase):
    def audited(self, client=None, source=None):
        client = client or RecordingClient()
        source = source or Source()
        with patch('coverage_two_marketplace.selected_marketplace_offer',
                   return_value=offers()[URL]):
            report = audit(client=client, source=source)
        return report, client, source

    def publish_with_exact_page(self, report, client, source):
        with patch('coverage_two_marketplace.selected_marketplace_offer',
                   return_value=offers()[URL]):
            return publish(report, client=client, source=source)

    def test_audit_is_read_only_and_counts_all_thirteen_sellers(self):
        report, client, source = self.audited()
        self.assertEqual(report['status'], 'passed', report.get('catalog_error'))
        self.assertEqual(source.direct, [URL])
        self.assertEqual(client.writes, [])
        evidence = report['catalog_evidence']
        self.assertEqual((evidence['target_count'], evidence['invalid_count'],
                          evidence['raw_seller_proof']['stored_rows_checked']), (12, 1, 13))
        self.assertEqual(evidence['invalid_reasons']['unsupported_host'], 1)

    def test_one_exact_record_requires_second_live_get_ack_and_readback(self):
        report, client, source = self.audited()
        result = self.publish_with_exact_page(report, client, source)
        self.assertEqual(result['accepted'], 1)
        self.assertEqual(source.direct, [URL, URL])
        self.assertEqual(len(client.writes), 1)
        self.assertEqual(len(client.writes[0]), 1)
        record = client.writes[0][0]
        self.assertEqual((record['product_id'], record['product_slug'],
                          record['seller'], record['source_url'], record['price']),
                         (238, PROFILE['slug'], PROFILE['seller_name'], URL, 30499.0))

    def test_stale_or_tampered_audit_never_writes(self):
        report, client, source = self.audited()
        changes = [
            lambda data: data.update(status='failed'),
            lambda data: data.update(checked_at=(datetime.now(timezone.utc)
                - timedelta(minutes=6)).isoformat()),
            lambda data: data['verified_offer'].update(gtin13='wrong'),
            lambda data: data['verified_offer'].update(price=30999),
            lambda data: data['catalog_evidence'].update(invalid_count=2),
            lambda data: data['source_attempt'].update(http_status=403),
        ]
        for change in changes:
            with self.subTest(change=change):
                candidate = deepcopy(report)
                change(candidate)
                self.assertIsNone(verified_offer(candidate))
                with self.assertRaises(ValueError):
                    self.publish_with_exact_page(candidate, client, source)
                self.assertEqual(client.writes, [])

    def test_live_price_seller_and_stock_drift_never_write(self):
        report, client, source = self.audited()
        for field, value in (('price', 30999), ('seller_name', 'Different Seller'),
                             ('availability', 'out_of_stock'), ('sku', 'other')):
            with self.subTest(field=field):
                changed = offers()[URL]
                changed[field] = value
                with patch('coverage_two_marketplace.selected_marketplace_offer',
                           return_value=changed):
                    with self.assertRaises(ValueError):
                        publish(report, client=client, source=source)
                self.assertEqual(client.writes, [])

    def test_raw_rejected_row_seller_and_snapshot_drift_never_write(self):
        report, _, source = self.audited()
        for drift in ('seller_collision', 'snapshot', 'reason', 'now_priced'):
            with self.subTest(drift=drift):
                client = RecordingClient()
                if drift == 'seller_collision':
                    client.raw_other_url_rows = 1
                elif drift == 'snapshot':
                    client.snapshot = 'b' * 64
                elif drift == 'reason':
                    client.reasons.update(unsupported_host=0, sensitive_query=1)
                else:
                    client.prices[PROFILE['id']] = (PROFILE['slug'], True)
                with self.assertRaises(ValueError):
                    self.publish_with_exact_page(report, client, source)
                self.assertEqual(client.writes, [])

    def test_commit_ack_and_authenticated_readback_fail_closed(self):
        report, client, source = self.audited()
        client.ack = {'committed': True, 'accepted': 0,
                      'stale_ignored': 1, 'variant_rejected': 0}
        with self.assertRaisesRegex(RuntimeError, 'not_accepted'):
            self.publish_with_exact_page(report, client, source)
        client.writes.clear()
        client.ack = {'committed': True, 'accepted': 1,
                      'stale_ignored': 0, 'variant_rejected': 0}
        client.readback_seller = 'Other Seller'
        with self.assertRaisesRegex(RuntimeError, 'readback_missing'):
            self.publish_with_exact_page(report, client, source)


if __name__ == '__main__':
    unittest.main()
