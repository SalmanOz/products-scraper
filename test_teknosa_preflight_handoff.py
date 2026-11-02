import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from audit_teknosa_first_party import run_audit
from teknosa_reviewed_source import TeknosaReviewedSource, URL, parse_exact_first_party_offer
from test_teknosa_reviewed_source import page
from verify_teknosa_scoped_refresh import validate_scoped_refresh


class PreflightHandoffTests(unittest.TestCase):
    def audit(self):
        class Reader:
            def read_offer(self, url):
                offer, reason = parse_exact_first_party_offer(page(), url)
                return offer, reason, 200
        report = run_audit(Reader())
        self.assertTrue(report['verified'])
        self.assertIs(report['writes'], False)
        return report

    def reader(self, audit):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'teknosa-audit.json'
        path.write_text(json.dumps(audit))
        forbidden_network = Mock(side_effect=AssertionError('preflight must not refetch'))
        return TeknosaReviewedSource(
            get=forbidden_network, post=forbidden_network, audit_file=path,
            now=lambda: datetime.now(timezone.utc)), forbidden_network

    def test_same_run_exact_preflight_reuses_actual_observation_without_network(self):
        audit = self.audit()
        reader, network = self.reader(audit)
        offer, reason, status = reader.read_offer(URL)
        self.assertEqual((reason, status, offer['price']),
                         ('observed_exact_first_party_cash_offer_from_preflight', 200, 68999.0))
        self.assertEqual(offer['observed_at'], audit['offer']['observed_at'])
        network.assert_not_called()

    def test_old_or_tampered_preflight_fails_closed(self):
        baseline = self.audit()
        cases = [
            {'checked_at': (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()},
            {'catalog_id': 247}, {'slug': 'apple-iphone-17'},
            {'name': 'Apple iPhone 17'}, {'specs': {'ram_gb': 8, 'storage_gb': 128,
                                                  'network_generation': '5g'}},
            {'http_status': 403}, {'verified': False},
            {'offer': {**baseline['offer'], 'merchant': 'Kamer Online'}},
            {'offer': {**baseline['offer'], 'source_title':
                       'Apple iPhone 17e 512GB Beyaz Akıllı Telefon'}},
            {'offer': {**baseline['offer'], 'observed_at':
                       (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()}},
        ]
        for change in cases:
            with self.subTest(change=change):
                reader, network = self.reader({**baseline, **change})
                self.assertEqual(reader.read_offer(URL),
                                 (None, 'preflight_invalid_or_stale', None))
                network.assert_not_called()

    def test_scoped_gate_requires_teknosa_in_prepared_and_accepted_records(self):
        audit = self.audit()
        offer = audit['offer']
        prepared = {
            'product_id': 248, 'product_slug': 'apple-iphone-17e',
            'seller': 'Teknosa', 'price': offer['price'],
            'source_url': URL, 'availability': 'in_stock', 'currency': 'TRY',
            'source_title': offer['source_title'],
            'observed_at': offer['observed_at'], 'checked_at': offer['observed_at'],
        }
        writer = {
            'status': 'completed', 'mode': 'apply', 'known_pdp_only': True,
            'products': [{
                'slug': 'apple-iphone-17e',
                'source_attempts': [{'source_url': URL,
                                     'reason': 'observed_exact_first_party_cash_offer_from_preflight',
                                     'http_status': 200}],
                'prepared_offers': [prepared],
            }],
            'ingestion': {'committed': True, 'accepted': 1,
                          'stale_ignored': 0, 'variant_rejected': 0},
        }
        self.assertEqual(validate_scoped_refresh(audit, writer),
                         (True, 'exact Teknosa offer committed'))
        failures = []
        missing = deepcopy(writer)
        missing['products'][0]['prepared_offers'] = [{**prepared, 'seller': 'Hepsiburada'}]
        failures.append(missing)
        transport_error = deepcopy(writer)
        transport_error['products'][0]['source_attempts'][0]['reason'] = 'robots_solver_http_500'
        failures.append(transport_error)
        stale = deepcopy(writer)
        stale['ingestion']['accepted'] = 0
        stale['ingestion']['stale_ignored'] = 1
        failures.append(stale)
        for case in failures:
            with self.subTest(case=case):
                self.assertFalse(validate_scoped_refresh(audit, case)[0])


if __name__ == '__main__':
    unittest.main()
