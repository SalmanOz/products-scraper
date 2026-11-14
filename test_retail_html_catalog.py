import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from retail_html_catalog import HB, TY, catalog_gtin, database, export, product_url, read_category, read_product
from retail_catalog_publish import merge, ready_for_publication


URL = 'https://www.trendyol.com/lenovo/ideapad-83k10016tr-p-1132995554'
GTIN = '0198155448171'
IMAGE = 'https://cdn.dsmcdn.com/ty1000/image.jpg'


class RetailHtmlCatalogTest(TestCase):
    def listing(self, *, title=None, identity=1132995554, barcode=GTIN, winner_barcode=GTIN, price=31000,
                campaign=False):
        heading = title or 'Lenovo Ideapad 83K10016TR'
        state = {
            'id': identity, 'name': 'Ideapad 83K10016TR', 'brand': {'name': 'Lenovo'},
            'category': {'name': 'Dizüstü Bilgisayar'}, 'images': [IMAGE], 'inStock': True,
            'attributes': [{'key': {'name': 'Model kodu'}, 'value': {'name': '83K10016TR'}},
                           {'key': {'name': 'Bellek'}, 'value': {'name': '16 GB'}}],
            'variants': [{'isSelected': True, 'itemNumber': 27, 'barcode': barcode}],
            'merchantListing': {'merchant': {'name': 'Mağaza'},
                                'winnerVariant': {'itemNumber': 27, 'barcode': winner_barcode,
                                                  'sellable': True, 'inStock': True,
                                                  'price': {'currency': 'TRY', 'sellingPrice': {'value': price}}}},
        }
        schema = {'@type': 'Product', '@id': URL, 'offers': {
            '@type': 'Offer', 'availability': 'https://schema.org/InStock', 'url': URL,
            'priceCurrency': 'TRY', 'itemCondition': 'https://schema.org/NewCondition', 'price': 31000,
        }}
        banner = ''
        if campaign:
            state['merchantListing']['winnerVariant']['price']['discountedPriceAfterNoLimitPromotions'] = {'value': price}
            banner = ('<div class="campaign-price-content"><p class="text">Sepette</p>'
                      '<p class="new-price">31.000 TL</p><p class="old-price">32.000 TL</p></div>')
        return (f'<link rel="canonical" href="{URL}"><h1>{heading}</h1>{banner}<script>'
                f'window["__envoy__SHARED_PROPS"] = {json.dumps({"product": state})}</script>'
                f'<script type="application/ld+json">{json.dumps(schema)}</script>').encode()

    def test_category_never_emits_a_price_or_an_external_product(self):
        category = 'https://www.hepsiburada.com/tablet-c-3008012'
        destination = f'https://{HB}/tablet-p-HBCV0000ABC123'
        items = [{'item': {'url': url, 'name': 'Tablet', 'offers': {'price': 10}}}
                 for url in [destination, 'https://example.com/tablet-p-HBCV000ABC123']]
        body = '<script type="application/ld+json">' + json.dumps(
            {'@type': 'ItemList', 'itemListElement': items}) + '</script>'
        self.assertEqual(read_category(body.encode(), category, HB), {destination: 'Tablet'})
        self.assertIsNone(product_url(destination + '#different', category))

    def test_selected_product_and_offer_must_agree_on_identity(self):
        model = read_product(self.listing(), URL, 'laptop')
        self.assertEqual((model['gtin'], model['model'], model['offer']['price']), (GTIN, '83K10016TR', 31000))
        self.assertIsNone(read_product(self.listing(identity=19), URL, 'laptop'))
        self.assertIsNone(read_product(self.listing(winner_barcode='4711081273981'), URL, 'laptop'))
        self.assertIsNone(read_product(self.listing(), URL, 'tablet'))
        self.assertIsNone(read_product(self.listing(title='Lenovo Ideapad ikinci el'), URL, 'laptop'))
        self.assertEqual(read_product(self.listing(price=30000), URL, 'laptop')['offer'], None)

    def test_trendyol_seller_link_requires_that_seller_and_its_visible_price(self):
        raw = self.listing().replace(b'"merchant": {', b'"merchant": {"id": 42, ')
        raw = raw.replace(b'<script>', b'<div class="normal-price"><span class="price-container">31.000 TL</span></div><script>', 1)
        url = URL + '?merchantId=42'
        proof = read_product(raw, url, 'laptop')
        self.assertEqual((proof['gtin'], proof['offer']['price'], proof['offer']['url']), (GTIN, 31000, url))
        self.assertIsNone(read_product(raw, URL + '?merchantId=43', 'laptop'))
        self.assertIsNone(read_product(raw.replace(b'31.000 TL', b'30.000 TL'), url, 'laptop')['offer'])

    def test_store_and_region_codes_are_not_global_product_ids(self):
        for code in ('0000000022507', '2728119910147', '2482120394890'):
            with self.subTest(code=code):
                self.assertIsNone(catalog_gtin(code))
        self.assertEqual(catalog_gtin(GTIN[1:]), GTIN)

    def test_trendyol_uses_the_normal_price_when_a_conditional_offer_is_advertised(self):
        accepted = read_product(self.listing(price=32000, campaign=True), URL, 'laptop')
        self.assertEqual(accepted['offer']['price'], 32000)
        mismatch = read_product(self.listing(price=33000, campaign=True), URL, 'laptop')
        self.assertIsNone(mismatch['offer'])

    def test_publish_requires_a_current_offer_and_a_verified_image(self):
        now = datetime(2026, 11, 14, tzinfo=timezone.utc)
        row = {'image': IMAGE, 'offers': [{'checkedAt': now.isoformat()}]}
        self.assertTrue(ready_for_publication(row, lambda _: True, now))
        self.assertFalse(ready_for_publication(row, lambda _: False, now))
        self.assertFalse(ready_for_publication({**row, 'offers': []}, lambda _: True, now))
        self.assertFalse(ready_for_publication(row, lambda _: True, now + timedelta(hours=73)))

    def test_merge_keeps_one_barcode_and_excludes_existing_site_products(self):
        row = read_product(self.listing(), URL, 'laptop')
        with TemporaryDirectory() as root:
            folder = Path(root)
            for name, key in (('laptopPilot', 'models'), ('monitorPilot', 'models'),
                              ('pcParts', 'parts'), ('twsOfferSnapshot', 'models')):
                (folder / (name + '.json')).write_text(json.dumps({key: []}))
            db = database(folder / 'queue.sqlite')
            db.execute('INSERT INTO queue(url,host,kind,title,status,data) VALUES (?,?,?,?,?,?)',
                       (URL, TY, 'laptop', row['name'], 'verified', json.dumps(row)))
            path = folder / 'staged.json'
            export(db, path)
            db.close()
            rows = json.loads(path.read_text())
            catalog = folder / 'catalog.json'
            catalog.write_text('[]')
            skipped, rejected = merge(rows, folder, lambda _: False, catalog)
            self.assertFalse(skipped)
            self.assertEqual(rejected['image_unavailable'], 1)
            merged, counts = merge(rows + rows, folder, lambda _: True, catalog)
            self.assertEqual(len(merged), 1)
            self.assertEqual(counts['added'], 1)
            catalog.write_text(json.dumps(merged))
            other = {**row, 'gtin': '4711081273981', 'offers': [],
                     'source': URL.replace('1132995554', '1132995556')}
            merged, counts = merge([other], folder, lambda _: True, catalog)
            self.assertEqual([item['gtin'] for item in merged], [GTIN])
            self.assertEqual(counts['existing_identity'], 1)
            checked = {**rows[0], 'offers': [], 'checkedSources': {URL: rows[0]['checkedAt']}}
            self.assertEqual(merge([checked], folder, lambda _: True, catalog)[0][0]['offers'], [])
            (folder / 'laptopPilot.json').write_text(json.dumps({'models': [{'gtin': GTIN}]}))
            self.assertFalse(merge(rows, folder, lambda _: True, catalog)[0])
