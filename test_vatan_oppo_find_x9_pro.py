"""Offline selected-PDP proof and one-SKU writer gates."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from audit_vatan_oppo_find_x9_pro import PROFILE, SPECS, URL, exact_catalog_product, run_audit
from refresh_vatan_oppo_find_x9_pro import ScopedRefreshError, prepare, publish
from test_vatan_category_unpriced import Catalog, TITLES, offers
from vatan_oppo_find_x9_pro_preflight import validated_offer


NOW = datetime(2026, 11, 2, 12, 1, tzinfo=timezone.utc)


def report():
    offer = offers()[0]
    offer['selected_vatan_variant_verified'] = True
    return {
        'purpose': 'read_only_vatan_oppo_find_x9_pro_exact_pdp',
        'checked_at': '2026-11-02T12:00:30Z',
        'status': 'passed', 'catalog_status': 'ready',
        'catalog_variant': {'id': 290, 'slug': 'oppo-find-x9-pro',
                            'name': 'OPPO Find X9 Pro', 'specs': SPECS},
        'catalog_confirmed': True, 'url': URL, 'offer_verified': True,
        'offer': offer,
        'source_attempts': [{'source_url': URL, 'merchant': 'Vatan Bilgisayar',
                             'reason': 'observed_exact_offer', 'http_status': 200}],
        'vatan_source': {'robots_status': 'ready'}, 'writes': False,
    }


class Reader:
    def __init__(self, offer):
        self.offer = offer
        self.vatan_reviewed_source = self

    def refresh_existing_offer_targets(self, name, targets, specs):
        assert name == PROFILE['name'] and specs == SPECS
        assert targets == [{'source_url': URL}]
        return [self.offer] if self.offer else []

    def target_report(self, *_args):
        return [{'source_url': URL, 'merchant': 'Vatan Bilgisayar',
                 'reason': 'observed_exact_offer', 'http_status': 200}]

    def report(self):
        return {'robots_status': 'ready'}


class Client:
    secret = 'x' * 32

    def __init__(self, products=None, ingestion=None, coverage=None):
        self.products = products if products is not None else Catalog().products
        self.ingestion = ingestion if ingestion is not None else {
            'committed': True, 'accepted': 1, 'stale_ignored': 0,
            'variant_rejected': 0,
        }
        self.coverage = coverage if coverage is not None else {
            'slug': PROFILE['slug'], 'matchedMerchants': 1,
            'matchedLowestPrice': 104999,
        }
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
        return '2026-11-02T12:01:00Z'

    def live_offer_is_valid(self, product, offer, specs):
        return (product['id'] == 290 and offer['url'] == URL and specs == SPECS
                and offer['selected_vatan_variant_verified'] is True)

    def prepare_product_offers(self, product, found, _observed_at, checked_at):
        offer = found[0]
        return [{'product_id': product['id'], 'product_slug': product['slug'],
                 'seller': offer['merchant'], 'price': offer['price'],
                 'currency': offer['currency'], 'availability': offer['availability'],
                 'source_url': offer['url'], 'source_title': offer['source_title'],
                 'observed_at': offer['observed_at'], 'checked_at': checked_at}]


class OppoScopedTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'audit.json'
        self.path.write_text(json.dumps(report()))

    def test_catalog_identity_requires_exact_live_memory_and_radio(self):
        self.assertEqual(exact_catalog_product(Catalog().products)['id'], 290)
        for changed in ({'slug': 'other'},
                        {'attributes': {'ram_gb': 8, 'storage_gb': 512,
                                        'Connectivity': {'5G': True}}},
                        {'attributes': {'ram_gb': 16, 'storage_gb': 512}}):
            products = Catalog().products
            products[0] = {**products[0], **changed}
            self.assertIsNone(exact_catalog_product(products))

    def test_audit_needs_selected_vatan_variant_gate(self):
        current = offers()[0]
        current['observed_at'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        current['selected_vatan_variant_verified'] = True
        self.assertEqual(run_audit(Reader(current), Catalog())['status'], 'passed')
        self.assertEqual(run_audit(Reader({**current,
                                           'selected_vatan_variant_verified': False}),
                                   Catalog())['status'], 'partial')
        self.assertEqual(run_audit(Reader({**current, 'url': URL.replace('16-512', '8-256')}),
                                   Catalog())['status'], 'partial')

    def test_preflight_rejects_tampered_proof_and_old_observation(self):
        self.assertEqual(validated_offer(report(), NOW)['price'], 104999)
        changes = (
            lambda row: row.update(writes=True),
            lambda row: row.update(status='partial'),
            lambda row: row.update(checked_at='2026-11-02T11:54:00Z'),
            lambda row: row.update(catalog_confirmed=False),
            lambda row: row.update(url=URL.replace('16-512', '8-256')),
            lambda row: row['source_attempts'][0].update(http_status=403),
            lambda row: row['offer'].update(merchant='Other'),
            lambda row: row['offer'].update(selected_vatan_variant_verified=False),
            lambda row: row['offer'].update(source_title=TITLES[0].replace('16/512', '8/256')),
            lambda row: row['offer'].update(observed_at='2026-11-02T11:54:00Z'),
        )
        for change in changes:
            candidate = deepcopy(report())
            change(candidate)
            self.assertIsNone(validated_offer(candidate, NOW))

    def test_dry_run_one_record_then_accepted_one_and_readback(self):
        client = Client()
        _, record = prepare(self.path, client=client, updater=Updater(), now=NOW)
        self.assertEqual(record['product_id'], 290)
        self.assertEqual(record['source_title'], TITLES[0])
        self.assertEqual(client.submitted, [])
        result, posted, readback = publish(self.path, client=client,
                                           updater=Updater(), now=NOW)
        self.assertEqual(result['accepted'], 1)
        self.assertEqual(client.submitted, [[posted]])
        self.assertEqual(readback['matched_lowest_price'], 104999)

    def test_writer_rejects_ingestion_and_readback_failure(self):
        for response in ({'committed': True, 'accepted': 0, 'stale_ignored': 1,
                          'variant_rejected': 0},
                         {'committed': True, 'accepted': 0, 'stale_ignored': 0,
                          'variant_rejected': 1}):
            with self.assertRaisesRegex(ScopedRefreshError, 'not_accepted'):
                publish(self.path, client=Client(ingestion=response),
                        updater=Updater(), now=NOW)
        with self.assertRaisesRegex(ScopedRefreshError, 'commercially_visible'):
            publish(self.path, client=Client(coverage={
                'slug': PROFILE['slug'], 'matchedMerchants': 0,
                'matchedLowestPrice': None}), updater=Updater(), now=NOW)
        products = Catalog().products
        products[0] = {**products[0], 'attributes': {'ram_gb': 8,
                                                      'storage_gb': 512,
                                                      'Connectivity': {'5G': True}}}
        client = Client(products=products)
        with self.assertRaisesRegex(ScopedRefreshError, 'catalog_oppo_variant_changed'):
            publish(self.path, client=client, updater=Updater(), now=NOW)
        self.assertEqual(client.submitted, [])


if __name__ == '__main__':
    unittest.main()
