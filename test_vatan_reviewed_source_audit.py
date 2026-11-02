"""The runner pilot calls the production reader and never an ingestion client."""

import unittest

from audit_vatan_reviewed_source import format_summary, run_audit
from vatan_reviewed_source import reviewed_profiles


class FakeSource:
    def report(self):
        return {'robots_status': 'ready', 'browser_requests': 6,
                'reviewed_urls': 6, 'browser_html_responses': 6}


class FakeReader:
    def __init__(self):
        self.vatan_reviewed_source = FakeSource()
        self.calls = []

    def refresh_existing_offer_targets(self, name, targets, specs):
        self.calls.append((name, targets, specs))
        return [{'merchant': 'Vatan Bilgisayar', 'price': 1, 'currency': 'TRY',
                 'availability': 'in_stock', 'url': targets[0]['source_url']}]

    def target_report(self, _name, targets, _specs):
        return [{'source_url': targets[0]['source_url'],
                 'reason': 'observed_exact_offer', 'http_status': 200}]


class AuditTests(unittest.TestCase):
    def test_all_six_profiles_use_production_target_and_reader_path(self):
        reader = FakeReader()
        report = run_audit(reader)
        self.assertEqual(report['purpose'], 'read_only_reviewed_vatan_production_reader_pilot')
        self.assertEqual(report['verified_products'], 6)
        self.assertEqual(len(reader.calls), len(reviewed_profiles()))
        for profile, (name, targets, specs) in zip(reviewed_profiles(), reader.calls):
            self.assertEqual(name, profile['name'])
            self.assertEqual([row['source_url'] for row in targets], profile['targets'])
            self.assertEqual(specs['network_generation'], profile['network_generation'])
        self.assertIn('Verified 6/6', format_summary(report))


if __name__ == '__main__':
    unittest.main()
