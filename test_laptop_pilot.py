from copy import deepcopy
import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from bs4 import BeautifulSoup

from laptop_pilot import build_model, compatible, read_retailer, select_offers, valid_pair
from laptop_retailer_sources import laptop_facts, selected_n11

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
