"""Catalog-derived HB selected seller proof; no production requests or writes."""
import json
import unittest
from pathlib import Path
from bs4 import BeautifulSoup

from hepsiburada_catalog_source import HepsiburadaCatalogSource, catalog_offer_matches, parse_catalog_buy_box
from retailer_offer_refresh import RetailerOfferRefresher
from test_hepsiburada_iphone16pm_readonly import (
    COLOR, GTIN13, PRODUCT_ID, SCHEMA_URL, SKU, URL, fixture,
)
from update_prices import PriceUpdater

NAME = 'Samsung Galaxy A36 5G'
SPECS = {'ram_gb': 8, 'storage_gb': 128, 'network_generation': '5g'}
TARGET = 'https://www.hepsiburada.com/samsung-galaxy-a36-5g-128-gb-8-gb-ram-antrasit-pm-HBC0000TEST00'


def document(changes=None):
    html = fixture(changes)
    for old, new in ((URL, TARGET), (SCHEMA_URL, TARGET),
                     ('Apple iPhone 16 Pro Max', NAME), ('256', '128'),
                     (SKU, 'HBCV0000TEST01'), (PRODUCT_ID, 'HBC0000TEST00'),
                     (COLOR, 'Antrasit'), (GTIN13, '8806094821437')):
        html = html.replace(old, new)
    return html.replace('128GB Antrasit', '128 GB 8 GB RAM Antrasit')


