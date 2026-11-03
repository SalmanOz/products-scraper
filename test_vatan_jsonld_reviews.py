"""A review encoding defect must not hide or change a selected Vatan offer."""

import json
import unittest

from bs4 import BeautifulSoup

from retailer_offer_refresh import _product_offer, _schema_nodes
from test_retailer_offer_refresh import VATAN_URL, vatan_variant_page, vatan_variant_schema


def review_page(*, control='\n', changes=None, visible_price='89.999 TL', cart=True):
    schema = vatan_variant_schema()
    schema['review'] = [{'@type': 'Review', 'reviewBody': 'BeforeCONTROLAfter'}]
    if changes:
        changes(schema)
    html = vatan_variant_page(schema, visible_price=visible_price, cart=cart)
    return html.replace('BeforeCONTROLAfter', 'Before' + control + 'After')


class VatanReviewJsonldTests(unittest.TestCase):
    def test_unlisted_sku_review_controls_preserve_selected_offer(self):
        for control in ('\n', '\r\n', '\t', '\x01'):
            with self.subTest(control=repr(control)):
                soup = BeautifulSoup(review_page(control=control), 'html.parser')
                product = next(n for n in _schema_nodes(soup, VATAN_URL)
                               if n.get('@type') == 'Product')
                self.assertEqual(product['review'][0]['reviewBody'], 'Before' + control + 'After')
                self.assertEqual(product['offers']['price'], '89999')
                offer = _product_offer(soup, VATAN_URL)
                self.assertEqual((offer['price'], offer['url'], offer['merchant']),
                                 (89999, VATAN_URL, 'Vatan Bilgisayar'))
                self.assertTrue(offer['selected_vatan_variant_verified'])

    def test_repaired_review_does_not_relax_seller_stock_variant_or_cash(self):
        changes = (
            lambda n: n['offers']['seller'].update(name='Other'),
            lambda n: n['offers'].update(availability='https://schema.org/OutOfStock'),
            lambda n: n['offers'].update(url=VATAN_URL.replace('256', '512')),
            lambda n: n.update(sku='999999'),
            lambda n: n.update(hasVariant=[]),
            lambda n: n.update(hasVariant=None),
        )
        for change in changes:
            with self.subTest(change=change):
                self.assertIsNone(_product_offer(BeautifulSoup(review_page(changes=change),
                                                             'html.parser'), VATAN_URL))
        for options in ({'visible_price': '79.999 TL'}, {'cart': False}):
            self.assertIsNone(_product_offer(BeautifulSoup(review_page(**options),
                                                         'html.parser'), VATAN_URL))

    def test_raw_control_outside_review_and_non_vatan_documents_remain_invalid(self):
        html = review_page().replace('"price": "89999"', '"price": "89\n999"')
        self.assertIsNone(_product_offer(BeautifulSoup(html, 'html.parser'), VATAN_URL))
        soup = BeautifulSoup(review_page(), 'html.parser')
        for url in ('https://www.amazon.com.tr/dp/B123456789',
                    VATAN_URL + '?seller=other',
                    VATAN_URL.replace('https:', 'http:'),
                    VATAN_URL.replace('www.vatanbilgisayar.com', 'attacker.example')):
            self.assertEqual(_schema_nodes(soup, url), [])

    def test_already_valid_json_preserves_escaped_review_text(self):
        schema = vatan_variant_schema()
        schema['review'] = [{'reviewBody': 'Escaped\\n and a quote: "'}]
        soup = BeautifulSoup(vatan_variant_page(schema), 'html.parser')
        product = next(n for n in _schema_nodes(soup, VATAN_URL) if n.get('@type') == 'Product')
        self.assertEqual(product, json.loads(json.dumps(schema)))


if __name__ == '__main__':
    unittest.main()
