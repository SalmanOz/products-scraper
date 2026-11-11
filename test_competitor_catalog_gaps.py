import unittest

from competitor_catalog_gaps import (AKAKCE, CIMRI, EPEY, catalog_match, category_url, comparison_cards, comparison_retailer_leads,
                                     identity_index, in_scope, merge_state, read_state)


class CompetitorCatalogGapsTest(unittest.TestCase):
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
            {**card, 'firstSeenAt': 'first', 'lastSeenAt': 'first', 'catalogSlug': 'example'},
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
