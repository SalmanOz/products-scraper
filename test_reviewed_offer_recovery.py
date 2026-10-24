"""Offline scope, evidence and no-write guarantees for reviewed recovery."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import socket
import unittest
from unittest.mock import Mock, patch

import requests

from recover_reviewed_offers import load_profiles, recover, rejection_reason, validate_browser_evidence, validate_catalog
from source_ingestion import CatalogProduct


NOW = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)


class ReviewedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('No live network'))
        self.http = patch.object(requests.sessions.Session, 'request', side_effect=AssertionError('No live HTTP'))
        self.network.start()
        self.http.start()
        self.addCleanup(self.network.stop)
        self.addCleanup(self.http.stop)
        self.profiles = load_profiles()
        self.profile = next(p for p in self.profiles if p['slug'] == 'samsung-galaxy-a57-5g')
        self.offer = {
            'merchant': 'Teknosa', 'price': 28699.0, 'currency': 'TRY',
            'availability': 'in_stock', 'observed_at': NOW.isoformat(),
            'url': self.profile['targets'][0],
            'source_title': 'Samsung Galaxy A57 5G 128 GB 8 GB RAM Lila Cep Telefonu (Samsung Türkiye Garantili)',
        }
        self.catalog = [CatalogProduct(
            id=p['id'], slug=p['slug'], name=p['name'],
            attributes={'ram_gb': p['ram_gb'], 'storage_gb': p['storage_gb']},
            data_quality_status='pending', data_quality_issues=({'code': 'source_unverified'},),
        ) for p in self.profiles]
        self.targets = [{'id': p['id'], 'slug': p['slug'], 'offer_targets': []} for p in self.profiles]

    def test_reviewed_batch_is_exactly_eight_existing_references(self):
        self.assertEqual(len(self.profiles), 8)
        validate_catalog(self.profiles, self.catalog)
        for field, value in [('id', 999), ('name', 'Different phone'), ('attributes', {'ram_gb': 12, 'storage_gb': 256})]:
            changed = list(self.catalog)
            changed[0] = CatalogProduct(**{**changed[0].__dict__, field: value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_catalog(self.profiles, changed)
        with self.assertRaises(ValueError):
            validate_catalog(self.profiles, self.catalog[:-1])

    def test_eligible_exact_offer(self):
        self.assertIsNone(rejection_reason(self.profile, self.offer, NOW))

    def test_wrong_memory_network_model_refurb_and_warranty_fail(self):
        for title in [
            self.offer['source_title'].replace('128 GB', '256 GB'),
            self.offer['source_title'].replace('8 GB RAM', '12 GB RAM'),
            self.offer['source_title'].replace('5G', '4G'),
            self.offer['source_title'].replace('A57', 'A56'),
            self.offer['source_title'] + ' Yenilenmiş',
            self.offer['source_title'].replace('Samsung Türkiye Garantili', 'İthalatçı Garantili'),
            self.offer['source_title'].replace('(Samsung Türkiye Garantili)', ''),
        ]:
            with self.subTest(title=title):
                self.assertIsNotNone(rejection_reason(self.profile, {**self.offer, 'source_title': title}, NOW))

    def test_stock_currency_price_timestamp_and_host_fail_closed(self):
        for changes in [
            {'availability': 'out_of_stock'}, {'availability': 'preorder'},
            {'availability': 'unknown'}, {'currency': 'USD'}, {'price': True},
            {'price': float('nan')}, {'price': 0},
            {'observed_at': (NOW - timedelta(minutes=16)).isoformat()},
            {'observed_at': (NOW + timedelta(minutes=2)).isoformat()},
            {'observed_at': 'not-a-date'}, {'observed_at': '2026-10-25T12:00:00'},
            {'url': 'https://teknosa.com.attacker.test/a57-128gb-8gb'},
            {'merchant': 'n11'},
        ]:
            with self.subTest(changes=changes):
                self.assertIsNotNone(rejection_reason(self.profile, {**self.offer, **changes}, NOW))

    def test_spark_requires_explicit_5g_not_global_slim_4g(self):
        profile = self.profiles[0]
        base = {**self.offer, 'merchant': 'idefix',
                'url': 'https://www.idefix.com/tecno-spark-slim-256gb-8gb-p-123'}
        for title in ['Tecno Spark Slim 256GB 8GB RAM Türkiye Garantili',
                      'Tecno Spark Slim 4G 256GB 8GB RAM Türkiye Garantili']:
            self.assertIsNotNone(rejection_reason(profile, {**base, 'source_title': title}, NOW))
        self.assertIsNone(rejection_reason(profile, {**base,
            'source_title': 'Tecno Spark Slim 5G 256GB 8GB RAM Türkiye Garantili'}, NOW))

    def test_audit_never_writes_and_reports_quality_blockers_honestly(self):
        reader = Mock()
        reader.refresh_existing_offer_targets.return_value = []
        report = recover(self.profiles, self.catalog, self.targets, reader, now=NOW)
        self.assertEqual((report['mode'], report['submitted_offers'], report['ingestion']), ('audit', 0, None))
        self.assertEqual(len(report['products']), 8)
        self.assertTrue(all(not p['fresh_pair_observed'] for p in report['products']))
        self.assertEqual(report['products'][0]['quality_issues'], [{'code': 'source_unverified'}])

    def test_apply_collapses_same_merchant_without_claiming_two_shops(self):
        reader, writer = Mock(), Mock()
        cheaper = {**self.offer, 'price': 28000}
        reader.refresh_existing_offer_targets.side_effect = lambda name, *args: (
            [self.offer, cheaper] if name == self.profile['name'] else [])
        writer.ingest_offers.return_value = {'committed': True, 'accepted': 1}
        report = recover(self.profiles, self.catalog, self.targets, reader, writer, NOW)
        self.assertEqual(report['submitted_offers'], 1)
        submitted = writer.ingest_offers.call_args.args[0]
        self.assertEqual(submitted[0]['price'], 28000)
        self.assertEqual(submitted[0]['product_id'], 264)
        self.assertEqual(submitted[0]['checked_at'], self.offer['observed_at'])
        self.assertFalse(next(p for p in report['products'] if p['slug'] == self.profile['slug'])['fresh_pair_observed'])

    def test_scope_failure_does_not_write_anything(self):
        reader, writer = Mock(), Mock()
        changed = deepcopy(self.targets)
        changed[-1]['id'] = 999
        reader.refresh_existing_offer_targets.return_value = []
        with self.assertRaises(ValueError):
            recover(self.profiles, self.catalog, changed, reader, writer, NOW)
        writer.ingest_offers.assert_not_called()

    def test_reviewed_browser_evidence_expires_and_cannot_invent_price_or_purchase_state(self):
        row = {**self.offer, 'product_id': self.profile['id'], 'product_slug': self.profile['slug'],
               'evidence_method': 'live_browser_selected_pdp', 'purchase_enabled': True,
               'selected_seller': 'Example Store', 'displayed_cash_price': '28.699 TL',
               'proof_quote': self.offer['source_title'] + '\n28.699 TL\nExample Store\nSepete Ekle'}
        validate_browser_evidence(self.profiles, [row], NOW)
        for changes in [
            {'price': 20000}, {'purchase_enabled': False}, {'proof_quote': ''},
            {'product_id': 999}, {'product_slug': 'unreviewed-phone'},
            {'url': 'https://www.teknosa.com/another-a57-128gb-8gb'},
            {'evidence_method': 'search_snippet'},
            {'observed_at': (NOW - timedelta(minutes=16)).isoformat()},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_browser_evidence(self.profiles, [{**row, **changes}], NOW)

    def test_browser_evidence_obeys_same_variant_rules_and_is_not_used_by_default(self):
        reader = Mock()
        reader.refresh_existing_offer_targets.return_value = []
        row = {**self.offer, 'product_id': self.profile['id'], 'product_slug': self.profile['slug'],
               'evidence_method': 'live_browser_selected_pdp', 'purchase_enabled': True,
               'selected_seller': 'Example Store', 'displayed_cash_price': '28.699 TL',
               'proof_quote': self.offer['source_title'] + '\n28.699 TL\nExample Store\nSepete Ekle'}
        self.assertEqual(recover(self.profiles, self.catalog, self.targets, reader, now=NOW)['eligible_offers'], 0)
        report = recover(self.profiles, self.catalog, self.targets, reader, now=NOW, browser_evidence=[row])
        self.assertEqual(report['eligible_offers'], 1)
        self.assertEqual(report['submitted_offers'], 0)
        wrong = {**row, 'source_title': row['source_title'].replace('128 GB', '256 GB')}
        wrong['proof_quote'] = wrong['source_title'] + '\n28.699 TL\nExample Store\nSepete Ekle'
        with self.assertRaises(ValueError):
            validate_browser_evidence(self.profiles, [wrong], NOW)

    def test_literal_warranty_field_is_supported_without_fabricating_title(self):
        title = self.offer['source_title'].replace('(Samsung Türkiye Garantili)', '').strip()
        row = {**self.offer, 'source_title': title, 'product_id': self.profile['id'],
               'product_slug': self.profile['slug'], 'evidence_method': 'live_browser_selected_pdp',
               'purchase_enabled': True, 'selected_seller': 'Example Store',
               'displayed_cash_price': '28.699 TL',
               'warranty_evidence': 'Garanti Tipi\nSamsung Türkiye Garantili',
               'proof_quote': title + '\n28.699 TL\nExample Store\nSepete Ekle\nGaranti Tipi\nSamsung Türkiye Garantili'}
        validate_browser_evidence(self.profiles, [row], NOW)
        reader, writer = Mock(), Mock()
        reader.refresh_existing_offer_targets.return_value = []
        writer.ingest_offers.return_value = {'committed': True, 'accepted': 1}
        recover(self.profiles, self.catalog, self.targets, reader, writer, NOW, [row])
        self.assertEqual(writer.ingest_offers.call_args.args[0][0]['source_title'], title)
        for changes in [{'warranty_evidence': 'Not on the page'},
                        {'proof_quote': title + '\n28.699 TL\nExample Store\nSepete Ekle'},
                        {'warranty_evidence': 'Garanti Tipi\nİthalatçı Garantili',
                         'proof_quote': title + '\n28.699 TL\nExample Store\nSepete Ekle\nGaranti Tipi\nİthalatçı Garantili'}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_browser_evidence(self.profiles, [{**row, **changes}], NOW)


if __name__ == '__main__':
    unittest.main()
