import unittest

from manufacturer_specs import (vivo_spec_links, parse_vivo_specifications,
    fill_missing_records, missing_critical_facts, vivo_product_images,
    manufacturer_spec_links, parse_manufacturer_specifications)
from release_dates import normalize_release_date

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
    def test_other_manufacturers_follow_only_observed_exact_turkish_specs(self):
        for brand, page in [('apple', 'https://www.apple.com/tr/iphone-18-pro/'),
                            ('samsung', 'https://www.samsung.com/tr/smartphones/galaxy-a/galaxy-a56-5g/'),
                            ('xiaomi', 'https://www.mi.com/tr/product/xiaomi-15t-pro/'),
                            ('poco', 'https://www.mi.com/tr/product/poco-f8-ultra/'),
                            ('oppo', 'https://www.oppo.com/tr/smartphones/series-reno/reno15/')]:
            c = {'brandSlug': brand, 'evidenceUrl': page}
            self.assertEqual(manufacturer_spec_links('<a href="specs/">Specs</a>', c), [page + 'specs/'])
            for raw in [page.replace('/tr/', '/global/') + 'specs/', page + 'specs/?token=x',
                        page.replace('https:', 'http:') + 'specs/', page.replace('www.', 'www.evil.') + 'specs/']:
                self.assertEqual(manufacturer_spec_links(f'<a href="{raw}">Specs</a>', c), [])
            self.assertEqual(manufacturer_spec_links('<p>No observed specs link</p>', c), [])

    def test_apple_model_columns_never_mix_pro_max_or_unnamed_columns(self):
        c = {'name': 'Apple iPhone 18 Pro', 'slug': 'apple-iphone-18-pro', 'brandSlug': 'apple',
             'evidenceUrl': 'https://www.apple.com/tr/iphone-18-pro/'}
        def row(title, cols):
            return '<div class="techspecs-row"><div class="techspecs-rowheader">' + title + '</div>' + cols + '</div>'
        def col(model, text):
            return '<div class="techspecs-column"><strong class="techspecs-small-heading">' + model + '</strong>' + text + '</div>'
        html = row('Kapasite', col('iPhone 18 Pro', '256 GB; 512 GB') + col('iPhone 18 Pro Max', '1 TB'))
        html += row('Ekran', col('iPhone 18 Pro', '6,3 inç <p class="techspecs-caption">6,27 inç</p>') + col('iPhone 18 Pro Max', '6,9 inç'))
        html += row('Çip', '<div class="techspecs-column" aria-colspan="2">A20 Pro</div>')
        html += row('Güç ve Pil', '<div class="techspecs-column">31 saat</div><div class="techspecs-column">37 saat</div>')
        records = parse_manufacturer_specifications(html, c['evidenceUrl'] + 'specs/', c, '2026-10-19T12:00:00Z')
        values = {r['attribute_key']: r['value'] for r in records}
        self.assertEqual(values['screen_size_inch'], 6.3)
        self.assertEqual(values['Performance & Hardware']['Çip'], 'A20 Pro')
        for field in ['ram_gb', 'storage_gb', 'battery_mah', 'Batarya', 'camera_score']:
            self.assertNotIn(field, values)
        c['name'] = 'Apple iPhone 18 Pro Max'
        values = {r['attribute_key']: r['value'] for r in parse_manufacturer_specifications(html, c['evidenceUrl'] + 'specs/', c, '2026-10-19T12:00:00Z')}
        self.assertEqual(values['screen_size_inch'], 6.9)

    def test_oppo_real_component_separates_video_from_camera_and_virtual_memory(self):
        c = {'name': 'OPPO Reno15 5G', 'slug': 'oppo-reno15-5g', 'brandSlug': 'oppo',
             'evidenceUrl': 'https://www.oppo.com/tr/smartphones/series-reno/reno15/'}
        html = '<h2 class="detail-name">OPPO Reno15 5G ( CPH2825 )</h2>'
        for section, fields in [('Depolama', [('ram-and-rom-capacities','12 GB + 256 GB'), ('extended-ram','12 GB')]),
                                ('Ekran', [('size','6,59inç')]), ('Batarya',[('battery','6500 mAh / 25,48 Wh (Tipik)')]),
                                ('Kamera',[('rear','50 MP OIS')]), ('Video',[('rear','4K 60fps')])]:
            html += '<div class="param-detail-item"><div class="item-left">' + section + '</div>'
            html += ''.join(f'<div class="item-right-list" data-labelkey="{k}"><p class="key">{k}</p><p class="value">{v}</p></div>' for k,v in fields) + '</div>'
        values = {r['attribute_key']: r['value'] for r in parse_manufacturer_specifications(html, c['evidenceUrl'] + 'specs/', c, '2026-10-19T12:00:00Z')}
        self.assertEqual((values['ram_gb'], values['storage_gb'], values['battery_mah']), (12,256,6500))
        self.assertEqual(values['screen_size_inch'],6.59)
        self.assertEqual(len(values['Camera']),2)
        with self.assertRaises(ValueError):
            parse_manufacturer_specifications(html.replace('Reno15 5G','Reno15 Pro 5G'), c['evidenceUrl'] + 'specs/', c, '2026-10-19T12:00:00Z')

    def test_xiaomi_semantic_rows_keep_multiple_skus_ambiguous(self):
        c = {'name':'Xiaomi 15T Pro','slug':'xiaomi-15t-pro','brandSlug':'xiaomi',
             'evidenceUrl':'https://www.mi.com/tr/product/xiaomi-15t-pro/'}
        html = '<h1>Xiaomi 15T Pro</h1><main><div><h2>RAM ve Depolama</h2><div>12+256 GB | 12+512 GB</div></div>'
        html += '<div><h2>Pil ve Şarj</h2><div>5500 mAh (typ) 90 W</div></div><div><h2>Ekran</h2><div>6,83 inç AMOLED</div></div></main>'
        values = {r['attribute_key']:r['value'] for r in parse_manufacturer_specifications(html,c['evidenceUrl']+'specs/',c,'2026-10-19T12:00:00Z')}
        self.assertEqual(values['battery_mah'],5500)
        self.assertEqual(values['screen_size_inch'],6.83)
        self.assertNotIn('ram_gb',values)
        self.assertNotIn('storage_gb',values)
        single = html.replace('12+256 GB | 12+512 GB', '12+256 GB')
        values = {r['attribute_key']:r['value'] for r in parse_manufacturer_specifications(single,c['evidenceUrl']+'specs/',c,'2026-10-19T12:00:00Z')}
        self.assertEqual((values['ram_gb'],values['storage_gb']),(12,256))

    def test_samsung_empty_dynamic_table_is_not_success_and_related_products_not_evidence(self):
        c = {'name':'Samsung Galaxy S26 Ultra','slug':'samsung-galaxy-s26-ultra','brandSlug':'samsung',
             'evidenceUrl':'https://www.samsung.com/tr/smartphones/galaxy-s26-ultra/'}
        for html in ['<h1>Galaxy S26 Ultra</h1><div class="js-spec-data"></div>',
                     '<script type="application/ld+json">{"@type":"Product","name":"Galaxy S26","isRelatedTo":{"@type":"Product","name":"Galaxy S26 Ultra"}}</script>']:
            with self.assertRaises(ValueError):
                parse_manufacturer_specifications(html,c['evidenceUrl']+'specs/',c,'2026-10-19T12:00:00Z')

    def test_release_precision_and_explicit_release_only(self):
        self.assertEqual(normalize_release_date('February 2026, 7 months ago'),'February 2026')
        self.assertEqual(normalize_release_date('2026-02-28'),'2026-02-28')
        for raw in ['2026-02-30','2026','02/03/2026','Coming soon','March 2026 (rumored)','7 months ago']:
            self.assertIsNone(normalize_release_date(raw))
        c = {'name':'Samsung Galaxy S26 Ultra','slug':'samsung-galaxy-s26-ultra','brandSlug':'samsung',
             'evidenceUrl':'https://www.samsung.com/tr/smartphones/galaxy-s26-ultra/'}
        html = '<script type="application/ld+json">{"@type":"Product","name":"Galaxy S26 Ultra","releaseDate":"2026-02-28","dateModified":"2026-10-19"}</script>'
        records = parse_manufacturer_specifications(html,c['evidenceUrl']+'specs/',c,'2026-10-19T12:00:00Z')
        self.assertEqual(records[0]['value'],{'Release date':'2026-02-28'})
        with self.assertRaises(ValueError):
            parse_manufacturer_specifications(html.replace('releaseDate','datePublished'),c['evidenceUrl']+'specs/',c,'2026-10-19T12:00:00Z')

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
