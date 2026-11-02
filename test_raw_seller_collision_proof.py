"""Raw seller proof is read-only and independent of filtered target hints."""

import unittest
from unittest.mock import patch

from coverage_two_marketplace import PROFILES, audit
from offer_ingestion_client import IngestionClientError, OfferIngestionClient
from test_coverage_two_marketplace import Client, Source, offers


SECRET = 's' * 32


def proof(product_id, *, checked=13, invalid=1, invalid_other=1,
          unknown=0, conflict=0, complete=True):
    reasons = (["stored_row_limit_exceeded"] if not complete else []) + (
        ["unknown_seller_identity"] if unknown else []) + (
        ["proposed_seller_on_other_url_or_platform"] if conflict else [])
    return {'purpose': 'read_only_raw_seller_collision_proof', 'writes': False,
            'product_id': product_id, 'complete': complete,
            'blocked': bool(reasons), 'reasons': reasons,
            'stored_rows_checked': checked, 'unknown_seller_rows': unknown,
            'invalid_url_rows': invalid,
            'invalid_url_other_seller_rows': invalid_other,
            'invalid_url_proposed_seller_rows': 0,
            'proposed_seller_other_url_rows': conflict,
            'exact_seller_url_rows': 0,
            'invalid_row_identity_rows': 0,
            'refresh_targets_total': checked - invalid,
            'refresh_targets_invalid_count': invalid,
            'refresh_targets_duplicate_count': 0,
            'refresh_targets_invalid_reasons': {
                'missing_or_oversize_url': 0, 'malformed_url': 0,
                'non_https_url': 0, 'unsupported_host': 0,
                'credentials_or_port': 0, 'retailer_query_restricted': 0,
                'sensitive_query': invalid, 'missing_product_path': 0,
                'seller_identity': 0,
            },
            'refresh_targets_snapshot': 'a' * 64}


class Response:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class Session:
    def __init__(self, body):
        self.body = body
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return Response(self.body)


class RawSellerProofTests(unittest.TestCase):
    def test_authenticated_read_accepts_complete_invalid_historical_other_seller(self):
        url, profile = next(iter(PROFILES.items()))
        session = Session(proof(profile['id']))
        client = OfferIngestionClient('https://teknoskor.com', SECRET,
                                      session=session, sleep=lambda _: None)
        result = client.get_raw_seller_collision_proof(
            profile['id'], profile['seller_name'], url)
        self.assertIs(result['blocked'], False)
        self.assertEqual(result['stored_rows_checked'], 13)
        self.assertEqual(result['invalid_url_other_seller_rows'], 1)
        method, request_url, kwargs = session.calls[0]
        self.assertEqual(method, 'GET')
        self.assertTrue(request_url.endswith('/api/ingestion/offer-target-collision'))
        self.assertEqual(kwargs['headers']['Authorization'], f'Bearer {SECRET}')
        self.assertEqual(kwargs['params']['source_url'], url)

    def test_unknown_or_conflicting_seller_blocks_and_inconsistent_safe_claim_fails(self):
        url, profile = next(iter(PROFILES.items()))
        for changes in ({'unknown': 1}, {'conflict': 1}):
            session = Session(proof(profile['id'], **changes))
            client = OfferIngestionClient('https://teknoskor.com', SECRET,
                                          session=session, sleep=lambda _: None)
            self.assertIs(client.get_raw_seller_collision_proof(
                profile['id'], profile['seller_name'], url)['blocked'], True)
        forged = proof(profile['id'], unknown=1)
        forged['blocked'] = False
        client = OfferIngestionClient('https://teknoskor.com', SECRET,
                                      session=Session(forged), sleep=lambda _: None)
        with self.assertRaisesRegex(IngestionClientError, 'inconsistent'):
            client.get_raw_seller_collision_proof(
                profile['id'], profile['seller_name'], url)

    def test_refresh_counts_and_rejection_reasons_must_cover_the_same_raw_rows(self):
        url, profile = next(iter(PROFILES.items()))
        for changed in ('count', 'reason', 'snapshot'):
            with self.subTest(changed=changed):
                body = proof(profile['id'])
                if changed == 'count':
                    body['refresh_targets_total'] = 11
                elif changed == 'reason':
                    body['refresh_targets_invalid_reasons']['sensitive_query'] = 0
                else:
                    body['refresh_targets_snapshot'] = 'invalid'
                client = OfferIngestionClient('https://teknoskor.com', SECRET,
                                              session=Session(body),
                                              sleep=lambda _: None)
                with self.assertRaisesRegex(IngestionClientError, 'invalid'):
                    client.get_raw_seller_collision_proof(
                        profile['id'], profile['seller_name'], url)

    def test_safe_raw_proof_does_not_clear_existing_complete_target_gate(self):
        class ProofClient(Client):
            def get_complete_offer_targets(self, product_id):
                raise IngestionClientError('Complete target source has invalid rows')

            def get_raw_seller_collision_proof(self, product_id, seller, url):
                return proof(product_id)

        client = ProofClient()
        client.rows[0]['offer_targets'] = [
            {'merchant_name': 'MediaMarkt', 'seller_name': 'Other Seller',
             'source_url': f'https://www.mediamarkt.com.tr/tr/product/_phone-{i}.html'}
            for i in range(12)]
        client.rows[0]['offer_targets_truncated'] = True
        source = Source()
        selected = offers()
        with patch('coverage_two_marketplace.selected_marketplace_offer',
                   side_effect=lambda body, url: selected[url]):
            report = audit(client=client, source=source)
        slug = next(iter(PROFILES.values()))['slug']
        self.assertTrue(report['raw_seller_collisions'][slug]['available'])
        self.assertFalse(report['raw_seller_collisions'][slug]['proof']['blocked'])
        self.assertTrue(report['catalog_collisions'][slug]['blocked'])
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(client.writes, [])


if __name__ == '__main__':
    unittest.main()
