"""Offline exact-two gates; fake ingestion client records every attempted write."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from poco_x8_a27_manual_coverage import PROFILES, audit, publish, verified_offers
from test_coverage_two_marketplace import Source


URLS = list(PROFILES)
SNAPSHOT = 'a' * 64
REASONS = {'missing_or_oversize_url': 0, 'malformed_url': 0,
           'non_https_url': 0, 'unsupported_host': 0,
           'credentials_or_port': 0, 'retailer_query_restricted': 0,
           'sensitive_query': 0, 'missing_product_path': 0,
           'seller_identity': 0}


def offers():
    return {
        url: {'url': url, 'platform': 'MediaMarkt',
              'merchant': profile['seller_name'],
              'seller_name': profile['seller_name'],
              'seller_id': profile['seller_id'], 'sku': profile['sku'],
              'gtin13': profile['gtin13'],
              'selected_variant_gtin13': profile['gtin13'],
              'source_title': profile['source_title'],
              'ram_gb': profile['ram_gb'],
              'storage_gb': profile['storage_gb'],
              'network_generation': profile['network_generation'],
              'offer_id': str(1866651081 + index),
              'condition': 'new', 'availability': 'in_stock',
              'currency': 'TRY', 'price': float(profile['price'])}
        for index, (url, profile) in enumerate(PROFILES.items())
    }


def target(index, seller='Other Seller'):
    return {'merchant_name': 'MediaMarkt', 'seller_name': seller,
            'source_url': f'https://www.mediamarkt.com.tr/tr/product/_other-{index}.html',
            'checked_at': None}


class Client:
    def __init__(self):
        self.products = {}
        self.targets = {}
        self.snapshots = {}
        self.invalid = {}
        self.reasons = {}
        self.collisions = {}
        self.prices = {}
        self.writes = []
        self.ack = {'committed': True, 'accepted': 2,
                    'stale_ignored': 0, 'variant_rejected': 0}
        self.readback_seller = {}
        for url, profile in PROFILES.items():
            product_id = profile['id']
            self.targets[product_id] = [target(index) for index in range(2)]
            self.snapshots[product_id] = SNAPSHOT
            self.invalid[product_id] = 0
            self.reasons[product_id] = deepcopy(REASONS)
            self.collisions[product_id] = 0
            self.products[product_id] = {
                'id': product_id, 'slug': profile['slug'], 'name': profile['name'],
                'attributes': {'ram_gb': profile['ram_gb'],
                               'storage_gb': profile['storage_gb'],
                               'Connectivity': {'5G': True}},
                'offer_targets': deepcopy(self.targets[product_id]),
                'offer_targets_truncated': False,
            }
            self.prices[product_id] = (profile['slug'], False)
            self.readback_seller[product_id] = profile['seller_name']

    def get_published_products(self):
        for product_id, row in self.products.items():
            row['offer_targets'] = deepcopy(self.targets[product_id][:12])
            row['offer_targets_truncated'] = (
                len(self.targets[product_id]) > 12 or self.invalid[product_id] > 0)
        return deepcopy(list(self.products.values()))

    def get_public_price_priority(self):
        return deepcopy(self.prices)

    def get_raw_seller_collision_proof(self, product_id, seller, url):
        total = len(self.targets[product_id])
        invalid = self.invalid[product_id]
        collision = self.collisions[product_id]
        return {'purpose': 'read_only_raw_seller_collision_proof',
                'writes': False, 'product_id': product_id,
                'complete': True, 'blocked': bool(collision),
                'reasons': ['proposed_seller_on_other_url_or_platform']
                    if collision else [],
                'stored_rows_checked': total + invalid,
                'unknown_seller_rows': 0, 'invalid_url_rows': 0,
                'invalid_url_other_seller_rows': 0,
                'invalid_url_proposed_seller_rows': 0,
                'proposed_seller_other_url_rows': collision,
                'exact_seller_url_rows': 0, 'invalid_row_identity_rows': 0,
                'refresh_targets_total': total,
                'refresh_targets_invalid_count': invalid,
                'refresh_targets_duplicate_count': 0,
                'refresh_targets_invalid_reasons': deepcopy(self.reasons[product_id]),
                'refresh_targets_snapshot': self.snapshots[product_id]}

    def get_offer_target_inspection(self, product_id):
        row = self.products[product_id]
        return {'id': row['id'], 'slug': row['slug'], 'name': row['name'],
                'attributes': deepcopy(row['attributes']),
                'offer_targets': deepcopy(self.targets[product_id]),
                'offer_targets_total': len(self.targets[product_id]),
                'offer_targets_invalid_count': self.invalid[product_id],
                'offer_targets_duplicate_count': 0,
                'offer_targets_invalid_reasons': deepcopy(self.reasons[product_id]),
                'offer_targets_truncated': bool(self.invalid[product_id]),
                'offer_targets_snapshot': self.snapshots[product_id]}

    def get_complete_offer_targets(self, product_id):
        row = self.products[product_id]
        return {'id': row['id'], 'slug': row['slug'], 'name': row['name'],
                'offer_targets': deepcopy(self.targets[product_id]),
                'offer_targets_total': len(self.targets[product_id]),
                'offer_targets_truncated': False,
                'offer_targets_snapshot': self.snapshots[product_id]}

    def ingest_offers(self, records):
        self.writes.append(deepcopy(records))
        return deepcopy(self.ack)

    def get_offer_identity_readiness(self):
        records = self.writes[-1]
        return {'evidenceVersion': 'product-offer-match-v1',
                'mode': 'shadow_audit_only',
                'auditedAt': datetime.now(timezone.utc).isoformat(),
                'productCoverage': [{
                    'slug': record['product_slug'],
                    'matchedOffers': [{
                        'merchant_name': self.readback_seller[record['product_id']],
                        'source_url': record['source_url'],
                        'price': record['price']}],
                } for record in records]}


class ExactTwoManualCoverageTests(unittest.TestCase):
    def audited(self, client=None, source=None, candidates=None):
        client = client or Client()
        source = source or Source()
        candidates = candidates or offers()
        with patch('coverage_two_marketplace.selected_marketplace_offer',
                   side_effect=lambda body, url: candidates[url]):
            report = audit(client=client, source=source)
        return report, client, source

    def publish_exact(self, report, client, source, candidates=None):
        candidates = candidates or offers()
        with patch('coverage_two_marketplace.selected_marketplace_offer',
                   side_effect=lambda body, url: candidates[url]):
            return publish(report, client=client, source=source)

    def test_audit_reads_exact_two_and_never_writes(self):
        report, client, source = self.audited()
        self.assertEqual(report['status'], 'passed', report['catalog_error'])
        self.assertEqual(source.direct, URLS)
        self.assertEqual(client.writes, [])
        self.assertEqual([row['catalog_evidence']['target_count']
                          for row in report['rows']], [2, 2])
        self.assertEqual(set(verified_offers(report)), set(URLS))

    def test_full_thirteen_target_page_and_one_policy_reject_are_checked(self):
        client = Client()
        first, second = (PROFILES[url]['id'] for url in URLS)
        client.targets[first] = [target(index) for index in range(13)]
        client.invalid[second] = 1
        client.reasons[second]['unsupported_host'] = 1
        report, _, _ = self.audited(client=client)
        self.assertEqual(report['status'], 'passed', report['catalog_error'])
        self.assertEqual([row['catalog_evidence']['target_count']
                          for row in report['rows']], [13, 2])
        self.assertEqual([row['catalog_evidence']['raw_rows_checked']
                          for row in report['rows']], [13, 3])
        self.assertEqual(client.writes, [])

    def test_selected_price_seller_gtin_radio_and_stock_drift_fail_audit(self):
        for field, value in [('price', 37000), ('seller_id', '999'),
                             ('gtin13', '0000000000000'),
                             ('network_generation', '4g'),
                             ('availability', 'out_of_stock')]:
            with self.subTest(field=field):
                candidates = offers()
                candidates[URLS[0]][field] = value
                report, client, _ = self.audited(candidates=candidates)
                self.assertEqual(report['status'], 'failed')
                self.assertIsNone(report['rows'][0]['verified_offer'])
                self.assertEqual(client.writes, [])

    def test_raw_collision_or_snapshot_drift_fail_audit(self):
        for drift in ('seller_collision', 'target_collision', 'snapshot',
                      'priced', 'wrong_variant'):
            with self.subTest(drift=drift):
                client = Client()
                product_id = PROFILES[URLS[0]]['id']
                if drift == 'seller_collision':
                    client.collisions[product_id] = 1
                elif drift == 'target_collision':
                    client.targets[product_id][0]['seller_name'] = PROFILES[URLS[0]]['seller_name']
                elif drift == 'snapshot':
                    original = client.get_offer_target_inspection
                    client.get_offer_target_inspection = lambda ident: {
                        **original(ident), 'offer_targets_snapshot': 'b' * 64}
                elif drift == 'priced':
                    client.prices[product_id] = (PROFILES[URLS[0]]['slug'], True)
                else:
                    client.products[product_id]['attributes']['ram_gb'] = 12
                report, _, _ = self.audited(client=client)
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(client.writes, [])

    def test_stale_or_tampered_report_never_writes(self):
        report, client, source = self.audited()
        changes = [
            lambda item: item.update(checked_at=(datetime.now(timezone.utc)
                - timedelta(minutes=6)).isoformat()),
            lambda item: item['rows'][0]['verified_offer'].update(price=37000),
            lambda item: item['rows'][0]['catalog_evidence'].update(target_snapshot='bad'),
            lambda item: item['rows'][0]['source_attempt'].update(http_status=403),
            lambda item: item.update(github_run_id='different-run'),
        ]
        for change in changes:
            with self.subTest(change=change):
                candidate = deepcopy(report)
                change(candidate)
                self.assertIsNone(verified_offers(candidate))
                with self.assertRaises(ValueError):
                    self.publish_exact(candidate, client, source)
                self.assertEqual(client.writes, [])

    def test_exact_two_publication_needs_second_live_read_and_readback(self):
        report, client, source = self.audited()
        result = self.publish_exact(report, client, source)
        self.assertEqual(result['accepted'], 2)
        self.assertEqual(source.direct, URLS + URLS)
        self.assertEqual(len(client.writes), 1)
        self.assertEqual(len(client.writes[0]), 2)
        self.assertEqual({row['product_id'] for row in client.writes[0]},
                         {237, 279})
        self.assertEqual({row['price'] for row in client.writes[0]},
                         {36999.0, 20379.0})

    def test_second_get_or_catalog_drift_never_writes(self):
        report, client, source = self.audited()
        candidates = offers()
        candidates[URLS[1]]['selected_variant_gtin13'] = 'different'
        with self.assertRaisesRegex(ValueError, 'live_offer_changed'):
            self.publish_exact(report, client, source, candidates)
        self.assertEqual(client.writes, [])
        client.snapshots[PROFILES[URLS[1]]['id']] = 'b' * 64
        with self.assertRaisesRegex(ValueError, 'catalog_or_raw_proof_changed'):
            self.publish_exact(report, client, source)
        self.assertEqual(client.writes, [])

    def test_failed_ack_or_readback_cannot_report_success(self):
        report, client, source = self.audited()
        client.ack.update(accepted=1, stale_ignored=1)
        with self.assertRaisesRegex(RuntimeError, 'not_accepted'):
            self.publish_exact(report, client, source)
        client.writes.clear()
        client.ack.update(accepted=2, stale_ignored=0)
        client.readback_seller[237] = 'Other Seller'
        with self.assertRaisesRegex(RuntimeError, 'readback_missing'):
            self.publish_exact(report, client, source)


if __name__ == '__main__':
    unittest.main()
