"""Offline A55 two-seller gates; the fake client records every attempted write."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from a55_exact_manual_coverage import (
    PROFILES, SELLER_IDENTITIES, _seller_profiles, audit, main, publish,
    summary, verified_offers,
)
from test_coverage_two_marketplace import Source


URLS = list(PROFILES)
PRODUCT_ID = 32
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
              'ram_gb': 8, 'storage_gb': 128,
              'network_generation': '5g',
              'offer_id': str(1866651081 + index),
              'condition': 'new', 'availability': 'in_stock',
              'currency': 'TRY', 'price': float(profile['price'])}
        for index, (url, profile) in enumerate(PROFILES.items())
    }


def seller_urls():
    return ['https://www.mediamarkt.com.tr/tr/marketplace/seller/'
            + profile['seller_id'] for profile in SELLER_IDENTITIES.values()]


class A55Source(Source):
    def __init__(self):
        super().__init__()
        self.seller_text = {}
        for profile in SELLER_IDENTITIES.values():
            url = ('https://www.mediamarkt.com.tr/tr/marketplace/seller/'
                   + profile['seller_id'])
            self.seller_text[url] = (
                '<div>Firma adı: ' + profile['legal_name'] + '</div>'
                '<div>MERSİS No: ' + profile['mersis'] + '</div>'
                '<div>Vergi No: ' + profile['tax'] + '</div>')

    def _direct(self, url, limit, accept):
        if url in self.seller_text:
            self.direct.append(url)
            return self.seller_text[url].encode(), 'ready', 200
        return super()._direct(url, limit, accept)


class Client:
    def __init__(self):
        self.targets = [self.target(index) for index in range(2)]
        self.snapshot = SNAPSHOT
        self.invalid = 0
        self.reasons = deepcopy(REASONS)
        self.collisions = set()
        self.price_status = ('samsung-galaxy-a55-5g', False)
        self.product = {
            'id': PRODUCT_ID, 'slug': 'samsung-galaxy-a55-5g',
            'name': 'Samsung Galaxy A55 5G',
            'attributes': {'ram_gb': 8, 'storage_gb': 128,
                           'Connectivity': {'5G': True}},
            'offer_targets': deepcopy(self.targets),
            'offer_targets_truncated': False,
        }
        self.writes = []
        self.atomic_flags = []
        self.ack = {'committed': True, 'accepted': 2,
                    'stale_ignored': 0, 'variant_rejected': 0}
        self.readback_seller_drift = None

    @staticmethod
    def target(index, seller='Other Seller'):
        return {'merchant_name': 'MediaMarkt', 'seller_name': seller,
                'source_url': ('https://www.mediamarkt.com.tr/tr/product/'
                               f'_other-{index}.html'), 'checked_at': None}

    def get_published_products(self):
        row = deepcopy(self.product)
        row['offer_targets'] = deepcopy(self.targets[:12])
        row['offer_targets_truncated'] = len(self.targets) > 12 or self.invalid > 0
        return [row]

    def get_public_price_priority(self):
        return {PRODUCT_ID: self.price_status}

    def get_raw_seller_collision_proof(self, product_id, seller, url):
        collision = seller in self.collisions
        return {'complete': True, 'blocked': collision,
                'reasons': ['proposed_seller_on_other_url_or_platform'] if collision else [],
                'stored_rows_checked': len(self.targets) + self.invalid,
                'unknown_seller_rows': 0, 'invalid_url_rows': 0,
                'invalid_row_identity_rows': 0,
                'proposed_seller_other_url_rows': int(collision),
                'refresh_targets_total': len(self.targets),
                'refresh_targets_invalid_count': self.invalid,
                'refresh_targets_duplicate_count': 0,
                'refresh_targets_invalid_reasons': deepcopy(self.reasons),
                'refresh_targets_snapshot': self.snapshot}

    def get_offer_target_inspection(self, product_id):
        return {'id': PRODUCT_ID, 'slug': self.product['slug'],
                'name': self.product['name'],
                'attributes': deepcopy(self.product['attributes']),
                'offer_targets': deepcopy(self.targets),
                'offer_targets_total': len(self.targets),
                'offer_targets_invalid_count': self.invalid,
                'offer_targets_duplicate_count': 0,
                'offer_targets_invalid_reasons': deepcopy(self.reasons),
                'offer_targets_truncated': bool(self.invalid),
                'offer_targets_snapshot': self.snapshot}

    def get_complete_offer_targets(self, product_id):
        return {'id': PRODUCT_ID, 'slug': self.product['slug'],
                'name': self.product['name'],
                'offer_targets': deepcopy(self.targets),
                'offer_targets_total': len(self.targets),
                'offer_targets_truncated': False,
                'offer_targets_snapshot': self.snapshot}

    def ingest_offers(self, records, *, require_all_accepted=False):
        self.atomic_flags.append(require_all_accepted)
        self.writes.append(deepcopy(records))
        return deepcopy(self.ack)

    def get_offer_identity_readiness(self):
        rows = [{'merchant_name': record['seller'],
                 'source_url': record['source_url'],
                 'price': record['price']}
                for record in self.writes[-1]]
        if self.readback_seller_drift:
            rows[0]['merchant_name'] = self.readback_seller_drift
        return {'evidenceVersion': 'product-offer-match-v1',
                'mode': 'shadow_audit_only',
                'auditedAt': datetime.now(timezone.utc).isoformat(),
                'productCoverage': [{'slug': self.product['slug'],
                                     'matchedOffers': rows}]}


class A55ExactManualCoverageTests(unittest.TestCase):
    def audited(self, client=None, source=None, candidates=None):
        client = client or Client()
        source = source or A55Source()
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

    def test_read_only_audit_checks_two_legal_sellers_and_two_pdp(self):
        report, client, source = self.audited()
        self.assertEqual(report['status'], 'passed', report)
        self.assertEqual(report['seller_status'], 'passed')
        self.assertEqual(source.direct, seller_urls() + URLS)
        self.assertEqual(client.writes, [])
        self.assertEqual(set(verified_offers(report)), set(URLS))

    def test_changed_legal_seller_blocks_audit(self):
        for drift in ('legal_name', 'tax'):
            with self.subTest(drift=drift):
                source = A55Source()
                url = seller_urls()[0]
                source.seller_text[url] = source.seller_text[url].replace(
                    SELLER_IDENTITIES['Güzel Teknoloji'][drift], 'changed')
                report, client, _ = self.audited(source=source)
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(client.writes, [])

    def test_selected_price_gtin_seller_radio_or_stock_drift_blocks(self):
        for field, value in [('price', 37500), ('gtin13', '0000000000000'),
                             ('seller_id', '999'), ('network_generation', '4g'),
                             ('availability', 'out_of_stock')]:
            with self.subTest(field=field):
                candidates = offers()
                candidates[URLS[0]][field] = value
                report, client, _ = self.audited(candidates=candidates)
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(client.writes, [])

    def test_all_catalog_targets_and_both_seller_collisions(self):
        client = Client()
        client.targets = [client.target(index) for index in range(13)]
        report, _, _ = self.audited(client=client)
        self.assertEqual(report['status'], 'passed', report['catalog_error'])
        self.assertEqual([row['catalog_evidence']['target_count']
                          for row in report['rows']], [13, 13])
        for seller in SELLER_IDENTITIES:
            blocked = Client()
            blocked.collisions.add(seller)
            report, _, _ = self.audited(client=blocked)
            self.assertEqual(report['status'], 'failed')
            self.assertEqual(blocked.writes, [])

    def test_only_safe_invalid_target_reasons_can_pass_catalog_audit(self):
        safe = Client()
        safe.invalid = 1
        safe.reasons['unsupported_host'] = 1
        report, _, _ = self.audited(client=safe)
        self.assertEqual(report['status'], 'passed', report['catalog_error'])
        self.assertEqual(report['rows'][0]['catalog_evidence']['invalid_reasons']['unsupported_host'], 1)
        unsafe = Client()
        unsafe.invalid = 1
        unsafe.reasons['malformed_url'] = 1
        report, _, _ = self.audited(client=unsafe)
        self.assertEqual(report['status'], 'failed')
        self.assertIn('raw_seller_or_target_rejection', report['catalog_error'])
        self.assertEqual(unsafe.writes, [])

    def test_priced_variant_or_target_snapshot_drift_blocks_audit(self):
        for drift in ('priced', 'variant', 'target', 'snapshot'):
            with self.subTest(drift=drift):
                client = Client()
                if drift == 'priced':
                    client.price_status = (client.product['slug'], True)
                elif drift == 'variant':
                    client.product['attributes']['ram_gb'] = 12
                elif drift == 'target':
                    client.targets[0]['seller_name'] = 'Güzel Teknoloji'
                else:
                    original = client.get_offer_target_inspection
                    client.get_offer_target_inspection = lambda ident: {
                        **original(ident), 'offer_targets_snapshot': 'b' * 64}
                report, _, _ = self.audited(client=client)
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(client.writes, [])

    def test_stale_or_tampered_audit_never_writes(self):
        report, client, source = self.audited()
        changes = [
            lambda row: row.update(checked_at=(datetime.now(timezone.utc)
                - timedelta(minutes=6)).isoformat()),
            lambda row: row['rows'][0]['verified_offer'].update(price=37500),
            lambda row: row['rows'][1]['catalog_evidence'].update(target_snapshot='bad'),
            lambda row: row['seller_profiles']['Güzel Teknoloji'].update(tax='bad'),
            lambda row: row.update(github_run_id='different-run'),
        ]
        for change in changes:
            with self.subTest(change=change):
                modified = deepcopy(report)
                change(modified)
                self.assertIsNone(verified_offers(modified))
                with self.assertRaises(ValueError):
                    self.publish_exact(modified, client, source)
                self.assertEqual(client.writes, [])

    def test_single_atomic_batch_has_one_product_and_two_distinct_sellers(self):
        report, client, source = self.audited()
        result = self.publish_exact(report, client, source)
        self.assertEqual(result['accepted'], 2)
        self.assertEqual(len(client.writes), 1)
        self.assertEqual(client.atomic_flags, [True])
        rows = client.writes[0]
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['product_id'] for row in rows}, {PRODUCT_ID})
        self.assertEqual({row['seller'] for row in rows}, set(SELLER_IDENTITIES))
        self.assertEqual({row['price'] for row in rows}, {37499.0, 44999.0})
        self.assertEqual(source.direct, seller_urls() + URLS + URLS + seller_urls())

    def test_second_get_or_seller_identity_change_never_writes(self):
        report, client, source = self.audited()
        candidates = offers()
        candidates[URLS[1]]['price'] += 1
        with self.assertRaisesRegex(ValueError, 'live_offer_changed'):
            self.publish_exact(report, client, source, candidates)
        self.assertEqual(client.writes, [])
        source.seller_text[seller_urls()[0]] = '<div>seller changed</div>'
        with self.assertRaisesRegex(ValueError, 'seller_identity_changed'):
            self.publish_exact(report, client, source)
        self.assertEqual(client.writes, [])

    def test_catalog_snapshot_change_after_audit_never_writes(self):
        report, client, source = self.audited()
        client.snapshot = 'b' * 64
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
        client.readback_seller_drift = 'Wrong seller'
        with self.assertRaisesRegex(RuntimeError, 'readback_missing'):
            self.publish_exact(report, client, source)

    def test_final_summary_uses_actual_publication_result(self):
        report, client, source = self.audited()
        self.assertIn('Denetim aşaması: yazım yok', summary(report))
        self.assertIn('Yayın istenmedi; bu koşuda fiyat yazımı yok',
                      summary(report, publish_requested=False))
        failed = {**report, 'status': 'failed'}
        self.assertIn('Denetim başarısız; yayın adımı çalıştırılmadı',
                      summary(failed, publish_requested=True))
        result = self.publish_exact(report, client, source)
        self.assertIn('Yayın tamamlandı: iki teklif kabul edildi',
                      summary(report, publish_requested=True, publication=result))
        for incomplete in (None, {**result, 'accepted': 1},
                           {**result, 'readback': 'missing'}):
            self.assertIn('Yayın sonucu doğrulanamadı',
                          summary(report, publish_requested=True,
                                  publication=incomplete))

    def test_summarize_command_handles_missing_publication(self):
        report, client, source = self.audited()
        with TemporaryDirectory() as directory:
            audit_file = Path(directory) / 'audit.json'
            publish_file = Path(directory) / 'publish.json'
            output_file = Path(directory) / 'summary.md'
            audit_file.write_text(json.dumps(report))
            self.assertEqual(main(['summarize', '--audit-file', str(audit_file),
                                   '--publish-file', str(publish_file),
                                   '--output-file', str(output_file),
                                   '--publish-requested', 'true']), 0)
            self.assertIn('Yayın sonucu doğrulanamadı', output_file.read_text())


if __name__ == '__main__':
    unittest.main()
