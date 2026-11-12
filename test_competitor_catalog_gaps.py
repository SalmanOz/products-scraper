import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from competitor_catalog_gaps import (AKAKCE, CIMRI, EPEY, KARSILASTIRICI, PAHALIMI, URUNAI, akakce_limits, candidate_family, cimri_sitemap_cards, epey_earbud_title, epey_sitemap_cards, export_source_snapshot, import_source_snapshot, product_state_key, scan_product_sitemap, scan_source, urunai_market_slugs, urunai_sitemap_cards, catalog_match, category_url, comparison_cards, comparison_retailer_leads,
                                     identity_index, in_scope, merge_state, read_state)
from competitor_catalog_gaps import (AMAHANGISI, INDIRIMLI, TUBERKARAS, comparison_lead_destination,
                                     lead_inventory, phone_family, retailer_destination, scan_retailer_leads)


class CompetitorCatalogGapsTest(unittest.TestCase):
    def test_pazarama_comparison_seller_link_contributes_only_a_canonical_lead(self):
        canonical = 'https://www.pazarama.com/aerofit-2-p-0194644298876'
        selected = canonical + '?magaza=dunya-marketi'
        self.assertIsNone(retailer_destination(selected))
        self.assertEqual(comparison_lead_destination(selected), canonical)
        self.assertIsNone(comparison_lead_destination(selected + '&magaza=second-seller'))
        self.assertIsNone(comparison_lead_destination(selected + '#offers'))

    def test_indirimli_reads_catalog_titles_and_embedded_original_store_links(self):
        from urllib.parse import urlencode

        source = f'https://{INDIRIMLI}/urun/iphone-15-plus-128-gb'
        store = 'https://www.trendyol.com/apple/iphone-15-plus-128gb-p-12345678?merchantId=17'
        route = '/api/affiliate/redirect?' + urlencode({'product_url': store})
        html = f'''<link rel="canonical" href="{source}">
          <a class="product-card-refresh" href="{source}"><p class="product-card-title">iPhone 15 Plus 128 GB</p>
          40.000 TL</a><a rel="sponsored" href="{route}">Buy</a>
          <a rel="sponsored" href="https://example.com{route}">External wrapper</a>
          <a rel="sponsored" href="{route}&amp;product_url=https://example.com">Ambiguous wrapper</a>'''
        cards, _ = comparison_cards(html, INDIRIMLI, category_url(INDIRIMLI, '/kategori/cep-telefonu', 1))
        self.assertEqual(list(cards), [{'id': 'iphone-15-plus-128-gb', 'title': 'Apple iPhone 15 Plus 128 GB', 'url': source}])
        self.assertEqual(category_url(INDIRIMLI, '/kategori/cep-telefonu', 2),
                         f'https://{INDIRIMLI}/kategori/cep-telefonu?sayfa=2')
        self.assertEqual(comparison_retailer_leads(html, INDIRIMLI, source), [store])
        self.assertEqual(comparison_retailer_leads(html, INDIRIMLI, source + '-wrong'), [])
        self.assertEqual(phone_family('Samsung Galaxy S26 Ultra 1 TB 16 GB Beyaz'), 'samsung galaxy s26 ultra')

    def test_tuberkaras_cards_bind_links_to_their_own_product_rows(self):
        store = 'https://www.n11.com/urun/sony-wf-1000xm6-123456'
        html = f'''<article class="urun-satir"><div class="satir-bilgi"><h3>
          <a href="/urun/sony-wf-1000xm6">Sony WF-1000XM6 TWS</a></h3></div>
          <a rel="sponsored" href="{store}">Buy</a></article>
          <article class="urun-satir"><div class="satir-bilgi"><h3>
          <a href="/urun/other-earbud">Other Earbud TWS</a></h3></div></article>'''
        cards, _ = comparison_cards(html, TUBERKARAS, f'https://{TUBERKARAS}/kategori/kulaklik')
        first, second = cards
        self.assertEqual(first['retailerLeads'], [store])
        self.assertNotIn('retailerLeads', second)

    def test_lead_queue_looks_for_second_store_and_keeps_regular_rotation(self):
        def row(source, key, title, *, family=None, leads=()):
            return {'source': source, 'category': 'phone', 'id': key, 'title': title,
                    'url': f'https://{source}/urun/{key}', 'catalogFamily': family,
                    'retailerLeads': leads, 'firstSeenAt': '2026-11-01'}

        original = row(PAHALIMI, 'source', 'OPPO Find X9',
                       leads=['https://www.n11.com/urun/oppo-find-x9-123456'])
        regular = row(KARSILASTIRICI, 'a-regular', 'Samsung Galaxy S26')
        gap = row(KARSILASTIRICI, 'b-gap', 'Apple iPhone 16 Plus', family='apple-iphone-16-plus')
        complement = row(KARSILASTIRICI, 'c-complement', 'Oppo Find X9')
        sibling = row(KARSILASTIRICI, 'd-sibling', 'Oppo Find X9')
        state = {'products': {row['id']: row for row in (original, regular, gap, complement, sibling)}}
        reader = Mock()
        reader._read_document.return_value = ('<html></html>', 'document_read', 200)
        with patch('competitor_catalog_gaps.comparison_reader', return_value=reader):
            scan_retailer_leads(state, KARSILASTIRICI, ['phone'], 3, '2026-11-12',
                                priority={'apple-iphone-16-plus'}, known_leads=lead_inventory(state))
        self.assertEqual([call.args[0] for call in reader._read_document.call_args_list],
                         [gap['url'], complement['url'], regular['url']])

    def test_amahangisi_discovers_only_native_sitemap_products_and_direct_offer_links(self):
        root = f'https://{AMAHANGISI}'
        product = root + '/akilli-telefon/iphone/apple-iphone-16-plus'
        shard = root + '/sitemap/urunler-1.xml'
        reader = Mock()
        reader._read_document.side_effect = [
            (f'<sitemapindex><sitemap><loc>{root}/sitemap/kategoriler-1.xml</loc></sitemap>'
             f'<sitemap><loc>{shard}</loc></sitemap></sitemapindex>', 'ready', 200),
            (f'<urlset><url><loc>{product}</loc></url><url><loc>{product}</loc></url>'
             f'<url><loc>{root}/televizyon/other-product</loc></url></urlset>', 'ready', 200),
        ]
        indexes = {'phone': identity_index([{'name': 'Apple iPhone 16 Plus', 'slug': 'apple-iphone-16-plus'}])}
        reports, rows = scan_product_sitemap(AMAHANGISI, ('phone',), indexes, {}, reader=reader)
        self.assertEqual(reports[0]['shardsRead'], [1])
        self.assertEqual(reports[0]['matched'], 1)
        self.assertEqual([call.args[0] for call in reader._read_document.call_args_list], [root + '/sitemap.xml', shard])
        self.assertEqual([(row['url'], row['catalogSlug']) for row in rows], [(product, 'apple-iphone-16-plus')])
        merchant = 'https://www.trendyol.com/apple/iphone-16-plus-p-857296119?merchantId=624588'
        html = f'''
          <link rel="canonical" href="{product}">
          <section id="offers"><div id="price-panel-offers">
          <a href="{merchant}&amp;utm_source=compare" target="_blank" rel="noopener noreferrer">₺1</a>
          <a href="/api/go?sp=1" target="_blank" rel="noopener noreferrer">Other</a>
          </div></section>
          <aside><a href="https://www.trendyol.com/apple/iphone-16-p-857296118"
            target="_blank" rel="noopener">Another phone</a></aside>
        '''
        self.assertEqual(comparison_retailer_leads(html, AMAHANGISI, product), [merchant])
        self.assertEqual(comparison_retailer_leads(html, AMAHANGISI, product + '-other'), [])

    def test_akakce_budget_keeps_every_category_and_reserves_product_reads(self):
        self.assertEqual(akakce_limits(26, 12, 3, 36), (1, 14))
        self.assertEqual(akakce_limits(24, 12, 3, 0), (2, 0))

    def test_epey_snapshot_import_keeps_usable_links_without_prices_or_false_freshness(self):
        old, new = '2026-11-11T18:00:00+00:00', '2026-11-11T20:00:00+00:00'
        retailer = 'https://www.n11.com/urun/sony-wf-1000xm6-12345678'
        product = {'source': EPEY, 'category': 'tws', 'title': 'Sony WF-1000XM6 Tam Kablosuz Kulaklık',
                   'url': f'https://{EPEY}/kulaklik/sony-wf-1000xm6.html', 'id': '12345',
                   'firstSeenAt': old, 'lastSeenAt': old, 'lastLeadAttemptAt': old,
                   'retailerLeads': [retailer, 'https://evil.example/sony'], 'price': 1}
        key = product_state_key(product)
        live = {k: v for k, v in product.items() if k not in ('retailerLeads', 'price')}
        live.update(lastSeenAt=new, lastLeadAttemptAt=new, leadStatus='http_403')
        state = {'products': {key: live}}
        with TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / 'epey.json.gz'
            self.assertEqual(export_source_snapshot(snapshot, {'products': {key: product}}, EPEY), 1)
            report = import_source_snapshot(snapshot, state, EPEY)
            self.assertEqual(import_source_snapshot(snapshot, state, EPEY), report)
        self.assertEqual(report['withRetailerLinks'], 1)
        self.assertEqual(state['products'][key]['retailerLeads'], [retailer])
        self.assertEqual(state['products'][key]['lastLeadAttemptAt'], new)
        self.assertEqual(state['products'][key]['leadStatus'], 'http_403')
        self.assertNotIn('price', state['products'][key])

    def test_cimri_snapshot_derives_product_identity_and_discards_misclassified_rows(self):
        stamp = '2026-11-11T18:00:00+00:00'
        retailer = 'https://www.n11.com/urun/sony-wf-1000xm6-12345678'
        product = {'source': CIMRI, 'category': 'tws', 'title': 'Sony WF-1000XM6 Tam Kablosuz Kulaklık',
                   'url': f'https://{CIMRI}/bluetooth-kulaklik/en-ucuz-sony-wf-1000xm6-fiyatlari,a12345',
                   'id': 'incorrect', 'firstSeenAt': stamp, 'lastSeenAt': stamp, 'lastLeadAttemptAt': stamp,
                   'retailerLeads': [retailer, 'https://evil.example/sony'], 'stock': True, 'price': 1}
        state = {'products': {}}
        with TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / 'cimri.json.gz'
            original = {'products': {'product': product, 'wrongCategory': {**product, 'category': 'phone'}}}
            self.assertEqual(export_source_snapshot(snapshot, original, CIMRI), 1)
            self.assertEqual(import_source_snapshot(snapshot, state, CIMRI)['withRetailerLinks'], 1)
            with self.assertRaises(ValueError):
                import_source_snapshot(snapshot, state, EPEY)
        row = state['products'][f'{CIMRI}/tws/a12345']
        self.assertEqual(row['retailerLeads'], [retailer])
        self.assertNotIn('price', row)
        self.assertNotIn('stock', row)

    def test_akakce_deduplicates_product_ids_and_keeps_native_pagination(self):
        url = 'https://' + AKAKCE + '/ssd.html'
        html = '''
            <ul id="CPL">
            <a class="iC" href="/ssd/en-ucuz-example-fiyati%2C123456.html?ref=list" title="Example SSD">₺500</a>
            <a class="iC" href="/ssd/en-ucuz-example-fiyati,123456.html" title="Example SSD">₺510</a>
            <a class="iC" href="https://other.example/ssd/en-ucuz-other-fiyati,987654.html" title="Other SSD">Other</a>
            </ul>
            <footer><a href="/ssd/en-ucuz-other-fiyati,123457.html" title="Unrelated">Footer</a></footer>
            <link rel="next" href="/ssd,2.html">
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
            <a href="/islemci/en-ucuz-intel-example-fiyatlari%2Ca345678" title="Intel Example"><h3>Intel Example</h3></a>
            <a href="/islemci/en-ucuz-intel-example-fiyatlari,987654" title="Intel Example">Footer</a>
        '''
        cards, _ = comparison_cards(html, CIMRI, url)
        self.assertEqual([r['id'] for r in cards], ['123456', 'a345678'])

    def test_epey_deduplicates_native_rows_and_ignores_recommendations(self):
        url = category_url(EPEY, '/kulaklik/', 1)
        html = '''
          <div id="listele">
            <ul class="metin row" id="765432"><li><a class="urunadi" href="/kulaklik/example.html"
              title="Example (Model A) Tam Kablosuz Kulaklık">Example</a></li></ul>
            <ul class="metin row" id="765432"><li><a class="urunadi" href="/kulaklik/example.html"
              title="Example (Model A) Tam Kablosuz Kulaklık">Example</a></li></ul>
          </div>
          <a class="urunadi" href="/kulaklik/other.html" title="Other">Related</a>
          <a href="/kulaklik/2/">İleri</a>
        '''
        cards, links = comparison_cards(html, EPEY, url)
        self.assertEqual([r['id'] for r in cards], ['765432'])
        self.assertIn(category_url(EPEY, '/kulaklik/', 2), links)

    def test_epey_direct_leads_drop_tracking_and_ignore_displayed_prices(self):
        html = '''
          <a class="git" data-link="https%3A%2F%2Fwww.teknosa.com%2Fornek-p-100000063496">
            <span class="urun_adi">Model</span><span class="urun_fiyat">999 TL</span></a>
          <a class="git" data-link="https://www.epey.com/yonlendir/123"><span class="urun_adi">Model</span></a>
          <a class="git" data-link="https://www.teknosa.com.evil.example/ornek-p-100000063496">
            <span class="urun_adi">Model</span></a>
        '''
        self.assertEqual(comparison_retailer_leads(html, EPEY, 'https://www.epey.com/kulaklik/ornek.html'),
                         ['https://www.teknosa.com/ornek-p-100000063496'])

    def test_cimri_only_reads_direct_links_of_the_requested_product(self):
        url = 'https://www.cimri.com/kulaklik/en-ucuz-example-fiyatlari,765432'
        html = '''
          <script type="application/ld+json">{
            "@type":"Product","url":"https://www.cimri.com/kulaklik/en-ucuz-example-fiyatlari%2C765432",
            "offers":{"@type":"AggregateOffer","offers":[
              {"url":"https://www.cimri.com/offer/1","price":500},
              {"url":"https://www.trendyol.com/apple/airpods-5-p-1198452610?merchantId=968&utm_source=cimri"}
            ]}
          }</script>
          <script type="application/ld+json">{
            "@type":"Product","url":"https://www.cimri.com/kulaklik/en-ucuz-example-fiyatlari,765433",
            "offers":{"@type":"AggregateOffer","offers":[{"url":"https://www.teknosa.com/ornek-p-100000063496"}]}
          }</script>
        '''
        self.assertEqual(comparison_retailer_leads(html, CIMRI, url),
                         ['https://www.trendyol.com/apple/airpods-5-p-1198452610?merchantId=968'])

    def test_resume_keeps_earlier_rows_and_reconciles_against_current_catalog(self):
        state = read_state(None)
        card = {'id': '765432', 'source': EPEY, 'category': 'tws', 'title': 'Example', 'url': 'url'}
        report = {'source': EPEY, 'category': 'tws', 'nextPage': 3, 'status': 'page_limit'}
        indexes = {'tws': identity_index([])}
        merge_state(state, [report], [card], indexes, 'first')
        self.assertEqual(state['sources'][EPEY + '/tws']['nextPage'], 3)
        indexes['tws'] = identity_index([{'name': 'Example', 'slug': 'example'}])
        merge_state(state, [{**report, 'nextPage': None, 'status': 'no_next_page_link'}], [], indexes, 'second')
        self.assertEqual(state['sources'][EPEY + '/tws']['completedPasses'], 1)
        self.assertEqual(list(state['products'].values()), [
            {**card, 'firstSeenAt': 'first', 'lastSeenAt': 'first', 'catalogSlug': 'example', 'catalogFamily': 'example'},
        ])

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

    def test_urunai_sitemap_stays_on_host_and_preserves_category(self):
        index = identity_index([{'name': 'Apple iPhone 16 Plus', 'slug': 'apple-iphone-16-plus'}])
        xml = """<urlset>
          <url><loc>https://urunai.com/telefon/apple/iphone-16-plus</loc></url>
          <url><loc>https://other.example/telefon/apple/iphone-16-plus</loc></url>
          <url><loc>https://urunai.com/televizyon/samsung/foo</loc></url>
          <url><loc>https://urunai.com/telefon/apple/iphone-16-plus?ignored=true</loc></url>
        </urlset>"""
        self.assertEqual(list(urunai_sitemap_cards(xml, ['phone'], {'phone': index})), [{
            'id': 'apple/iphone-16-plus', 'title': 'apple iphone 16 plus',
            'url': 'https://urunai.com/telefon/apple/iphone-16-plus', 'source': URUNAI,
            'category': 'phone', 'catalogSlug': 'apple-iphone-16-plus',
        }])

    def test_comparison_sites_supply_product_destinations_only(self):
        target = 'https://www.idefix.com/apple-iphone-16-plus-128-gb-p-3832277'
        for host, html in (
            (URUNAI, f'<a rel="sponsored" href="{target}?vendorId=220&amp;utm_source=example">1299 TL</a>'),
            (KARSILASTIRICI, f'<section id="fiyatlar"><a class="store-link" rel="sponsored" href="{target}?vendorId=220">1299 TL</a></section>'),
            (PAHALIMI, f'<a class="kat-teklif-satir" rel="sponsored" href="{target}?vendorId=220">1299 TL</a>'),
        ):
            html += '<a rel="sponsored" href="https://www.teknosa.com/sepet">Other</a>'
            self.assertEqual(comparison_retailer_leads(html, host, f'https://{host}/urun/ornek'),
                             [target + '?vendorId=220'])

    def test_known_family_does_not_claim_variant_coverage(self):
        index = identity_index([{'name': 'Apple iPhone 16 Plus', 'slug': 'apple-iphone-16-plus'}])
        title = 'Apple iPhone 16 Plus (256 GB)'
        self.assertIsNone(catalog_match(title, index))
        self.assertEqual(candidate_family('phone', title, index), 'apple-iphone-16-plus')
        self.assertIsNone(candidate_family('phone', 'Apple iPhone 16 Pro (256 GB)', index))

    def test_color_suffix_retains_family_for_discovery_only(self):
        index = identity_index([{'name': 'Xiaomi Redmi 17 5G', 'slug': 'xiaomi-redmi-17-5g'}])
        title = 'Xiaomi Redmi 17 5G 128 GB 4 GB RAM Derin Mavi'
        self.assertIsNone(catalog_match(title, index))
        self.assertEqual(candidate_family('phone', title, index), 'xiaomi-redmi-17-5g')
        self.assertIsNone(candidate_family('phone', 'Xiaomi Redmi 17 4G 128 GB 4 GB RAM Derin Mavi', index))


    def test_market_sitemap_only_prioritizes_native_product_price_pages(self):
        xml = """<urlset>
          <url><loc>https://urunai.com/en-ucuz/apple-iphone-16-plus</loc></url>
          <url><loc>https://urunai.com/en-ucuz/apple-iphone-16-plus?anything=yes</loc></url>
          <url><loc>https://else.example/en-ucuz/apple-iphone-17</loc></url>
          <url><loc>https://urunai.com/telefon/apple/iphone-18</loc></url>
        </urlset>"""
        self.assertEqual(urunai_market_slugs(xml), {'apple-iphone-16-plus'})

    def test_cimri_sitemap_routes_only_native_product_rows(self):
        xml = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
            xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
          <url><loc>https://www.cimri.com/cep-telefonlari/en-ucuz-apple-iphone-16-fiyatlari%2Ca12345</loc>
            <image:image><image:loc>https://www.cimri.com/cep-telefonlari/en-ucuz-other-fiyatlari,a23456</image:loc></image:image></url>
          <url><loc>https://other.example/cep-telefonlari/en-ucuz-apple-iphone-16-fiyatlari,a12345</loc></url>
          <url><loc>https://www.cimri.com/bluetooth-kulaklik/en-ucuz-sony-wf-1000xm6-fiyatlari,a34567</loc></url>
          <url><loc>https://www.cimri.com/bluetooth-kulaklik/en-ucuz-sony-kulak-ustu-kulaklik-fiyatlari,a45678</loc></url>
        </urlset>"""
        indexes = {'phone': identity_index([{'name': 'Apple iPhone 16', 'slug': 'apple-iphone-16'}]),
                   'tws': identity_index([])}
        cards = cimri_sitemap_cards(xml, indexes, indexes)
        self.assertEqual([(row['category'], row['id'], row['catalogSlug']) for row in cards],
                         [('phone', 'a12345', 'apple-iphone-16'), ('tws', 'a34567', None)])

    def test_cimri_sitemap_cursor_rotates_only_after_successful_read(self):
        index = """<sitemapindex>
          <sitemap><loc>https://www.cimri.com/sitemaps/product_8.xml</loc></sitemap>
          <sitemap><loc>https://www.cimri.com/sitemaps/product_3.xml</loc></sitemap>
          <sitemap><loc>https://other.example/sitemaps/product_20.xml</loc></sitemap>
        </sitemapindex>"""
        reader = Mock()
        reader._read_document.side_effect = [(index, 'document_read', 200), ('<urlset/>', 'document_read', 200)]
        rows, _ = scan_product_sitemap(CIMRI, ['phone'], {'phone': identity_index([])}, {}, reader=reader)
        self.assertEqual((rows[0]['shard'], rows[0]['nextPage']), (8, 3))
        reader._read_document.side_effect = [(index, 'document_read', 200), (None, 'http_429', 429)]
        rows, _ = scan_product_sitemap(CIMRI, ['phone'], {'phone': identity_index([])},
                                     {f'{CIMRI}/product_sitemap': {'nextPage': 3}}, reader=reader)
        self.assertEqual((rows[0]['shard'], rows[0]['nextPage'], rows[0]['status']), (3, 3, 'http_429'))

    def test_cimri_sitemap_is_read_before_a_category_denial(self):
        index = '<sitemapindex><sitemap><loc>https://www.cimri.com/sitemaps/product_8.xml</loc></sitemap></sitemapindex>'
        xml = '<urlset><url><loc>https://www.cimri.com/cep-telefonlari/en-ucuz-example-fiyatlari,123456</loc></url></urlset>'
        reader = Mock()
        reader._read_document.side_effect = [(index, 'document_read', 200), (xml, 'document_read', 200),
                                             (None, 'http_403', 403)]
        with patch('competitor_catalog_gaps.comparison_reader', return_value=reader):
            reports, candidates = scan_source(CIMRI, ['phone'], {'phone': identity_index([])}, 1, 1, cimri_sitemap=True)
        self.assertEqual([r['status'] for r in reports], ['sitemap_read', 'http_403'])
        self.assertEqual([r['id'] for r in candidates], ['123456'])

    def test_cimri_sitemap_only_preserves_candidates_and_retries_the_failed_shard(self):
        index = ('<sitemapindex>' + ''.join(f'<sitemap><loc>https://{CIMRI}/sitemaps/product_{i}.xml</loc></sitemap>'
                                         for i in (8, 3, 1)) + '</sitemapindex>')
        xml = f'<urlset><url><loc>https://{CIMRI}/cep-telefonlari/en-ucuz-example-fiyatlari,123456</loc></url></urlset>'
        reader = Mock()
        reader._read_document.side_effect = [(index, 'document_read', 200), (xml, 'document_read', 200),
                                             (None, 'http_429', 429)]
        with patch('competitor_catalog_gaps.comparison_reader', return_value=reader):
            reports, candidates = scan_source(CIMRI, ['phone'], {'phone': identity_index([])}, 1, 1, sitemap_only=4)
        self.assertEqual((reports[0]['shardsRead'], reports[0]['nextPage'], reports[0]['status']), ([8], 3, 'http_429'))
        self.assertEqual([r['id'] for r in candidates], ['123456'])
        self.assertEqual(reader._read_document.call_count, 3)

    def test_epey_sitemap_and_category_cards_share_one_product_identity(self):
        url = f'https://{EPEY}/kulaklik/sony-wf-1000xm6.html'
        xml = f'<urlset><url><loc>{url}</loc></url><url><loc>https://other.example/kulaklik/other.html</loc></url></urlset>'
        indexes = {'tws': identity_index([])}
        rows = epey_sitemap_cards(xml, indexes, indexes)
        self.assertEqual([r['url'] for r in rows], [url])
        card = {**rows[0], 'id': '765432', 'title': 'Sony WF-1000XM6 Tam Kablosuz Kulaklık'}
        del card['titleFromSitemap']
        state = read_state(None)
        merge_state(state, [], [card, *rows], indexes, 'first')
        self.assertEqual([r['title'] for r in state['products'].values()], [card['title']])
        html = (f'<link rel="canonical" href="{url}"><h1><a href="{url}">{card["title"]}</a></h1>'
                f'<div id="oncelikli"><a href="https://{EPEY}/kulaklik/baglanti-tipi/tam-kablosuz/">Tam Kablosuz</a></div>')
        self.assertEqual(epey_earbud_title(html, url), (card['title'], None))
        self.assertEqual(epey_earbud_title(html.replace('id="oncelikli"', ''), url)[0], None)
