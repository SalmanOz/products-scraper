import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, call, patch

from ayaz_phone_source import AYAZ_BASE, AYAZ_COMPANY, AYAZ_HOST, AYAZ_TERMS
from comparison_phone_offers import (audit_queue, candidate_urls, capacity_priority, checked_offers, load_reviewed_profiles,
                                     hepsiburada_phone_leads, load_iphone_16_plus_profiles, n11_phone_leads,
                                     publishable_pair, published_offer_urls, run, trendyol_phone_leads)
from offer_ingestion_client import IngestionClientError
from ozgur_phone_source import OZGUR_HOST, OZGUR_TERMS, ozgur_offer
from unlisted_phone_audit import candidate_queue


MM = 'https://www.mediamarkt.com.tr/tr/product/_apple-iphone-16-plus-128-gb-akilli-telefon-mavi-146562812.html'
PZ = 'https://www.pazarama.com/apple-iphone-16-plus-128-gb-ultramarine-p-195949722813'
HB = 'https://www.hepsiburada.com/apple-iphone-16-plus-128-gb-mavi-p-HBCV00007ABC1'
VATAN = 'https://www.vatanbilgisayar.com/apple-iphone-16-plus-128gb-akilli-telefon.html'
NAME = 'Apple iPhone 16 Plus'
TITLE = NAME + ' 8 GB 128 GB 5G'
SPECS = {'ram_gb': 8, 'storage_gb': 128}
GTIN = '0195949722813'


