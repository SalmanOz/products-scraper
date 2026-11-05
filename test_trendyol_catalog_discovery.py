from copy import deepcopy
import json
import unittest
from bs4 import BeautifulSoup

from priority_retailer_transport import PriorityRetailerTransport
from trendyol_catalog_discovery import CATEGORY_URL, category_page_number, parse_category_document
from teknosa_catalog_discovery import match_catalog_destinations
from test_pasaj_source import Response
from pathlib import Path
from trendyol_catalog_discovery import category_identity, discovery_links, recovery_seed_targets


TITLE = 'Xiaomi REDMI A7 Pro 4GB+128GB Siyah (Xiaomi Türkiye Garantili)'
URL = 'https://www.trendyol.com/xiaomi/redmi-a7-pro-4gb-128gb-siyah-xiaomi-turkiye-garantili-p-1175413438'


def card(title=TITLE, url=URL):
    return (f'<a class="product-card" href="{url}?boutiqueId=689770&amp;merchantId=968">'
            f'<h2 class="title">{title}</h2><b>1 TL</b><button>Sepete Ekle</button></a>')


def document(body=None, page=1):
    return ('<h1 class="title">Cep Telefonu</h1>' + (card() if body is None else body) +
            (f'<a href="/cep-telefonu-x-c103498?pi={page+1}">{page+1}</a>' if page < 2 else
             '<a href="/cep-telefonu-x-c103498">1</a>'))


