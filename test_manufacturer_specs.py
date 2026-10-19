import unittest

from manufacturer_specs import (vivo_spec_links, parse_vivo_specifications,
    fill_missing_records, missing_critical_facts, vivo_product_images)

OVERVIEW = 'https://www.vivo.com/tr/products/v70'
SPEC = 'https://www.vivo.com/tr/products/param/v70'
CANDIDATE = {'name': 'vivo V70', 'slug': 'vivo-v70', 'brandSlug': 'vivo', 'evidenceUrl': OVERVIEW}


def official_html(model='V70', pair='12 GB + 256 GB'):
    sections = {
        'Temel Özellikler': {'Android Sürümü': 'Android 16'},
        'Platform': {'CPU Çekirdek Sayısı': '8'},
        'Depolama': {'RAM ve ROM': pair, 'Genişletilebilir RAM Kapasitesi': '12 GB'},
        'Pil': {'Pil': '6500 mAh (TİPİK)', 'Şarj Gücü': '90 W'},
        'Ekran': {'Boyut': '16,74 cm (6,59 inç)', 'Tür': 'AMOLED'},
        'Kamera': {'Kamera': 'Ön: 50 MP Arka: 50 MP OIS'},
        'Ağ': {'5G': 'n1/n3/n78', 'Ülke/Bölge': 'Türkiye'},
        'Kutu İçeriği': {'Model': model},
    }
    return ''.join('<li class="parameter-item"><h2 class="parameter-title">' + title + '</h2>' +
        ''.join('<li class="attr-item"><p class="attr-item-name">' + label + '</p><div class="attr-item-text">' +
                value + '</div></li>' for label, value in rows.items()) + '</li>' for title, rows in sections.items())


class ManufacturerSpecsTests(unittest.TestCase):
    def test_only_observed_matching_turkish_spec_links_are_followed(self):
        html = ('<a href="http://www.vivo.com/tr/products/param/v70">Parametre</a>' +
            '<a href="https://www.vivo.com/en/products/param/v70">Global</a>')
        self.assertEqual(vivo_spec_links(html, OVERVIEW), [SPEC])
        self.assertEqual(vivo_spec_links(html, OVERVIEW + '-fe'), [])
        self.assertEqual(vivo_spec_links(html, 'https://user@www.vivo.com/tr/products/v70'), [])

    def test_observed_turkish_physical_memory_battery_and_diagonal_are_preserved(self):
        records = parse_vivo_specifications(official_html(), SPEC, CANDIDATE, '2026-10-19T12:00:00Z')
        values = {record['attribute_key']: record['value'] for record in records}
        self.assertEqual(values['ram_gb'], 12)  # Not 24 with virtual RAM.
        self.assertEqual(values['storage_gb'], 256)
        self.assertEqual(values['battery_mah'], 6500)
        self.assertEqual(values['screen_size_inch'], 6.59)  # Not 16.74 cm.
        self.assertTrue(all(record['value_origin'] == 'manufacturer' and record['source_url'] == SPEC for record in records))
        self.assertNotIn('Model', values['Performance & Hardware'])  # Missing CPU is not guessed from global specs.
        self.assertNotIn('performance_score', values)
        self.assertIn('performance_score', missing_critical_facts(records))
        self.assertIn('release_date', missing_critical_facts(records))

    def test_multiple_skus_never_select_an_arbitrary_first_variant(self):
        values = {record['attribute_key']: record['value'] for record in parse_vivo_specifications(
            official_html(pair='8 GB + 256 GB; 12 GB + 512 GB'), SPEC, CANDIDATE, '2026-10-19T12:00:00Z')}
        self.assertNotIn('ram_gb', values)
        self.assertNotIn('storage_gb', values)

    def test_unknown_model_and_other_locale_or_model_urls_cannot_supply_facts(self):
        with self.assertRaises(ValueError):
            parse_vivo_specifications(official_html(model='V70 FE'), SPEC, CANDIDATE, '2026-10-19T12:00:00Z')
        with self.assertRaises(ValueError):
            parse_vivo_specifications(official_html(), SPEC, {**CANDIDATE, 'name': 'vivo V70+'}, '2026-10-19T12:00:00Z')
        for url in [SPEC.replace('/tr/', '/en/'), SPEC + '-fe', SPEC.replace('https:', 'http:'),
                    SPEC.replace('www.vivo.com', '127.0.0.1'), SPEC + '?token=secret']:
            with self.assertRaises(ValueError):
                parse_vivo_specifications(official_html(), url, CANDIDATE, '2026-10-19T12:00:00Z')

    def test_missing_only_merge_preserves_group_origins_and_reports_sku_conflicts(self):
        existing = [{'attribute_key': 'ram_gb', 'value': 8}, {'attribute_key': 'Camera', 'value': {'Rear': '48 MP'}}]
        official = [{'attribute_key': 'ram_gb', 'value': 12}, {'attribute_key': 'Camera', 'value': {'Rear': '50 MP'}},
                    {'attribute_key': 'battery_mah', 'value': 6500}]
        merged, conflicts = fill_missing_records(existing, official)
        self.assertEqual(conflicts, ['ram_gb'])
        self.assertEqual(merged[:2], existing)
        self.assertEqual(merged[-1]['value'], 6500)

    def test_only_product_color_renders_from_the_observed_official_cdn_are_candidates(self):
        html = '<div class="parameter-color"><ul class="color-image-list"><img src="https://de-gdpr-exstatic-vivofs.vivo.com/path/phone.png"></ul></div>'
        self.assertEqual(len(vivo_product_images(html)), 1)
        self.assertEqual(vivo_product_images(html.replace('de-gdpr-exstatic-vivofs.vivo.com', 'evil.test')), [])
        self.assertEqual(vivo_product_images(html.replace('parameter-color', 'footer-logo')), [])


if __name__ == '__main__':
    unittest.main()
