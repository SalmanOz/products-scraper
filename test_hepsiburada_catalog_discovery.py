from copy import deepcopy
import unittest
from unittest.mock import patch

from hepsiburada_catalog_discovery import CATEGORY_URL, card_destination, category_identity, parse_category_document, discovery_links, discover_product_siblings, discover_product_sellers
from priority_retailer_transport import PriorityRetailerTransport
from test_pasaj_source import Response
from test_teknosa_catalog_discovery import PRODUCT

PDP = 'https://www.hepsiburada.com/apple-iphone-15-128-gb-mavi-p-HBCV00004X9ZCK'
SAMSUNG = 'https://www.hepsiburada.com/samsung-cep-telefonlari-xc-371965-b8848'


def document(brand=False):
    # Whitelisted public Nov 4 category structure; no prices or ad/session IDs.
    return (f'<h1>{"Samsung Telefonlar" if brand else "Cep Telefonu Fiyatları ve Modelleri"}</h1>'
            f'<h2 data-test-id="title-1"><a title="Apple iPhone 15 128 GB Mavi" href="{PDP}">'
            'Apple iPhone 15 128 GB Mavi</a></h2>'
            f'<a href="{SAMSUNG}">Samsung</a><a href="{SAMSUNG}">Samsung</a>'
            '<a href="/colezium-cep-telefonlari-xc-371965-b6647">Colezium</a>')


