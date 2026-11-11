import json
import unittest

from pc_parts_multistore import (company_key, hepsiburada_pc_seller_identity, hepsiburada_pc_seller_targets,
                                 independent_offers, processor_packaging, trendyol_pc_seller_targets)


class PcSellerIndependenceTest(unittest.TestCase):
    def test_same_company_is_one_seller_across_marketplaces(self):
        direct = company_key('İncehesap Sanal Mağazacılık Ticaret Anonim Şirketi')
        marketplace = company_key('İNCEHESAP SANAL MAĞAZACILIK TİCARET A.Ş.')
        self.assertEqual(direct, marketplace)
        offers = [
            {'sellerKey': direct, 'platform': 'İncehesap', 'price': 100, 'url': 'https://www.incehesap.com/a'},
            {'sellerKey': marketplace, 'platform': 'Trendyol', 'price': 90, 'url': 'https://www.trendyol.com/b'},
            {'sellerKey': company_key('Beta Oyun Teknoloji A.Ş.'), 'platform': 'Gaming.Gen.TR',
             'price': 110, 'url': 'https://www.gaming.gen.tr/c'},
        ]
        self.assertEqual([row['price'] for row in independent_offers(offers)], [90, 110])

    def test_public_tax_identity_connects_company_name_aliases(self):
        observations = [
            {'sellerKey': 'company-a', 'taxKey': 'tax-a', 'platform': 'Trendyol', 'price': 90, 'url': 'https://a.example'},
            {'sellerKey': 'company-b', 'platform': 'Gaming.Gen.TR', 'price': 100, 'url': 'https://b.example'},
            {'sellerKey': 'company-b', 'taxKey': 'tax-a', 'platform': 'Hepsiburada', 'price': 110, 'url': 'https://c.example'},
        ]
        self.assertEqual(independent_offers(observations), observations[:1])

    def test_affiliated_beta_storefronts_do_not_count_twice(self):
        self.assertEqual(company_key('Beta Oyun Teknoloji Anonim Şirketi'),
                         company_key('Beta Plus Teknoloji Hizmetleri Sanayi Ticaret Limited Şirketi'))

    def test_public_hepsiburada_profile_must_match_selected_merchant(self):
        url = 'https://www.hepsiburada.com/magaza/ornek'
        offer = {'merchant': 'Örnek', 'merchant_id': 'id-one', 'seller_url': url}
        merchant = {'id': 'id-one', 'name': 'Örnek', 'urlPostfix': 'ornek'}
        detail = {**merchant, 'properties': [
            {'key': 'Ticaret Ünvanı', 'val': 'Örnek Ticaret Ltd. Şti.'},
            {'key': 'VKN', 'val': '0123456789'},
        ]}
        state = json.dumps({'merchantState': {'merchantDetail': merchant}})
        profile = json.dumps({'data': {'merchantDetail': detail}})
        html = f"""<link rel="canonical" href="{url}">
        <script id="reduxStore">{state}</script><section><script>
        window.MCONTENT.MERCHANTPROFILE = Object.assign(window.MCONTENT.MERCHANTPROFILE || {{}}, {{
            'component-id': {{'STATE': {profile}}}
        }})
        </script><div class="MerchantProfile" id="MerchantProfile_component-id"></div></section>"""
        self.assertEqual(hepsiburada_pc_seller_identity(html, offer),
                         {'_legal_name': 'Örnek Ticaret Ltd. Şti.', '_tax_key': '0123456789'})
        self.assertIsNone(hepsiburada_pc_seller_identity(html, {**offer, 'merchant_id': 'different'}))
        self.assertIsNone(hepsiburada_pc_seller_identity(html.replace("'component-id'", "'wrong'"), offer))

    def test_trendyol_seller_urls_require_an_exact_in_stock_variant(self):
        url = 'https://www.trendyol.com/asus/prime-b850-p-123456'
        variant = {'barcode': '4711387981016', 'sellable': True, 'inStock': True}
        seller = {'id': 123, 'url': url + '?boutiqueId=61&merchantId=123&sav=true', 'variants': [variant]}
        def document(row):
            state = json.dumps({'product': {'id': 123456, 'merchantListing': {'otherMerchants': [row]}}})
            return '<script>window["__envoy__SHARED_PROPS"] = ' + state + ';</script>'
        self.assertEqual(trendyol_pc_seller_targets(document(seller), url, variant['barcode']),
                         [seller['url']])
        self.assertEqual(trendyol_pc_seller_targets(document(seller), url, '4718017826396'), [])
        self.assertEqual(trendyol_pc_seller_targets(document({**seller, 'id': 456}), url, variant['barcode']), [])

    def test_hepsiburada_group_inventory_can_discover_an_exact_seller_route(self):
        url = 'https://www.hepsiburada.com/ram-pm-HBC000066SY76'
        gtin = '0649528937810'
        merchant_id = '11111111-1111-4111-8111-111111111111'
        merchant = {'id': merchant_id, 'name': 'Örnek', 'urlPostfix': 'ornek'}
        listing = {'merchantId': merchant_id, 'listingId': '22222222-2222-4222-8222-222222222222',
                   'merchantName': 'Örnek', 'merchantInfo': merchant, 'isSalable': True, 'isBundle': False}

        def document(row):
            product = {'productId': 'HBC000066SY76', 'sku': 'HBCV000066SY77', 'barcode': gtin,
                       'merchant': {'id': 'selected'}, 'isBundle': False, 'listings': [row]}
            state = json.dumps({'productState': {'product': product}})
            return f'<script id="reduxStore">{state}</script>'

        self.assertEqual(hepsiburada_pc_seller_targets(document(listing), url, gtin), [url + '?magaza=%C3%96rnek'])
        self.assertEqual(hepsiburada_pc_seller_targets(document(listing), url, '4718017826396'), [])
        self.assertEqual(hepsiburada_pc_seller_targets(document({**listing, 'isSalable': False}), url, gtin), [])
        self.assertEqual(hepsiburada_pc_seller_targets(document({**listing, 'merchantInfo': {**merchant, 'id': 'other'}}),
                                                       url, gtin), [])

    def test_processor_packaging_distinguishes_same_barcode_tray_and_box(self):
        self.assertEqual(processor_packaging('AMD Ryzen 5600X (Kutusuz Fansız)'), 'tray')
        self.assertEqual(processor_packaging('AMD Ryzen 5600X BOX FANLI'), 'box')
        self.assertEqual(processor_packaging('AMD Ryzen 5600 MPK kutulu'), 'mpk')


if __name__ == '__main__':
    unittest.main()
