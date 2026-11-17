import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from retail_html_catalog import HB, TY, catalog_gtin, crawl, database, export, make_row, merge_specifications, properties, product_url, read_category, read_product
from retail_catalog_publish import main as publish_main, invalidate_changed_identities, merge, ready_for_publication


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


    def test_full_selected_technical_sheet_is_not_truncated(self):
        facts = properties([{'name': f'Teknik alan {i}', 'value': 'USB-C bağlantı ' * 20}
                            for i in range(30)])
        row = make_row(TY, 'laptop', URL, 'Lenovo Ideapad', 'Lenovo', GTIN, '83K10016TR', [IMAGE], facts)
        self.assertEqual(len(row['specs']), 30)
        self.assertEqual(row['specs']['Teknik alan 29'], ('USB-C bağlantı ' * 20).strip())
        self.assertEqual(row['specSources']['Teknik alan 29'],
                         {'source': URL, 'checkedAt': row['checkedAt']})

    def test_newer_shorter_sheet_updates_values_without_redating_retained_facts(self):
        old = {'source': URL, 'checkedAt': '2026-11-16T00:00:00+00:00',
               'specs': {'Bellek': '8 GB', 'İşlemci': 'Intel Core i5'}}
        new = {'source': URL, 'checkedAt': '2026-11-17T00:00:00+00:00', 'specs': {'Bellek': '16 GB'}}
        merged = merge_specifications(old, new)
        self.assertEqual(merged['specs'], {'Bellek': '16 GB', 'İşlemci': 'Intel Core i5'})
        self.assertEqual(merged['specSources']['İşlemci']['checkedAt'], old['checkedAt'])
        self.assertEqual(merged['specSources']['Bellek']['checkedAt'], new['checkedAt'])
        self.assertFalse(merged['specConflicts'])

    def test_cross_source_disagreement_is_retained_until_corrected(self):
        first = {'source': URL, 'checkedAt': '2026-11-16T00:00:00+00:00',
                 'specs': {'Bellek': '8 GB', 'İşlemci': 'Intel Core i5'}}
        second_url = 'https://www.hepsiburada.com/ideapad-p-HBCV0000ABC123'
        second = {'source': second_url, 'checkedAt': '2026-11-17T00:00:00+00:00',
                  'specs': {'Bellek': '16 GB', 'Ekran': '15.6 inç'}}
        merged = merge_specifications(first, second)
        self.assertNotIn('Bellek', merged['specs'])
        self.assertEqual(len(merged['specConflicts']['Bellek']), 2)
        self.assertEqual(set(merged['specs']), {'İşlemci', 'Ekran'})
        corrected = {**first, 'checkedAt': '2026-11-17T01:00:00+00:00', 'specs': {'Bellek': '16 GB'}}
        resolved = merge_specifications(merged, corrected)
        self.assertFalse(resolved['specConflicts'])
        self.assertEqual(resolved['specs']['Bellek'], '16 GB')

    def test_export_combines_complementary_facts_from_exact_barcode(self):
        first = read_product(self.listing(), URL, 'laptop')
        first['specSources'] = {}
        other = {**first, 'source': 'https://www.hepsiburada.com/ideapad-p-HBCV0000ABC123',
                 'specs': {'Ekran': '15.6 inç'}, 'offer': None}
        with TemporaryDirectory() as root:
            db = database(Path(root) / 'queue.sqlite')
            for row in (first, other):
                db.execute('INSERT INTO queue(url,host,kind,title,status,data) VALUES (?,?,?,?,?,?)',
                           (row['source'], TY, 'laptop', row['name'], 'verified', json.dumps(row)))
            destination = Path(root) / 'catalog.json'
            export(db, destination)
            db.close()
            result = json.loads(destination.read_text())[0]
            self.assertEqual(set(result['specs']), {'Model kodu', 'Bellek', 'Ekran'})
            self.assertEqual(result['specSources']['Ekran']['source'], other['source'])


    def test_publisher_refreshes_shorter_sheet_and_keeps_old_field_evidence(self):
        row = read_product(self.listing(), URL, 'laptop')
        row.update(offers=[row.pop('offer')], checkedAt='2026-11-16T00:00:00+00:00')
        row.pop('specSources')
        row['specs']['Bellek'] = '8 GB'
        with TemporaryDirectory() as root:
            folder = Path(root)
            for name, key in (('laptopPilot', 'models'), ('monitorPilot', 'models'),
                              ('pcParts', 'parts'), ('twsOfferSnapshot', 'models')):
                (folder / (name + '.json')).write_text(json.dumps({key: []}))
            catalog = folder / 'catalog.json'
            catalog.write_text(json.dumps([row]))
            fresh = {**row, 'checkedAt': '2026-11-17T00:00:00+00:00',
                     'specs': {'Bellek': '16 GB', 'Ekran': '15.6 inç'}}
            merged, _ = merge([fresh], folder, lambda _: True, catalog)
            self.assertEqual(merged[0]['specs']['Bellek'], '16 GB')
            self.assertEqual(merged[0]['specSources']['Model kodu'], {'source': URL})
            wrong = {**fresh, 'model': 'DIFFERENT-MODEL'}
            held, counters = merge([wrong], folder, lambda _: True, catalog)
            self.assertEqual(counters['identity_conflict'], 1)
            self.assertEqual(held[0]['specs']['Bellek'], '8 GB')


    def test_refresh_respects_category_and_rejects_reused_product_url(self):
        row = read_product(self.listing(), URL, 'laptop')
        with TemporaryDirectory() as root:
            db = database(Path(root) / 'queue.sqlite')
            db.executemany('INSERT INTO queue(url,host,kind,title,status,data) VALUES (?,?,?,?,?,?)', [
                (URL, TY, 'laptop', row['name'], 'verified', json.dumps(row)),
                (URL + '-tablet', TY, 'tablet', 'Tablet', 'verified', json.dumps(row)),
            ])
            args = SimpleNamespace(spacing=.5, host=[TY], kind=['laptop'], refresh=True, verify_only=False,
                                   retry_unverified=False, retry_redirects=False, discovery_only=False,
                                   maximum=10, workers=1)
            changed = {**row, 'gtin': '4711081273981'}
            with patch('retail_html_catalog.Host.get', new_callable=AsyncMock, return_value=(200, b'html')) as get:
                with patch('retail_html_catalog.read_product', return_value=changed):
                    asyncio.run(crawl(args, db))
            self.assertEqual(get.await_count, 1)
            self.assertEqual(get.await_args.args, (URL,))
            retained = json.loads(db.execute('SELECT data FROM queue WHERE url=?', (URL,)).fetchone()[0])
            self.assertIsNone(retained['offer'])
            self.assertEqual(retained['specs'], row['specs'])
            self.assertEqual(retained['specSources'], row['specSources'])
            self.assertEqual(retained['checkedAt'], row['checkedAt'])
            self.assertGreaterEqual(retained['offerCheckedAt'], row['checkedAt'])
            self.assertIsNone(db.execute('SELECT checked_at FROM queue WHERE kind="tablet"').fetchone()[0])
            db.execute('DELETE FROM queue WHERE kind="tablet"')
            staged = Path(root) / 'staged.json'
            export(db, staged)
            output = json.loads(staged.read_text())[0]
            self.assertEqual(output['offers'], [])
            self.assertEqual(output['checkedSources'][URL], retained['offerCheckedAt'])
            self.assertEqual(output['checkedAt'], row['checkedAt'])
            for name, key in (('laptopPilot', 'models'), ('monitorPilot', 'models'),
                              ('pcParts', 'parts'), ('twsOfferSnapshot', 'models')):
                (Path(root) / (name + '.json')).write_text(json.dumps({key: []}))
            published = {**row, 'offers': [row['offer']]}
            published.pop('offer')
            catalog = Path(root) / 'catalog.json'
            catalog.write_text(json.dumps([published]))
            merged, _ = merge([output], Path(root), lambda _: True, catalog)
            self.assertEqual(merged[0]['offers'], [])
            self.assertEqual(merged[0]['specs'], row['specs'])
            attempt = db.execute('SELECT outcome,status,attempted_at FROM refresh_attempts WHERE url=?', (URL,)).fetchone()
            self.assertEqual(attempt[:2], ('identity_changed', 200))
            self.assertTrue(attempt[2])
            self.assertEqual(db.execute('SELECT count(*) FROM refresh_attempts').fetchone()[0], 1)

            db.close()


    def test_publisher_keeps_existing_specs_after_offer_expiry(self):
        row = read_product(self.listing(), URL, 'laptop')
        row['offers'] = [{**row.pop('offer'), 'checkedAt': '2020-01-01T00:00:00+00:00'}]
        with TemporaryDirectory() as root:
            folder = Path(root)
            for name, key in (('laptopPilot', 'models'), ('monitorPilot', 'models'),
                              ('pcParts', 'parts'), ('twsOfferSnapshot', 'models')):
                (folder / (name + '.json')).write_text(json.dumps({key: []}))
            catalog, report, output = [folder / name for name in ('catalog.json', 'report.json', 'out.json')]
            catalog.write_text(json.dumps([row]))
            report.write_text('[]')
            command = ['retail_catalog_publish.py', str(report), str(folder), '--catalog', str(catalog),
                       '--image-cache', str(folder / 'images.sqlite'), '--output', str(output)]
            with patch('sys.argv', command), patch('retail_catalog_publish.verify_images',
                                                  new_callable=AsyncMock, return_value={IMAGE}):
                publish_main()
            result = json.loads(output.read_text())
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]['specs'], row['specs'])
            self.assertEqual(result[0]['offers'][0]['checkedAt'], '2020-01-01T00:00:00+00:00')
            row['specs'] = {'Stok Adedi': '20', 'Stok Kodu': 'ABC123'}
            catalog.write_text(json.dumps([row]))
            with patch('sys.argv', command), patch('retail_catalog_publish.verify_images',
                                                  new_callable=AsyncMock, return_value={IMAGE}):
                publish_main()
            incomplete = json.loads(output.read_text())
            self.assertEqual(len(incomplete), 1)
            self.assertEqual(incomplete[0]['specs'], {})



    def test_property_values_keep_units_and_reject_contradictory_selected_fields(self):
        self.assertEqual(properties([
            {'name': 'Frekans', 'value': 5.1, 'unitText': 'GHz'},
            {'name': 'Kodek', 'value': ['AAC', 'SBC']},
            {'name': 'Bellek', 'value': '8 GB'},
            {'name': 'Bellek', 'value': '16 GB'},
        ]), {'Frekans': '5.1 GHz', 'Kodek': 'AAC, SBC'})


    def test_stock_and_media_metadata_do_not_count_as_technical_details(self):
        self.assertEqual(properties([
            {'name': 'Stok Adedi', 'value': '20'},
            {'name': 'Stok Kodu', 'value': 'ABC123'},
            {'name': 'Paket Görseli (ön)', 'value': 'gorsel.png'},
            {'name': 'Kullanım Talimatı/Uyarıları', 'value': 'https://example.com/manual.pdf'},
            {'name': 'Bluetooth', 'value': '5.3'},
        ]), {'Bluetooth': '5.3'})


    def test_blocked_refresh_records_attempt_without_discarding_previous_evidence(self):
        row = read_product(self.listing(), URL, 'laptop')
        with TemporaryDirectory() as root:
            db = database(Path(root) / 'queue.sqlite')
            db.execute('INSERT INTO queue(url,host,kind,title,status,data) VALUES (?,?,?,?,?,?)',
                       (URL, TY, 'laptop', row['name'], 'verified', json.dumps(row)))
            args = SimpleNamespace(spacing=.5, host=[TY], kind=['laptop'], refresh=True, verify_only=False,
                                   retry_unverified=False, retry_redirects=False, discovery_only=False,
                                   maximum=10, workers=1)
            for status in (403, 429, 503, 0):
                with patch('retail_html_catalog.Host.get', new_callable=AsyncMock, return_value=(status, b'')):
                    asyncio.run(crawl(args, db))
                attempt = db.execute('SELECT outcome,status FROM refresh_attempts WHERE url=?', (URL,)).fetchone()
                self.assertEqual(attempt, (f'http_{status}', status))
                saved = db.execute('SELECT status,data FROM queue WHERE url=?', (URL,)).fetchone()
                self.assertEqual(saved[0], 'verified')
                self.assertEqual(json.loads(saved[1]), row)
                self.assertEqual(db.execute('SELECT count(*) FROM refresh_attempts').fetchone()[0], 1)
            db.close()


    def test_queue_identity_rejections_apply_when_product_cannot_export(self):
        row = read_product(self.listing(), URL, 'laptop')
        cutoff = '2026-11-17T12:00:00+00:00'
        old_offer = {**row['offer'], 'checkedAt': '2026-11-17T11:00:00+00:00'}
        newer_offer = {**row['offer'], 'checkedAt': '2026-11-17T13:00:00+00:00'}
        published = {**row, 'offers': [old_offer]}
        other_gtin = {**published, 'gtin': '4711081273981'}
        with TemporaryDirectory() as root:
            path = Path(root) / 'queue.sqlite'
            db = database(path)
            unexportable = {**row, 'specs': {}, 'offer': None, 'offerCheckedAt': cutoff}
            db.execute('INSERT INTO queue(url,host,kind,title,status,data) VALUES (?,?,?,?,?,?)',
                       (URL, TY, 'laptop', row['name'], 'verified', json.dumps(unexportable)))
            db.execute('INSERT INTO refresh_attempts(url,host,kind,outcome,status) VALUES (?,?,?,?,?)',
                       (URL, TY, 'laptop', 'identity_changed', 200))
            db.commit()
            db.close()
            result, count = invalidate_changed_identities([published, other_gtin], path)
            self.assertEqual(count, 1)
            self.assertEqual(result[0]['offers'], [])
            self.assertEqual(result[0]['specs'], row['specs'])
            self.assertEqual(result[0]['checkedAt'], row['checkedAt'])
            self.assertEqual(result[1], other_gtin)
            result, count = invalidate_changed_identities([{**published, 'offers': [newer_offer]}], path)
            self.assertEqual(count, 0)
            self.assertEqual(result[0]['offers'], [newer_offer])
            result, count = invalidate_changed_identities([published], path, scope={other_gtin['gtin']})
            self.assertEqual((result, count), ([published], 0))
            # A later transport failure must not erase a confirmed identity rejection.
            db = database(path)
            db.execute('UPDATE refresh_attempts SET outcome="http_403", status=403 WHERE url=?', (URL,))
            db.commit()
            db.close()
            result, count = invalidate_changed_identities([published], path)
            self.assertEqual((result[0]['offers'], count), ([], 1))
            for name, key in (('laptopPilot', 'models'), ('monitorPilot', 'models'),
                              ('pcParts', 'parts'), ('twsOfferSnapshot', 'models')):
                (Path(root) / (name + '.json')).write_text(json.dumps({key: []}))
            catalog, staged, output = [Path(root) / name for name in ('catalog.json', 'staged.json', 'output.json')]
            catalog.write_text(json.dumps([published]))
            staged.write_text('[]')
            command = ['retail_catalog_publish.py', str(staged), root, '--catalog', str(catalog),
                       '--image-cache', str(Path(root) / 'images.sqlite'), '--output', str(output),
                       '--queue', str(path)]
            with patch('sys.argv', command), patch('retail_catalog_publish.verify_images',
                                                  new_callable=AsyncMock, return_value={IMAGE}):
                publish_main()
            persisted = json.loads(output.read_text())[0]
            self.assertEqual(persisted['offers'], [])
            self.assertEqual(persisted['specs'], published['specs'])
