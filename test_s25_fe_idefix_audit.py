"""Offline contract tests for the one-URL idefix diagnostic runner."""

import json
import unittest

from audit_s25_fe_idefix import ROBOTS_URL, URL, audit_html, run_audit


TITLE = 'Samsung Galaxy S25 Fe 256 Gb 8 Gb Ram Buz Mavisi (Samsung Türkiye Garantili)'


class Response:
    def __init__(self, text, url, status=200):
        self.text, self.content, self.url, self.status_code = (
            text, text.encode('utf-8'), url, status)


def page(price=37919.04):
    schema = {'@type': 'Product', 'name': TITLE, 'sku': '8806097662235',
              'offers': {'@type': 'Offer', 'url': URL, 'priceCurrency': 'TRY',
                         'price': price, 'availability': 'https://schema.org/InStock',
                         'priceSpecification': {'@type': 'UnitPriceSpecification',
                                                'priceCurrency': 'TRY', 'price': 39499,
                                                'priceType': 'https://schema.org/ListPrice'}}}
    return ('<html><h1>' + TITLE + '</h1><main>Sepette İndirim 37.919,04 TL '
            '39.499,00 TL Sepete Ekle</main><script type="application/ld+json">'
            + json.dumps(schema, ensure_ascii=False) + '</script></html>')


class IdefixS25FeAuditTests(unittest.TestCase):
    def test_basket_discount_is_diagnostic_only(self):
        observed = audit_html(page())
        self.assertEqual(observed['status'], 'basket_discount_no_publishable_cash_offer')
        self.assertEqual(observed['schema_offer_price'], 37919.04)
        self.assertEqual(observed['schema_list_price'], 39499)
        self.assertTrue(observed['generic_reader_rejected'])
        self.assertEqual(observed['seller_identity'], 'unverified')

    def test_robots_gate_and_read_only_report(self):
        calls = []
        def fetch(url, **_kwargs):
            calls.append(url)
            return Response('User-agent: *\nDisallow:\n' if url == ROBOTS_URL else page(), url)
        result = run_audit(fetch=fetch, sleep=lambda _: None)
        self.assertEqual(calls, [ROBOTS_URL, URL])
        self.assertEqual(result['status'], 'basket_discount_no_publishable_cash_offer')
        self.assertEqual(result['robots_status'], 'ready')
        self.assertIs(result['writes'], False)

        calls.clear()
        def denied(url, **_kwargs):
            calls.append(url)
            return Response('User-agent: *\nDisallow: /\n', url)
        blocked = run_audit(fetch=denied, sleep=lambda _: None)
        self.assertEqual(calls, [ROBOTS_URL])
        self.assertEqual(blocked['robots_status'], 'disallowed')
        self.assertEqual(blocked['status'], 'not_attempted')

    def test_http_denial_never_uses_cached_or_third_party_price(self):
        def fetch(url, **_kwargs):
            if url == ROBOTS_URL:
                return Response('User-agent: *\nDisallow:\n', url)
            return Response('Forbidden', url, status=403)
        result = run_audit(fetch=fetch, sleep=lambda _: None)
        self.assertEqual(result['status'], 'http_403')
        self.assertEqual(result['http_status'], 403)
        self.assertNotIn('schema_offer_price', result)


if __name__ == '__main__':
    unittest.main()
