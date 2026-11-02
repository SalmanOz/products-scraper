"""Offline same-run preflight, one-record writer and commercial readback gates."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from audit_vatan_vivo_exact_readonly import (PROFILE, SELECTED_H1, SPECS, URL,
                                             exact_catalog_product, run_audit)
from refresh_vatan_vivo_x300 import ScopedRefreshError, prepare, publish
from test_vatan_vivo_exact_readonly import Catalog, Reader
from vatan_vivo_x300_preflight import validated_offer


NOW = datetime(2026, 11, 2, 16, 45, tzinfo=timezone.utc)


def report():
    return run_audit(Reader(), Catalog(), NOW)


class Client:
    secret = 'x' * 32

    def __init__(self, products=None, ingestion=None, coverage=None):
        self.products = products if products is not None else [deepcopy(Catalog().product)]
        self.ingestion = ingestion if ingestion is not None else {
            'committed': True, 'accepted': 1, 'stale_ignored': 0,
            'variant_rejected': 0}
        self.coverage = coverage if coverage is not None else {
            'slug': PROFILE['slug'], 'matchedMerchants': 1,
            'matchedLowestPrice': 75999,
            'matchedOffers': [{'merchant_name': 'Vatan Bilgisayar',
                               'source_url': URL, 'price': 75999}]}
        self.submitted = []

    def get_published_products(self):
        return self.products

    def ingest_offers(self, records):
        self.submitted.append(records)
        self.products[0]['offer_targets'] = [{'source_url': URL}]
        return self.ingestion

    def _request_json(self, method, path, **kwargs):
        assert method == 'GET' and path == '/api/ingestion/price-index-readiness'
        assert kwargs['headers']['Authorization'] == 'Bearer ' + self.secret
        return {'offerIdentity': {'productCoverage': [self.coverage]}}


class Updater:
    def utc_timestamp(self):
        return '2026-11-02T16:45:00Z'

    def live_offer_is_valid(self, product, offer, specs):
        return (product['id'] == PROFILE['id'] and offer['url'] == URL
                and offer['source_title'] == SELECTED_H1 and specs == SPECS
                and offer['selected_vatan_variant_verified'] is True)

    def prepare_product_offers(self, product, found, _observed_at, checked_at):
        offer = found[0]
        return [{'product_id': product['id'], 'product_slug': product['slug'],
                 'seller': offer['merchant'], 'price': offer['price'],
                 'currency': offer['currency'], 'availability': offer['availability'],
                 'source_url': offer['url'], 'source_title': offer['source_title'],
                 'observed_at': offer['observed_at'], 'checked_at': checked_at}]


class VivoScopedTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'audit.json'
        self.path.write_text(json.dumps(report()))

    def test_catalog_and_same_run_preflight_are_exact(self):
        self.assertEqual(exact_catalog_product([Catalog().product])['id'], 286)
        self.assertEqual(validated_offer(report(), NOW)['price'], 75999)
        for change in (
                lambda row: row.update(writes=True),
                lambda row: row.update(status='partial'),
                lambda row: row.update(checked_at='2026-11-02T16:39:00Z'),
                lambda row: row.update(catalog_confirmed=False),
                lambda row: row.update(url=URL + '?seller=other'),
                lambda row: row['source_attempts'][0].update(http_status=403),
                lambda row: row['offer'].update(merchant='Other'),
                lambda row: row['offer'].update(selected_vatan_variant_verified=False),
                lambda row: row['offer'].update(source_title='Vivo X300 8/256GB'),
                lambda row: row['offer'].update(observed_at='2026-11-02T16:39:00Z')):
            candidate = deepcopy(report())
            change(candidate)
            self.assertIsNone(validated_offer(candidate, NOW))

    def test_dry_run_then_exact_one_offer_write_and_readback(self):
        client = Client()
        _, record = prepare(self.path, client=client, updater=Updater(), now=NOW)
        self.assertEqual(record['product_id'], 286)
        self.assertEqual(record['source_title'], SELECTED_H1)
        self.assertEqual(client.submitted, [])
        result, posted, readback = publish(self.path, client=client,
                                           updater=Updater(), now=NOW)
        self.assertEqual(result['accepted'], 1)
        self.assertEqual(client.submitted, [[posted]])
        self.assertEqual(readback['exact_offer_price'], 75999)
        self.assertEqual(readback['matched_lowest_price'], 75999)

    def test_rejects_stale_or_variant_ingestion_and_wrong_readback(self):
        for response in ({'committed': True, 'accepted': 0, 'stale_ignored': 1,
                          'variant_rejected': 0},
                         {'committed': True, 'accepted': 0, 'stale_ignored': 0,
                          'variant_rejected': 1}):
            with self.assertRaisesRegex(ScopedRefreshError, 'not_accepted'):
                publish(self.path, client=Client(ingestion=response),
                        updater=Updater(), now=NOW)
        base = Client().coverage
        for changed in (
                {'matchedOffers': []},
                {'matchedOffers': [{'merchant_name': 'Other', 'source_url': URL,
                                    'price': 75999}]},
                {'matchedOffers': [{'merchant_name': 'Vatan Bilgisayar',
                                    'source_url': URL + '?seller=other',
                                    'price': 75999}]},
                {'matchedOffers': [{'merchant_name': 'Vatan Bilgisayar',
                                    'source_url': URL, 'price': 74999}]},
                {'matchedOffers': base['matchedOffers'] * 2}):
            with self.subTest(changed=changed), self.assertRaisesRegex(
                    ScopedRefreshError, 'commercially_visible'):
                publish(self.path, client=Client(coverage={**base, **changed}),
                        updater=Updater(), now=NOW)
        changed = {**Catalog().product, 'attributes': {
            'ram_gb': 8, 'storage_gb': 512, 'Connectivity': {'5G': True}}}
        client = Client(products=[changed])
        with self.assertRaisesRegex(ScopedRefreshError, 'catalog_vivo_variant_changed'):
            publish(self.path, client=client, updater=Updater(), now=NOW)
        self.assertEqual(client.submitted, [])


if __name__ == '__main__':
    unittest.main()
