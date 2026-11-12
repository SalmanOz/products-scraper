from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from bs4 import BeautifulSoup

from audit_hepsiburada_iphone16pm_readonly import _first_party_laptop_cash, _ordinary_selected_cash
from laptop_direct_source import selected_direct_laptop
from laptop_hepsiburada_source import hepsiburada_seller_profile_matches
from laptop_pilot import build_model, compatible, read_retailer, select_offers, valid_pair
from laptop_retailer_sources import laptop_facts, marketplace_seller_identity, selected_n11

ATTRS = {
    'bellek kapasitesi': '16 GB', 'disk kapasitesi': '512 GB', 'disk turu': 'SSD',
    'islemci modeli': 'Intel Core i5-13420H', 'isletim sistemi': 'Windows 11 Home',
    'ekran boyutu': '15.3"', 'ekran cozunurlugu': '1920 x 1200',
}
TITLE = 'Lenovo IdeaPad Slim 3 15IRH10 83K10016TR i5-13420H 16 GB 512 GB SSD 15.3" W11H Dizüstü Bilgisayar'
URL = 'https://www.n11.com/urun/lenovo-ideapad-slim-3-12345678'
EAN = '0198155448171'


def observation(index=1, platform='n11', merchant='Mağaza A', legal='magaza a anonim sirketi'):
    return {
        'platform': platform, 'merchant_id': index, 'merchant': merchant, '_legal_name': legal,
        'url': f'{URL}?magaza=store-{index}', 'gtin13': EAN, 'price': 30_000,
        'facts': laptop_facts(TITLE, ATTRS, 'Lenovo'), 'observed_at': '2026-11-07T08:00:00+00:00',
        'source_title': TITLE,
    }


def laptop_document(change=None):
    # Start with the marketplace's real SSR structure, replacing only the
    # selected product/schema and its laptop identity-only SKU fields.
    soup = BeautifulSoup(Path(__file__).with_name('fixtures').joinpath('n11-selected-ordinary-cash.html').read_text(), 'html.parser')
    script = next(s for s in soup.select('script') if 'window.model' in s.get_text())
    state = json.loads(script.get_text().split('=', 1)[1].strip().removesuffix(';'))
    p, m = state['product'], state['productMeta']
    shop = soup.select_one('.sidebarSellerArea-top-name')['href'].rsplit('/', 1)[-1]
    p.update(title=TITLE, groupId=12345678, productUrlWithoutSellerShop=URL,
             skuUrl=f'{URL}?magaza={shop}', defaultGtin=EAN, productBrand='Lenovo',
             category={'id': 1000271, 'seoUrl': '/bilgisayar/dizustu-bilgisayar'},
             productAttributes=[{'key': k, 'value': v} for k, v in ATTRS.items()],
             images=[{'isVideoBannerImage': False, 'path': 'https://n11scdn.akamaized.net/a1/{0}/laptop.jpg'}])
    p['seller']['companyName'] = 'Example Laptop Inc'
    m.update(groupId=12345678)
    state['skuDefinitions'] = '[]'
    state['skus'] = json.dumps([dict.fromkeys(
        ('productId', 'stock', 'currentStock', 'storeName', 'price', 'displayPrice',
         'displayPriceNumber', 'instantDiscountedPriceForSku'),
        None) | {'id': int(p['defaultSkuId']), 'gtin': EAN, 'skuAttributes': [], 'outOfStock': True}])
    soup.select_one('h1.title').string = TITLE
    soup.select_one('button.sku-selection').decompose()
    for tag in soup.select('script[type="application/ld+json"]'):
        item = json.loads(tag.get_text())
        if not isinstance(item, dict) or item.get('@type') != 'Product':
            continue
        item['name'] = TITLE + ' Fiyatları ve Özellikleri'
        item['offers']['url'] = URL
        tag.string = json.dumps(item)
    if change:
        change(state, soup)
    script.string = 'window.model = ' + json.dumps(state) + ';'
    return str(soup), shop


