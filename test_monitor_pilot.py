import json
from unittest import TestCase
from unittest.mock import patch

from monitor_pilot import independent_pair, materialize, read_targets
from monitor_sources import (ENSONU_CATEGORY, ENSONU_HOST, RAMPAGE_HOST, monitor_facts, selected_asempi,
                             selected_direct, selected_ensonu, selected_overgame, selected_pckolik, selected_rampage)


class MonitorPilotTests(TestCase):
    def setUp(self):
        self.facts = monitor_facts('Samsung LS32FM700UUXUF 32" IPS 4K 60Hz Monitör',
                                   'Samsung', 'ls32fm700uuxuf')
        self.first = {
            'code': 'ls32fm700uuxuf', 'gtin13': '8806097322344', 'facts': self.facts,
            'platform': 'n11', 'merchant': 'Satıcı A', '_legal_name': 'satici a limited sirketi',
            '_tax_key': 'same-company', 'price': 10000, '_image': 'https://example.com/monitor.png',
            'observed_at': '2026-11-08T21:00:00+00:00', 'url': 'https://www.n11.com/monitor',
        }
        self.second = {
            **self.first, 'platform': 'Trendyol', 'merchant': 'Satıcı B', '_legal_name': 'satici b',
            '_tax_key': 'other-company',
        }

    def test_pair_requires_a_distinct_platform_and_company(self):
        self.assertTrue(independent_pair(self.first, self.second))
        for patch_fields in ({'platform': 'n11'}, {'_tax_key': 'same-company'},
                             {'_legal_name': 'satici a anonim sirketi'}):
            self.assertFalse(independent_pair(self.first, {**self.second, **patch_fields}))

    def test_pair_rejects_conflicting_variant_facts_and_barcode(self):
        self.assertFalse(independent_pair(self.first, {**self.second, 'gtin13': '8806097931393'}))
        self.assertFalse(independent_pair(self.first, {**self.second, 'facts': {**self.facts, 'refresh': 120}}))

    def test_verified_monitor_category_can_supply_a_truncated_title(self):
        title = 'ASUS VG249QM5A 23.8 inç 240Hz Fast IPS Gaming Monitö'
        self.assertIsNone(monitor_facts(title, 'ASUS', 'vg249qm5a'))
        facts = monitor_facts(title, 'ASUS', 'vg249qm5a', category_verified=True)
        self.assertEqual(facts['refresh'], 240)
        self.assertIsNone(monitor_facts(title + ' askı aparatı', 'ASUS', 'vg249qm5a', category_verified=True))

    def test_rampage_binds_price_and_barcode_to_cart_variant(self):
        url = f'https://{RAMPAGE_HOST}/en/products/crimson-cr25r180'
        title = "25 (24.5'') 200Hz Fast IPS Gaming Monitor"
        barcode = '8680096133428'
        product = {'@type': 'Product', 'gtin': barcode, 'name': f'Crimson CR25R180 {title} - black',
                   'offers': {'@type': 'Offer', 'url': f'{url}?variant=420', 'priceCurrency': 'TRY',
                              'price': '5599.00', 'availability': 'https://schema.org/InStock'}}
        group = {'@type': 'ProductGroup', 'url': url, 'brand': {'name': 'Rampage'}, 'hasVariant': [product]}
        html = f'''<link rel="canonical" href="{url}">
          <script type="application/ld+json">{json.dumps(group)}</script>
          <main><product-info class="product__info"><div class="product__title">
            <h1>{title}</h1><div class="product__price">5,599.00TL</div>
          </div><form id="selected" class="product-form" method="post" action="/en/cart/add">
            <input name="id" value="420"><button type="submit" name="add" form="selected">Sepete Ekle</button>
          </form></product-info></main>'''
        offer, _ = selected_rampage(html, url, 'cr25r180')
        self.assertEqual((offer['gtin13'], offer['price'], offer['facts']['size']), (barcode, 5599, 24.5))
        for broken in (html.replace('value="420"', 'value="421"'), html.replace('5,599.00TL', '5,499.00TL'),
                       html.replace('form="selected"', 'form="another"'), html.replace('InStock', 'OutOfStock')):
            self.assertIsNone(selected_rampage(broken, url, 'cr25r180')[0])

    def test_ensonu_checks_visible_barcode_and_unconditional_single_payment(self):
        url = f'https://{ENSONU_HOST}/urun/crimson-cr27r200/'
        title = 'Rampage Crimson CR27R200 27" 200Hz IPS FHD Monitör'
        barcode = '8680096144554'
        schema = {'@type': 'Product', 'name': title, 'url': url, 'gtin': barcode,
                  'offers': [{'@type': 'Offer', 'url': url, 'availability': 'https://schema.org/InStock',
                              'seller': {'name': 'EnSonu', 'url': f'https://{ENSONU_HOST}'},
                              'priceSpecification': [{'@type': 'UnitPriceSpecification', 'priceCurrency': 'TRY',
                                                      'price': '8262.97', 'valueAddedTaxIncluded': True}]}]}
        html = f'''<link rel="canonical" href="{url}">
          <script type="application/ld+json">{json.dumps(schema)}</script>
          <main><div class="product-info summary">
            <div class="woocommerce-breadcrumb"><a href="{ENSONU_CATEGORY}">Monitör</a></div>
            <h1 class="product_title">{title}</h1><p class="product-page-price">₺ 8,262.97</p>
            <table><tr><td>Banka ve Kredi Kartı ile Ödeme (Tek Çekim)</td><td>₺ 8,262.97</td></tr></table>
            <p class="stock in-stock">Stokta</p><form class="cart" action="{url}" method="post">
              <button class="single_add_to_cart_button" name="add-to-cart" value="64108">Sepete Ekle</button>
            </form>
          </div><div class="product-page-sections"><div class="product-section">
            <div class="panel entry-content">EAN: {barcode} · 24 ay garanti</div>
          </div></div></main>'''
        offer, _ = selected_ensonu(html, url, 'cr27r200')
        self.assertEqual((offer['gtin13'], offer['price']), (barcode, 8262.97))
        for broken in (html.replace(f'EAN: {barcode}', 'EAN: 8680096133428'),
                       html.replace('<td>₺ 8,262.97', '<td>₺ 8,001.31'), html.replace('>Stokta<', '>Stokta Yok<'),
                       html.replace('value="64108"', 'value="64108" disabled')):
            self.assertIsNone(selected_ensonu(broken, url, 'cr27r200')[0])

    def test_asempi_requires_selected_barcode_visible_cash_and_stock(self):
        url = 'https://www.asempi.com/urun.php?id=16268'
        title = 'ASUS VG27AQML5F 27" Fast IPS 2560x1440 320 Hz Monitör'
        gtin = '4711636437509'
        schema = {'@type': 'Product', 'name': title, 'brand': {'name': 'ASUS'}, 'sku': 'ASM-16268', 'gtin13': gtin,
                  'offers': {'@type': 'Offer', 'url': url, 'priceCurrency': 'TRY', 'price': '21808.99',
                             'availability': 'https://schema.org/InStock'}}
        category = {'@type': 'BreadcrumbList', 'itemListElement': [{'item': 'https://www.asempi.com/kategori.php?id=1810'}]}
        html = f'''<link rel="canonical" href="{url}">
          <script type="application/ld+json">{json.dumps(schema)}</script>
          <script type="application/ld+json">{json.dumps(category)}</script>
          <div class="urun-detay"><h1 class="urun-baslik">{title}</h1>
            <div class="urun-meta-satir"><span>Barkod: <span class="mono">{gtin}</span></span><span>24 ay garanti</span></div>
            <div class="urun-fiyat-kutu"><span class="urun-fiyat-buyuk">21.808,99 ₺</span>
              <div class="taksit-satir"><span>Tek Çekim</span><span class="mono">21.808,99 ₺</span></div>
              <div class="urun-stok-satir stok-var">5 adet stokta — Son 5 adet!</div>
            </div><form id="sepeteEkleForm" method="post"><input name="adet" min="1" max="5" value="1">
              <button class="sepete-ekle-btn" type="submit">Sepete Ekle</button></form>
          </div>'''
        offer, _ = selected_asempi(html, url, 'vg27aqml5f')
        self.assertEqual((offer['gtin13'], offer['price']), (gtin, 21808.99))
        for changed in (html.replace('max="5"', 'max="6"'), html.replace('21.808,99', '20.808,99'),
                        html.replace(f'>{gtin}<', '>8806097322344<')):
            self.assertIsNone(selected_asempi(changed, url, 'vg27aqml5f')[0])

    def test_overgame_checks_selected_sale_price_and_crossed_out_regular_price(self):
        slug = 'msi-mag-255xf-300hz-fhd-ips-monitor'
        url = f'https://www.overgameweb.com/{slug}'
        title = 'MSI MAG 255XF 24.5" IPS FHD 300Hz Monitör'
        barcode = '4711377251204'
        product = {'@type': 'Product', 'name': title, 'sku': '123', 'productID': '42', 'gtin13': barcode,
                   'brand': {'name': 'MSI'}, 'offers': {'@type': 'Offer', 'url': url, 'price': '8500.60',
                   'priceCurrency': 'TRY', 'availability': 'https://schema.org/InStock',
                   'itemCondition': 'https://schema.org/NewCondition'}}
        state = {'id': '42', 'name': title, 'code': '123', 'subproduct_id': 0, 'url': slug, 'barcode': barcode,
                 'available': True, 'quantity': 10, 'currency_target': 'TL', 'total_price': 10000,
                 'total_base_price': 10000, 'total_sale_price': 8500.60}

        def document(**changes):
            data = json.dumps(json.dumps({**state, **changes}))[1:-1]
            return f'''<link rel="canonical" href="{url}"><h1>{title}</h1>
              <script type="application/ld+json">{json.dumps(product)}</script>
              <script>PRODUCT_DATA.push(JSON.parse('{data}'));</script>
              <div class="mainPrices"><span class="discountedPrice">10.000 TL</span>
              <span class="discountPrice">8.501 TL</span></div>
              <div id="mobileBuyBtn"><a id="addCartBtn" class="addCartBtn" onclick="Add2Cart(42, 0, 1)">Sepete Ekle</a></div>'''

        offer, _ = selected_overgame(document(), url, '255xf')
        self.assertEqual((offer['gtin13'], offer['price']), (barcode, 8500.60))
        for changes in ({'total_sale_price': 8501}, {'total_price': 11000}, {'barcode': '8806097322344'}, {'quantity': 0}):
            self.assertIsNone(selected_overgame(document(**changes), url, '255xf')[0])

    def test_sinerji_uses_selected_cart_gtin_and_technical_table(self):
        url = 'https://www.sinerji.gen.tr/msi-mag401qr-monitor-p-58167'
        title = 'MSI MAG401QR 40" 155Hz IPS 4K Monitör'
        gtin = '4711377080514'
        schema = {'@type': 'Product', 'name': title, 'brand': {'name': 'MSI'}, 'sku': '123', 'mpn': '123',
                  'gtin': gtin, 'offers': {'@type': 'Offer', 'url': url, 'priceCurrency': 'TRY', 'price': '12999.49',
                    'availability': 'http://schema.org/InStock', 'itemCondition': 'http://schema.org/NewCondition'}}
        html = f'''<html><head><link rel="canonical" href="{url}">
            <script type="application/ld+json">{json.dumps(schema)};</script></head>
            <body id="P58167"><div class="productDetail"><h1>{title}</h1>
            <div class="priceWrapper"><span class="price"><small>₺</small> <span>12.999,49</span></span></div>
            <button class="addToCart" type="submit" value="58167">SEPETE EKLE</button>
            <div id="nav-technical-specs"><table><tr><td>Çözünürlük</td><td>3440 x 1440 (4K)</td></tr>
            <tr><td>Barkod GTIN</td><td>{gtin}</td></tr></table></div></div></body></html>'''
        offer, _ = selected_direct(html, url, 'mag401qr')
        self.assertEqual(offer['facts']['resolution'], '3440 × 1440')
        self.assertEqual(offer['gtin13'], gtin)
        self.assertEqual(offer['price'], 12999.49)
        for broken in (html.replace(f'<td>{gtin}', '<td>4711377151849'),
                       html.replace('value="58167"', 'value="58166"'), html.replace('12.999,49', '12.999,50')):
            self.assertIsNone(selected_direct(broken, url, 'mag401qr')[0])

    def test_pckolik_requires_the_visible_stock_code_and_unconditional_price(self):
        url = 'https://pckolik.com.tr/urun/asus-proart-pa279crv'
        title, stock_code, gtin = 'ASUS PA279CRV 27" 60Hz IPS', '90LM08E0-B01K70', '4711081951506'
        nodes = [
            {'@type': 'BreadcrumbList', 'itemListElement': [{'item': 'https://pckolik.com.tr/monitorler'}]},
            {'@type': 'Product', 'name': title, 'brand': {'name': 'ASUS'}, 'sku': stock_code, 'mpn': stock_code,
             'gtin13': gtin, 'offers': {'@type': 'Offer', 'url': url, 'priceCurrency': 'TRY', 'price': '17999',
                  'availability': 'https://schema.org/InStock', 'itemCondition': 'https://schema.org/NewCondition'}},
        ]
        html = f'''<link rel="canonical" href="{url}"><script type="application/ld+json">{json.dumps(nodes)}</script>
            <main><h1>{title}</h1><table><tr><td>Barkod</td><td>{gtin}</td></tr>
            <tr><td>Stok kodu</td><td>{stock_code}</td></tr></table>
            <div class="lg:col-start-3 lg:row-start-1"><span class="price-now">17.999 TL</span>
            <button>Sepete ekle</button></div><section><p>Tek çekim: <span>17.999 TL</span></p></section></main>'''
        offer, _ = selected_pckolik(html, url, 'pa279crv')
        self.assertEqual((offer['gtin13'], offer['price']), (gtin, 17999))
        for broken in (html.replace(f'<td>{gtin}', '<td>4711081273981'),
                       html.replace(f'<td>{stock_code}', '<td>wrong-stock'),
                       html.replace('Tek çekim:', 'Üyelere özel:'), html.replace('<button>', '<button disabled>')):
            self.assertIsNone(selected_pckolik(broken, url, 'pa279crv')[0])

    def test_only_exact_manufacturer_model_may_use_the_declared_equivalence(self):
        first = {**self.first, 'code': '273qp', 'gtin13': '4711377281690', 'facts': {**self.facts, 'brand': 'MSI', 'panel': 'OLED'}}
        second = {**self.second, 'code': '273qp', 'gtin13': '4711377281690', 'facts': {**self.facts, 'brand': 'MSI', 'panel': 'QD-OLED'}}
        self.assertTrue(independent_pair(first, second))
        self.assertFalse(independent_pair({**first, 'code': 'other'}, {**second, 'code': 'other'}))

    def test_redirected_product_is_skipped_but_denial_stops_source(self):
        host = 'www.mediamarkt.com.tr'
        rows = [{'url': f'https://{host}/tr/product/_monitor-{sku}.html',
                 'gtin': self.first['gtin13'], 'code': self.first['code']} for sku in (123456, 123457)]
        for reason, expected_reads in [('redirect', 2), ('http_403', 1)]:
            with self.subTest(reason=reason), patch('monitor_pilot.Reader') as factory:
                reader = factory.return_value
                reader.stop = False

                def unavailable(url):
                    reader.last_reason = reason
                    reader.stop = True
                    return None, None

                reader.read.side_effect = unavailable
                offers, rejected = read_targets(host, rows)
                self.assertEqual(offers, [])
                self.assertEqual(rejected, {reason: expected_reads})
                self.assertEqual(reader.read.call_count, expected_reads)

    def test_transport_failure_retries_only_the_unfinished_rows_once(self):
        host = 'www.mediamarkt.com.tr'
        rows = [{'url': f'https://{host}/tr/product/_monitor-{sku}.html',
                 'gtin': self.first['gtin13'], 'code': self.first['code']} for sku in (123456, 123457)]
        with patch('monitor_pilot.Reader') as factory, patch('monitor_pilot.sleep') as wait:
            reader = factory.return_value

            def fail(url):
                reader.last_reason = 'transport_error'
                reader.stop = True
                return None, None

            reader.read.side_effect = fail
            factory.side_effect = lambda *args, **kwargs: self.reset_reader(reader)
            _, rejected = read_targets(host, rows)
            self.assertEqual([call.args[0] for call in reader.read.call_args_list], [rows[0]['url']] * 2)
            self.assertEqual(rejected, {'transport_error': 2})
            wait.assert_called_once_with(8)

    @staticmethod
    def reset_reader(reader):
        reader.stop = False
        return reader

    def test_manufacturer_spec_has_precedence_for_the_exact_gtin(self):
        with patch('monitor_pilot._image', return_value=b'photo'):
            model, _ = materialize((self.first,), [self.first])
        panel = next(spec for spec in model['specs'] if spec['label'] == 'Panel tipi')
        self.assertEqual(model['panel'], 'VA')
        self.assertEqual(panel['source']['name'], 'Samsung')
        self.assertEqual(model['offers'][0]['price'], self.first['price'])
