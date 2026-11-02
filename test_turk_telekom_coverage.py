"""Read-only catalog/stock-card audit contracts."""

import json
import unittest
from datetime import datetime, timezone

from audit_turk_telekom_coverage import CATALOG_URL, TARGET, run_audit


class Response:
    def __init__(self, rows):
        self.url = CATALOG_URL
        self.status_code = 200
        self.body = json.dumps(rows).encode()
        self.headers = {'Content-Type': 'application/json',
                        'Content-Length': str(len(self.body))}

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset:offset + chunk_size]

    def close(self):
        pass


class Source:
    def __init__(self, selected=True):
        self.selected = selected
        self.reads = []

    def discover(self, name, specs):
        if name == TARGET[2] and specs.get('ram_gb') == 12 and specs.get('storage_gb') == 256:
            return [{'source_url': TARGET[-1]}]
        return []

    def read_offer(self, url):
        self.reads.append(url)
        if not self.selected:
            return None, 'no_selected_cash_offer', 200
        return ({'merchant': 'Türk Telekom eMağaza', 'price': 151999,
                 'currency': 'TRY', 'availability': 'in_stock',
                 'source_title': 'SAMSUNG GALAXY Z FOLD8 ULTRA 12/256GB GKREM',
                 'url': url, 'observed_at': datetime.now(timezone.utc).isoformat()},
                'observed_exact_cash_offer', 200)

    def report(self):
        return {'status': 'ready', 'robots_status': 'allowed',
                'category_requests': 3, 'stock_card_urls': 42,
                'pdp_requests': len(self.reads)}


class CoverageAuditTests(unittest.TestCase):
    def setUp(self):
        self.products = [
            {'id': TARGET[0], 'slug': TARGET[1], 'name': TARGET[2],
             'attributes': {'ram_gb': 12, 'storage_gb': 256},
             'fresh_lowest_price': None},
            {'id': 295, 'slug': 'xiaomi-17t', 'name': 'Xiaomi 17T',
             'attributes': {'ram_gb': 12, 'storage_gb': 256},
             'fresh_lowest_price': 54999},
        ]

    def test_reads_only_exact_unpriced_candidate_and_checks_selected_offer(self):
        source = Source()
        report = run_audit(source, catalog_get=lambda *_args, **_kwargs: Response(self.products))
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['catalog_total'], 2)
        self.assertEqual(report['catalog_unpriced'], 1)
        self.assertEqual(report['matched_models'], 1)
        self.assertEqual(report['verified_unpriced_offers'][0]['price'], 151999)
        self.assertEqual(source.reads, [TARGET[-1]])
        self.assertFalse(report['writes'])

    def test_missing_cash_offer_fails_closed(self):
        source = Source(selected=False)
        report = run_audit(source, catalog_get=lambda *_args, **_kwargs: Response(self.products))
        self.assertEqual(report['status'], 'no_exact_target_offer')
        self.assertEqual(report['verified_unpriced_offers'], [])

    def test_changed_catalog_capacity_stops_before_pdp(self):
        self.products[0]['attributes']['storage_gb'] = 512
        source = Source()
        report = run_audit(source, catalog_get=lambda *_args, **_kwargs: Response(self.products))
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['error'], 'target_catalog_identity_or_variant_changed')
        self.assertEqual(source.reads, [])


if __name__ == '__main__':
    unittest.main()