class TrendyolDiscoveryTests(unittest.TestCase):
    def test_observed_anchorless_xiaomi_state_reaches_second_public_html_page_without_api_or_prices(self):
        base = 'https://www.trendyol.com/xiaomi-android-cep-telefonu-x-b101939-c164461'
        pages = [Path('fixtures/trendyol-xiaomi-public-'+part+'-page-routing.html').read_text()
                 for part in ('first', 'second')]
        rows = []
        for index, html in enumerate(pages, 1):
            diagnostic = {}
            found, next_url = parse_category_document(html, base + (f'?pi={index}' if index > 1 else ''), diagnostic)
            self.assertEqual(next_url, base + f'?pi={index+1}')
            self.assertEqual(diagnostic['status'], 'parsed')
            self.assertEqual(diagnostic['pagination_source'], 'public_category_state_html_projection')
            self.assertEqual(len(found), 36)
            self.assertTrue(all(set(row) == {'source_url','discovery_title','discovery_product_id','stock_priority'} for row in found))
            self.assertTrue(all(row['source_url'].startswith('https://www.trendyol.com/') and '?' not in row['source_url'] for row in found))
            rows.append(found)
        old_model = {'id': 215, 'slug': 'xiaomi-13t', 'name': 'Xiaomi 13T',
                     'attributes': {'ram_gb': 12, 'storage_gb': 256, 'Connectivity': {'5G': True}}}
        self.assertEqual(match_catalog_destinations(rows[0], [old_model]), {})
        self.assertIn(old_model['id'], match_catalog_destinations(rows[1], [old_model]))
        no_state = BeautifulSoup(pages[0], 'html.parser')
        no_state.select_one('script').decompose()
        diagnostic = {}
        self.assertIsNone(parse_category_document(str(no_state), base, diagnostic)[1])
        self.assertEqual(diagnostic['status'], 'pagination_unrecognized')

    def test_serialized_pagination_cannot_change_category_host_index_or_public_query_scope(self):
        base = 'https://www.trendyol.com/xiaomi-android-cep-telefonu-x-b101939-c164461'
        html = Path('fixtures/trendyol-xiaomi-public-first-page-routing.html').read_text()
        soup = BeautifulSoup(html, 'html.parser')
        script = soup.select_one('script')
        original = json.loads(script.get_text().split('=',1)[1].rstrip(';'))
        mutations = []
        for key, value in [('title','Samsung Android Cep Telefonu'), ('pageIndex',2),
                           ('pageIndex',True), ('total',1), ('total',True), ('total',100001)]:
            state = deepcopy(original); state['data'][key] = value; mutations.append(state)
        for field in ('current','next'):
            link = original['data']['_links'][field]
            for changed in [link.replace('https:', 'http:'), link.replace('apigw.trendyol.com','evil.example'),
                            link.replace('apigw.trendyol.com','apigw.trendyol.com:443'), link+'#extra',
                            link+'&token=private',link+'&pi=2',link.replace('pathModel=xiaomi-','pathModel=samsung-'),
                            link.replace('pageSize=36','pageSize=360'),link.replace('pi=1','pi=2').replace('pi=2','pi=3')]:
                state=deepcopy(original);state['data']['_links'][field]=changed;mutations.append(state)
        state=deepcopy(original);del state['data']['_links']['next'];mutations.append(state)
        state=deepcopy(original);state['data']['_links']['next']=None;mutations.append(state)
        for state in mutations:
            changed=BeautifulSoup(html,'html.parser')
            changed.select_one('script').string='window["__single-search-result__PROPS"]='+json.dumps(state)+';'
            diagnostic={}
            self.assertIsNone(parse_category_document(str(changed),base,diagnostic)[1], state['data'])
            self.assertEqual(diagnostic['status'],'pagination_unrecognized')
        soup.select_one('script').insert_after(deepcopy(soup.select_one('script')))
        self.assertIsNone(parse_category_document(str(soup),base)[1])
        consistent=html+f'<a href="{base}?pi=2">2</a>'
        self.assertEqual(parse_category_document(consistent,base)[1],base+'?pi=2')
        terminal=deepcopy(original);terminal['data']['total']=36;terminal['data']['_links']['next']=None
        soup=BeautifulSoup(html,'html.parser')
        soup.select_one('script').string='window["__single-search-result__PROPS"]='+json.dumps(terminal)+';'
        diagnostic={}
        self.assertIsNone(parse_category_document(str(soup),base,diagnostic)[1])
        self.assertEqual(diagnostic['status'],'parsed')

    def test_full_sweep_visits_observed_second_html_page_but_never_publishes_category_amounts(self):
        from unittest.mock import patch
        from sweep_priority_catalog import sweep
        base='https://www.trendyol.com/xiaomi-android-cep-telefonu-x-b101939-c164461'
        pages=[Path('fixtures/trendyol-xiaomi-public-'+part+'-page-routing.html').read_text()
               for part in ('first','second')]
        product={'id':215,'slug':'xiaomi-13t','name':'Xiaomi 13T',
                 'attributes':{'ram_gb':12,'storage_gb':256,'Connectivity':{'5G':True}},
                 'offer_targets':[],'offer_targets_truncated':False}
        class Client:
            def get_published_products(self): return [deepcopy(product)]
            def get_public_price_priority(self): return {215:('xiaomi-13t',False)}
        calls=[]
        class Transport:
            def configure_catalog_budget(self,count): pass
            def read_trendyol_category(self,url):
                calls.append(('category',url))
                if url==CATEGORY_URL:
                    body=f'<h1 class="title">Cep Telefonu</h1><a href="{CATEGORY_URL}">1</a><a href="{base}">Xiaomi</a>'
                elif url in (base,base+'?pi=2'):
                    body=pages[0 if url==base else 1]
                else:
                    assert url==base+'?pi=3'
                    body=f'<h1 class="title">Xiaomi Android Cep Telefonu</h1><a href="{base}">1</a>'
                return body,'document_read',200
            def read_document(self,url):
                calls.append(('pdp',url))
                return '<h1>Incomplete selected offer</h1>','document_read',200
            def resolved_url(self,url): return url
            def report(self): return {}
        with patch('sweep_priority_catalog.refresh_candidates') as writer:
            result=sweep(client=Client(),publish=True,transport=Transport(),discover_trendyol=True)
        self.assertEqual([u for kind,u in calls if kind=='category'],
                         [CATEGORY_URL,base,base+'?pi=2',base+'?pi=3'])
        self.assertIn(('pdp','https://www.trendyol.com/xiaomi/13t-256-gb-12-gb-ram-siyah-cep-telefonu-xiaomi-turkiye-garantili-p-767729000'),calls)
        self.assertFalse(any('apigw.trendyol.com' in url for _,url in calls))
        self.assertEqual((result['accepted'],result['writes']),(0,False))
        writer.assert_not_called()

    def test_observed_public_brand_page_has_only_raw_product_hints_and_exact_brand_heading(self):
        url = 'https://www.trendyol.com/infinix-android-cep-telefonu-x-b158303-c164461'
        body = Path('fixtures/trendyol-public-brand-category.html').read_text()
        diagnostic = {}
        rows, _ = parse_category_document(body, url, diagnostic)
        self.assertEqual(len(rows), 4)
        self.assertEqual(diagnostic['category_scope'], 'phone_brand')
        self.assertEqual(diagnostic['status'], 'parsed')
        for row in rows:
            self.assertNotIn('?', row['source_url'])
            for key in ('price', 'merchant_name', 'ram_gb', 'storage_gb', 'source_variant_evidence'):
                self.assertNotIn(key, row)
        for wrong in ['Samsung Android Cep Telefonu', 'INFINIX Cep Telefonu', 'INFINIX Telefon Kılıfları']:
            self.assertEqual(parse_category_document(body.replace('INFINIX Android Cep Telefonu', wrong), url)[0], [])
        product = {'id': 219, 'name': 'Infinix Note 40', 'attributes': {'ram_gb': 8, 'storage_gb': 256}}
        self.assertEqual(match_catalog_destinations(rows, [product]), {})

    def test_brand_scope_and_pagination_never_mix_other_categories_or_private_query_parameters(self):
        brand = 'https://www.trendyol.com/infinix-android-cep-telefonu-x-b158303-c164461'
        body = '<h1 class="title">INFINIX Android Cep Telefonu</h1>' + card() + ''.join(
            f'<a href="{u}">Page</a>' for u in (brand, brand+'?pi=2', CATEGORY_URL+'?pi=2',
                brand.replace('infinix', 'samsung')+'?pi=2'))
        self.assertEqual(parse_category_document(body, brand)[1], brand+'?pi=2')
        for bad in [brand+'?pi=2&token=x',brand+'?b=158303',brand+'?pi=01',brand+'#x',
                brand.replace('https:', 'http:'),brand.replace('.com/', '.com.evil.test/'),
                brand.replace('-c164461','-c103498'),brand.replace('infinix','inventedbrand')]:
            self.assertIsNone(category_identity(bad), bad)

    def test_observed_model_filter_routes_missing_radio_variant_to_fresh_pdp_before_brand_scan(self):
        from unittest.mock import patch
        from sweep_priority_catalog import sweep

        base = 'https://www.trendyol.com/redmi-note-13-pro-cep-telefonu-x-c103498-a292-v10617480'
        brand = 'https://www.trendyol.com/xiaomi-cep-telefonu-x-b101939-c103498'
        target = ('https://www.trendyol.com/xiaomi/redmi-note-13-pro-5g-8gb-ram-256-gb-'
                  'akilli-cep-telefonu-turkiye-garantili-p-930528202')
        product = {'id': 275, 'slug': 'xiaomi-redmi-note-13-pro', 'name': 'Xiaomi Redmi Note 13 Pro',
                   'attributes': {'ram_gb': 8, 'storage_gb': 128, 'Connectivity': {'5G': True}},
                   'offer_targets': []}
        root = document(
            f'<a href="{brand}">Xiaomi</a><a href="{base}">Redmi Note 13 Pro Cep Telefonu</a>'
            f'<a href="{base.replace("10617480", "10617481")}">Redmi Note 13 Pro Plus</a>'
            f'<a href="{base.replace(".com/", ".com.evil.test/")}">Redmi Note 13 Pro</a>')
        selected = ('<input type="checkbox" id="checkbox-web-aggregations-checkbox-292-10617480-10617480" '
                    'aria-label="Redmi Note 13 Pro" checked="" value="10617480">')
        model = ('<h1 class="title">Redmi Note 13 Pro Cep Telefonu</h1>' + selected
                 + card('Xiaomi Redmi Note 13 Pro 5G 8GB RAM 256 GB Akıllı Cep Telefonu', target))
        self.assertEqual(discovery_links(root, CATEGORY_URL, [product]), [base, brand])
        for bad in [base + '?pi=2&token=x', base + '#x', base.replace('-a292-', '-a299-')]:
            self.assertIsNone(category_identity(bad), bad)
        for bad in [model.replace(' checked=""', ''), model.replace('value="10617480"', 'value="10617481"'),
                    model + selected, model.replace('Pro Cep Telefonu</h1>', 'Pro Plus Cep Telefonu</h1>')]:
            self.assertEqual(parse_category_document(bad, base), ([], None))

        class Client:
            def get_published_products(self): return [deepcopy(product)]
            def get_public_price_priority(self): return {275: (product['slug'], False)}

        calls = []
        class Transport:
            def read_trendyol_category(self, url):
                calls.append(url)
                if url == CATEGORY_URL:
                    return root, 'document_read', 200
                if url == base:
                    return model, 'document_read', 200
                return None, 'http_404', 404

            def read_document(self, url):
                calls.append(url)
                return None, 'http_404', 404

            def report(self): return {}

        with patch('sweep_priority_catalog.refresh_candidates') as writer:
            result = sweep(client=Client(), transport=Transport(), publish=True, discover_trendyol=True,
                           memory_discovery=True)
        self.assertEqual(calls, [CATEGORY_URL, base, target, brand, CATEGORY_URL + '?pi=2'])
        discovery = result['trendyol_category_discovery']
        self.assertEqual(discovery['pages'][1]['parser']['category_scope'], 'phone_model')
        hint, = discovery['memory_variant_hints']
        self.assertEqual(hint['source_url'], target)
        self.assertNotIn('price', hint)
        self.assertFalse(result['writes'])
        writer.assert_not_called()

    def test_pdp_breadcrumbs_discover_only_observed_catalog_brand_phone_routes_even_when_sold_out(self):
        from audit_priority_retailers import inspect_target
        from test_trendyol_catalog_source import document as pdp, URL as target, NAME, SPECS
        brand = 'https://www.trendyol.com/samsung-cep-telefonu-x-b794-c103498'
        # Isolated fixture category ID; production uses only freshly observed hrefs.
        links = [brand,brand,brand.replace('samsung','infinix'),brand+'?token=private',
            brand.replace('.com/', '.com.evil.test/'),brand.replace('-c103498','-c104025')]
        extra = ''.join(f'<a href="{u}">Brand</a>' for u in links)
        body = pdp({('state','product','inStock'):False,('cart_attributes',):'disabled',('extra',):extra})
        parent = {'id':1,'slug':'samsung-galaxy-a54-5g','name':NAME,'attributes':SPECS}
        class Transport:
            def read_document(self,url): return body,'document_read',200
            def resolved_url(self,url): return url
        row = inspect_target(Transport(),target,parent)
        self.assertIsNone(row['prepared_offer'])
        self.assertEqual(row['reader_reason'],'selected_product_mismatch')
        self.assertEqual([h['source_url'] for h in row['discovered_categories']],[brand])
        self.assertEqual(row['discovered_categories'][0]['source_document_url'],target.split('?')[0])
        self.assertEqual(discovery_links(body.replace(NAME,NAME.replace(' 5G','')),target,[parent]),[brand])
        self.assertEqual(discovery_links(body.replace(NAME,'Samsung Galaxy A55 5G'),target,[parent]),[])

    def test_brand_seeds_reorder_only_existing_targets_and_are_capped_without_promoting_price_hints(self):
        rows=[]
        for index,brand in enumerate(['Samsung','Samsung','Infinix','Tecno','Apple','Honor','Vivo','Oppo','Realme','Nothing']):
            p={'id':index+1,'slug':f'phone-{index}','name':brand+' Phone 10','attributes':{}}
            rows.append((f'https://www.trendyol.com/{brand.lower()}/phone-10-p-{5000+index}',p))
        prices={p['id']:(p['slug'],True) for _,p in rows}
        prices[2]=(rows[1][1]['slug'],False)
        before=[(u,dict(p)) for u,p in rows]
        seeds=recovery_seed_targets(rows,prices)
        self.assertEqual(len(seeds),8)
        self.assertEqual(seeds[0],rows[1])
        self.assertNotIn(rows[0],seeds)
        self.assertEqual(rows,before)
        self.assertEqual(recovery_seed_targets(rows,prices,maximum=0),[])
        with self.assertRaises(ValueError): recovery_seed_targets(rows,prices,maximum=9)
        subbrands=[(f'https://www.trendyol.com/xiaomi/phone-p-{6000+i}',
            {'id':100+i,'slug':f'redmi-{i}','name':name}) for i,name in
            enumerate(['Xiaomi Redmi Note 14','Redmi Note 13'])]
        self.assertEqual(recovery_seed_targets(subbrands,{}),subbrands[:1])

    def test_brand_and_root_pages_share_existing_category_budget_and_robots_with_pdp_quota_preserved(self):
        brand='https://www.trendyol.com/infinix-android-cep-telefonu-x-b158303-c164461'
        calls=[]
        def get(url,**_):
            calls.append(url)
            return Response(url,'User-agent: *\nAllow: /\n' if url.endswith('robots.txt') else '<h1>Public</h1>')
        transport=PriorityRetailerTransport(get=get,sleep=lambda _:None,maximum_per_host=1)
        for root in (CATEGORY_URL,brand):
            for page in range(1,51):
                self.assertEqual(transport.read_trendyol_category(root+(f'?pi={page}' if page>1 else ''))[1],'document_read')
        self.assertEqual(transport.read_trendyol_category(brand+'?pi=51')[1],'category_budget_exhausted')
        self.assertEqual(transport.read_document(URL)[1],'document_read')
        self.assertEqual((transport.report()['Trendyol']['category_requests'],transport.report()['Trendyol']['pdp_requests']),(100,1))
        self.assertEqual(len(calls),102)
        denied=PriorityRetailerTransport(get=lambda url,**_:Response(url,
            'User-agent: *\nDisallow: /infinix-android-cep-telefonu-x-b158303-c164461\n'),sleep=lambda _:None)
        self.assertEqual(denied.read_trendyol_category(brand)[1],'robots_disallowed')

    def test_sold_out_seed_breadcrumb_reaches_new_brand_listing_then_fresh_atomic_publication(self):
        from sweep_priority_catalog import sweep
        from test_priority_candidates import Client
        from test_trendyol_catalog_source import document as pdp, URL as seed, NAME, SPECS
        from retailer_offer_refresh import RetailerOfferRefresher
        from trendyol_catalog_source import TrendyolCatalogSource
        canonical = seed.split('?')[0]
        destination = canonical.replace('676507062','676507063')
        brand = 'https://www.trendyol.com/samsung-cep-telefonu-x-b794-c103498'
        for valid_heading in (True,False):
            class ScopedClient(Client):
                def ingest_listing_offers(self,records,*,require_all_accepted=True):
                    return {**super().ingest_offers(records,require_all_accepted=require_all_accepted),
                        'offer_identity_contract':'seller-source-url-v1'}
            client=ScopedClient()
            client.product.update(name=NAME,attributes=SPECS,offer_targets=[{'source_url':canonical}])
            client.get_public_price_priority=lambda:{client.product['id']:(client.product['slug'],False)}
            calls=[]
            class Transport:
                def configure_catalog_budget(self,count): pass
                def read_document(self,url):
                    calls.append(('pdp',url))
                    if url==canonical:
                        return pdp({('state','product','inStock'):False,('cart_attributes',):'disabled',
                            ('extra',):f'<a href="{brand}">Samsung Cep Telefonu</a>'}),'document_read',200
                    self_case.assertEqual(url,destination)
                    return pdp().replace('676507062','676507063'),'document_read',200
                def read_trendyol_category(self,url):
                    calls.append(('category',url))
                    if url==CATEGORY_URL:
                        return document('',page=2),'document_read',200
                    self_case.assertEqual(url,brand)
                    title='Samsung Cep Telefonu' if valid_heading else 'Huawei Cep Telefonu'
                    return (f'<h1 class="title">{title}</h1>'+card(NAME+' 8GB RAM 128GB',destination)+
                        f'<a href="{brand}">1</a>'),'document_read',200
                def resolved_url(self,url): return url
                def report(self): return {}
            self_case=self
            transport=Transport()
            reader=RetailerOfferRefresher(trendyol_catalog_source=TrendyolCatalogSource(transport=transport))
            result=sweep(client=client,publish=True,transport=transport,reader=reader,discover_trendyol=True)
            self.assertEqual(calls[:3],[('pdp',canonical),('category',CATEGORY_URL),('category',brand)])
            self.assertEqual(result['trendyol_brand_category_discovery']['observed_category_urls'],[brand])
            self.assertIsNone(result['rows'][0]['prepared_offer'])
            self.assertEqual(result['accepted'],int(valid_heading))
            self.assertEqual(len(client.writes),int(valid_heading))
            if valid_heading:
                self.assertEqual(calls[3:],[('pdp',destination),('pdp',destination)])
                self.assertEqual(client.writes[0][0]['source_url'],destination)
                self.assertEqual(client.writes[0][0]['price'],34997)
                self.assertTrue(result['rows'][1]['publication']['commercial_readback_verified'])
            else:
                self.assertEqual(len(calls),3)

    def test_observed_g_pair_category_card_is_a_raw_destination_hint_not_selected_memory_or_price(self):
        product = {'id': 1, 'name': 'Xiaomi 17', 'slug': 'xiaomi-17',
                   'attributes': {'ram_gb': 12, 'storage_gb': 256}}
        title = 'Xiaomi 17 12G+256G Siyah'
        url = 'https://www.trendyol.com/xiaomi/17-12g-256g-siyah-p-1110127495'
        rows, _ = parse_category_document(document(card(title, url)), CATEGORY_URL)
        before = [dict(r) for r in rows]
        self.assertEqual(match_catalog_destinations(rows, [product])[1], rows)
        self.assertEqual(rows, before)
        self.assertEqual(rows[0]['discovery_title'], title)
        for forbidden in ['price', 'ram_gb', 'storage_gb', 'source_variant_evidence']:
            self.assertNotIn(forbidden, rows[0])
        for changed in ['Xiaomi 17 8G+256G Siyah', 'Xiaomi 17 12G+512G Siyah',
                        'Xiaomi 17 Plus 12G+256G Siyah', 'Xiaomi 17 Pro 12G+256G Siyah',
                        'Xiaomi 17 12G+256G / 12G+512G Siyah']:
            other, _ = parse_category_document(document(card(changed, url)), CATEGORY_URL)
            self.assertEqual(match_catalog_destinations(other, [product]), {}, changed)

    def test_real_card_shape_discovers_only_canonical_destinations_not_card_amounts(self):
        rows, next_url = parse_category_document(document(), CATEGORY_URL)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source_url'], URL)
        self.assertEqual(next_url, CATEGORY_URL + '?pi=2')
        self.assertEqual(rows[0]['discovery_title'], TITLE)
        self.assertNotIn('price', rows[0])
        product = {'id': 1, 'name': 'Xiaomi Redmi A7 Pro', 'slug': 'redmi-a7-pro',
                   'attributes': {'ram_gb': 4, 'storage_gb': 128}}
        self.assertEqual(match_catalog_destinations(rows, [product])[1], rows)
        self.assertEqual(match_catalog_destinations(rows, [{**product, 'attributes': {'ram_gb': 6, 'storage_gb': 128}}]), {})

    def test_only_exact_category_pages_and_unambiguous_public_product_cards_are_accepted(self):
        for bad in [CATEGORY_URL + '?pi=0', CATEGORY_URL + '?pi=01', CATEGORY_URL + '?pi=2&token=x',
                    CATEGORY_URL + '#fragment', CATEGORY_URL.replace('https:', 'http:'),
                    CATEGORY_URL.replace('.com/', '.com.evil.test/')]:
            self.assertIsNone(category_page_number(bad))
        for changed in [document().replace('product-card', 'seller-store-product-card'),
                        document().replace('www.trendyol.com/xiaomi', 'evil.test/xiaomi'),
                        document().replace('merchantId=968', 'token=private'),
                        document().replace('h2 class="title"', 'h2'),
                        document().replace('Cep Telefonu</h1>', 'Telefon Aksesuarları</h1>')]:
            self.assertEqual(parse_category_document(changed, CATEGORY_URL)[0], [])
        rows, _ = parse_category_document(document(card() + card()), CATEGORY_URL)
        self.assertEqual(len(rows), 1)

    def test_observed_sequential_pagination_and_missing_pagination_are_reported_honestly(self):
        diagnostic = {}
        self.assertEqual(parse_category_document(document(), CATEGORY_URL, diagnostic)[1], CATEGORY_URL + '?pi=2')
        self.assertEqual(diagnostic['status'], 'parsed')
        self.assertIsNone(parse_category_document(document(page=2), CATEGORY_URL + '?pi=2', diagnostic)[1])
        self.assertEqual(diagnostic['status'], 'parsed')
        broken = document().replace('?pi=2', '?pi=84')
        self.assertIsNone(parse_category_document(broken, CATEGORY_URL, diagnostic)[1])
        self.assertEqual(diagnostic['status'], 'pagination_unrecognized')
        parse_category_document('<h1 class="title">Cep Telefonu</h1>' + card(), CATEGORY_URL, diagnostic)
        self.assertEqual(diagnostic['status'], 'pagination_unrecognized')

    def test_category_reads_obey_robots_and_reserve_a_separate_bounded_document_budget(self):
        calls = []
        def get(url, **_):
            calls.append(url)
            return Response(url, 'User-agent: *\nAllow: /\n' if url.endswith('robots.txt') else document())
        transport = PriorityRetailerTransport(get=get, sleep=lambda _: None, maximum_per_host=1)
        self.assertEqual(transport.read_trendyol_category(CATEGORY_URL)[1], 'document_read')
        self.assertEqual(transport.read_trendyol_category(CATEGORY_URL + '?pi=2')[1], 'document_read')
        self.assertEqual(transport.read_document(URL)[1], 'document_read')
        self.assertEqual(transport.read_document(URL + '1')[1], 'pdp_budget_exhausted')
        self.assertEqual(transport.report()['Trendyol']['category_requests'], 2)
        self.assertEqual(transport.report()['Trendyol']['pdp_requests'], 1)
        self.assertEqual(transport.read_trendyol_category(CATEGORY_URL + '?pi=1000')[1], 'invalid_catalog_category')
        denied = PriorityRetailerTransport(get=lambda url, **_: Response(url,
            'User-agent: *\nDisallow: /cep-telefonu-x-c103498\n'), sleep=lambda _: None)
        self.assertEqual(denied.read_trendyol_category(CATEGORY_URL)[1], 'robots_disallowed')

    def test_full_catalog_discovery_has_room_for_three_pdp_destinations_per_110_models(self):
        transport = PriorityRetailerTransport(get=lambda url, **_: Response(url,
            'User-agent: *\nAllow: /\n' if url.endswith('robots.txt') else '<h1>Phone</h1>'), sleep=lambda _: None)
        transport.configure_catalog_budget(350)
        for number in range(330):
            url = URL.replace('1175413438', str(1175413438 + number))
            self.assertEqual(transport.read_document(url)[1], 'document_read')
        self.assertEqual(transport.report()['Trendyol']['pdp_requests'], 330)
        with self.assertRaises(ValueError):
            PriorityRetailerTransport(maximum_per_host=401)

    def test_discovery_reads_live_pdp_again_before_one_atomic_write_and_commercial_readback(self):
        from sweep_priority_catalog import sweep
        from test_priority_candidates import Client
        from test_trendyol_catalog_source import document as pdp, URL as target, NAME, SPECS
        from retailer_offer_refresh import RetailerOfferRefresher
        from trendyol_catalog_source import TrendyolCatalogSource
        canonical = target.split('?')[0]
        class ScopedClient(Client):
            def ingest_listing_offers(self, records, *, require_all_accepted=True):
                return {**super().ingest_offers(records, require_all_accepted=require_all_accepted),
                        'offer_identity_contract': 'seller-source-url-v1'}
        client = ScopedClient()
        client.product.update(name=NAME, attributes=SPECS, offer_targets=[])
        client.get_public_price_priority = lambda: {}
        calls = []
        class Transport:
            def configure_catalog_budget(self, count):
                pass
            def read_trendyol_category(self, url):
                calls.append(('category', url))
                return document(card(NAME + ' 8GB RAM 128GB', canonical), page=1 if '?' not in url else 2), 'document_read', 200
            def read_document(self, url):
                calls.append(('pdp', url))
                return pdp(), 'document_read', 200
            def resolved_url(self, url):
                return url
            def report(self):
                return {}
        transport = Transport()
        reader = RetailerOfferRefresher(trendyol_catalog_source=TrendyolCatalogSource(transport=transport))
        result = sweep(client=client, publish=True, transport=transport, reader=reader, discover_trendyol=True)
        self.assertEqual(result['accepted'], 1)
        self.assertEqual(len(client.writes), 1)
        self.assertEqual(client.writes[0][0]['price'], 34997)
        self.assertEqual(client.writes[0][0]['source_url'], canonical)
        self.assertEqual(client.writes[0][0]['source_variant_evidence']['ram_gb'], 8)
        self.assertEqual([kind for kind, _ in calls], ['category', 'category', 'pdp', 'pdp'])
        self.assertTrue(result['rows'][0]['publication']['commercial_readback_verified'])
        self.assertTrue(result['trendyol_category_discovery']['pagination_complete'])


if __name__ == '__main__':
    unittest.main()
