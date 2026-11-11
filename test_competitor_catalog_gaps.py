import unittest

from competitor_catalog_gaps import AKAKCE, CIMRI, catalog_match, comparison_cards, identity_index, in_scope


class CompetitorCatalogGapsTest(unittest.TestCase):
    def test_akakce_deduplicates_product_ids_and_keeps_native_pagination(self):
        url = 'https://' + AKAKCE + '/ssd.html'
        html = '''
            <a href="/ssd/en-ucuz-example-fiyati%2C123456.html?ref=list" title="Example SSD">₺500</a>
            <a href="/ssd/en-ucuz-example-fiyati,123456.html" title="Example SSD">₺510</a>
            <a href="https://other.example/ssd/en-ucuz-other-fiyati,987654.html" title="Other SSD">Other</a>
            <a href="/ssd,2.html">Next</a>
        '''
        cards, links = comparison_cards(html, AKAKCE, url)
        self.assertEqual(list(cards), [{'id': '123456', 'title': 'Example SSD',
                                        'url': 'https://' + AKAKCE + '/ssd/en-ucuz-example-fiyati,123456.html'}])
        self.assertIn('https://' + AKAKCE + '/ssd,2.html', links)

    def test_cimri_ignores_brand_navigation_and_non_card_links(self):
        url = 'https://' + CIMRI + '/islemci'
        html = '''
            <a href="/islemci/en-ucuz-amd-islemci-fiyatlari" title="AMD İşlemci">AMD</a>
            <a href="/islemci/en-ucuz-amd-example-fiyatlari,123456" title="AMD Example"><h3>AMD Example</h3></a>
            <a href="/islemci/en-ucuz-intel-example-fiyatlari,987654" title="Intel Example">Footer</a>
        '''
        cards, _ = comparison_cards(html, CIMRI, url)
        self.assertEqual([r['id'] for r in cards], ['123456'])

    def test_requires_whole_and_unambiguous_manufacturer_code(self):
        parts = [
            {'name': 'Örnek 16 GB DDR5', 'slug': 'example-16', 'mpn': 'NH.QZ7EY.004'},
            {'name': 'Örnek 32 GB DDR5', 'slug': 'example-32', 'mpn': 'EX-5678'},
        ]
        index = identity_index(parts)
        self.assertEqual(catalog_match('Acer Nitro NH.QZ7EY.004 16 GB', index), 'example-16')
        self.assertIsNone(catalog_match('Acer Nitro NH.QZ7EY.004-V2 16 GB', index))
        self.assertIsNone(catalog_match('Set NH.QZ7EY.004 ve EX-5678', index))
        self.assertIsNone(catalog_match('Örnek 64 GB DDR5', index))

    def test_discards_explicitly_out_of_scope_parts(self):
        self.assertFalse(in_scope('ssd', 'Samsung T7 Taşınabilir SSD'))
        self.assertFalse(in_scope('ram', 'Kingston 16 GB SO-DIMM'))
        self.assertFalse(in_scope('phone', 'Yenilenmiş Telefon'))
        self.assertTrue(in_scope('ssd', 'Lexar NM610PRO 500 GB SSD'))
