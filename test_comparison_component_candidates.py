import unittest

from comparison_component_candidates import candidate_groups, selected_images

PRIMARY = 'https://www.incehesap.com/board-fiyati-12345/'
NEXT = 'https://www.incehesap.com/board-fiyati-23456/'
SIBLING = 'https://www.sinerji.gen.tr/board-p-56789'


class ComponentDiscoveryTest(unittest.TestCase):
    def test_rotation_and_reviewed_profiles_keep_two_source_candidates(self):
        state = {'products': {
            'first': {'category': 'motherboard', 'retailerLeads': [PRIMARY, SIBLING]},
            'next': {'category': 'motherboard', 'retailerLeads': [NEXT, SIBLING]},
            'incomplete': {'category': 'psu', 'retailerLeads': [PRIMARY]},
        }, 'componentLeadAudits': {PRIMARY: '2026-11-12'}}
        catalog = {'parts': []}
        self.assertEqual(candidate_groups(state, catalog, 1), [('motherboard', NEXT, [SIBLING])])
        self.assertEqual(candidate_groups(state, catalog, 1, {PRIMARY}), [('motherboard', PRIMARY, [SIBLING])])
        catalog['parts'].append({'sourceUrl': PRIMARY, 'offerTargets': [PRIMARY]})
        self.assertEqual(candidate_groups(state, catalog, 1, {PRIMARY}), [('motherboard', NEXT, [SIBLING])])

    def test_selected_photo_stays_on_the_primary_product_gallery(self):
        html = ('<div class="prod"><img itemprop="image" alt="Board" src="/resim/urun/202611/a_500.webp"></div>'
                '<meta property="og:image" content="https://www.incehesap.com/resim/og_resimler/a_og.jpg">')
        self.assertEqual(selected_images(html, 'Board'),
                         ['https://www.incehesap.com/resim/urun/202611/a_500.webp',
                          'https://www.incehesap.com/resim/og_resimler/a_og.jpg'])
        self.assertEqual(selected_images(html, 'Other'), [])
        self.assertEqual(selected_images(html.replace('/resim/urun/202611/', '//other.com/'), 'Board'), [])



if __name__ == '__main__':
    unittest.main()
