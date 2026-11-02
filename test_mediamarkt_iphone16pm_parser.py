"""The exact iPhone page has one selected new offer plus cheaper refurbished cards."""

import json
import unittest
from copy import deepcopy

from audit_mediamarkt_iphone16pm_readonly import (
    CATALOG, SELLER, TITLE, URL, exact_iphone16pm_offer,
)
from mediamarkt_marketplace_audit import selected_marketplace_offer
from test_mediamarkt_marketplace_audit import fixture, render


OFFER_ID = '1823637090'
GTIN = '0195949805981'
PRICE = 125874.05
OPTIONS = {'allow_refurbished_alternatives': True, 'allow_cents': True,
           'require_selected_condition_radio': True,
           'allow_delivery_card': True, 'require_gtin_variant': True}


def iphone_fixture():
    base = fixture()
    replacements = (
        ('https://www.mediamarkt.com.tr/tr/product/_samsung-galaxy-s25-ultra-5g-12-gb-256-gb-akilli-telefon-titanyum-mavi-166438395.html', URL),
        ('SAMSUNG Galaxy S25 Ultra 12 GB 256 GB Akıllı Telefon Titanyum Mavi', TITLE),
        ('166438395', '147052697'),
        ('1647130524', OFFER_ID),
        ('CepHane Teknoloji', SELLER),
        ('11289', '12945'),
        ('78899', str(PRICE)),
    )
    serialized = json.dumps(base, ensure_ascii=False)
    for old, new in replacements:
        serialized = serialized.replace(old, new)
    data = json.loads(serialized)
    data['action']['object']['brand']['name'] = 'APPLE'
    data['action']['object']['gtin13'] = GTIN
    data['action']['object']['hasVariant'].append({
        '@type': 'Product', 'sku': '1239589', 'gtin13': GTIN,
        'name': 'APPLE iPhone 16 Pro Max 256 GB Akıllı Telefon Beyaz Titanium MYWW3TU/A'})
    data['action']['object']['offers'].append({
        '@type': 'Offer', 'url': URL, 'price': 94599,
        'priceCurrency': 'TRY',
        'itemCondition': 'https://schema.org/RefurbishedCondition',
        'availability': 'https://schema.org/InStock',
        'seller': {'@type': 'Organization', 'name': 'Garantili Teknoloji',
                   'id': '12544'}})
    data['apollo']['GraphqlProductFeature:RAM']['value'] = '8'
    data['apollo']['GraphqlProductFeatureGroups:147052697']['featureGroups'][0]['features'].append(
        {'__ref': 'GraphqlProductFeature:Radio'})
    data['apollo']['GraphqlProductFeature:Radio'] = {
        '__typename': 'GraphqlProductFeature', 'name': 'Mobil Telefon Standardı',
        'value': '5G'}
    data['visible_price'] = '₺ 125.874,'
    data['stock'] = ''
    data['stock_test'] = 'empty-stock-area'
    return data


def iphone_html(data, *, checked=OFFER_ID, new_delivery=True):
    html = render(data)
    html = html.replace('</span><p>Satış ve sevkiyat',
                        '</span><span data-test="branded-price-decimal-value">05</span>'
                        '<p>Satış ve sevkiyat')
    new_stock = ('<div data-test="mms-cofr-delivery_AVAILABLE">'
                 'Adrese teslimata uygun.</div>' if new_delivery else '')
    selector = (
        '<section data-test="pdp-refurbished-selector"><ul>'
        f'<li><label for="{OFFER_ID}"><input name="pdp-condition-selector" '
        f'id="{OFFER_ID}" value="{OFFER_ID}" type="radio" '
        f'{"checked" if checked == OFFER_ID else ""}>'
        f'Yeni ürünler {SELLER}</label>{new_stock}</li>'
        '<li><label for="refurbished"><input name="pdp-condition-selector" '
        'id="refurbished" value="refurbished" type="radio" '
        f'{"checked" if checked == "refurbished" else ""}>'
        'Yenilenmiş ürünler</label><div data-test="mms-cofr-delivery_AVAILABLE">'
        'Adrese teslimata uygun.</div></li></ul></section>')
    return html.replace('</body>', selector + '</body>')


class Iphone16PMParserTests(unittest.TestCase):
    def test_selected_new_cents_offer_survives_cheaper_refurbished_alternative(self):
        data = iphone_fixture()
        observed = selected_marketplace_offer(iphone_html(data), URL, **OPTIONS)
        self.assertIsNotNone(observed)
        self.assertEqual((observed['price'], observed['offer_id'],
                          observed['seller_id'], observed['gtin13'],
                          observed['selected_variant_sku']),
                         (PRICE, OFFER_ID, '12945', GTIN, '1239589'))
        self.assertTrue(exact_iphone16pm_offer(observed, CATALOG))
        self.assertIsNone(selected_marketplace_offer(iphone_html(data), URL))

    def test_refurbished_selection_and_second_new_offer_fail_closed(self):
        data = iphone_fixture()
        self.assertIsNone(selected_marketplace_offer(
            iphone_html(data, checked='refurbished'), URL, **OPTIONS))
        duplicate = deepcopy(data)
        duplicate['action']['object']['offers'][1]['itemCondition'] = (
            'https://schema.org/NewCondition')
        self.assertIsNone(selected_marketplace_offer(
            iphone_html(duplicate), URL, **OPTIONS))

    def test_visible_price_and_selected_delivery_must_match(self):
        data = iphone_fixture()
        self.assertIsNone(selected_marketplace_offer(
            iphone_html(data, new_delivery=False), URL, **OPTIONS))
        unrelated_delivery = deepcopy(data)
        unrelated_delivery['stock'] = 'Adrese teslimata uygun.'
        unrelated_delivery['stock_test'] = 'mms-cofr-delivery_AVAILABLE'
        self.assertIsNone(selected_marketplace_offer(
            iphone_html(unrelated_delivery, new_delivery=False), URL, **OPTIONS))
        self.assertIsNone(selected_marketplace_offer(
            iphone_html(data).replace('branded-price-decimal-value">05',
                                      'branded-price-decimal-value">06'),
            URL, **OPTIONS))

    def test_gtin_must_bind_one_independent_retail_variant(self):
        data = iphone_fixture()
        data['action']['object']['hasVariant'][-1]['gtin13'] = '0195949805982'
        self.assertIsNone(selected_marketplace_offer(
            iphone_html(data), URL, **OPTIONS))


if __name__ == '__main__':
    unittest.main()