class ComparisonPhoneOffersTest(unittest.TestCase):
    def test_n11_cash_conflict_rereads_one_source_declared_selector_in_discovery_only(self):
        base = 'https://www.n11.com/urun/apple-iphone-17-pro-max-2-tb-apple-turkiye-garantili-100819983'
        selected = base + '?magaza=erateknoloji&renk=gumus'
        html = Path('fixtures/n11-stale-shop-native-selected-sku-public.html').read_text()
        specs = {'ram_gb': 12, 'storage_gb': 2048, 'network_generation': '5g'}
        for discovery, expected_reads in ((False, [base]), (True, [base, selected])):
            transport = Mock()
            transport.resolved_url.side_effect = lambda url: url
            transport._read_document.return_value = html, 'document_read', 200
            with patch('comparison_phone_offers.n11_listing',
                       return_value=(None, 'schema_selected_cash_offer_mismatch')):
                _, offers, rejects = checked_offers([base], 'Apple iPhone 17 Pro Max', specs,
                                                     transport, discover_n11=discovery)
            self.assertEqual(offers, [])
            self.assertEqual([entry['url'] for entry in rejects], expected_reads)
            self.assertEqual(transport._read_document.call_args_list,
                             [call(url, 'www.n11.com') for url in expected_reads])

    def test_trendyol_category_recovery_uses_observed_exact_capacity_links(self):
        root = 'https://www.trendyol.com/cep-telefonu-x-c103498'
        model = 'https://www.trendyol.com/iphone-17e-cep-telefonu-x-c103498-a292-v10651727'
        base = 'https://www.trendyol.com/apple/iphone-17e-'
        black = base + '512gb-siyah-p-1113019279'
        white = base + '512gb-beyaz-p-1113019295'
        smaller = base + '256gb-siyah-p-1113019214'
        def card(url, capacity):
            return f'<a class="product-card" href="{url}"><h2 class="title">Apple iPhone 17e {capacity}GB</h2></a>'
        root_html = f'<h1 class="title">Cep Telefonu</h1><a href="{model}">iPhone 17e Cep Telefonu</a>'
        model_html = ('<h1 class="title">iPhone 17e Cep Telefonu</h1>'
                      '<input type="checkbox" id="checkbox-web-aggregations-checkbox-292-10651727-10651727" '
                      'aria-label="iPhone 17e" checked value="10651727">'
                      + card(black, 512) + card(white, 512) + card(smaller, 256))
        reader = Mock()
        reader.read_trendyol_category.side_effect = lambda url: ({root: root_html, model: model_html}[url], 'document_read', 200)
        cache = {}
        specs = {'storage_gb': 512, 'ram_gb': 8}
        self.assertEqual(trendyol_phone_leads('Apple iPhone 17e', specs, reader, {white}, cache)[0], [black])
        self.assertEqual(trendyol_phone_leads('Apple iPhone 17e', specs, reader, {black}, cache)[0], [white])
        self.assertEqual([call.args[0] for call in reader.read_trendyol_category.call_args_list], [root, model])

    def test_ayaz_selected_phone_requires_live_sku_price_stock_and_legal_identity(self):
        url = AYAZ_BASE + '/urun/samsung-galaxy-a27-5g-8-gb-256-gb-siyah'
        title = 'Samsung Galaxy A27 5G 8 GB 256 GB Siyah'
        gtin = '8806099359300'
        html = f'''<link rel="canonical" href="{url}">
          <div id="product-detail-container"><h1>{title}</h1>
            <div class="product-list-row"><span class="product-list-title">Barkod Kodu</span>
              <span class="product-list-content">{gtin}</span></div>
            <div class="product-price-wrapper"><div class="product-price-new">₺22.249,00</div></div>
            <a data-context="detail" data-selector="add-to-cart" data-product-id="1239"><span>Sepete Ekle</span></a>
          </div>
          <div itemscope itemtype="https://schema.org/Product">
            <span itemprop="name">{title}</span><span itemprop="sku">575675</span>
            <meta itemprop="gtin13" content="{gtin}">
            <div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
              <meta itemprop="price" content="22249.00"><meta itemprop="priceCurrency" content="TRY">
              <link itemprop="availability" href="https://schema.org/InStock"><span itemprop="url">{url}</span>
            </div><meta itemprop="itemCondition" content="http://schema.org/NewCondition">
          </div><script>pageParams = {{product: {{id: "1239",sku: "575675",parentId: "0"}}}};</script>'''
        specs = {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'}
        legal = f'<p>Ünvanı : <span>{AYAZ_COMPANY}</span></p>'
        transport = Mock()
        for page, terms, passed in ((html, legal, True),
                                    (html.replace('InStock', 'OutOfStock'), legal, False),
                                    (html.replace('₺22.249,00', '₺21.249,00'), legal, False),
                                    (html.replace('data-product-id="1239"', 'data-product-id="9000"'), legal, False),
                                    (html, '<p>Ünvanı : başka satıcı</p>', False)):
            transport._read_document.side_effect = [(page, 'document_read', 200), (terms, 'document_read', 200)]
            _, offers, rejects = checked_offers([url], 'Samsung Galaxy A27 5G', specs,
                                                 Mock(), direct={AYAZ_HOST: transport})
            self.assertEqual([(o['gtin13'], o['price']) for o in offers], [(gtin, 22249.0)] if passed else [])
            self.assertEqual(bool(rejects), not passed)
        transport._read_document.assert_any_call(AYAZ_TERMS, AYAZ_HOST, category=True)

    def test_phone_queue_uses_exact_storage_selectors_and_tb_url_hints(self):
        root = 'https://www.n11.com/urun/phone-'
        self.assertEqual(capacity_priority(root + '8gb128gb-123456', 128), 0)
        self.assertEqual(capacity_priority(root + '12-gb-1-tb-123456', '1024 GB'), 0)
        self.assertEqual(capacity_priority(root + '128-gb-123456?dahili-hafiza=256-gb', 256), 0)
        self.assertEqual(capacity_priority(root + '12-gb-128-gb-123456', 256), 2)
        self.assertEqual(capacity_priority(root + '8gb-123456', 128), 1)
        mm = ('https://www.mediamarkt.com.tr/tr/product/'
              '_samsung-galaxy-a27-5g-8256gb-akilli-telefon-siyah-1255283.html')
        self.assertEqual(capacity_priority(mm, 256), 0)
        self.assertEqual(capacity_priority(mm, 128), 2)

    def test_phone_queue_services_distinct_families_before_sibling_capacities(self):
        families = ('apple-iphone-16-plus', 'honor-600')
        state = {'products': {name: {'catalogFamily': name, 'category': 'phone',
                                    'retailerLeads': [HB, VATAN]} for name in families}}
        names = (families[0], families[0] + '-8gb-256gb', families[1])
        products = [{'slug': name, 'name': name, 'attributes': {}, 'fresh_offer_count': 0} for name in names]
        self.assertEqual([row['slug'] for row in audit_queue(state, products, maximum=2)], list(families))

    def test_phone_queue_keeps_one_source_gaps_and_prioritizes_compatible_urls(self):
        family = 'apple-iphone-16-plus'
        state = {'products': {
            'first': {'catalogFamily': family, 'category': 'phone', 'retailerLeads': [HB, VATAN]},
            'second': {'catalogFamily': 'honor-600', 'category': 'phone', 'retailerLeads': [
                'https://www.n11.com/urun/honor-600-512-gb-987654']}}}
        products = [{'slug': slug, 'name': slug, 'attributes': {'storage_gb': storage}, 'fresh_offer_count': 0}
                    for slug, storage in ((family, 256), (family + '-8gb-128gb', 128), ('honor-600', 512))]
        self.assertEqual(audit_queue(state, products), [products[1], products[2], products[0]])

    def test_phone_search_supplies_only_matching_new_destination_urls(self):
        search = Mock()
        cards = ''.join(f'<a href="{url}"><h2 class="product-item-title">{title}</h2></a>'
                        for url, title in (
                            ('/urun/iphone-16-plus-128-gb-123456', 'Apple iPhone 16 Plus 128 GB'),
                            ('/urun/iphone-16-plus-256-gb-234567', 'Apple iPhone 16 Plus 256 GB'),
                            ('/urun/iphone-16-128-gb-345678', 'Apple iPhone 16 128 GB')))
        search.read_n11_phone_search.return_value = cards, 'document_read', 200
        url = 'https://www.n11.com/urun/iphone-16-plus-128-gb-123456'
        self.assertEqual(n11_phone_leads(NAME, SPECS, search, ()),
                         ([url], {'reason': 'document_read', 'httpStatus': 200, 'destinations': 1}))
        search.read_n11_phone_search.assert_called_once_with('Apple iPhone 16 Plus 128 GB')
        self.assertEqual(n11_phone_leads(NAME, SPECS, search, [url])[0], [])

    def test_phone_queue_reserves_a_slot_for_an_unpriced_variant_without_store_leads(self):
        products = [{'slug': f'family-{i}', 'name': f'Family {i}',
                     'attributes': {'storage_gb': 128}, 'fresh_offer_count': 0} for i in range(7)]
        state = {'products': {}, 'phoneNativeLeads': {product['slug']: [HB] for product in products[:5]},
                 'phoneOfferAudits': {'family-5': '2026-11-12T05:00:00+00:00'}}
        self.assertEqual(audit_queue(state, products, maximum=4), [*products[:3], products[6]])

    def test_unpriced_variants_get_most_slots_even_when_priced_variants_are_unaudited(self):
        products = [{'slug': f'model-{i}', 'name': f'Model {i}', 'attributes': {'storage_gb': 128},
                     'fresh_offer_count': 0 if i < 5 else 2} for i in range(12)]
        state = {'products': {}, 'phoneNativeLeads': {row['slug']: [HB] for row in products if row is not products[4]},
                 'phoneOfferAudits': {row['slug']: '2026-11-12T07:00:00+00:00' for row in products[:4]}}
        self.assertEqual(audit_queue(state, products, maximum=4), [*products[:2], products[10], products[4]])
        self.assertEqual(set(row['slug'] for row in audit_queue(state, products, maximum=24)),
                         set(row['slug'] for row in products))

    def test_priced_slot_prioritizes_offers_near_expiry_without_repeating_todays_audit(self):
        products = [{'slug': slug, 'name': slug, 'attributes': {}, 'fresh_offer_count': int(checked is not None),
                     'latest_offer_checked_at': checked}
                    for slug, checked in (('unpriced', None), ('newer', '2026-11-11T10:00:00Z'),
                                          ('expiring', '2026-11-10T10:00:00Z'),
                                          ('already-checked', '2026-11-10T06:00:00Z'))]
        state = {'products': {}, 'phoneNativeLeads': {row['slug']: [HB] for row in products},
                 'phoneOfferAudits': {'already-checked': '2026-11-12T08:00:00+00:00'}}
        now = datetime(2026, 11, 12, 11, tzinfo=timezone.utc)
        self.assertEqual(audit_queue(state, products, maximum=4, now=now)[:2], [products[0], products[2]])

    def test_hepsiburada_phone_search_requires_the_exact_echo_and_catalog_memory(self):
        search = Mock()
        wide = HB.replace('128-gb', '256-gb')
        html = f'''<h1>"{NAME}" araması (2 ürün)</h1>
            <h2 data-test-id="title-1"><a href="{HB}" title="{NAME} 128 GB">{NAME} 128 GB</a></h2>
            <h2 data-test-id="title-2"><a href="{wide}" title="{NAME} 256 GB">{NAME} 256 GB</a></h2>'''
        search.read_hepsiburada_search.return_value = html, 'document_read', 200
        urls, report = hepsiburada_phone_leads(NAME, SPECS, search, ())
        self.assertEqual(urls, [HB])
        self.assertEqual(report['destinations'], 1)
        self.assertEqual(hepsiburada_phone_leads(NAME, SPECS, search, [HB])[0], [])
        cache = {}
        hepsiburada_phone_leads(NAME, SPECS, search, (), cache)
        search.read_hepsiburada_search.reset_mock()
        self.assertEqual(hepsiburada_phone_leads(NAME, {**SPECS, 'storage_gb': 256}, search, (), cache)[0], [wide])
        search.read_hepsiburada_search.assert_not_called()
        search.read_hepsiburada_search.return_value = html.replace('<h1>', '<h1>wrong '), 'document_read', 200
        self.assertEqual(hepsiburada_phone_leads(NAME, SPECS, search, ())[0], [])

    def test_hepsiburada_recovers_phone_with_capacity_search_after_accessory_results(self):
        model = 'Samsung Galaxy A55 5G'
        query = 'Samsung Galaxy A55 256 GB'
        url = ('https://www.hepsiburada.com/samsung-galaxy-a55-256-gb-8-gb-ram-samsung-turkiye-'
               'garantili-siyah-p-HBCV000062N7C3')
        title = 'Samsung Galaxy A55 256 GB 8 GB Ram (Samsung Türkiye Garantili) Siyah'
        search = Mock()
        accessory = 'Samsung Galaxy A15 A25 A35 A55 5G Hızlı Şarj Adaptörü'
        accessory_url = 'https://www.hepsiburada.com/samsung-galaxy-a55-5g-sarj-adaptoru-p-HBCV0000000001'
        def html(echo, label, destination):
            return (f'<h1>"{echo}" araması (1 ürün)</h1><h2 data-test-id="title-1">'
                    f'<a href="{destination}" title="{label}">{label}</a></h2>')
        search.read_hepsiburada_search.side_effect = [
            (html(model, accessory, accessory_url), 'document_read', 200),
            (html(query, title, url), 'document_read', 200),
        ]
        specs = {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'}
        urls, diagnostics = hepsiburada_phone_leads(model + ' 8GB 256GB', specs, search, ())
        self.assertEqual(urls, [url])
        self.assertEqual(diagnostics['query'], query)
        self.assertEqual(search.read_hepsiburada_search.call_args_list, [call(model), call(query)])

    def test_ozgur_uses_the_selected_upc_without_inventing_missing_sku_or_radio(self):
        url = 'https://ozguriletisimankara.com/urun/iphone-15-mavi-128-gb'
        photo = 'https://ozguriletisimankara.com/uploads/photo.jpg'
        title = 'iPhone 15 128 GB Mavi'
        product = {'@type': 'Product', 'url': url, 'name': title, 'brand': {'name': 'Apple'},
                   'mpn': 'MTP43TU/A', 'gtin12': '195949036583', 'image': [photo],
                   'offers': {'@type': 'Offer', 'url': url, 'priceCurrency': 'TRY', 'price': '59999.00',
                              'seller': {'name': 'Özgür İletişim'},
                              'availability': 'https://schema.org/InStock',
                              'itemCondition': 'https://schema.org/NewCondition'}}
        html = f'''
        <link rel="canonical" href="{url}">
        <script type="application/ld+json">{json.dumps(product)}</script>
        <div class="oi-product-detail" data-product-id="145" data-oi-view-variant="853">
          <img id="oi-product-main-img" src="{photo}">
          <div class="oi-pd-buybox">
            <h1 class="oi-pd-title">{title}</h1><div class="oi-pd-model"><span>MTP43TU/A</span></div>
            <span class="oi-pd-stockline is-in">Stokta</span>
            <span data-oi-price>59.999,00 TL</span>
            <button data-oi-add-cart data-id="145">Sepete Ekle</button>
          </div>
        </div>'''
        legal = '<p>Satıcı: Örnek İletişim Ticaret Limited Şirketi Vergi dairesi: Örnek</p>'
        direct = Mock()
        direct._read_document.side_effect = [(html, 'document_read', 200), (legal, 'document_read', 200)]
        specs = {'ram_gb': 6, 'storage_gb': 128, 'network_generation': '5g'}
        _, offers, rejections = checked_offers([url], 'Apple iPhone 15', specs, Mock(), direct={OZGUR_HOST: direct})
        self.assertFalse(rejections)
        self.assertEqual([(o['gtin13'], o['price']) for o in offers], [('0195949036583', 59999)])
        self.assertNotIn('source_variant_evidence', offers[0])
        direct._read_document.assert_any_call(OZGUR_TERMS, OZGUR_HOST, category=True)
        for invalid in (html.replace('195949036583', '195949036589'),
                        html.replace('195949036583', '195949036583", "gtin13": "0195949723001'),
                        html.replace('data-id="145"', 'data-id="146"'),
                        html.replace('59.999,00 TL', '59.990,00 TL')):
            with self.assertRaises(ValueError):
                ozgur_offer(invalid, legal, url=url, name='Apple iPhone 15', specs=specs)

    def test_public_phone_page_only_contributes_supported_offer_links(self):
        response = Mock(status_code=200)
        response.iter_content.return_value = [f'''<a href="{PZ}">Other link</a>
            <article data-current-offer>
              <a href="{MM}">Store</a><a href="{MM}">Duplicate</a>
              <a href="https://example.com/phone">Unrecognized source</a>
            </article>'''.encode()]
        with patch('comparison_phone_offers.requests.get') as get:
            get.return_value.__enter__.return_value = response
            self.assertEqual(published_offer_urls('apple-iphone-16-plus'), [MM])
        self.assertTrue(get.call_args.kwargs['stream'])
        self.assertFalse(get.call_args.kwargs['allow_redirects'])

    def test_teknosa_alternate_seller_is_verified_from_the_canonical_document(self):
        canonical = 'https://www.teknosa.com/apple-iphone-16-plus-128gb-mavi-akilli-telefon-p-125079728'
        url = canonical + '?shopId=2078'
        transport = Mock()
        transport._read_document.return_value = 'document', 'document_read', 200
        offer = {'url': url, 'platform': 'Teknosa', 'merchant': 'Gençpa', 'gtin13': GTIN,
                 '_legal_name': 'gencpa', 'price': 85000, 'availability': 'in_stock',
                 'currency': 'TRY', 'source_title': TITLE}
        with patch('comparison_phone_offers.teknosa_selected', return_value=offer) as selected:
            _, found, rejected = checked_offers([url], NAME, SPECS, transport)
        transport._read_document.assert_called_once_with(canonical, 'www.teknosa.com')
        selected.assert_called_once_with('document', url, NAME, SPECS)
        self.assertEqual([o['url'] for o in found], [url])
        self.assertFalse(rejected)

    def test_comparison_urls_never_reach_retailer_verifier(self):
        state = {'products': {'x': {'catalogFamily': 'apple-iphone-16-plus', 'category': 'phone',
                                   'retailerLeads': ['https://urunai.com/telefon/apple/iphone-16-plus', MM, PZ, PZ]}}}
        self.assertEqual(candidate_urls(state, 'apple-iphone-16-plus-8gb-256gb'), [MM, PZ])

    def test_unlisted_audit_finds_cross_platform_leads_without_a_catalog_slug(self):
        slug = 'apple-iphone-16-plus'
        state = {'products': {
            'one': {'category': 'phone', 'title': TITLE, 'retailerLeads': [HB]},
            'two': {'category': 'phone', 'title': NAME + ' 5G 128 GB', 'retailerLeads': [MM]},
            'three': {'category': 'phone', 'title': NAME + ' 8 GB 128 GB', 'retailerLeads': [PZ, HB]},
        }}
        candidate = {'name': NAME, 'slug': slug, 'evidenceUrl': 'https://www.apple.com/tr/iphone-16-plus/'}
        self.assertEqual(candidate_urls(state, slug, expanded=True), [])
        count, queue = candidate_queue(state, [], [candidate, candidate], 4)
        self.assertEqual(count, 1)
        self.assertEqual(queue[0]['leadPlatforms'], 2)
        self.assertEqual(candidate_queue(state, [{'slug': slug + '-8gb-256gb'}], [candidate], 4), (0, []))
        with self.assertRaisesRegex(ValueError, 'read-only'):
            run(state, [slug], products=[], publish=True, unlisted_audit=True)

    def test_phone_discovery_prioritizes_exact_capacity_links_and_keeps_family_fallbacks(self):
        slug = 'apple-iphone-16-plus-8gb-256gb'
        state = {'products': {
            'family': {'category': 'phone', 'catalogFamily': 'apple-iphone-16-plus',
                       'retailerLeads': [MM, PZ]},
            'exact': {'category': 'phone', 'catalogFamily': 'apple-iphone-16-plus', 'catalogSlug': slug,
                      'retailerLeads': [PZ, HB]},
            'other-capacity': {'category': 'phone', 'catalogSlug': 'apple-iphone-16-plus-8gb-512gb',
                               'retailerLeads': [VATAN]},
        }}
        self.assertEqual(candidate_urls(state, slug, expanded=True), [PZ, HB, MM])

    def test_public_queue_admits_new_direct_sources_and_requires_their_exact_barcodes(self):
        slug = 'apple-iphone-16-plus'
        product = {'slug': slug, 'name': NAME, 'attributes': {}}
        state = {'products': {'x': {'catalogFamily': slug, 'category': 'phone', 'retailerLeads': [HB, VATAN]}}}
        self.assertEqual(audit_queue(state, [product]), [product])
        self.assertEqual(candidate_urls(state, slug), [])
        self.assertEqual(candidate_urls(state, slug, expanded=True), [HB, VATAN])
        reader = Mock()
        reader._read_document.return_value = 'document', 'document_read', 200
        direct = {'www.hepsiburada.com': reader, 'www.vatanbilgisayar.com': reader}
        base = {'price': 89599, 'availability': 'in_stock', 'currency': 'TRY', 'source_title': TITLE}
        vatan = {**base, 'gtin13': GTIN, 'url': VATAN, 'platform': 'Vatan Bilgisayar',
                 'merchant': 'Vatan Bilgisayar', '_legal_name': 'vatan bilgisayar sanayi ve ticaret anonim sirketi'}
        hb = {**base, 'url': HB, 'platform': 'Hepsiburada', 'merchant': 'Hepsiburada',
              '_legal_name': 'd market elektronik hizmetler ve ticaret anonim sirketi'}
        with patch('comparison_phone_offers.parse_catalog_buy_box') as parse, \
             patch('comparison_phone_offers.selected_vatan_tws', return_value=vatan), \
             patch('comparison_phone_offers.verify_seller', return_value=True):
            parse.return_value = ({**hb, 'gtin13': None}, 'observed')
            self.assertEqual(checked_offers([HB, VATAN], NAME, SPECS, Mock(), direct=direct)[0], [])
            parse.return_value = ({**hb, 'gtin13': GTIN}, 'observed')
            self.assertEqual(len(checked_offers([HB, VATAN], NAME, SPECS, Mock(), direct=direct)[0]), 2)

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

    def test_publisher_accepts_only_proven_different_gtin_collisions(self):
        offers = [
            {'price': 89599, 'url': MM, 'merchant': 'Seller A', 'platform': 'MediaMarkt',
             'gtin13': GTIN, '_legal_name': 'seller a', '_tax_key': '1'},
            {'price': 90699, 'url': PZ, 'merchant': 'Seller B', 'platform': 'Pazarama',
             'gtin13': GTIN, '_legal_name': 'seller b', '_tax_key': '2'},
        ]
        clean = {'complete': True, 'blocked': False, 'reasons': []}
        collision = {'complete': True, 'blocked': True,
                     'reasons': ['proposed_seller_on_other_url_or_platform'],
                     'proposed_seller_other_url_rows': 1, 'proposed_seller_historical_other_url_rows': 0}
        verified = {'gtin13': GTIN, 'distinct_variant_other_url_rows': 1,
                    'unresolved_other_url_rows': 0, 'conflicting_exact_url_rows': 0}
        for scope in (verified, {**verified, 'distinct_variant_other_url_rows': 0, 'unresolved_other_url_rows': 1},
                      {**verified, 'conflicting_exact_url_rows': 1}):
            client = Mock()
            client.get_raw_seller_collision_proof.side_effect = [{**collision, 'verified_variant_scope': scope}, clean]
            with patch('comparison_phone_offers.selected_sku_evidence', side_effect=[{'gtin13': GTIN}, None]):
                self.assertEqual(publishable_pair(offers, client, {'id': 1})[0], offers if scope == verified else [])
            self.assertEqual(client.get_raw_seller_collision_proof.call_args_list[0].kwargs, {'gtin13': GTIN})

    def test_reviewed_capacity_rejects_a_pair_with_a_different_barcode(self):
        slug = 'apple-iphone-16-plus'
        product = {'id': 1, 'slug': slug, 'name': NAME, 'attributes': {}}
        profiles = {slug: {'urls': [MM, PZ], 'gtin': GTIN}}
        wrong = [{'url': url, 'gtin13': '0195949722240'} for url in (MM, PZ)]
        client = Mock()
        with patch('comparison_phone_offers.checked_offers', return_value=(wrong, wrong, [])):
            report = run({}, [slug], client, publish=True, products=[product], profiles=profiles)
        self.assertIsNone(report[0]['sameGtin'])
        self.assertEqual(report[0]['rejections'], [
            {'url': url, 'reason': 'reviewed_gtin_mismatch', 'observedGtin': '0195949722240'} for url in (MM, PZ)])
        client.get_raw_seller_collision_proof.assert_not_called()
        client.ingest_offers.assert_not_called()

    def test_post_commit_readback_failure_keeps_receipt_without_retrying_writes(self):
        slug = 'apple-iphone-16-plus'
        other_slug = 'apple-iphone-16-plus-8gb-256gb'
        product = {'id': 1, 'slug': slug, 'name': NAME, 'attributes': {}}
        products = [product, {**product, 'id': 2, 'slug': other_slug}]
        profiles = {key: {'urls': [MM, PZ], 'gtin': GTIN} for key in (slug, other_slug)}
        evidence = {'gtin13': GTIN, 'observed_at': datetime.now(timezone.utc).isoformat(),
                    'availability': 'in_stock', 'currency': 'TRY', 'source_title': TITLE}
        pair = [{**evidence, 'url': MM, 'merchant': 'Seller A', 'platform': 'MediaMarkt',
                 '_legal_name': 'seller a', 'price': 89599},
                {**evidence, 'url': PZ, 'merchant': 'Seller B', 'platform': 'Pazarama',
                 '_legal_name': 'seller b', 'price': 90699}]
        for response, reason in (({'productCoverage': []}, 'readback_missing_after_commit'),
                                 (IngestionClientError('Readback unavailable'), 'readback_unavailable_after_commit')):
            with self.subTest(reason=reason):
                client = Mock()
                client.get_raw_seller_collision_proof.return_value = {'complete': True, 'blocked': False}
                client.ingest_offers.return_value = {'committed': True, 'accepted': 2}
                client.get_offer_identity_readiness.side_effect = [response]
                with patch('comparison_phone_offers.checked_offers', return_value=(pair, pair, [])):
                    (result,) = run({}, [slug, other_slug], client, publish=True, products=products, profiles=profiles)
                self.assertEqual((result['slug'], result['published'], result['readback'], result['publicationReason']),
                                 (slug, 2, 'unverified', reason))
                client.ingest_offers.assert_called_once()
                self.assertTrue(client.ingest_offers.call_args.kwargs['require_all_accepted'])

    def test_reviewed_candidate_file_accepts_only_two_platforms_and_no_price(self):
        slug = 'apple-iphone-16-plus'
        profile = {'slug': slug, 'gtin': GTIN, 'urls': [MM, PZ]}
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'pairs.json'
            path.write_text(json.dumps([profile]))
            self.assertEqual(load_reviewed_profiles(path), {slug: profile})
            vatan_profile = {**profile, 'urls': [MM, VATAN]}
            path.write_text(json.dumps([vatan_profile]))
            self.assertEqual(load_reviewed_profiles(path), {slug: vatan_profile})
            hb_profile = {**profile, 'urls': [MM, HB]}
            path.write_text(json.dumps([hb_profile]))
            self.assertEqual(load_reviewed_profiles(path), {slug: hb_profile})
            for invalid in ({**profile, 'price': 1}, {**profile, 'urls': [MM, MM, PZ]},
                            {**profile, 'urls': [MM, VATAN + '?secret=private']}):
                path.write_text(json.dumps([invalid]))
                with self.assertRaises(ValueError):
                    load_reviewed_profiles(path)

    def test_daily_iphone_refresh_rejects_an_expanded_or_changed_scope(self):
        profiles = list(load_iphone_16_plus_profiles().values())
        self.assertEqual(len(profiles), 2)
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'pairs.json'
            extra = {**profiles[0], 'slug': 'another-phone'}
            changed = {**profiles[0], 'gtin': GTIN}
            for rows in (profiles[:1], [*profiles, extra], [changed, profiles[1]]):
                path.write_text(json.dumps(rows))
                with self.assertRaisesRegex(ValueError, 'exactly the approved slugs and GTINs'):
                    load_iphone_16_plus_profiles(path)
