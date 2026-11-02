import unittest
from datetime import datetime, timezone

from audit_xiaomi_17t_first_party import REVIEWED, run_audit


class Response:
    status_code = 200
    headers = {}

    def __init__(self, profile):
        self.profile = profile

    def iter_content(self, chunk_size):
        import json
        id, slug, name, ram, storage, *_ = self.profile
        yield json.dumps({'id': id, 'slug': slug, 'name': name,
                          'attributes': {'ram_gb': ram, 'storage_gb': storage,
                                         'Connectivity': {'5G': 'n1 (2100)'}}}).encode()

    def close(self):
        pass


class Reader:
    def __init__(self, changes=None):
        self.changes = changes or {}
        self.mediamarkt_source = self

    def refresh_existing_offer_targets(self, name, targets, specs):
        row = next(p for p in REVIEWED if p[2] == name)
        assert specs == {'ram_gb': row[3], 'storage_gb': row[4], 'network_generation': row[5]}
        assert targets == [{'source_url': row[6]}]
        offer = {'merchant': 'MediaMarkt', 'price': 62999 if row[1].endswith('pro') else 54999,
                 'currency': 'TRY', 'availability': 'in_stock',
                 'source_title': f'{name} {row[3]}/{row[4]}GB Akıllı Telefon {row[7]}',
                 'url': row[6],
                 'observed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}
        return [{**offer, **self.changes.get(name, {})}]

    def target_report(self, _name, targets, _specs):
        return [{'source_url': targets[0]['source_url'], 'merchant': 'MediaMarkt',
                 'reason': 'observed_exact_offer', 'http_status': 200}]

    def report(self):
        return {'robots_status': 'ready', 'browser_requests': 0, 'browser_html_responses': 0}


def catalog_get(url, **_kwargs):
    return Response(next(row for row in REVIEWED if url.endswith('/' + str(row[0]))))


class Xiaomi17TFirstPartyAuditTests(unittest.TestCase):
    def test_exact_catalog_and_selected_first_party_offers(self):
        report = run_audit(Reader(), catalog_get)
        self.assertEqual(report['verified_products'], 2)
        self.assertFalse(report['writes'])

    def test_seller_color_url_currency_stock_and_cash_price_fail_closed(self):
        for change in ({'merchant': 'Başaran Ticaret'},
                       {'source_title': 'Xiaomi 17T Pro 12/512GB Akıllı Telefon Siyah'},
                       {'url': REVIEWED[0][6].replace('1254681', '1254676')},
                       {'currency': 'USD'}, {'availability': 'out_of_stock'},
                       {'price': 0}):
            with self.subTest(change=change):
                report = run_audit(Reader({'Xiaomi 17T Pro': change}), catalog_get)
                self.assertEqual(report['verified_products'], 1)

    def test_catalog_capacity_drift_prevents_pdp_probe(self):
        def drifted(url, **kwargs):
            response = catalog_get(url, **kwargs)
            if url.endswith('/270'):
                response.profile = (*response.profile[:4], 256, *response.profile[5:])
            return response
        report = run_audit(Reader(), drifted)
        self.assertEqual(report['verified_products'], 1)
        self.assertEqual(report['products'][0]['catalog_reason'], 'catalog_identity_or_variant_changed')
        self.assertEqual(report['products'][0]['source_attempts'], [])


if __name__ == '__main__':
    unittest.main()