class HepsiburadaCatalogTests(unittest.TestCase):
    def test_price_skeleton_requires_ordinary_cash_and_matching_buy_box_events(self):
        soup = BeautifulSoup(document(), 'html.parser')
        redux = soup.select_one('script#reduxStore')
        state = json.loads(redux.get_text())
        product = state['productState']['product']
        price = float(product['variants'][0]['price'])
        formatted = f'{price:,.2f}'.translate(str.maketrans(',.', '.,'))
        product.update(prices=[{'value': price, 'formattedPrice': formatted, 'currency': 0, 'discountRate': 0}],
                       campaignIds=[], campaignNote='', isCampaignParticipationClosed=True)
        redux.string = json.dumps(state)
        region = soup.select_one('[data-test-id="default-price"]')
        region['data-test-id'] = 'price-skeleton'
        region.clear()
        html = str(soup)
        offer, reason = parse_catalog_buy_box(html, TARGET, NAME, SPECS)
        self.assertIsNotNone(offer, reason)
        self.assertEqual(offer['price'], price)
        for changed in (html.replace('price-skeleton', 'unrelated-region'),
                        html.replace('"campaignIds": []', '"campaignIds": ["coupon"]'),
                        html.replace(formatted, '1.234,00')):
            self.assertIsNone(parse_catalog_buy_box(changed, TARGET, NAME, SPECS)[0])

    def test_actual_selected_one_tb_label_control_and_technical_cell_agree_on_1024_gb(self):
        from automatic_memory_variants import variant_selected_evidence
        from observed_memory_variants import inspect_hepsiburada_memory_variant
        from copy import deepcopy
        html = Path('fixtures/hepsiburada-selected-iphone17pro-1tb-public.html').read_text()
        url = 'https://www.hepsiburada.com/apple-iphone-17-pro-1-tb-abis-p-HBCV00009Z403Y'
        specs = {'ram_gb': 12, 'storage_gb': 1024, 'network_generation': '5g'}
        offer, reason = parse_catalog_buy_box(html, url, 'Apple iPhone 17 Pro', specs)
        self.assertEqual(reason, 'selected_new_cash_offer_observed')
        self.assertEqual((offer['merchant'], offer['price'], offer['gtin13']), ('Hepsiburada', 123999, '0195950628807'))
        self.assertEqual(offer['source_title'], 'Apple iPhone 17 Pro 1 TB Abis')
        proof = variant_selected_evidence(offer, 'Apple iPhone 17 Pro', specs)
        self.assertEqual((proof['ram_gb'], proof['storage_gb'], proof['network_generation']), (12, 1024, '5g'))
        original = {**specs, 'storage_gb': 256}; before = deepcopy(original)
        preview = inspect_hepsiburada_memory_variant(html, url, 'Apple iPhone 17 Pro', original)
        self.assertEqual(preview['observed_specs'], specs)
        self.assertFalse(preview['writes'])
        self.assertEqual(original, before)
        for patch in ({'ram_gb': 8}, {'storage_gb': 1000}, {'storage_gb': 2048}, {'network_generation': '4g'}):
            self.assertIsNone(parse_catalog_buy_box(html, url, 'Apple iPhone 17 Pro', {**specs, **patch})[0])

    def test_tb_unit_conversion_cannot_replace_selected_control_or_disagreeing_commercial_evidence(self):
        import json
        from copy import deepcopy
        from observed_memory_variants import inspect_hepsiburada_memory_variant
        html = Path('fixtures/hepsiburada-selected-iphone17pro-1tb-public.html').read_text()
        url = 'https://www.hepsiburada.com/apple-iphone-17-pro-1-tb-abis-p-HBCV00009Z403Y'
        specs = {'ram_gb': 12, 'storage_gb': 1024, 'network_generation': '5g'}
        changes = []
        for text in ('2 TB', '1000 GB', '1 TB / 2 TB', '1 TB + 512 GB', '1 TB veya 512 GB', 'Var', '1,024 TB'):
            soup = BeautifulSoup(html, 'html.parser')
            label = soup.select_one('#techSpecs').find(string='Dahili Hafıza')
            label.parent.find_next_sibling().string = text
            changes.append(str(soup))
        for value in ('2 TB', '1000 GB', '1 TB/2 TB', '1tb', '1 TB RAM'):
            soup = BeautifulSoup(html, 'html.parser'); script = soup.select_one('script#reduxStore')
            state = json.loads(script.string)
            selected = next(v for v in state['productState']['product']['variants'] if v['isDefault'])
            next(p for p in selected['properties'] if p['name'] == 'Kapasite')['valueObject']['actualValue'] = value
            script.string = json.dumps(state); changes.append(str(soup))
        soup = BeautifulSoup(html, 'html.parser'); soup.select_one('button[data-test-id="variant-box-Kapasite-1 TB"]').decompose(); changes.append(str(soup))
        soup = BeautifulSoup(html, 'html.parser'); soup.select_one('button[data-test-id="variant-box-Kapasite-1 TB"]')['aria-label'] = 'Kapasite 1 TB'; changes.append(str(soup))
        soup = BeautifulSoup(html, 'html.parser'); cell=soup.select_one('#techSpecs').find(string='Dahili Hafıza').parent.parent; cell.insert_after(deepcopy(cell)); changes.append(str(soup))
        for before, after in [('0195950628807', '0195950628808'), ('"isInStock": true', '"isInStock": false'),
                              ('123999.00', '123998.00'),
                              ('>5G<', '>4.5G<'), ('data-test-id="addToCart"', 'disabled data-test-id="addToCart"')]:
            self.assertIn(before, html)
            changes.append(html.replace(before, after, 1))
        for index, changed in enumerate(changes):
            self.assertIsNone(parse_catalog_buy_box(changed, url, 'Apple iPhone 17 Pro', specs)[0], index)
            self.assertIsNone(inspect_hepsiburada_memory_variant(changed, url, 'Apple iPhone 17 Pro', {**specs, 'storage_gb': 256}), index)
        changed = html.replace('>12 GB RAM<', '>8 GB RAM<')
        self.assertIsNone(parse_catalog_buy_box(changed, url, 'Apple iPhone 17 Pro', specs)[0])
        preview = inspect_hepsiburada_memory_variant(changed, url, 'Apple iPhone 17 Pro', {**specs, 'storage_gb': 256})
        # A preview reports the independent source RAM, never the parent's.
        # It cannot publish before a separate technical document agrees.
        self.assertEqual(preview['observed_specs']['ram_gb'], 8)
        self.assertFalse(preview['writes'])

    def test_identical_duplicate_selected_tables_keep_full_sku_and_seller_proof(self):
        from copy import deepcopy
        soup = BeautifulSoup(document(), 'html.parser')
        table = soup.select_one('#techSpecs')
        table.insert_after(deepcopy(table))
        html = str(soup)
        offer, reason = parse_catalog_buy_box(html, TARGET, NAME, SPECS)
        self.assertIsNotNone(offer, reason)
        self.assertTrue(catalog_offer_matches(offer, NAME, SPECS))
        for before, after in [
                ('8806094821437', '8806094821438'),
                ('data-test-id="addToCart"', 'disabled data-test-id="addToCart"'),
                ('Seçili Kapasite 128 GB', 'Kapasite 128 GB')]:
            with self.subTest(before=before):
                self.assertIn(before, html)
                self.assertIsNone(parse_catalog_buy_box(html.replace(before, after), TARGET, NAME, SPECS)[0])

    def test_duplicate_tables_with_any_different_fact_or_excess_copies_are_rejected(self):
        from copy import deepcopy
        original = BeautifulSoup(document(), 'html.parser')
        text = original.select_one('#techSpecs').get_text(' ', strip=True)
        for before, after in [
                ('Dahili Hafıza 128 GB', 'Dahili Hafıza 256 GB'),
                ('8 GB RAM', '12 GB RAM'),
                ('Mobil Bağlantı Hızı 5G', 'Mobil Bağlantı Hızı 4.5G'),
                ('HBCV0000TEST01', 'HBCV0000OTHER1'),
                ('Stok Kodu', 'Kondisyon C Grade Stok Kodu')]:
            with self.subTest(before=before):
                self.assertIn(before, text)
                for changed_first in (False, True):
                    soup = deepcopy(original)
                    table = soup.select_one('#techSpecs')
                    other = deepcopy(table)
                    (table if changed_first else other).string = text.replace(before, after)
                    table.insert_after(other)
                    self.assertIsNone(parse_catalog_buy_box(str(soup), TARGET, NAME, SPECS)[0])
        soup = deepcopy(original)
        table = soup.select_one('#techSpecs')
        table.insert_after(deepcopy(table))
        table.insert_after(deepcopy(table))
        self.assertIsNone(parse_catalog_buy_box(str(soup), TARGET, NAME, SPECS)[0])

    def test_information_advisory_is_not_a_hardware_variant_but_requires_its_exact_selected_control(self):
        import json
        from copy import deepcopy
        label = 'İSİM VE AÇIKLAMALAR KISMINDAKİ BİLGİLER DOĞRUDUR'
        base = BeautifulSoup(document(), 'html.parser')
        state = json.loads(base.select_one('script#reduxStore').text)
        option = {'name': 'Seçenek', 'isShowPrice': False, 'valueObject': {'actualValue': label}}
        def render(property_patch=None, *, control_label=label, selected=True):
            soup = BeautifulSoup(str(base), 'html.parser')
            changed = deepcopy(state)
            changed['productState']['product']['variants'][0]['properties'].append({**option, **(property_patch or {})})
            soup.select_one('script#reduxStore').string = json.dumps(changed)
            control = '' if control_label is None else ('<button data-test-id="variant-box-Seçenek-' +
                control_label + '" aria-label="' + ('Seçili ' if selected else '') + 'Seçenek ' + control_label + '">' + control_label + '</button>')
            return str(soup) + control
        offer, reason = parse_catalog_buy_box(render(), TARGET, NAME, SPECS)
        self.assertIsNotNone(offer, reason)
        self.assertTrue(catalog_offer_matches(offer, NAME, SPECS))
        for index, changed in enumerate([render(control_label=None), render(selected=False),
                        render({'isShowPrice': True}), render({'isShowPrice': None}),
                        render({'valueObject': {'actualValue': 'Aksesuarlı Paket'}}),
                        render({'valueObject': {'actualValue': 'Yenilenmiş Ürün'}}),
                        render().replace('Mobil Bağlantı Hızı <span>5G</span>', 'Mobil Bağlantı Hızı <span>4.5G</span>'),
                        render().replace('8 GB RAM', '12 GB RAM'),
                        render().replace('8806094821437', '8806094821438'),
                        render().replace('data-test-id="addToCart"', 'disabled data-test-id="addToCart"')]):
            self.assertTrue(changed != render(), f'No-op negative fixture {index}')
            self.assertIsNone(parse_catalog_buy_box(changed, TARGET, NAME, SPECS)[0], f'Negative fixture {index}')

    def test_literal_storage_presence_cell_requires_actual_selected_sku_capacity_and_visible_control(self):
        # Public Redmi Note 15 Pro PDPs observed Nov 4: "Dahili Hafıza Var"
        # with separately selected 256 GB controls and SKU properties.
        soup = BeautifulSoup(document(), 'html.parser')
        table = soup.select_one('#techSpecs')
        table.string = table.get_text(' ', strip=True).replace('Dahili Hafıza 128 GB', 'Dahili Hafıza Var')
        html = str(soup)
        offer, reason = parse_catalog_buy_box(html, TARGET, NAME, SPECS)
        self.assertIsNotNone(offer, reason)
        self.assertEqual(offer['storage_gb'], 128)
        self.assertEqual(offer['storage_evidence_source'], 'selected_sku_and_visible_capacity_control')
        self.assertTrue(catalog_offer_matches(offer, NAME, SPECS))
        for before, after in [
                ('Dahili Hafıza Var', 'Dahili Hafıza Yok'),
                ('Dahili Hafıza Var', 'Dahili Hafıza Unknown'),
                ('Dahili Hafıza Var', 'Dahili Hafıza 256 GB'),
                ('Dahili Hafıza Var', 'Dahili Hafıza Var Dahili Hafıza 256 GB'),
                ('Seçili Kapasite 128 GB', 'Kapasite 128 GB'),
                ('variant-box-Kapasite-128 GB', 'variant-box-Kapasite-256 GB'),
                ('"actualValue": "128 GB"', '"actualValue": "256 GB"'),
                ('8 GB RAM', '12 GB RAM'),
                ('Mobil Bağlantı Hızı 5G', 'Mobil Bağlantı Hızı 4.5G'),
                ('8806094821437', '8806094821438'),
                ('data-test-id="addToCart"', 'disabled data-test-id="addToCart"')]:
            with self.subTest(before=before, after=after):
                self.assertIn(before, html)
                self.assertIsNone(parse_catalog_buy_box(html.replace(before, after), TARGET, NAME, SPECS)[0])

    def test_64gb_iphone_storage_is_observed_without_inventing_ram_or_accepting_other_capacities(self):
        from tr_price_scraper import TRPriceScraper
        url = 'https://www.hepsiburada.com/apple-iphone-11-64-gb-beyaz-p-HBV0000120X66'
        html = fixture({('visible_ram',): '4 GB RAM'})
        for old, new in [(URL, url), (SCHEMA_URL, url), ('Apple iPhone 16 Pro Max', 'Apple iPhone 11'),
                         ('256', '64'), (SKU, 'HBV0000120X66'), (PRODUCT_ID, 'HB00000NSBZ4'), (COLOR, 'Beyaz')]:
            html = html.replace(old, new)
        specs = {'ram_gb': 4, 'storage_gb': 64, 'network_generation': '4g'}
        soup = BeautifulSoup(html, 'html.parser')
        table = soup.select_one('#techSpecs')
        detail = table.get_text(' ', strip=True)
        self.assertIn('Mobil Bağlantı Hızı 5G', detail)
        table.string = detail.replace('Mobil Bağlantı Hızı 5G', 'Mobil Bağlantı Hızı 4.5G')
        html = str(soup)
        offer, why = parse_catalog_buy_box(html, url, 'Apple iPhone 11', specs)
        self.assertIsNotNone(offer, why)
        self.assertTrue(catalog_offer_matches(offer, 'Apple iPhone 11', specs))
        matcher = TRPriceScraper()
        for title in ['Apple iPhone 11 64 GB Beyaz', 'Apple iPhone 11 64 GB RED Special Edition',
                      'Apple iPhone 11 64 GB ROM Beyaz']:
            self.assertTrue(matcher.has_observed_offer_evidence('Apple iPhone 11', dict(offer, source_title=title), specs), title)
        for title in ['Apple iPhone 11 8GB 64GB', 'Apple iPhone 11 64 GB RAM',
                      'Apple iPhone 11 64/128 GB', 'Apple iPhone 11 64GB 64GB',
                      'Apple iPhone 11 64GB Sanal RAM', 'Apple iPhone 11 128GB Beyaz']:
            self.assertFalse(matcher.has_observed_offer_evidence('Apple iPhone 11', dict(offer, source_title=title), specs), title)
        for patch in [{'storage_gb': 128}, {'ram_gb': 6}, {'network_generation': '5g'}]:
            self.assertIsNone(parse_catalog_buy_box(html, url, 'Apple iPhone 11', {**specs, **patch})[0])

    def test_legacy_hb_product_and_hbv_sku_keep_the_same_full_selected_offer_contract(self):
        # Public iPhone 11 category/PDP observed Nov 4, including its separately
        # selected accessory-free box. No legacy identifier is rewritten.
        old_product, old_sku = 'HBC0000TEST00', 'HBCV0000TEST01'
        product, sku = 'HB00000NSBZ4', 'HBV0000120X65'
        target = TARGET.replace(old_product, product)
        html = document().replace(old_product, product).replace(old_sku, sku)
        offer, why = parse_catalog_buy_box(html, target, NAME, SPECS)
        self.assertIsNotNone(offer, why)
        self.assertEqual((offer['product_id'], offer['sku']), (product, sku))
        soup = BeautifulSoup(html, 'html.parser')
        import json
        script = soup.select_one('script#reduxStore')
        data = json.loads(script.text)
        data['productState']['product']['variants'][0]['properties'].append(
            {'name': 'Seçenek', 'valueObject': {'actualValue': 'Aksesuarsız Kutu'}})
        script.string = json.dumps(data)
        control = '<button data-test-id="variant-box-Seçenek-Aksesuarsız Kutu" aria-label="Seçili Seçenek Aksesuarsız Kutu">Aksesuarsız Kutu</button>'
        selected_html = str(soup) + control
        self.assertIsNotNone(parse_catalog_buy_box(selected_html, target, NAME, SPECS)[0])
        for changed in [str(soup), selected_html.replace('Aksesuarsız Kutu', 'Aksesuarlı Paket'),
                        selected_html.replace('Seçili Seçenek', 'Seçenek'),
                        selected_html.replace(product, 'INVALID00000'),
                        selected_html.replace(sku, 'INVALID00000')]:
            self.assertIsNone(parse_catalog_buy_box(changed, target, NAME, SPECS)[0])

    def test_closed_catalog_flag_cannot_hide_an_independently_live_selected_merchant(self):
        # Observed on HB's public Galaxy S23 8/128 AKSOYLU PDP on 2026-11-03:
        # closed=true, live=true, product+warehouse+selected-SKU stock=true,
        # InStock/New schema, cash/selection events and enabled checkout.
        base = {('redux', 'productState', 'product', 'isClosedProduct'): True}
        offer, _ = parse_catalog_buy_box(document(base), TARGET, NAME, SPECS)
        self.assertIsNotNone(offer)
        for path, value in (
            (('redux', 'productState', 'product', 'isProductLive'), False),
            (('redux', 'productState', 'product', 'isProductLive'), None),
            (('redux', 'productState', 'product', 'isInStock'), False),
            (('redux', 'productState', 'product', 'stockInformation'), None),
            (('redux', 'productState', 'product', 'stockInformation', 'isInStock'), False),
            (('redux', 'productState', 'product', 'variants', 0, 'isInStock'), False),
            (('cart_disabled',), True),
            (('schema', '@graph', 0, 'offers', 'availability'), 'https://schema.org/OutOfStock'),
            (('visible_seller',), 'Other seller'),
        ):
            with self.subTest(path=path, value=value):
                self.assertIsNone(parse_catalog_buy_box(document({**base, path: value}), TARGET, NAME, SPECS)[0])

    def test_live_new_table_and_multiple_barcodes_keep_exact_sku_without_inventing_one_gtin(self):
        html = Path(__file__).with_name('fixtures').joinpath('hepsiburada-selected-multiple-barcodes.html').read_text()
        url = 'https://www.hepsiburada.com/apple-iphone-13-128-gb-siyah-p-HBCV00000ODHHF'
        specs = {'ram_gb': 4, 'storage_gb': 128, 'network_generation': '5g'}
        offer, reason = parse_catalog_buy_box(html, url, 'Apple iPhone 13', specs)
        self.assertEqual(reason, 'selected_new_cash_offer_observed')
        self.assertEqual(offer['merchant'], 'Mobilpasaj')
        self.assertEqual(offer['observed_gtins'], ['0194252707258', '0194252707289'])
        self.assertIsNone(offer['gtin13'])
        self.assertIsNone(offer['network_generation'])  # Not stated by this table.
        self.assertTrue(catalog_offer_matches(offer, 'Apple iPhone 13', specs))
        for before, after in (
            ('128 GB', '256 GB'), ('4 GB RAM', '6 GB RAM'),
            ('Ürün özellikleri', 'Other table'),
            ('20ccd78c-21b9-4c68-9943-6ff05e78c18a', '20ccd78c-21b9-4c68-9943-6ff05e78c18b'),
            ('0194252707289', '0194252707288'),
            ('data-test-id="addToCart"', 'disabled data-test-id="addToCart"'),
        ):
            with self.subTest(before=before):
                self.assertIn(before, html)
                # Changing the listing in every layer is not a contradiction;
                # change its table event alone in that case.
                changed = html.replace(before, after, 1) if before.startswith('20cc') else html.replace(before, after)
                self.assertIsNone(parse_catalog_buy_box(changed, url, 'Apple iPhone 13', specs)[0])

    def test_missing_radio_never_creates_radio_evidence_or_selects_a_dual_radio_family(self):
        soup = BeautifulSoup(document(), 'html.parser')
        table = soup.select_one('#techSpecs')
        table.string = table.get_text(' ', strip=True).replace('Mobil Bağlantı Hızı 5G', '')
        html = str(soup)
        # The A36 heading explicitly says 5G and all physical SKU proof remains.
        offer, _ = parse_catalog_buy_box(html, TARGET, NAME, SPECS)
        self.assertIsNotNone(offer)
        self.assertIsNone(offer['network_generation'])
        ambiguous = html.replace('A36 5G', 'A15').replace('a36-5g', 'a15')
        url = TARGET.replace('a36-5g', 'a15')
        self.assertIsNone(parse_catalog_buy_box(ambiguous, url, 'Samsung Galaxy A15', SPECS)[0])

    def test_used_technical_condition_cannot_be_overruled_by_new_schema_condition(self):
        soup = BeautifulSoup(document(), 'html.parser')
        table = soup.select_one('#techSpecs')
        table.string = table.get_text(' ', strip=True) + ' Kondisyon C Grade - İyi'
        html = str(soup)
        self.assertEqual(parse_catalog_buy_box(html, TARGET, NAME, SPECS)[1], 'selected_used_condition')

    def test_automatically_bound_android_variant_preserves_actual_seller_to_preparation(self):
        class Transport:
            def read_document(self, url):
                self.url = url
                return document(), 'document_read', 200
        transport = Transport()
        source = HepsiburadaCatalogSource(transport=transport)
        reader = RetailerOfferRefresher(hepsiburada_catalog_source=source)
        targets = [{'merchant_name': 'Hepsiburada', 'source_url': TARGET}]
        offers = reader.refresh_existing_offer_targets(NAME, targets, SPECS)
        self.assertEqual(transport.url, TARGET)
        self.assertEqual(len(offers), 1)
        offer = offers[0]
        self.assertTrue(catalog_offer_matches(offer, NAME, SPECS))
        self.assertNotIn(offer['merchant'], ('Apple', 'Samsung', 'Hepsiburada'))
        product = {'id': 999, 'slug': 'samsung-galaxy-a36-5g', 'name': NAME,
                   'offer_targets': targets, 'offer_targets_count': 1,
                   'offer_targets_truncated': False}
        updater = PriceUpdater(ingestion_client=object(), offer_refresher=reader)
        self.assertTrue(updater.live_offer_is_valid(product, offer, SPECS))
        self.assertEqual(len(updater.prepare_product_offers(product, offers,
                         updater.utc_timestamp(), updater.utc_timestamp())), 1)
        # An actual seller already bound to a different URL cannot be overwritten.
        product['offer_targets'].append({'merchant_name': offer['merchant'],
                                        'source_url': TARGET + '?magaza=other'})
        product['offer_targets_count'] = 2
        self.assertFalse(updater.live_offer_is_valid(product, offer, SPECS))
        self.assertFalse(updater.live_offer_is_valid({'name': NAME}, offer, SPECS))

    def test_independent_selected_evidence_rejects_drift(self):
        self.assertIsNotNone(parse_catalog_buy_box(document(), TARGET, NAME, SPECS)[0])
        cases = [
            {('redux', 'productState', 'product', 'merchantId'): 'wrong'},
            {('redux', 'productState', 'product', 'listingId'): 'wrong'},
            {('redux', 'productState', 'product', 'stockInformation', 'isInStock'): False},
            {('redux', 'productState', 'product', 'variants'): {}},
            {('redux', 'productState', 'product', 'variants', 0, 'barcodes'): ['8806094821438']},
            {('redux', 'productState', 'product', 'variants', 0, 'isInStock'): False},
            {('redux', 'productState', 'product', 'variants', 0, 'properties', 1,
              'valueObject', 'actualValue'): '512 GB'},
            {('visible_seller',): 'Other seller'}, {('visible_ram',): '12 GB RAM'},
            {('selected_capacity',): '512 GB'}, {('cart_disabled',): True},
            {('visible_price',): '99.999,00 TL'},
            {('schema', '@graph', 0, 'offers', 'price'): '99999'},
            {('schema', '@graph', 0, 'offers', 'url'): 'https://attacker.example/p'},
            {('events', 0, 'data', 'merchant_name'): 'Samsung'},
            {('events', 1, 'data', 'price'): '99999'},
            {('events', 1, 'data', 'listing_id'): 'wrong'},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertIsNone(parse_catalog_buy_box(document(changes), TARGET, NAME, SPECS)[0])
        for url in (TARGET + '?magaza=Samsung', TARGET + '#selected',
                    TARGET.replace('https:', 'http:'), TARGET.replace('TEST00', 'TEST02')):
            self.assertIsNone(parse_catalog_buy_box(document(), url, NAME, SPECS)[0])
        self.assertIsNone(parse_catalog_buy_box(document(), TARGET, NAME, {**SPECS, 'ram_gb': 12})[0])
        self.assertIsNone(parse_catalog_buy_box(document(), TARGET, NAME, {**SPECS, 'storage_gb': 256})[0])
        self.assertIsNone(parse_catalog_buy_box(document(), TARGET, NAME, {})[0])

    def test_unknown_and_ambiguous_catalog_hints_do_not_fetch(self):
        class NoTransport:
            def read_document(self, url):
                raise AssertionError('unbound URL must not be requested')
        source = HepsiburadaCatalogSource(transport=NoTransport())
        self.assertEqual(source.read_offer(TARGET)[1], 'unmatched_catalog_pdp')
        source.register_catalog_targets('Samsung Galaxy S26', [{'source_url': TARGET}], SPECS)
        self.assertEqual(source.read_offer(TARGET)[1], 'unmatched_catalog_pdp')
        source.register_catalog_targets(NAME, [{'source_url': TARGET}], SPECS)
        source.register_catalog_targets('Samsung Galaxy A36', [{'source_url': TARGET}], SPECS)
        self.assertEqual(source.read_offer(TARGET)[1], 'unmatched_catalog_pdp')

    def test_stale_seller_hint_fetches_sku_default_and_never_labels_it_as_historical_seller(self):
        calls = []
        class Transport:
            def read_document(self, url):
                calls.append(url)
                return document(), 'document_read', 200
        source = HepsiburadaCatalogSource(transport=Transport())
        hint = TARGET + '?magaza=Historical+Seller'
        source.register_catalog_targets(NAME, [{'source_url': hint}], SPECS)
        offer, reason, status = source.read_offer(hint)
        self.assertEqual(calls, [hint, TARGET])
        self.assertEqual(offer['url'], TARGET)
        self.assertNotEqual(offer['merchant'], 'Historical Seller')
        self.assertEqual(reason, 'selected_new_cash_offer_observed')
        self.assertEqual(status, 200)
        # Direct seller-scoped parsing still rejects a changed selected seller.
        self.assertIsNone(parse_catalog_buy_box(document(), hint, NAME, SPECS)[0])

    def test_explicit_live_seller_selection_keeps_its_query_and_never_falls_back_after_denial(self):
        import json
        from urllib.parse import urlencode
        from hepsiburada_catalog_source import catalog_request_url
        selected = json.loads(BeautifulSoup(document(), 'html.parser').select_one('#reduxStore').text)['productState']['product']['merchant']
        target = TARGET + '?' + urlencode({'magaza': selected['name']})
        calls = []
        class Transport:
            denied = False
            def read_document(self, url):
                calls.append(url)
                return (None, 'robots_disallowed', None) if self.denied else (document(), 'document_read', 200)
        transport = Transport()
        source = HepsiburadaCatalogSource(transport=transport)
        source.register_catalog_targets(NAME, [{'source_url': target}], SPECS)
        quote, reason, _ = source.read_offer(target)
        self.assertIsNotNone(quote, reason)
        self.assertEqual(quote['url'], target)
        self.assertEqual(quote['merchant'], selected['name'])
        self.assertEqual(calls, [target])
        self.assertEqual(catalog_request_url(target.replace('+', '%20') + '&'), target)
        calls.clear()
        transport.denied = True
        self.assertIsNone(source.read_offer(target)[0])
        self.assertEqual(calls, [target])

    def test_another_selected_seller_requires_its_own_fresh_matching_document(self):
        import json
        from urllib.parse import urlencode
        original = document()
        selected = json.loads(BeautifulSoup(original, 'html.parser').select_one('#reduxStore').text)['productState']['product']
        other_name = 'Other Public Shop'
        other_doc = original.replace(selected['merchant']['name'], other_name)
        for old, new in [(selected['merchant']['id'], '11111111-2222-3333-4444-555555555555'),
                         (selected['listingId'], '66666666-7777-8888-9999-000000000000'),
                         (selected['merchant']['urlPostfix'], 'other-public-shop')]:
            other_doc = other_doc.replace(old, new)
        target = TARGET + '?' + urlencode({'magaza': other_name})
        class Transport:
            def __init__(self):
                self.calls = []
            def read_document(self, url):
                self.calls.append(url)
                return (other_doc if url == target else original), 'document_read', 200
        transport = Transport()
        source = HepsiburadaCatalogSource(transport=transport)
        source.register_catalog_targets(NAME, [{'source_url': target}], SPECS)
        quote, reason, _ = source.read_offer(target)
        self.assertIsNotNone(quote, reason)
        self.assertEqual((quote['merchant'], quote['merchant_id'], quote['url']),
                         (other_name, '11111111-2222-3333-4444-555555555555', target))
        self.assertEqual(transport.calls, [target])
        self.assertIsNone(parse_catalog_buy_box(original, target, NAME, SPECS)[0])

    def test_generic_selected_cash_does_not_depend_on_redundant_apple_summary_flags(self):
        changes = {('redux', 'productState', 'product', key): None for key in (
            'merchantName', 'merchantId', 'isInStock', 'stockInformation',
            'isProductLive', 'isClosedProduct', 'isPreOrder', 'prices')}
        self.assertIsNotNone(parse_catalog_buy_box(document(changes), TARGET, NAME, SPECS)[0])
        for path, value in (
            (('redux', 'productState', 'product', 'isInStock'), False),
            (('redux', 'productState', 'product', 'isClosedProduct'), True),
            (('redux', 'productState', 'product', 'isProductLive'), False),
            (('redux', 'productState', 'product', 'isPreOrder'), True),
            (('redux', 'productState', 'product', 'variants', 0, 'isInStock'), False),
            (('redux', 'productState', 'product', 'variants', 0, 'price'), 99999),
            (('events', 0, 'data', 'price'), '99999'),
            (('schema', '@graph', 0, 'offers', 'price'), '99999'),
            (('visible_price',), '99.999,00 TL'),
            (('cart_disabled',), True),
        ):
            with self.subTest(path=path):
                self.assertIsNone(parse_catalog_buy_box(document({**changes, path: value}), TARGET, NAME, SPECS)[0])


if __name__ == '__main__':
    unittest.main()
