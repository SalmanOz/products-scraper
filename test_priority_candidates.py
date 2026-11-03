from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from hepsiburada_catalog_source import HepsiburadaCatalogSource, parse_catalog_buy_box
from publish_priority_candidates import refresh_candidates
from retailer_offer_refresh import RetailerOfferRefresher
from test_hepsiburada_catalog_source import document, TARGET, NAME, SPECS


class Client:
    def __init__(self):
        self.product = {'id': 999, 'slug': 'samsung-galaxy-a36-5g', 'name': NAME,
                        'attributes': {**SPECS, 'Connectivity': {'5G': True}},
                        'offer_targets': [{'merchant_name': 'Hepsiburada', 'source_url': TARGET}],
                        'offer_targets_truncated': False}
        self.writes, self.scores = [], 0
        self.existing = []

    def get_published_products(self):
        return [deepcopy(self.product)]

    def ingest_offers(self, records, *, require_all_accepted=False):
        assert require_all_accepted is True
        self.writes.append(deepcopy(records))
        return {'committed': True, 'accepted': len(records), 'stale_ignored': 0, 'variant_rejected': 0}

    def reconcile_scores(self):
        self.scores += 1
        return {'total': 1, 'scored': 0, 'skipped': 1}

    def get_offer_identity_readiness(self):
        records = self.existing + [r for batch in self.writes for r in batch]
        return {'evidenceVersion': 'product-offer-match-v1', 'mode': 'shadow_audit_only',
                'auditedAt': datetime.now(timezone.utc).isoformat(),
                'productCoverage': [{'slug': self.product['slug'], 'matchedOffers': [
                    {'source_url': r['source_url'], 'merchant_name': r['seller'],
                     'price': int(r['price']) if float(r['price']).is_integer() else r['price']}
                    for r in records]}]}


def audit():
    offer, _ = parse_catalog_buy_box(document(), TARGET, NAME, SPECS)
    return {'purpose': 'automatic_priority_retailer_diagnosis', 'writes': False,
            'catalog_count': 1, 'rows': [{'slug': 'samsung-galaxy-a36-5g',
                'retailer': 'Hepsiburada', 'reason': 'document_read', 'http_status': 200,
                'prepared_offer': offer}]}


def reader():
    class Transport:
        def read_document(self, url):
            return document(), 'document_read', 200
    return RetailerOfferRefresher(hepsiburada_catalog_source=HepsiburadaCatalogSource(transport=Transport()))


class PriorityCandidateTests(unittest.TestCase):
    def test_real_source_reread_and_all_live_gates_precede_one_atomic_batch(self):
        client = Client()
        result = refresh_candidates(audit(), client=client, reader=reader())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual((len(client.writes), len(client.writes[0]), client.scores), (1, 1, 1))
        self.assertEqual(client.writes[0][0]['source_url'], TARGET)
        self.assertEqual(client.writes[0][0]['source_title'], audit()['rows'][0]['prepared_offer']['source_title'])
        self.assertTrue(result['commercial_readback_verified'])

    def test_same_price_recently_published_by_scheduled_run_is_not_written_again(self):
        client, report = Client(), audit()
        offer = report['rows'][0]['prepared_offer']
        client.product['offer_targets'] = [{'source_url': TARGET, 'merchant_name': offer['merchant'],
                                           'checked_at': offer['observed_at']}]
        client.existing = [{'source_url': TARGET, 'seller': offer['merchant'], 'price': offer['price']}]
        result = refresh_candidates(report, client=client, reader=reader())
        self.assertEqual(result['status'], 'already_current')
        self.assertEqual(len(result['already_current']), 1)
        self.assertEqual((client.writes, client.scores), ([], 0))

    def test_price_drift_unknown_seller_incomplete_targets_and_catalog_drift_never_write(self):
        for kind in ('price', 'unknown_seller', 'incomplete', 'catalog_name'):
            client, report = Client(), audit()
            if kind == 'price':
                report['rows'][0]['prepared_offer']['price'] -= 1
            elif kind == 'unknown_seller':
                client.product['offer_targets'][0]['merchant_name'] = ''
            elif kind == 'incomplete':
                client.product['offer_targets_truncated'] = True
            else:
                client.product['name'] = 'Samsung Galaxy S26'
            with self.subTest(kind=kind):
                result = refresh_candidates(report, client=client, reader=reader())
                self.assertEqual(result['status'], 'no_verified_candidates')
                self.assertEqual((client.writes, client.scores), ([], 0))

    def test_committed_but_missing_commercial_readback_is_reported_as_failure(self):
        class MissingReadback(Client):
            def get_offer_identity_readiness(self):
                result = super().get_offer_identity_readiness()
                result['productCoverage'][0]['matchedOffers'] = []
                return result
        client = MissingReadback()
        with self.assertRaisesRegex(ValueError, 'not_visible'):
            refresh_candidates(audit(), client=client, reader=reader())
        self.assertEqual(len(client.writes), 1)

    def test_selected_sku_radio_proof_is_carried_with_the_unchanged_ambiguous_heading(self):
        from test_trendyol_catalog_source import document as ty_document, URL as ty_url
        from trendyol_catalog_source import TrendyolCatalogSource, parse_catalog_listing
        html = ty_document().replace('A54 5G', 'A15').replace('a54-5g', 'a15')
        url = ty_url.split('?')[0].replace('a54-5g', 'a15')
        name = 'Samsung Galaxy A15'
        observed, _ = parse_catalog_listing(html, url, name, SPECS)
        self.assertIsNotNone(observed)  # Actual SKU attributes prove the radio.
        class Transport:
            def read_document(self, target):
                return html, 'document_read', 200
        source = RetailerOfferRefresher(trendyol_catalog_source=TrendyolCatalogSource(transport=Transport()))
        client = Client()
        client.product.update(name=name, slug='samsung-galaxy-a15-5g', offer_targets=[
            {'merchant_name': 'Trendyol', 'source_url': url}])
        report = audit()
        report['rows'] = [{'slug': client.product['slug'], 'retailer': 'Trendyol',
                           'reason': 'document_read', 'http_status': 200, 'prepared_offer': observed}]
        result = refresh_candidates(report, client=client, reader=source)
        self.assertEqual(result['status'], 'completed')
        written = client.writes[0][0]
        self.assertEqual(written['source_title'], observed['source_title'])
        self.assertNotIn('5G', written['source_title'])
        self.assertEqual(written['source_variant_evidence']['network_generation'], '5g')
        self.assertEqual(written['source_variant_evidence']['sku'], str(observed['item_number']))

    def test_stale_duplicate_cross_platform_and_protected_a37_candidates_abort_before_writes(self):
        for kind in ('stale', 'duplicate', 'host', 'a37', 'catalog_count', 'writes', 'too_many'):
            client, report = Client(), audit()
            if kind == 'stale':
                report['rows'][0]['prepared_offer']['observed_at'] = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
            elif kind == 'duplicate':
                report['rows'].append(deepcopy(report['rows'][0]))
            elif kind == 'host':
                report['rows'][0]['prepared_offer']['url'] = 'https://attacker.example/phone'
            elif kind == 'a37':
                client.product['slug'] = report['rows'][0]['slug'] = 'samsung-galaxy-a37-5g'
            elif kind == 'catalog_count':
                report['catalog_count'] = 110
            elif kind == 'writes':
                report['writes'] = True
            else:
                report['rows'] = report['rows'] * 25
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                refresh_candidates(report, client=client, reader=reader())
            self.assertEqual((client.writes, client.scores), ([], 0))


if __name__ == '__main__':
    unittest.main()
