import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from comparison_phone_offers import (audit_queue, candidate_urls, capacity_priority, checked_offers, load_reviewed_profiles,
                                     n11_phone_leads, publishable_pair, published_offer_urls, run)
from ozgur_phone_source import OZGUR_HOST, OZGUR_TERMS, ozgur_offer


MM = 'https://www.mediamarkt.com.tr/tr/product/_apple-iphone-16-plus-128-gb-akilli-telefon-mavi-146562812.html'
PZ = 'https://www.pazarama.com/apple-iphone-16-plus-128-gb-ultramarine-p-195949722813'
HB = 'https://www.hepsiburada.com/apple-iphone-16-plus-128-gb-mavi-p-HBCV00007ABC1'
VATAN = 'https://www.vatanbilgisayar.com/apple-iphone-16-plus-128gb-akilli-telefon.html'
NAME = 'Apple iPhone 16 Plus'
TITLE = NAME + ' 8 GB 128 GB 5G'
SPECS = {'ram_gb': 8, 'storage_gb': 128}
GTIN = '0195949722813'


class ComparisonPhoneOffersTest(unittest.TestCase):
    def test_phone_queue_uses_exact_storage_selectors_and_tb_url_hints(self):
        root = 'https://www.n11.com/urun/phone-'
        self.assertEqual(capacity_priority(root + '8gb128gb-123456', 128), 0)
        self.assertEqual(capacity_priority(root + '12-gb-1-tb-123456', '1024 GB'), 0)
        self.assertEqual(capacity_priority(root + '128-gb-123456?dahili-hafiza=256-gb', 256), 0)
        self.assertEqual(capacity_priority(root + '12-gb-128-gb-123456', 256), 2)
        self.assertEqual(capacity_priority(root + '8gb-123456', 128), 1)

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
        wrong = [{'gtin13': '0195949722240'}, {'gtin13': '0195949722240'}]
        client = Mock()
        with patch('comparison_phone_offers.checked_offers', return_value=(wrong, wrong, [])):
            report = run({}, [slug], client, publish=True, products=[product], profiles=profiles)
        self.assertIsNone(report[0]['sameGtin'])
        client.get_raw_seller_collision_proof.assert_not_called()
        client.ingest_offers.assert_not_called()

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
