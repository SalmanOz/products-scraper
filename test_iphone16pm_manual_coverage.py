"""Offline exact-one iPhone 16 Pro Max publication tests; all writes use a fake client."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from audit_mediamarkt_iphone16pm_readonly import (
    CATALOG, LEGAL_NAME, MERSIS_NUMBER, SELLER, SELLER_URL, TAX_NUMBER,
    TITLE, URL,
)
from iphone16pm_manual_coverage import audit, main, publish, summary, verified_offer
from offer_ingestion_client import IngestionClientError, TARGET_INVALID_REASONS


EVIDENCE = {
    'platform': 'MediaMarkt', 'seller_name': SELLER, 'seller_id': '12945',
    'price': 125874.05, 'currency': 'TRY', 'availability': 'in_stock',
    'condition': 'new', 'source_title': TITLE, 'sku': '147052697',
    'gtin13': '0195949805981', 'selected_variant_gtin13': '0195949805981',
    'selected_variant_sku': '1239589',
    'offer_id': '1823637090', 'url': URL, 'ram_gb': 8,
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
                        'attributes': {'ram_gb': 8, 'storage_gb': 256,
                                       'Connectivity': {'5G': True}},
                        'offer_targets': [], 'offer_targets_truncated': False}
        self.prices = {CATALOG['id']: (CATALOG['slug'], False)}
        self.targets = []
        self.snapshot = SNAPSHOT
        self.raw_blocked = False
        self.complete_failure = False
        self.invalid_count = 0
        self.duplicate_count = 0
        self.invalid_reasons = {key: 0 for key in TARGET_INVALID_REASONS}
        self.raw_overrides = {}
        self.inspection_overrides = {}
        self.writes = []
        self.ack = {'committed': True, 'accepted': 1,
                    'stale_ignored': 0, 'variant_rejected': 0}
        self.readback_seller = SELLER

    def get_published_products(self):
        return [deepcopy(self.product)]

    def get_public_price_priority(self):
        return deepcopy(self.prices)

    def get_complete_offer_targets(self, product_id):
        if self.complete_failure:
            raise IngestionClientError('strict complete target reader rejected rows')
        return {'id': self.product['id'], 'slug': self.product['slug'],
                'name': self.product['name'], 'offer_targets': deepcopy(self.targets),
                'offer_targets_truncated': False,
                'offer_targets_total': len(self.targets),
                'offer_targets_snapshot': self.snapshot}

    def get_offer_target_inspection(self, product_id):
        return {'id': self.product['id'], 'slug': self.product['slug'],
                'name': self.product['name'],
                'attributes': deepcopy(self.product['attributes']),
                'offer_targets': deepcopy(self.targets),
                'offer_targets_total': len(self.targets),
                'offer_targets_invalid_count': self.invalid_count,
                'offer_targets_duplicate_count': self.duplicate_count,
                'offer_targets_invalid_reasons': deepcopy(self.invalid_reasons),
                'offer_targets_truncated': bool(self.invalid_count or self.duplicate_count),
                'offer_targets_snapshot': self.snapshot,
                **deepcopy(self.inspection_overrides)}

    def get_raw_seller_collision_proof(self, product_id, seller, url):
        return {'purpose': 'read_only_raw_seller_collision_proof',
                'writes': False, 'product_id': product_id,
                'complete': True, 'blocked': self.raw_blocked,
                'reasons': ['proposed_seller_on_other_url_or_platform']
                    if self.raw_blocked else [],
                'unknown_seller_rows': 0, 'invalid_row_identity_rows': 0,
                'invalid_url_rows': 0,
                'proposed_seller_other_url_rows': int(self.raw_blocked),
                'refresh_targets_invalid_count': self.invalid_count,
                'refresh_targets_duplicate_count': self.duplicate_count,
                'refresh_targets_invalid_reasons': deepcopy(self.invalid_reasons),
                'refresh_targets_total': len(self.targets),
                'stored_rows_checked': len(self.targets) + self.invalid_count +
                    self.duplicate_count,
                'refresh_targets_snapshot': self.snapshot,
                **deepcopy(self.raw_overrides)}

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


class Iphone16PMManualCoverageTests(unittest.TestCase):
    @staticmethod
    def safe_single_page_client():
        client = Client()
        client.complete_failure = True
        client.targets = [
            {'merchant_name': 'idefix', 'seller_name': 'Other Seller',
             'source_url': 'https://www.idefix.com/urun-123',
             'checked_at': None},
        ]
        client.product['offer_targets'] = deepcopy(client.targets)
        client.product['offer_targets_truncated'] = True
        client.invalid_count = 1
        client.invalid_reasons['unsupported_host'] = 1
        return client

    def audited(self, client=None, source=None):
        client = client or Client()
        source = source or Source()
        with patch('audit_mediamarkt_iphone16pm_readonly.selected_marketplace_offer',
                   return_value=deepcopy(EVIDENCE)):
            report = audit(client=client, source=source)
        return report, client, source

    def publish_exact(self, report, client, source, evidence=None):
        with patch('audit_mediamarkt_iphone16pm_readonly.selected_marketplace_offer',
                   return_value=deepcopy(evidence or EVIDENCE)):
            return publish(report, client=client, source=source)

    def test_default_audit_reads_exact_pages_and_catalog_without_writing(self):
        report, client, source = self.audited()
        self.assertEqual(report['status'], 'passed', report['catalog_error'])
        self.assertIs(report['writes'], False)
        self.assertEqual(report['catalog_evidence']['target_count'], 0)
        self.assertEqual(source.urls, [URL, SELLER_URL])
        self.assertEqual(client.writes, [])

    def test_same_pdp_under_another_stored_seller_blocks_audit(self):
        client = Client()
        client.targets = [{'merchant_name': 'MediaMarkt',
                           'source_url': URL, 'checked_at': None}]
        client.product['offer_targets'] = deepcopy(client.targets)
        report, client, source = self.audited(client)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['catalog_error'],
                         'catalog_source_url_other_seller_collision')
        self.assertEqual(source.urls, [])
        self.assertEqual(client.writes, [])

    def test_catalog_radio_may_be_unset_only_with_exact_5g_pdp_evidence(self):
        client = Client()
        client.product['attributes'].pop('Connectivity')
        report, client, _ = self.audited(client)
        self.assertEqual(report['status'], 'passed', report['catalog_error'])
        self.assertEqual(report['verified_offer']['network_generation'], '5g')
        self.assertEqual(client.writes, [])
        with patch('audit_mediamarkt_iphone16pm_readonly.selected_marketplace_offer',
                   return_value={**EVIDENCE, 'network_generation': '4g'}):
            report = audit(client=client, source=Source())
        self.assertEqual(report['status'], 'failed')
        self.assertIsNone(report['verified_offer'])
        self.assertEqual(client.writes, [])
        client.product['attributes']['Connectivity'] = {'5G': False}
        report, client, source = self.audited(client)
        self.assertEqual(report['catalog_status'], 'failed')
        self.assertEqual(source.urls, [])

    def test_single_page_fallback_proves_safe_rejected_row_and_rechecks_before_write(self):
        for allowed_reason in ('unsupported_host', 'retailer_query_restricted'):
            with self.subTest(allowed_reason=allowed_reason):
                client = self.safe_single_page_client()
                client.invalid_reasons['unsupported_host'] = 0
                client.invalid_reasons[allowed_reason] = 1
                report, client, source = self.audited(client)
                self.assertEqual(report['status'], 'passed', report['catalog_error'])
                evidence = report['catalog_evidence']
                self.assertEqual(evidence['target_mode'], 'single_page_raw_proven')
                self.assertEqual((evidence['target_count'], evidence['invalid_count']), (1, 1))
                self.assertEqual(evidence['invalid_reasons'][allowed_reason], 1)
                self.assertEqual(evidence['raw_seller_proof']['stored_rows_checked'], 2)
                self.assertEqual(client.writes, [])
                result = self.publish_exact(report, client, source)
                self.assertEqual(result['accepted'], 1)
                self.assertEqual(len(client.writes), 1)

    def test_single_page_fallback_rejects_missing_duplicate_or_unsafe_raw_rows(self):
        changes = (
            lambda client: setattr(client, 'duplicate_count', 1),
            lambda client: client.raw_overrides.update(stored_rows_checked=3),
            lambda client: client.raw_overrides.update(unknown_seller_rows=1),
            lambda client: setattr(client, 'raw_blocked', True),
            lambda client: client.raw_overrides.update(invalid_url_rows=1),
            lambda client: client.raw_overrides.update(invalid_row_identity_rows=1),
            lambda client: client.raw_overrides.update(
                proposed_seller_other_url_rows=1),
            lambda client: client.raw_overrides.update(
                refresh_targets_snapshot='b' * 64),
            lambda client: client.inspection_overrides.update(
                attributes={'storage_gb': 512}),
            lambda client: client.inspection_overrides.update(
                offer_targets_total=13),
            lambda client: client.product.update(offer_targets=[]),
            lambda client: (client.targets[0].update(seller_name=SELLER),
                            client.product['offer_targets'][0].update(
                                seller_name=SELLER)),
            lambda client: client.product.update(offer_targets_truncated=False),
            lambda client: setattr(client, 'snapshot', 'z' * 64),
            lambda client: (client.invalid_reasons.update(
                unsupported_host=0, sensitive_query=1)),
        )
        for change in changes:
            with self.subTest(change=change):
                client = self.safe_single_page_client()
                change(client)
                report, client, source = self.audited(client)
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(report['catalog_status'], 'failed')
                self.assertEqual(source.urls, [])
                self.assertEqual(client.writes, [])

    def test_single_page_fallback_blocks_publish_if_raw_proof_changes(self):
        report, _, source = self.audited(self.safe_single_page_client())
        self.assertEqual(report['status'], 'passed')
        client = self.safe_single_page_client()
        client.raw_overrides['refresh_targets_snapshot'] = 'b' * 64
        with self.assertRaises(ValueError):
            self.publish_exact(report, client, source)
        self.assertEqual(client.writes, [])

    def test_missing_complete_response_does_not_select_fallback(self):
        client, source = Client(), Source()
        client.get_complete_offer_targets = lambda _id: None
        report = audit(client=client, source=source)
        self.assertEqual(report['catalog_error'],
                         'complete_catalog_target_snapshot_changed')
        self.assertEqual(source.urls, [])
        self.assertEqual(client.writes, [])

    def test_complete_target_count_and_snapshot_must_be_consistent(self):
        for override in ({'offer_targets_total': 1},
                         {'offer_targets_snapshot': 'invalid'}):
            with self.subTest(override=override):
                client, source = Client(), Source()
                original = client.get_complete_offer_targets
                client.get_complete_offer_targets = (
                    lambda product_id, original=original, override=override:
                    {**original(product_id), **override})
                report = audit(client=client, source=source)
                self.assertEqual(report['catalog_status'], 'failed')
                self.assertEqual(report['catalog_error'],
                                 'complete_catalog_target_snapshot_changed')
                self.assertEqual(source.urls, [])
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
        client, source = self.safe_single_page_client(), Source()

        def fail_inspection(*_args, **_kwargs):
            raise RuntimeError(secret)

        client.get_offer_target_inspection = fail_inspection
        report = audit(client=client, source=source)
        self.assertEqual(report['catalog_error'], 'target_inspection_get_failed')
        self.assertEqual(source.urls, [])
        self.assertEqual(client.writes, [])
        self.assertNotIn(secret, str(report))

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
                         (24, CATALOG['slug'], SELLER, URL, TITLE, 125874.05))

    def test_slow_catalog_preparation_expires_audit_before_ingestion(self):
        report, client, source = self.audited()
        current = datetime.now(timezone.utc)
        late = current + timedelta(minutes=6)
        with patch('iphone16pm_manual_coverage.datetime', wraps=datetime) as clock:
            clock.now.side_effect = [current, current, late]
            with patch('iphone16pm_manual_coverage.verified_offer',
                       wraps=verified_offer) as gate:
                with patch('audit_mediamarkt_iphone16pm_readonly.selected_marketplace_offer',
                           return_value=deepcopy(EVIDENCE)):
                    with self.assertRaisesRegex(ValueError,
                                                'audit_stale_before_ingest'):
                        publish(report, client=client, source=source)
                self.assertEqual(gate.call_count, 2)
        self.assertEqual(client.writes, [])

    def test_other_variants_sellers_conditions_stock_and_price_never_write(self):
        report, client, source = self.audited()
        drifts = (
            {'source_title': TITLE.replace('iPhone 16 Pro Max', 'A55')},
            {'storage_gb': 512}, {'ram_gb': 6}, {'network_generation': '4g'},
            {'gtin13': '8806097425008'}, {'seller_id': '11289'},
            {'seller_name': 'Other seller'}, {'availability': 'out_of_stock'},
            {'condition': 'refurbished'}, {'price': 29999.0},
            {'offer_id': '1869831178'},
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
            lambda row: row['verified_offer'].update(price=29999.0),
            lambda row: row['source_audit']['offer'].update(sku='other'),
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
        for drift in ('priced', 'storage', 'other_seller_url', 'raw_collision',
                      'raw_invalid_url', 'raw_wrong_identity', 'snapshot'):
            with self.subTest(drift=drift):
                client = Client()
                if drift == 'priced':
                    client.prices[24] = (CATALOG['slug'], True)
                elif drift == 'storage':
                    client.product['attributes']['storage_gb'] = 512
                elif drift == 'other_seller_url':
                    target = {'merchant_name': 'MediaMarkt', 'seller_name': SELLER,
                              'source_url': 'https://www.mediamarkt.com.tr/tr/product/_other-123.html'}
                    client.product['offer_targets'] = [target]
                    client.targets = [target]
                elif drift == 'raw_collision':
                    client.raw_blocked = True
                elif drift == 'raw_invalid_url':
                    client.raw_overrides['invalid_url_rows'] = 1
                elif drift == 'raw_wrong_identity':
                    client.raw_overrides['product_id'] = 999
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

    def test_final_summary_uses_exact_publication_result(self):
        report, client, source = self.audited()
        self.assertIn('Denetim aşaması: yazım yok', summary(report))
        self.assertIn('Yayın istenmedi; bu koşuda fiyat yazımı yok',
                      summary(report, publish_requested=False))
        result = self.publish_exact(report, client, source)
        published = summary(report, publish_requested=True, publication=result)
        self.assertIn('Yayın tamamlandı: bir teklif kabul edildi', published)
        self.assertIn('readback doğrulandı', published)
        self.assertNotIn('yazım yok', published)
        incomplete = (None, {**result, 'accepted': 0},
                      {**result, 'submitted': True},
                      {**result, 'readback': 'missing'},
                      {**result, 'slug': 'other-product'},
                      {**result, 'seller': 'Other seller'},
                      {**result, 'source_url': 'https://example.com/other'},
                      {**result, 'price': result['price'] + 1})
        for candidate in incomplete:
            with self.subTest(candidate=candidate):
                rendered = summary(report, publish_requested=True,
                                   publication=candidate)
                self.assertIn('Yayın sonucu doğrulanamadı', rendered)
                self.assertNotIn('yazım yok', rendered)
        self.assertIn('Yayın sonucu doğrulanamadı',
                      summary({**report, 'status': 'failed'},
                              publish_requested=True, publication=result))
        failed_without_publication = summary({**report, 'status': 'failed'},
                                             publish_requested=True)
        self.assertIn('Denetim başarısız; yayın adımı çalışmadı, fiyat yazımı yok',
                      failed_without_publication)
        self.assertNotIn('Yayın sonucu doğrulanamadı', failed_without_publication)
        self.assertIn('Yayın sonucu doğrulanamadı',
                      summary(report, publish_requested=False,
                              publication=result))

    def test_summarize_command_handles_missing_or_invalid_publication(self):
        report, client, source = self.audited()
        result = self.publish_exact(report, client, source)
        with TemporaryDirectory() as directory:
            audit_file = Path(directory) / 'audit.json'
            publish_file = Path(directory) / 'publish.json'
            output_file = Path(directory) / 'summary.md'
            audit_file.write_text(json.dumps(report))
            publish_file.write_text(json.dumps(result))
            args = ['summarize', '--audit-file', str(audit_file),
                    '--publish-file', str(publish_file),
                    '--output-file', str(output_file),
                    '--publish-requested', 'true']
            self.assertEqual(main(args), 0)
            self.assertIn('Yayın tamamlandı', output_file.read_text())
            publish_file.write_text('{invalid json')
            self.assertEqual(main(args), 0)
            self.assertIn('Yayın sonucu doğrulanamadı', output_file.read_text())
            publish_file.unlink()
            self.assertEqual(main(args), 0)
            self.assertIn('Yayın sonucu doğrulanamadı', output_file.read_text())


if __name__ == '__main__':
    unittest.main()
