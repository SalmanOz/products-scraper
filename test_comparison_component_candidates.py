from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from comparison_component_candidates import INCEHESAP, SINERJI, candidate_groups, selected_images, stage

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
        self.assertEqual(candidate_groups(state, catalog, 1), [
            ('motherboard', SIBLING, [PRIMARY, NEXT]), ('motherboard', NEXT, [SIBLING])])
        self.assertEqual(candidate_groups(state, catalog, 1, {PRIMARY}), [
            ('motherboard', PRIMARY, [SIBLING]), ('motherboard', SIBLING, [PRIMARY, NEXT])])
        catalog['parts'].append({'sourceUrl': PRIMARY, 'offerTargets': [PRIMARY]})
        self.assertEqual(candidate_groups(state, catalog, 1, {PRIMARY}), [
            ('motherboard', NEXT, [SIBLING]), ('motherboard', SIBLING, [NEXT])])

    def test_source_circuit_leaves_other_component_candidates_available(self):
        reserve = 'https://www.teknosa.com/asus-board-p-12345678'
        state = {'products': {'board': {'category': 'motherboard', 'retailerLeads': [PRIMARY, SIBLING, reserve]}}}
        gtin = '4718466017901'
        profiles = {gtin: {'sourceUrl': SIBLING, 'kind': 'motherboard', 'brand': 'Asus', 'mpn': 'Board',
                           'specSourceUrl': 'https://www.asus.com/board/', 'specs': {'Yonga Seti': 'B650'}}}
        image = 'https://static.sinerji.gen.tr/Images/LG/board.jpg'
        sinerji = Mock()
        sinerji.transport._read_document.return_value = 'sinerji document', 'document_read', 200
        sinerji.parse.return_value = {'gtin13': gtin, 'brand': 'Asus', 'source_title': 'Asus Board', '_image': image}, 'verified'
        blocked = Mock()
        blocked.transport._read_document.return_value = None, 'http_403', 403
        offers = [{'gtin': gtin, 'url': SIBLING, 'price': 100, 'platform': 'Sinerji', 'sellerKey': 'sinerji'},
                  {'gtin': gtin, 'url': reserve, 'price': 110, 'platform': 'Teknosa', 'sellerKey': 'teknosa'}]
        with TemporaryDirectory() as directory, \
                patch('comparison_component_candidates.catalog_document_reader', return_value=Mock()), \
                patch('comparison_component_candidates.PcRetailer', side_effect=lambda host, **_: (
                    blocked if host == INCEHESAP else sinerji)), \
                patch('comparison_component_candidates.collect_targets', return_value=offers), \
                patch('comparison_component_candidates._image', return_value=b'photo') as download:
            report = stage(state, {'parts': []}, profiles, Path(directory), 1)
            self.assertEqual(report['primaryReader'], 'runner')
            self.assertEqual(report['rejections'], {'http_403': 1})
            part, = report['staged']
            self.assertEqual(part['sourceUrl'], SIBLING)
            self.assertEqual(part['offerTargets'], [SIBLING, reserve])
            self.assertTrue((Path(directory) / 'photos' / f'{gtin}.webp').is_file())
            download.assert_called_once_with(image, allowed_hosts={SINERJI, 'static.sinerji.gen.tr'})

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
