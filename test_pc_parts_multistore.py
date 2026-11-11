import unittest

from pc_parts_multistore import company_key, independent_offers


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


if __name__ == '__main__':
    unittest.main()
