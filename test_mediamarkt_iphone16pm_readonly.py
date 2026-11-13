"""Scoped acceptance and rejection tests for the iPhone 16 Pro Max research-only audit."""

import unittest
from unittest.mock import patch

from audit_mediamarkt_iphone16pm_readonly import (
    CATALOG, LEGAL_NAME, MERSIS_NUMBER, SELLER_URL, TAX_NUMBER, URL,
    exact_iphone16pm_offer, exact_seller_identity, run_audit,
)


EVIDENCE = {
    'platform': 'MediaMarkt', 'seller_name': 'Altınkoza Teknolojim',
    'seller_id': '12945', 'price': 125874.05, 'currency': 'TRY',
    'availability': 'in_stock', 'condition': 'new',
    'source_title': 'APPLE iPhone 16 Pro Max 8 GB 256 GB Akıllı Telefon Beyaz Titanyum',
    'sku': '147052697', 'gtin13': '0195949805981', 'offer_id': '1823637090',
    'selected_variant_gtin13': '0195949805981',
    'selected_variant_sku': '1239589',
    'url': URL, 'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g',
}
SELLER_HTML = (f'<main>Firma adı: {LEGAL_NAME} Şehir: Şanlıurfa '
               f'MERSİS No: {MERSIS_NUMBER} Vergi No: {TAX_NUMBER}</main>')


class Transport:
    def __init__(self, *, allowed=True, seller_html=SELLER_HTML,
                 pdp_status=200, browser_url=URL, browser_reason='ready'):
        self._rules = None
        self.allowed = allowed
        self.seller_html = seller_html
        self.pdp_status = pdp_status
        self.browser_url = browser_url
        self.browser_reason = browser_reason
        self.urls = []
        self.browser_urls = []

    def _load_robots(self):
        self._rules = self

    def can_fetch(self, bot, url):
        return self.allowed and url in (URL, SELLER_URL)

    def _direct(self, url, max_bytes, accept):
        self.urls.append(url)
        if url == URL and self.pdp_status != 200:
            return None, f'http_{self.pdp_status}', self.pdp_status
        return (b'<html></html>' if url == URL else self.seller_html,
                'ready', 200)

    def _browser_get(self, url):
        self.browser_urls.append(url)
        return (b'<html></html>' if self.browser_reason == 'ready' else None,
                self.browser_url, self.browser_reason)


class Iphone16PMAuditTests(unittest.TestCase):
    def test_exact_selected_offer_accepts_only_reviewed_catalog_and_seller(self):
        self.assertTrue(exact_iphone16pm_offer(EVIDENCE))
        for change in ({'seller_id': '11289'}, {'network_generation': '4g'},
                       {'storage_gb': 512}, {'source_title': EVIDENCE['source_title'].replace('iPhone 16 Pro Max', 'A55')},
                       {'condition': 'refurbished'}, {'availability': 'out_of_stock'},
                       {'price': 29999.0}, {'offer_id': '1869831178'}):
            with self.subTest(change=change):
                self.assertFalse(exact_iphone16pm_offer({**EVIDENCE, **change}))
        self.assertFalse(exact_iphone16pm_offer(EVIDENCE, {**CATALOG, 'id': 293}))

    def test_legal_identity_requires_company_and_numbers(self):
        self.assertTrue(exact_seller_identity(SELLER_HTML))
        self.assertFalse(exact_seller_identity(SELLER_HTML.replace(TAX_NUMBER, '0000000000')))

    @patch('audit_mediamarkt_iphone16pm_readonly.selected_marketplace_offer', return_value=EVIDENCE)
    def test_audit_uses_only_exact_pages_and_never_writes(self, parser):
        source = Transport()
        report = run_audit(source)
        self.assertTrue(report['verified'])
        self.assertFalse(report['writes'])
        self.assertEqual(source.urls, [URL, SELLER_URL])
        self.assertEqual(source.browser_urls, [])
        parser.assert_called_once_with(
            b'<html></html>', URL, allow_refurbished_alternatives=True,
            allow_cents=True, require_selected_condition_radio=True,
            allow_delivery_card=True, require_gtin_variant=True)
        self.assertEqual(run_audit(Transport(seller_html='<main>Other seller</main>'))['reason'],
                         'seller_identity_unverified')

    @patch('audit_mediamarkt_iphone16pm_readonly.selected_marketplace_offer', return_value=EVIDENCE)
    def test_only_exact_pdp_403_uses_bounded_browser_fallback(self, parser):
        source = Transport(pdp_status=403)
        report = run_audit(source)
        self.assertTrue(report['verified'])
        self.assertEqual(report['pdp_direct_http_status'], 403)
        self.assertEqual(report['pdp_http_status'], 200)
        self.assertEqual(report['pdp_read_mode'], 'browser_after_403')
        self.assertEqual(source.browser_urls, [URL])
        self.assertEqual(source.urls, [URL, SELLER_URL])
        parser.assert_called_once_with(
            b'<html></html>', URL, allow_refurbished_alternatives=True,
            allow_cents=True, require_selected_condition_radio=True,
            allow_delivery_card=True, require_gtin_variant=True)

        for source in (Transport(pdp_status=404),
                       Transport(pdp_status=403, browser_url=URL + '?changed'),
                       Transport(pdp_status=403, browser_reason='site_http_403')):
            with self.subTest(status=source.pdp_status,
                              browser_url=source.browser_url,
                              browser_reason=source.browser_reason):
                report = run_audit(source)
                self.assertFalse(report['verified'])
                self.assertEqual(source.browser_urls, [URL] if source.pdp_status == 403 else [])
                self.assertEqual(source.urls, [URL])


if __name__ == '__main__':
    unittest.main()
