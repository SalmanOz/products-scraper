import unittest
from unittest.mock import Mock, patch

from comparison_phone_offers import candidate_urls, checked_offers, publishable_pair


MM = 'https://www.mediamarkt.com.tr/tr/product/_apple-iphone-16-plus-128-gb-akilli-telefon-mavi-146562812.html'
PZ = 'https://www.pazarama.com/apple-iphone-16-plus-128-gb-ultramarine-p-195949722813'
NAME = 'Apple iPhone 16 Plus'
TITLE = NAME + ' 8 GB 128 GB 5G'
SPECS = {'ram_gb': 8, 'storage_gb': 128}
GTIN = '0195949722813'


class ComparisonPhoneOffersTest(unittest.TestCase):
    def test_comparison_urls_never_reach_retailer_verifier(self):
        state = {'products': {'x': {'catalogFamily': 'apple-iphone-16-plus', 'category': 'phone',
                                   'retailerLeads': ['https://urunai.com/telefon/apple/iphone-16-plus', MM, PZ, PZ]}}}
        self.assertEqual(candidate_urls(state, 'apple-iphone-16-plus-8gb-256gb'), [MM, PZ])

    def test_two_stores_must_show_the_same_barcode_and_different_companies(self):
        base = {'price': 89599, 'availability': 'in_stock', 'currency': 'TRY', 'source_title': TITLE,
                'gtin13': GTIN}
        first = {**base, 'merchant': 'Seller A', 'merchant_id': '11626', 'url': MM,
                 'platform': 'MediaMarkt', 'seller_url': 'https://www.mediamarkt.com.tr/tr/marketplace/seller/11626'}
        second = {**base, 'merchant': 'Seller B', 'merchant_id': 'seller-b', 'url': PZ,
                  'platform': 'Pazarama', 'seller_url': 'https://www.pazarama.com/magaza/seller-b'}
        transport = Mock()
        transport._read_document.return_value = 'document', 'document_read', 200
        with patch('comparison_phone_offers.mediamarkt_selected', return_value=first), \
             patch('comparison_phone_offers.pazarama_selected') as parse, \
             patch('comparison_phone_offers.seller_identity') as identity:
            parse.return_value = {**second, 'gtin13': '0195949722240'}
            identity.side_effect = [('seller a', '1'), ('seller b', '2')]
            self.assertEqual(checked_offers([MM, PZ], NAME, SPECS, transport)[0], [])
            parse.return_value = second
            identity.side_effect = [('seller a', '1'), ('seller b', '1')]
            self.assertEqual(checked_offers([MM, PZ], NAME, SPECS, transport)[0], [])
            identity.side_effect = [('seller a', '1'), ('seller b', '2')]
            self.assertEqual(len(checked_offers([MM, PZ], NAME, SPECS, transport)[0]), 2)

    def test_publisher_requires_collision_proof_or_all_old_urls_expired(self):
        offers = [
            {'price': 89599, 'url': MM, 'merchant': 'Seller A', 'platform': 'MediaMarkt',
             'gtin13': GTIN, '_legal_name': 'seller a', '_tax_key': '1'},
            {'price': 90699, 'url': PZ, 'merchant': 'Seller B', 'platform': 'Pazarama',
             'gtin13': GTIN, '_legal_name': 'seller b', '_tax_key': '2'},
        ]
        clean = {'complete': True, 'blocked': False, 'reasons': []}
        history = {'complete': True, 'blocked': True,
                   'reasons': ['proposed_seller_on_other_url_or_platform'],
                   'proposed_seller_other_url_rows': 2}
        client = Mock()
        client.get_raw_seller_collision_proof.side_effect = [
            {**history, 'proposed_seller_historical_other_url_rows': 1}, clean]
        self.assertEqual(publishable_pair(offers, client, {'id': 1})[0], [])
        client.get_raw_seller_collision_proof.side_effect = [
            {**history, 'proposed_seller_historical_other_url_rows': 2}, clean]
        self.assertEqual(publishable_pair(offers, client, {'id': 1})[0], offers)
