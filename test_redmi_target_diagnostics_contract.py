"""Authenticated diagnostic page contract for the manual Redmi gate."""

from copy import deepcopy
import unittest

from offer_ingestion_client import IngestionClientError, OfferIngestionClient
from test_redmi_note_15_pro_manual_coverage import REASONS, SNAPSHOT


URL = 'https://www.mediamarkt.com.tr/tr/product/_phone-123456789.html'
SECRET = 's' * 32


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


def diagnostic():
    return {'products': [{
        'id': 238, 'slug': 'xiaomi-redmi-note-15-pro',
        'name': 'Xiaomi Redmi Note 15 Pro',
        'attributes': {'ram_gb': 8, 'storage_gb': 256},
        'offer_targets': [
            {'merchant_name': 'MediaMarkt', 'seller_name': 'Other Seller',
             'source_url': URL.replace('123456789', str(123456789 + index)),
             'checked_at': None}
            for index in range(12)
        ],
        'offer_targets_total': 12, 'offer_targets_invalid_count': 1,
        'offer_targets_duplicate_count': 0,
        'offer_targets_invalid_reasons': deepcopy(REASONS),
        'offer_targets_page': 1, 'offer_targets_truncated': True,
        'offer_targets_snapshot': SNAPSHOT,
    }]}


class RedmiTargetDiagnosticContractTests(unittest.TestCase):
    def client(self, body):
        session = Session(body)
        return (OfferIngestionClient('https://teknoskor.com', SECRET,
                                     session=session, sleep=lambda _: None), session)

    def test_exact_single_page_is_authenticated_and_keeps_only_safe_targets(self):
        client, session = self.client(diagnostic())
        result = client.get_offer_target_inspection(238)
        self.assertEqual(len(result['offer_targets']), 12)
        self.assertEqual(result['offer_targets_invalid_reasons']['unsupported_host'], 1)
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, 'GET')
        self.assertTrue(url.endswith('/api/ingestion/catalog'))
        self.assertEqual(kwargs['headers']['Authorization'], f'Bearer {SECRET}')
        self.assertEqual(kwargs['params']['offer_target_product_id'], '238')
        self.assertEqual(kwargs['params']['offer_target_page'], '1')

    def test_mismatched_count_reason_or_unsafe_target_fails_before_writer(self):
        for drift in ('count', 'reason', 'duplicate', 'unsafe_url', 'snapshot'):
            with self.subTest(drift=drift):
                body = diagnostic()
                row = body['products'][0]
                if drift == 'count':
                    row['offer_targets_total'] = 13
                elif drift == 'reason':
                    row['offer_targets_invalid_reasons']['unsupported_host'] = 0
                elif drift == 'duplicate':
                    row['offer_targets'][1]['source_url'] = row['offer_targets'][0]['source_url']
                elif drift == 'unsafe_url':
                    row['offer_targets'][0]['source_url'] = 'https://www.n11.com/phone?api_key=x'
                else:
                    row['offer_targets_snapshot'] = 'short'
                client, _ = self.client(body)
                with self.assertRaises(IngestionClientError):
                    client.get_offer_target_inspection(238)


if __name__ == '__main__':
    unittest.main()