class HepsiburadaDiscoveryTests(unittest.TestCase):
    def test_observed_bare_vivo_heading_matches_duplicated_brand_taxonomy_without_losing_model_qualifiers(self):
        url = 'https://www.hepsiburada.com/vivo-vivo-x300-android-telefonlar-xc-60005201-b8855-t6828'
        diagnostic = {}
        parse_category_document('<h1>Vivo X300</h1>', url, diagnostic)
        self.assertEqual(diagnostic['status'], 'parsed')
        for title in ['Vivo', 'X300', 'Vivo X300 Pro', 'Vivo X300 Ultra', 'Vivo X200', 'Xiaomi X300']:
            diagnostic = {}
            parse_category_document('<h1>' + title + '</h1>', url, diagnostic)
            self.assertEqual(diagnostic['status'], 'category_identity_missing', title)

    def test_observed_taxonomy_move_is_only_a_same_model_same_page_hint(self):
        from hepsiburada_catalog_discovery import category_redirect_target
        old = 'https://www.hepsiburada.com/samsung-galaxy-s25-fe-android-telefonlar-xc-60005201-b8848-t6765'
        new = old.replace('t6765', 't6774')  # Publicly observed Nov 4 canonical taxonomy move.
        self.assertEqual(category_redirect_target(old, new), new)
        for changed in [new.replace('s25-fe', 's25'), new.replace('s25', 's26'),
                        new.replace('samsung-', 'xiaomi-'), new.replace('.com/', '.com.evil.test/'),
                        new + '?sayfa=2', new + '?token=private', new + '#fragment', PDP, old]:
            self.assertIsNone(category_redirect_target(old, changed), changed)
        self.assertIsNone(category_redirect_target(PDP, PDP[:-1] + 'L'))

    def test_observed_radio_suffix_omission_only_opens_a_broader_category_not_another_radio_sku(self):
        from hepsiburada_catalog_discovery import category_redirect_target
        old = 'https://www.hepsiburada.com/xiaomi-redmi-note-12-pro-5g-android-telefonlar-xc-60005201-b1123-t236'
        canonical = 'https://www.hepsiburada.com/xiaomi-redmi-note-12-pro-android-telefonlar-xc-60005201-b1123-t230'
        self.assertEqual(category_redirect_target(old, canonical), canonical)
        d = {}
        parse_category_document('<h1>Xiaomi Redmi Note 12 Pro Fiyatları</h1>', canonical, d)
        self.assertEqual(d['status'], 'parsed')
        for target in [canonical.replace('12-pro', '13-pro'), canonical.replace('12-pro', '12-pro-plus'),
                       canonical.replace('12-pro-', '12-pro-4g-'), canonical + '?sayfa=2',
                       'https://www.hepsiburada.com/android-telefonlar-c-60005201', PDP]:
            self.assertIsNone(category_redirect_target(old, target), target)
        self.assertIsNone(category_redirect_target(canonical, old))

    def test_router_discards_redirected_body_reads_one_observed_target_and_bounds_second_hop(self):
        from sweep_priority_catalog import sweep
        from test_priority_candidates import Client
        old = 'https://www.hepsiburada.com/samsung-galaxy-s25-fe-android-telefonlar-xc-60005201-b8848-t6765'
        new = old.replace('t6765', 't6774')
        third = old.replace('t6765', 't6775')
        for second_hop in (False, True):
            client = Client()
            client.product.update(name='Samsung Galaxy S25 FE', attributes={'ram_gb': 8, 'storage_gb': 256}, offer_targets=[])
            client.get_public_price_priority = lambda: {}
            visited = []
            class Transport:
                def read_hepsiburada_category(self, url):
                    visited.append(url)
                    if url == CATEGORY_URL:
                        return '<h1>Cep Telefonu Fiyatları ve Modelleri</h1><a href="' + old + '">S25 FE</a>', 'document_read', 200
                    if url == old or second_hop:
                        return None, 'browser_changed_url', 200
                    return '<h1>Samsung Galaxy S25 FE Özellikleri</h1>', 'document_read', 200
                def category_redirect_url(self, url):
                    return {old: new, new: third}.get(url)
                def report(self):
                    return {}
            result = sweep(client=client, transport=Transport(), discover_hepsiburada=True, category_audit_only=True)
            self.assertEqual(visited, [CATEGORY_URL, old, new])
            report = result['hepsiburada_category_discovery']
            self.assertEqual(report['redirected_categories'], {old: new})
            self.assertEqual(report['branch_queue_complete'], not second_hop)
            self.assertFalse(result['writes'])
            self.assertEqual(result['rows'], [])

    def test_all_observed_run36_heading_failures_parse_without_accepting_another_model(self):
        import json
        from pathlib import Path
        rows = []
        for fixture, count in [('hepsiburada-observed-category-headings.json', 13),
                               ('hepsiburada-observed-family-headings.json', 11)]:
            observed = json.loads(Path(__file__).with_name('fixtures').joinpath(fixture).read_text())['categories']
            self.assertEqual(len(observed), count)
            rows.extend(observed)
        for row in rows:
            diagnostic = {}
            parse_category_document('<h1>' + row['heading'] + '</h1>', row['url'], diagnostic)
            self.assertEqual(diagnostic['status'], 'parsed', row)
        for name in ['Redmi Note 15 Pro Max Özellikleri', 'Redmi Note 14 Pro Özellikleri',
                     'Redmi Note 15 Özellikleri', 'Samsung Note 15 Pro Özellikleri']:
            d = {}
            parse_category_document('<h1>' + name + '</h1>', rows[2]['url'], d)
            self.assertEqual(d['status'], 'category_identity_missing', name)
        for brand, title in [('samsung', '2026 Samsung Televizyon Modelleri'),
                             ('honor', 'Honor Magic8 Telefon Fiyatları'),
                             ('poco', 'POCO Samsung Telefon Fiyatları'),
                             ('redmi', 'Xiaomi Telefon Fiyatları')]:
            url = f'https://www.hepsiburada.com/{brand}-android-telefonlar-xc-60005201-b8852'
            self.assertEqual(parse_category_document('<h1>' + title + '</h1>', url)[0], [], title)

    def test_observed_brand_pagination_can_use_equivalent_family_routes_but_not_change_brand(self):
        cases = [
            ('https://www.hepsiburada.com/apple-cep-telefonlari-xc-371965-b8849',
             'Apple Cep Telefonu Fiyatları ve Modelleri',
             'https://www.hepsiburada.com/iphone-ios-telefonlar-c-60005202?sayfa=2', 'iPhone iOS Telefonlar'),
            ('https://www.hepsiburada.com/honor-cep-telefonlari-xc-371965-b8852',
             'HONOR Cep Telefonu Fiyatları ve Modelleri',
             'https://www.hepsiburada.com/honor-android-telefonlar-xc-60005201-b8852?sayfa=2', 'Honor Android Telefonlar')]
        for source, title, target, next_title in cases:
            html = document().replace('Cep Telefonu Fiyatları ve Modelleri', title)
            html += '<link rel="next" href="' + target + '">'
            diagnostic = {}
            self.assertEqual(parse_category_document(html, source, diagnostic)[1], target)
            self.assertEqual(diagnostic['status'], 'parsed')
            d = {}
            parse_category_document('<h1>' + next_title + '</h1>', target, d)
            self.assertEqual(d['status'], 'parsed')
            for altered in [target.replace('sayfa=2', 'sayfa=3'), target + '&token=private',
                            target.replace('www.hepsiburada.com', 'evil.test'),
                            'https://www.hepsiburada.com/samsung-android-telefonlar-xc-60005201-b8848?sayfa=2']:
                d = {}
                self.assertIsNone(parse_category_document(html.replace(target, altered), source, d)[1])
                self.assertEqual(d['status'], 'continuation_unrecognized')

    def test_observed_model_editorial_headings_keep_full_generation_and_pro_max_boundaries(self):
        cases = [
            ('https://www.hepsiburada.com/iphone-16-pro-iphone-ios-telefonlar-xc-60005202-t29',
             'iPhone 16 Pro Özellikleri'),
            ('https://www.hepsiburada.com/iphone-15-pro-iphone-ios-telefonlar-xc-60005202-t2',
             'iPhone 15 Pro Modelleri 2026')]
        for url, title in cases:
            html = document().replace('Cep Telefonu Fiyatları ve Modelleri', title)
            diagnostic = {}
            self.assertEqual(len(parse_category_document(html, url, diagnostic)[0]), 1)
            self.assertEqual(diagnostic['status'], 'parsed')
            for changed in [title.replace(' Pro', ' Pro Max'), title.replace(' Pro', ''),
                            title.replace('16', '17').replace('15', '14'), title + ' ve Aksesuarları']:
                if changed == title:
                    continue
                altered = html.replace(title, changed)
                self.assertEqual(parse_category_document(altered, url)[0], [], (url, changed))
            self.assertEqual(parse_category_document(html, SAMSUNG)[0], [])

    def test_other_seller_selector_requires_visible_link_and_same_selected_sku_inventory_without_prices(self):
        import html as html_module
        import json
        from pathlib import Path
        fixture = json.loads(Path(__file__).with_name('fixtures').joinpath('hepsiburada-public-seller-selector-hints.json').read_text())
        def document(data):
            script = '<script id="reduxStore">' + json.dumps({'productState': {'product': data['product']}}) + '</script>'
            return script + ''.join('<div data-hbus="' + html_module.escape(json.dumps({'data': p['event']}), quote=True) + '">' +
                ''.join(f'<a href="{a["href"]}">{a["text"]}</a>' for a in p['links']) + '<button>Ürüne git</button></div>'
                for p in data['panels'])
        found = discover_product_sellers(document(fixture), fixture['url'])
        self.assertEqual([r['source_url'] for r in found], [fixture['url'] + '?magaza=Ada+iletisim+mersin'])
        self.assertEqual(found[0]['discovery_merchant_id'], fixture['product']['listings'][0]['merchantId'])
        self.assertNotIn('price', found[0])
        for path, value in [
            (('panels', 0, 'event', 'page_value'), 'HBCV0000OTHER00'),
            (('panels', 0, 'event', 'listing_id'), 'wrong'),
            (('panels', 0, 'event', 'merchant_id'), 'wrong'),
            (('panels', 0, 'event', 'product_id'), 'wrong'),
            (('panels', 0, 'links', 0, 'text'), 'Other shop'),
            (('panels', 0, 'links', 0, 'href'), 'https://evil.test/magaza/shop'),
            (('product', 'listings', 0, 'merchantId'), 'wrong'),
            (('product', 'listings', 0, 'listingId'), 'wrong'),
            (('product', 'listings', 0, 'isSalable'), False),
            (('product', 'listings', 0, 'isBundle'), True),
        ]:
            changed = deepcopy(fixture)
            node = changed
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
            self.assertEqual(discover_product_sellers(document(changed), fixture['url']), [], path)
        self.assertEqual(discover_product_sellers(document(fixture), fixture['url'] + '?magaza=Other'), [])
        changed = deepcopy(fixture)
        changed['panels'].append(deepcopy(changed['panels'][0]))
        self.assertEqual(discover_product_sellers(document(changed), fixture['url']), [])

    def test_sibling_destinations_require_explicit_same_memory_phone_links_not_variant_prices(self):
        import json
        root = 'https://www.hepsiburada.com/apple-iphone-11-64-gb-beyaz-p-HBV0000120X66'
        target = 'https://www.hepsiburada.com/apple-iphone-11-64-gb-siyah-p-HBV0000120X65'
        product = {'name': 'Apple iPhone 11', 'slug': 'apple-iphone-11',
                   'attributes': {'ram_gb': 4, 'storage_gb': 64}}
        sibling = {'name': 'Apple iPhone 11 64 GB Siyah', 'sku': 'HBV0000120X65',
                   'url': target, 'isDefault': False, 'isInStock': True, 'isPreOrder': False,
                   'price': 1, 'properties': [{'name': 'Kapasite', 'valueObject': {'actualValue': '64 GB'}}]}
        def html(row=sibling, sku='HBV0000120X66'):
            return '<script id="reduxStore">' + json.dumps({'productState': {'product': {
                'productId': 'HB00000NSBZ4', 'sku': sku, 'variants': [row]}}}) + '</script>'
        found = discover_product_siblings(html(), root, product)
        self.assertEqual([r['source_url'] for r in found], [target])
        self.assertNotIn('price', found[0])
        self.assertNotIn('merchant', found[0])
        for patch in [{'url': target + '?magaza=Other'}, {'url': target.replace('.com/', '.com.evil.test/')},
                      {'url': None}, {'sku': 'HBV0000120X67'}, {'isDefault': True},
                      {'isInStock': False}, {'isPreOrder': True}, {'name': 'Apple iPhone 11 Pro 64 GB Siyah'},
                      {'name': 'Apple Yenilenmiş iPhone 11 64 GB Siyah'},
                      {'name': 'Apple iPhone 11 128 GB Siyah'},
                      {'properties': [{'name': 'Kapasite', 'valueObject': {'actualValue': '128 GB'}}]}]:
            self.assertEqual(discover_product_siblings(html({**sibling, **patch}), root, product), [], patch)
        self.assertEqual(discover_product_siblings(html(sku='HBV0000120X67'), root, product), [])
        self.assertEqual(discover_product_siblings(html() + html(), root, product), [])

    def test_public_ad_link_discovers_only_its_explicit_canonical_product_without_tracking(self):
        from urllib.parse import urlencode
        tracker = 'https://adservice.hepsiburada.com/event/api/v1/track?' + urlencode({
            'event': 'not-retained', 'anonymousId': 'not-retained',
            'redirect': PDP + '?adsUniqueId=not-retained&magaza=OldSeller'})
        self.assertEqual(card_destination(tracker), PDP)
        rows, _ = parse_category_document(document().replace(PDP, tracker.replace('&', '&amp;')), CATEGORY_URL)
        self.assertEqual([r['source_url'] for r in rows], [PDP])
        self.assertNotIn('not-retained', str(rows))
        for bad in [tracker.replace('adservice.hepsiburada.com', 'evil.test'),
                    tracker.replace('event/api/v1/track', 'private/track'),
                    tracker + '&redirect=https%3A%2F%2Fevil.test',
                    'https://adservice.hepsiburada.com/event/api/v1/track?' + urlencode({'redirect':'https://evil.test/phone'})]:
            self.assertIsNone(card_destination(bad))

    def test_public_card_and_observed_brand_links_never_supply_prices(self):
        diagnostic = {}
        cards, next_page = parse_category_document(document() + '<b>1 TL</b>', CATEGORY_URL, diagnostic)
        self.assertEqual([c['source_url'] for c in cards], [PDP])
        self.assertNotIn('price', cards[0])
        self.assertNotIn('seller', cards[0])
        self.assertIsNone(next_page)
        self.assertEqual(diagnostic['scope'], 'bounded_observed_category_brand_and_model_pages')
        self.assertEqual(discovery_links(document(), CATEGORY_URL, [{'name': 'Samsung Galaxy S26'}]), [SAMSUNG])
        self.assertEqual(discovery_links(document(), CATEGORY_URL, [PRODUCT]), [])
        self.assertEqual(len(parse_category_document(document(True), SAMSUNG)[0]), 1)
        # Live Apple/Honor brand pages use this longer first-page heading.
        for brand, url in [('Apple', 'https://www.hepsiburada.com/apple-cep-telefonlari-xc-371965-b8849'),
                           ('HONOR', 'https://www.hepsiburada.com/honor-cep-telefonlari-xc-371965-b8852')]:
            html = document().replace('Cep Telefonu Fiyatları ve Modelleri', brand + ' Cep Telefonu Fiyatları ve Modelleri')
            self.assertEqual(len(parse_category_document(html, url)[0]), 1)

    def test_ads_selectors_changed_titles_and_nonphone_branches_are_rejected(self):
        diagnostic = {}
        unknown = document().replace('Cep Telefonu Fiyatları ve Modelleri', 'Cep Telefonları ve Akıllı Telefonlar')
        self.assertEqual(parse_category_document(unknown, CATEGORY_URL, diagnostic)[0], [])
        self.assertEqual(diagnostic['observed_headings'], ['Cep Telefonları ve Akıllı Telefonlar'])
        self.assertEqual(diagnostic['status'], 'category_identity_missing')
        for changed in [document().replace(PDP, 'https://adservice.hepsiburada.com/event/api/v1/track'),
                        document().replace(PDP, PDP + '?magaza=SomeShop'),
                        document().replace('title="Apple', 'title="Samsung'),
                        document().replace('<h1>', '<h3>').replace('</h1>', '</h3>')]:
            self.assertEqual(parse_category_document(changed, CATEGORY_URL)[0], [])
        for url in [CATEGORY_URL + '?sayfa=02', CATEGORY_URL + '#x', SAMSUNG + '?filtreler=satici:x',
                    SAMSUNG.replace('www.hepsiburada.com', 'evil.test'), SAMSUNG.replace('371965', '123456'),
                    SAMSUNG.replace('samsung-', 'colezium-')]:
            self.assertIsNone(category_identity(url))

    def test_observed_model_taxonomies_prioritize_catalog_needs_and_preserve_model_boundaries(self):
        iphone = 'https://www.hepsiburada.com/iphone-11-iphone-ios-telefonlar-xc-60005202-t12'
        pro = 'https://www.hepsiburada.com/iphone-16-pro-iphone-ios-telefonlar-xc-60005202-t29'
        pro_max = 'https://www.hepsiburada.com/iphone-16-pro-max-iphone-ios-telefonlar-xc-60005202-t27'
        a35 = 'https://www.hepsiburada.com/samsung-galaxy-a35-android-telefonlar-xc-60005201-b8848-t317'
        links = ''.join(f'<a href="{u}">Phone</a>' for u in [a35, pro_max, pro, iphone])
        products = [{'name': 'Apple iPhone 11'}, {'name': 'Apple iPhone 16 Pro'},
                    {'name': 'Samsung Galaxy A35 5G'}]
        self.assertEqual(discovery_links(document() + links, CATEGORY_URL, products),
                         [iphone, pro, a35, SAMSUNG])
        for url, heading in [(iphone, 'iPhone 11 İndirimleri'),
                             (a35, 'Samsung Galaxy A35 Android Telefon Modelleri ve Özellikleri')]:
            html = document().replace('Cep Telefonu Fiyatları ve Modelleri', heading)
            self.assertEqual([c['source_url'] for c in parse_category_document(html, url)[0]], [PDP])
            self.assertEqual(parse_category_document(html.replace(heading, 'Other Phone'), url)[0], [])
        for bad in [iphone.replace('60005202', '60005201'),
                    a35.replace('60005201', '60005202'), a35.replace('samsung-', 'colezium-'),
                    iphone + '?q=iphone', iphone.replace('-t12', '-t012')]:
            self.assertIsNone(category_identity(bad))

    def test_real_protocol_relative_rel_next_follows_only_the_same_immediate_category_page(self):
        html = document() + '<link rel="next" href="//www.hepsiburada.com/cep-telefonlari-c-371965?sayfa=2">'
        self.assertEqual(parse_category_document(html, CATEGORY_URL)[1], CATEGORY_URL + '?sayfa=2')
        for changed in [html.replace('sayfa=2', 'sayfa=3'),
                        html.replace('//www.hepsiburada.com/', '//evil.test/'),
                        html.replace('sayfa=2', 'sayfa=2&amp;filtreler=satici:x')]:
            diagnostic = {}
            self.assertIsNone(parse_category_document(changed, CATEGORY_URL, diagnostic)[1])
            self.assertEqual(diagnostic['status'], 'continuation_unrecognized')
        self.assertEqual(parse_category_document(html.replace('sayfa=2', 'sayfa=3'),
                         CATEGORY_URL + '?sayfa=2')[1], CATEGORY_URL + '?sayfa=3')
        actual_second = html.replace('Cep Telefonu Fiyatları ve Modelleri', 'Cep Telefonu').replace('sayfa=2', 'sayfa=3')
        self.assertEqual(parse_category_document(actual_second, CATEGORY_URL + '?sayfa=2')[1], CATEGORY_URL + '?sayfa=3')
        self.assertEqual(parse_category_document(actual_second, CATEGORY_URL)[0], [])

    def test_separate_category_budget_and_robots_denial(self):
        t = PriorityRetailerTransport(get=lambda url, **_: Response(url,
            'User-agent: *\nAllow: /\n' if url.endswith('robots.txt') else document()),
            sleep=lambda _: None, maximum_per_host=1)
        self.assertEqual(t.read_hepsiburada_category(CATEGORY_URL)[1], 'document_read')
        self.assertEqual(t.read_document(PDP)[1], 'document_read')
        self.assertEqual(t.report()['Hepsiburada']['category_requests'], 1)
        denied = PriorityRetailerTransport(get=lambda url, **_: Response(url,
            'User-agent: *\nDisallow: /cep-telefonlari\n'), sleep=lambda _: None)
        self.assertEqual(denied.read_hepsiburada_category(CATEGORY_URL)[1], 'robots_disallowed')

    def test_one_changed_brand_heading_does_not_hide_other_observed_category_pages(self):
        from sweep_priority_catalog import sweep
        from test_priority_candidates import Client
        client = Client()
        client.product.update(name='Samsung Galaxy S26', attributes={'ram_gb': 12, 'storage_gb': 256},
                              offer_targets=[])
        client.get_public_price_priority = lambda: {}
        visited = []
        class Transport:
            def read_hepsiburada_category(self, url):
                visited.append(url)
                if url == SAMSUNG:
                    html = '<h1>Changed category heading</h1>'
                elif url == CATEGORY_URL:
                    html = document() + '<link rel="next" href="//www.hepsiburada.com/cep-telefonlari-c-371965?sayfa=2">'
                else:
                    html = document().replace('Cep Telefonu Fiyatları ve Modelleri', 'Cep Telefonu')
                return html, 'document_read', 200
            def report(self):
                return {}
        result = sweep(client=client, transport=Transport(), discover_hepsiburada=True)
        self.assertEqual(visited, [CATEGORY_URL, SAMSUNG, CATEGORY_URL + '?sayfa=2'])
        discovery = result['hepsiburada_category_discovery']
        self.assertEqual(discovery['incomplete_pages'], [SAMSUNG])
        self.assertFalse(discovery['branch_queue_complete'])
        self.assertFalse(result['writes'])

    def test_transient_branch_queue_resumes_after_other_retailers_once_with_shared_page_budget(self):
        from sweep_priority_catalog import sweep
        from test_priority_candidates import Client
        from teknosa_catalog_discovery import CATEGORY_URL as TEKNOSA
        for mode in ['http_503', 'source_cooldown', 'http_403', 'budget', 'later_blocked']:
            client = Client()
            client.product.update(name='Samsung Galaxy S26', attributes={'ram_gb': 12, 'storage_gb': 256},
                                  offer_targets=[])
            client.get_public_price_priority = lambda: {}
            visited, independent_work = [], []
            class Transport:
                def read_hepsiburada_category(self, url):
                    visited.append(url)
                    if url == SAMSUNG and not independent_work:
                        reason = 'http_503' if mode in ('budget', 'later_blocked') else mode
                        return None, reason, 503 if reason == 'http_503' else 403 if reason == 'http_403' else None
                    if url == SAMSUNG:
                        return document(True), 'document_read', 200
                    page = 3 if url.endswith('sayfa=2') else 2
                    html = document().replace('Cep Telefonu Fiyatları ve Modelleri',
                                             'Cep Telefonu' if '?sayfa=' in url else 'Cep Telefonu Fiyatları ve Modelleri')
                    if url == CATEGORY_URL or mode == 'budget':
                        html += f'<link rel="next" href="{CATEGORY_URL}?sayfa={page}">'
                    return html, 'document_read', 200
                def read_teknosa_category(self, url):
                    independent_work.append(url)
                    visited.append(url)
                    return '<h1 class="plp-title">Cep Telefonu Fiyatları ve Modelleri</h1>', 'document_read', 200
                def report(self):
                    return {'Hepsiburada': {'blocked': mode == 'http_403' or
                                            mode == 'later_blocked' and bool(independent_work)}}
            with patch('hepsiburada_catalog_discovery.MAX_CATEGORY_PAGES', 3 if mode == 'budget' else 40):
                result = sweep(client=client, transport=Transport(), discover_hepsiburada=True, discover_teknosa=True)
            report = result['hepsiburada_category_discovery']
            if mode in ('http_403', 'later_blocked'):
                self.assertEqual(visited, [CATEGORY_URL, SAMSUNG, TEKNOSA])
                self.assertNotIn('resume_passes', report)
                if mode == 'later_blocked':
                    self.assertEqual(report['resume_status'], 'source_blocked')
            else:
                expected = [CATEGORY_URL, SAMSUNG, TEKNOSA]
                if mode == 'source_cooldown':
                    expected.append(SAMSUNG)  # Previously unrequested, not a repeated failed GET.
                expected.append(CATEGORY_URL + '?sayfa=2')
                self.assertEqual(visited, expected)
                self.assertEqual(report['resume_passes'], 1)
                self.assertLessEqual(len(report['pages']), 3 if mode == 'budget' else 40)
            self.assertEqual(report['branch_queue_complete'], mode == 'source_cooldown')
            self.assertFalse(report['pagination_complete'])
            self.assertFalse(result['writes'])

    def test_resumed_branch_cannot_expand_the_two_destination_budget_for_a_model(self):
        from sweep_priority_catalog import sweep
        from test_priority_candidates import Client
        client = Client()
        catalog = [deepcopy(PRODUCT), {'id': 2, 'slug': 'samsung-galaxy-s26', 'name': 'Samsung Galaxy S26',
                   'attributes': {'ram_gb': 12, 'storage_gb': 256}, 'offer_targets': []}]
        client.get_published_products = lambda: catalog
        client.get_public_price_priority = lambda: {}
        second, third = PDP[:-1] + 'L', PDP[:-1] + 'M'
        class Transport:
            def read_hepsiburada_category(self, url):
                if url == SAMSUNG:
                    return None, 'http_503', 503
                if url == CATEGORY_URL:
                    extra = (f'<h2 data-test-id="title-2"><a title="Apple iPhone 15 128 GB Siyah" href="{second}">'
                             'Apple iPhone 15 128 GB Siyah</a></h2>')
                    return document() + extra + f'<link rel="next" href="{CATEGORY_URL}?sayfa=2">', 'document_read', 200
                return document().replace(PDP, third).replace('Cep Telefonu Fiyatları ve Modelleri', 'Cep Telefonu'), 'document_read', 200
            def read_teknosa_category(self, url):
                return '<h1 class="plp-title">Cep Telefonu Fiyatları ve Modelleri</h1>', 'document_read', 200
            def report(self):
                return {}
        def inspect(transport, url, product):
            return {'slug': product['slug'], 'retailer': 'Hepsiburada', 'url': url,
                    'observed_url': url, 'reason': 'document_read', 'http_status': 200, 'prepared_offer': None}
        with patch('sweep_priority_catalog.inspect_target', side_effect=inspect) as reader:
            result = sweep(client=client, transport=Transport(), discover_hepsiburada=True, discover_teknosa=True)
        self.assertEqual(reader.call_count, 2)
        self.assertEqual({call.args[1] for call in reader.call_args_list}, {PDP, second})
        self.assertEqual(result['queued'], 2)
        self.assertEqual(result['hepsiburada_category_discovery']['resume_passes'], 1)
        self.assertEqual(len(result['hepsiburada_category_discovery']['new_targets']), 2)
        self.assertFalse(result['writes'])

    def test_sweep_follows_observed_branch_and_reverifies_only_fresh_product_pages(self):
        from sweep_priority_catalog import sweep
        from test_priority_candidates import Client
        from test_priority_catalog_sweep import sweep_reader
        client = Client()
        products = [deepcopy(PRODUCT), {'id': 2, 'slug': 'samsung-galaxy-s26', 'name': 'Samsung Galaxy S26',
                     'attributes': {'ram_gb': 12, 'storage_gb': 256}, 'offer_targets': []}]
        client.get_published_products = lambda: products
        client.get_public_price_priority = lambda: {p['id']: (p['slug'], False) for p in products}
        visited = []
        class Transport:
            def read_hepsiburada_category(self, url):
                visited.append(url)
                html = document(url == SAMSUNG)
                if url == CATEGORY_URL:
                    html += '<link rel="next" href="//www.hepsiburada.com/cep-telefonlari-c-371965?sayfa=2">'
                elif url == CATEGORY_URL + '?sayfa=2':
                    html = html.replace('Cep Telefonu Fiyatları ve Modelleri', 'Cep Telefonu')
                return html, 'document_read', 200
            def report(self):
                return {}
        row = {'slug': PRODUCT['slug'], 'retailer': 'Hepsiburada', 'url': PDP, 'observed_url': PDP,
               'reason': 'document_read', 'http_status': 200, 'prepared_offer': {'url': PDP}}
        with patch('sweep_priority_catalog.inspect_target', return_value=row) as inspect, \
                patch('sweep_priority_catalog.refresh_candidates', return_value={
                    'status': 'completed', 'writes': True, 'ingestion': {'accepted': 1}}) as publish:
            result = sweep(client=client, publish=True, reader=sweep_reader(), transport=Transport(),
                           discover_hepsiburada=True)
        self.assertEqual(visited, [CATEGORY_URL, SAMSUNG, CATEGORY_URL + '?sayfa=2'])
        inspect.assert_called_once()
        publish.assert_called_once()
        self.assertEqual(result['accepted'], 1)
        self.assertTrue(result['hepsiburada_category_discovery']['branch_queue_complete'])
        self.assertFalse(result['hepsiburada_category_discovery']['pagination_complete'])
