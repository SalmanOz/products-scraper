"""Offline exact-one Fold7 publication tests; all writes use a fake client."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from audit_mediamarkt_fold7_readonly import (
    CATALOG, LEGAL_NAME, MERSIS_NUMBER, SELLER, SELLER_URL, TAX_NUMBER,
    TITLE, URL,
)
from fold7_manual_coverage import audit, publish, summary, verified_offer


EVIDENCE = {
    'platform': 'MediaMarkt', 'seller_name': SELLER, 'seller_id': '12945',
    'price': 100000.0, 'currency': 'TRY', 'availability': 'in_stock',
    'condition': 'new', 'source_title': TITLE, 'sku': '167997743',
    'gtin13': '8806097425007', 'selected_variant_gtin13': None,
    'offer_id': '1836297896', 'url': URL, 'ram_gb': 12,
    'storage_gb': 256, 'network_generation': '5g',
}
LEGAL_HTML = (f'<main>Firma adı: {LEGAL_NAME} Şehir: Şanlıurfa '
              f'MERSİS No: {MERSIS_NUMBER} Vergi No: {TAX_NUMBER}</main>')
SNAPSHOT = 'a' * 64


class Source:
    def __init__(self):
        self._rules = None
        self.urls = []
        self.allowed = True
        self.legal_html = LEGAL_HTML

    def _load_robots(self):
        self._rules = self

    def can_fetch(self, bot, url):
        return self.allowed and url in (URL, SELLER_URL)

    def _direct(self, url, max_bytes, accept):
        self.urls.append(url)
        return (b'<html></html>' if url == URL else self.legal_html, 'ready', 200)


class Client:
    def __init__(self):
        self.product = {'id': CATALOG['id'], 'slug': CATALOG['slug'],
                        'name': CATALOG['name'],
                        'attributes': {'ram_gb': 12, 'storage_gb': 256,
                                       'Connectivity': {'5G': True}},
                        'offer_targets': [], 'offer_targets_truncated': False}
        self.prices = {CATALOG['id']: (CATALOG['slug'], False)}
        self.targets = []
        self.snapshot = SNAPSHOT
        self.raw_blocked = False
        self.writes = []
        self.ack = {'committed': True, 'accepted': 1,
                    'stale_ignored': 0, 'variant_rejected': 0}
        self.readback_seller = SELLER

    def get_published_products(self):
        return [deepcopy(self.product)]

    def get_public_price_priority(self):
        return deepcopy(self.prices)

    def get_complete_offer_targets(self, product_id):
        return {'id': self.product['id'], 'slug': self.product['slug'],
                'name': self.product['name'], 'offer_targets': deepcopy(self.targets),
                'offer_targets_truncated': False,
                'offer_targets_total': len(self.targets),
                'offer_targets_snapshot': self.snapshot}

    def get_raw_seller_collision_proof(self, product_id, seller, url):
        return {'complete': True, 'blocked': self.raw_blocked,
                'reasons': ['proposed_seller_on_other_url_or_platform']
                    if self.raw_blocked else [],
                'unknown_seller_rows': 0, 'invalid_row_identity_rows': 0,
                'proposed_seller_other_url_rows': int(self.raw_blocked),
                'refresh_targets_invalid_count': 0,
                'refresh_targets_duplicate_count': 0,
                'refresh_targets_total': len(self.targets),
                'stored_rows_checked': len(self.targets),
                'refresh_targets_snapshot': self.snapshot}

    def ingest_offers(self, rows):
        self.writes.append(deepcopy(rows))
        return deepcopy(self.ack)

    def get_offer_identity_readiness(self):
        row = self.writes[-1][0]
        return {'evidenceVersion': 'product-offer-match-v1',
                'mode': 'shadow_audit_only',
                'auditedAt': datetime.now(timezone.utc).isoformat(),
                'productCoverage': [{'slug': CATALOG['slug'],
                                     'matchedOffers': [{
                                         'merchant_name': self.readback_seller,
                                         'source_url': URL, 'price': row['price']}]}]}


class Fold7ManualCoverageTests(unittest.TestCase):
    def audited(self, client=None, source=None):
        client = client or Client()
        source = source or Source()
        with patch('audit_mediamarkt_fold7_readonly.selected_marketplace_offer',
                   return_value=deepcopy(EVIDENCE)):
            report = audit(client=client, source=source)
        return report, client, source

    def publish_exact(self, report, client, source, evidence=None):
        with patch('audit_mediamarkt_fold7_readonly.selected_marketplace_offer',
                   return_value=deepcopy(evidence or EVIDENCE)):
            return publish(report, client=client, source=source)

    def test_default_audit_reads_exact_pages_and_catalog_without_writing(self):
        report, client, source = self.audited()
        self.assertEqual(report['status'], 'passed', report['catalog_error'])
        self.assertIs(report['writes'], False)
        self.assertEqual(report['catalog_evidence']['target_count'], 0)
        self.assertEqual(source.urls, [URL, SELLER_URL])
        self.assertEqual(client.writes, [])

    def test_catalog_get_failures_report_only_fixed_stage_codes(self):
        stages = (
            ('get_published_products', 'catalog_primary_get_failed'),
            ('get_public_price_priority', 'public_price_status_get_failed'),
            ('get_complete_offer_targets', 'complete_targets_get_failed'),
            ('get_raw_seller_collision_proof', 'raw_seller_proof_get_failed'),
        )
        secret = 'secret-response-body-and-bearer-token'
        for method, stage in stages:
            with self.subTest(method=method):
                client, source = Client(), Source()

                def fail(*_args, **_kwargs):
                    raise RuntimeError(secret)

                setattr(client, method, fail)
                report = audit(client=client, source=source)
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(report['catalog_status'], 'failed')
                self.assertEqual(report['catalog_error'], stage)
                self.assertEqual(source.urls, [])
                self.assertEqual(client.writes, [])
                self.assertNotIn(secret, str(report))
                self.assertNotIn(secret, summary(report))

    def test_publish_rechecks_live_pdp_legal_seller_and_catalog_then_writes_one(self):
        report, client, source = self.audited()
        result = self.publish_exact(report, client, source)
        self.assertEqual(result['accepted'], 1)
        self.assertEqual(result['readback'], 'exact_selected_offer')
        self.assertEqual(source.urls, [URL, SELLER_URL, URL, SELLER_URL])
        self.assertEqual(len(client.writes), 1)
        self.assertEqual(len(client.writes[0]), 1)
        row = client.writes[0][0]
        self.assertEqual((row['product_id'], row['product_slug'], row['seller'],
                          row['source_url'], row['source_title'], row['price']),
                         (282, CATALOG['slug'], SELLER, URL, TITLE, 100000.0))

    def test_other_variants_sellers_conditions_stock_and_price_never_write(self):
        report, client, source = self.audited()
        drifts = (
            {'source_title': TITLE.replace('Fold 7', 'Fold 8')},
            {'storage_gb': 512}, {'ram_gb': 8}, {'network_generation': '4g'},
            {'gtin13': '8806097425008'}, {'seller_id': '11289'},
            {'seller_name': 'Other seller'}, {'availability': 'out_of_stock'},
            {'condition': 'refurbished'}, {'price': 99000.0},
            {'offer_id': '1836297897'},
        )
        for change in drifts:
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    self.publish_exact(report, client, source, {**EVIDENCE, **change})
                self.assertEqual(client.writes, [])

    def test_stale_tampered_or_other_workflow_audit_never_writes(self):
        report, client, source = self.audited()
        changes = (
            lambda row: row.update(status='failed'),
            lambda row: row.update(checked_at=(datetime.now(timezone.utc)
                - timedelta(minutes=6)).isoformat()),
            lambda row: row['verified_offer'].update(price=99000.0),
            lambda row: row['source_audit']['offer'].update(sku='other'),
            lambda row: row['source_audit'].update(robots_allowed=False),
            lambda row: row['catalog_evidence'].update(target_count=61),
        )
        for change in changes:
            with self.subTest(change=change):
                candidate = deepcopy(report)
                change(candidate)
                self.assertIsNone(verified_offer(candidate))
                with self.assertRaises(ValueError):
                    self.publish_exact(candidate, client, source)
                self.assertEqual(client.writes, [])
        with patch.dict('os.environ', {'GITHUB_RUN_ID': 'other-run'}):
            self.assertIsNone(verified_offer(report))

    def test_current_catalog_or_raw_seller_collision_blocks_write(self):
        report, _, source = self.audited()
        for drift in ('priced', 'storage', 'other_seller_url', 'raw_collision', 'snapshot'):
            with self.subTest(drift=drift):
                client = Client()
                if drift == 'priced':
                    client.prices[282] = (CATALOG['slug'], True)
                elif drift == 'storage':
                    client.product['attributes']['storage_gb'] = 512
                elif drift == 'other_seller_url':
                    target = {'merchant_name': 'MediaMarkt', 'seller_name': SELLER,
                              'source_url': 'https://www.mediamarkt.com.tr/tr/product/_other-123.html'}
                    client.product['offer_targets'] = [target]
                    client.targets = [target]
                elif drift == 'raw_collision':
                    client.raw_blocked = True
                else:
                    client.snapshot = 'b' * 64
                with self.assertRaises(ValueError):
                    self.publish_exact(report, client, source)
                self.assertEqual(client.writes, [])

    def test_accepted_one_and_commercial_readback_are_required(self):
        report, client, source = self.audited()
        client.ack['accepted'] = 0
        with self.assertRaisesRegex(RuntimeError, 'not_accepted'):
            self.publish_exact(report, client, source)
        client.writes.clear()
        client.ack['accepted'] = 1
        client.readback_seller = 'Other seller'
        with self.assertRaisesRegex(RuntimeError, 'readback_missing'):
            self.publish_exact(report, client, source)


if __name__ == '__main__':
    unittest.main()