class LaptopPilotTests(TestCase):
    def test_neva_reads_current_price_and_checks_enabled_cart(self):
        url = 'https://nevavm.com/urun/lenovo-ideapad-slim-3/'
        schema = {
            '@type': 'Product', 'name': TITLE + ' | Neva AVM', 'brand': 'Lenovo', 'gtin': EAN,
            'offers': {'@type': 'Offer', 'url': url, 'price': 30000, 'priceCurrency': 'TRY',
                       'availability': 'http://schema.org/InStock', 'itemCondition': 'https://schema.org/NewCondition'},
        }
        html = f'''<link rel="canonical" href="{url}">
        <script type="application/ld+json">{json.dumps(schema)}</script>
        <div class="summary entry-summary"><h1>{TITLE}</h1>
        <p class="price"><del><bdi>32.000 TL</bdi></del><ins><bdi>30.000 TL</bdi></ins></p>
        <form class="cart"><button name="add-to-cart">Sepete Ekle</button></form></div>'''
        offer, reason = selected_direct_laptop(html, url)
        self.assertIsNotNone(offer, reason)
        self.assertEqual(offer['price'], 30000)
        self.assertIsNone(selected_direct_laptop(html.replace('30.000 TL', '31.000 TL'), url)[0])
        self.assertIsNone(selected_direct_laptop(html.replace('<button ', '<button disabled '), url)[0])

    def test_matching_title_and_technical_record(self):
        facts = laptop_facts(TITLE, ATTRS, 'Lenovo')
        self.assertEqual((facts['mpn'], facts['cpu'], facts['ram'], facts['ssd'], facts['os']),
                         ('83K10016TR', 'Intel Core i5-13420H', 16, 512, 'Windows 11 Home'))
        self.assertIsNone(laptop_facts(TITLE.replace('16 GB', '32 GB'), ATTRS, 'Lenovo'))
        self.assertIsNone(laptop_facts(TITLE.replace('512 GB', '1 TB'), ATTRS, 'Lenovo'))
        self.assertIsNone(laptop_facts(TITLE.replace('W11H', 'FreeDOS'), ATTRS, 'Lenovo'))
        self.assertIsNone(laptop_facts(TITLE, ATTRS, 'Lenovo', '83K10017TR'))
        self.assertIsNone(laptop_facts(TITLE.replace('83K10016TR', '83K10016TR-32GB'), ATTRS, 'Lenovo'))
        self.assertIsNone(laptop_facts(TITLE + ' yenilenmiş', ATTRS, 'Lenovo'))

    def test_requires_independent_sellers_and_the_same_variant(self):
        first, second = observation(), observation(2, 'n11', 'Mağaza B', 'magaza b limited sirketi')
        self.assertTrue(valid_pair(first, second))
        for update in ({'_legal_name': 'magaza a limited sirketi'},
                       {'merchant': first['merchant']}, {'url': first['url']},
                       {'gtin13': '0198154123529'}, {'merchant_id': first['merchant_id']}):
            self.assertFalse(valid_pair(first, second | update), update)
        for spec, wrong in (('cpu', 'Intel Core i7-13620H'), ('ram', 32), ('ssd', 1024),
                            ('os', 'FreeDOS'), ('discrete', 'RTX 4050')):
            self.assertFalse(compatible(first, second | {'facts': second['facts'] | {spec: wrong}}), spec)

    def test_laptop_configuration_distinguishes_ram_from_gpu_memory(self):
        title = ('MSI Katana 15 HX B14WFK-450XTR i9-14900HX 15.6" '
                 '32 GB 1 TB SSD 8 GB RTX5060 115 W QHD 165Hz FreeDOS Gaming Laptop')
        facts = laptop_facts(title, {}, 'MSI')
        self.assertEqual((facts['ram'], facts['ssd'], facts['discrete']), (32, 1024, 'RTX 5060'))
        self.assertIsNone(laptop_facts(title, {'ram': '16 GB'}, 'MSI'))
        self.assertIsNone(laptop_facts(title + ' 512 GB SSD', {}, 'MSI'))

    def test_hepsiburada_client_price_needs_first_party_ordinary_sale(self):
        state = {
            'merchantId': 'e750130c-d0ff-469f-8767-66a63af8ea0c', 'merchantName': 'Hepsiburada',
            'campaignIds': [], 'campaignNote': '', 'isCampaignParticipationClosed': True,
            'prices': [{'value': 31499, 'discountRate': 0, 'currency': 0},
                       {'value': 30999, 'discountRate': 2, 'currency': 0}],
        }
        self.assertTrue(_first_party_laptop_cash(state, 30999))
        self.assertFalse(_first_party_laptop_cash(state, 31499))
        self.assertFalse(_first_party_laptop_cash(state | {'campaignIds': ['coupon']}, 30999))
        self.assertFalse(_first_party_laptop_cash(state | {'merchantName': 'Başka satıcı'}, 30999))

    def test_hepsiburada_marketplace_cash_requires_one_ordinary_price(self):
        price = {'value': 86999, 'formattedPrice': '86.999,00', 'discountRate': 0, 'currency': 0}
        state = {'prices': [price], 'campaignIds': [], 'campaignNote': '', 'isCampaignParticipationClosed': True}
        self.assertTrue(_ordinary_selected_cash(state, Decimal('86999')))
        for change in ({'campaignIds': ['offer']}, {'prices': [price, price]},
                       {'prices': [price | {'discountRate': 5}]},
                       {'prices': [price | {'formattedPrice': '79.999,00'}]}):
            self.assertFalse(_ordinary_selected_cash(state | change, Decimal('86999')))

    def test_hepsiburada_seller_profile_binds_account_not_just_display_name(self):
        url = 'https://www.hepsiburada.com/magaza/notebookstore'
        offer = {'merchant': 'notebookstore', 'merchant_id': 'selected-account', 'seller_url': url}
        state = {'merchantState': {'merchantDetail': {
            'name': 'notebookstore', 'urlPostfix': 'notebookstore', 'id': 'selected-account',
        }}}
        html = (f'<link rel="canonical" href="{url}"><h1>notebookstore</h1>'
                f'<script id="reduxStore">{json.dumps(state)}</script>')
        self.assertTrue(hepsiburada_seller_profile_matches(html, offer, 'notebookstore'))
        self.assertFalse(hepsiburada_seller_profile_matches(
            html, offer | {'merchant_id': 'different-account'}, 'notebookstore'))

    def test_published_platforms_must_remain_in_an_expansion(self):
        n11 = observation()
        trendyol = observation(2, 'Trendyol', 'Mağaza B', 'magaza b anonim sirketi')
        teknosa = observation(3, 'Teknosa', 'Mağaza C', 'magaza c anonim sirketi')
        mediamarkt = observation(4, 'MediaMarkt', 'Mağaza D', 'magaza d anonim sirketi')
        offers = select_offers([trendyol, teknosa, mediamarkt, n11], required_platforms={'n11', 'Teknosa'})
        self.assertTrue({'n11', 'Teknosa'} <= {o['platform'] for o in offers})
        self.assertFalse(select_offers([trendyol, teknosa], required_platforms={'n11'}))

    def test_seller_profile_binds_teknosa_shop_id_and_tax_number(self):
        profile = ('<h1 class="plp-title">Mağaza</h1><input id="SelectedShopId" value="123">'
                   '<div class="msd-section">Firma Resmi Adı: Örnek Limited Şirketi '
                   'Şehir: İstanbul Vergi No: 0123456789</div>')
        offer = {'platform': 'Teknosa', 'merchant': 'Mağaza', 'merchant_id': '123'}
        self.assertEqual(marketplace_seller_identity(profile, offer),
                         {'_legal_name': 'ornek limited sirketi', '_tax_key': '0123456789'})
        self.assertIsNone(marketplace_seller_identity(profile, offer | {'merchant_id': '124'}))
        self.assertIsNone(marketplace_seller_identity(profile, offer | {'merchant': 'Başka'}))

    def test_prefers_cross_platform_and_never_exports_company_identifiers(self):
        a = observation()
        b = observation(2, 'n11', 'Mağaza B', 'magaza b anonim sirketi')
        c = observation(3, 'Trendyol', 'Mağaza C', 'magaza c limited sirketi')
        d = observation(4, 'n11', 'Mağaza D', 'magaza d limited sirketi')
        offers = select_offers([a, b, c, d])
        self.assertEqual({o['platform'] for o in offers}, {'n11', 'Trendyol'})
        model = build_model(offers, a)
        self.assertEqual(model['slug'], 'lenovo-83k10016tr')
        self.assertGreaterEqual(len(model['specs']), 6)
        self.assertNotIn('_legal_name', json.dumps(model))

    def test_real_n11_singleton_shape_and_mutations(self):
        html, shop = laptop_document()
        offer, reason = selected_n11(html, f'{URL}?magaza={shop}')
        self.assertIsNotNone(offer, reason)
        self.assertEqual(offer['gtin13'], EAN)
        self.assertIn('640/laptop.jpg', offer['_image'])
        for mutation in (
            lambda state, _: state['product'].update(defaultGtin='0198154123529'),
            lambda state, _: state['productMeta'].update(stock=0),
            lambda state, _: state['product'].update(skuUrl=URL + '?magaza=other'),
            lambda state, _: state['product'].update(availableToBuy=False),
            lambda state, _: state['product'].update(displayPriceFloat=1),
            lambda state, _: state.update(skuDefinitions='[{}]'),
        ):
            html, shop = laptop_document(mutation)
            self.assertIsNone(selected_n11(html, f'{URL}?magaza={shop}')[0])

    def test_seller_selector_cannot_be_lost_on_redirect(self):
        target = {'url': URL + '?magaza=second', 'mpn': '83K10016TR', 'gtin': EAN}
        with patch('laptop_pilot.PriorityRetailerTransport') as transport:
            transport.return_value.read_fresh_document.return_value = ('<html></html>', 'ready', 200)
            transport.return_value.resolved_url.return_value = URL
            found, rejections = read_retailer('www.n11.com', [target])
        self.assertFalse(found)
        self.assertEqual(rejections['selected_seller_route_changed'], 1)

    def test_laptop_reader_resumes_targets_skipped_during_transient_cooldown(self):
        html, shop = laptop_document()
        first = URL + '?magaza=first'
        waiting = URL + '?magaza=' + shop
        targets = [{'url': url, 'mpn': '83K10016TR', 'gtin': EAN} for url in (first, waiting)]
        with patch('laptop_pilot.PriorityRetailerTransport') as factory:
            transport = factory.return_value
            transport.read_fresh_document.side_effect = [
                (None, 'http_503', 503), (None, 'source_cooldown', None), (html, 'document_read', 200),
            ]
            transport.resolved_url.side_effect = lambda url: url
            transport.wait_for_cooldown.return_value = True
            found, rejections = read_retailer('www.n11.com', targets)
        transport.wait_for_cooldown.assert_called_once_with(waiting)
        self.assertEqual([offer['url'] for offer in found], [waiting])
        self.assertEqual(rejections, {'http_503': 1})
