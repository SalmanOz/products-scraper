import json
from unittest import TestCase
from unittest.mock import Mock, patch

from tws_catalog_expansion import reread
from tws_offer_pilot import model_name
from tws_refresh import merge_snapshot, refresh
from tws_retailer_sources import selected_vatan_store_tws


class TwsRefreshTest(TestCase):
    def test_ozgur_refresh_checks_the_live_barcode_and_seller(self):
        host = 'ozguriletisimankara.com'
        url = f'https://{host}/urun/xiaomi-buds-5-kablosuz-kulaklik-gri'
        name, gtin = 'Xiaomi Buds 5', '6941812768518'
        photo = f'https://{host}/uploads/products/buds-5-gri.jpg'
        title = f'{name} Kablosuz Kulaklık Gri'
        merchant = 'Özgür İletişim'
        product = {'@type': 'Product', 'url': url, 'name': title, 'gtin13': gtin, 'image': [photo],
                   'brand': {'name': 'Xiaomi'}, 'mpn': 'BUDS5-GRI',
                   'offers': {'@type': 'Offer', 'url': url, 'price': '5499.00', 'priceCurrency': 'TRY',
                              'seller': {'name': merchant}, 'availability': 'https://schema.org/InStock',
                              'itemCondition': 'https://schema.org/NewCondition'}}
        html = f'''
          <link rel="canonical" href="{url}">
          <script type="application/ld+json">{json.dumps(product)}</script>
          <main class="oi-product-detail" data-product-id="81" data-oi-view-variant="813">
            <img id="oi-product-main-img" src="{photo}">
            <section class="oi-pd-buybox">
              <h1 class="oi-pd-title">{title}</h1><div class="oi-pd-model"><span>BUDS5-GRI</span></div>
              <div class="oi-pd-stockline is-in">Stokta</div><span data-oi-price>5.499,00 TL</span>
              <button data-oi-add-cart data-id="81">Sepete Ekle</button>
            </section>
          </main>
        '''
        terms = '<p>Satıcı: Özgür İletişim Limited Şirketi Vergi dairesi: Ankara</p>'
        target = {'url': url, 'merchant': merchant, 'gtin13': gtin}
        single_sku = html.replace('data-oi-view-variant="813"', 'data-oi-view-variant="0"').replace('</main>',
            f'<div id="oi-pd-features"><table class="oi-pd-feature-table"><tr>'
            f'<th>Barkod</th><td>{gtin}</td></tr></table></div></main>')
        for page, expected in (
            (html, 'verified'),
            (html.replace(gtin, '6941812772256'), 'unverified'),
            (html.replace('InStock', 'OutOfStock'), 'unverified'),
            (single_sku, 'verified'),
            (single_sku.replace('<th>Barkod</th>', '<th>Renk</th>'), 'unverified'),
            (single_sku.replace('<td>' + gtin, '<td>6941812772256'), 'unverified'),
            (single_sku.replace('</section>', '<button data-variant-id="123">Renk</button></section>'), 'unverified'),
        ):
            with self.subTest(status=expected), patch('tws_catalog_expansion.PriorityRetailerTransport') as factory:
                transport = factory.return_value
                transport._read_document.side_effect = [(terms, 'document_read', 200), (page, 'document_read', 200)]
                statuses = {}
                offers, _ = reread(host, [(name, target)], statuses)
                self.assertEqual(statuses[url], expected)
                self.assertEqual(bool(offers), expected == 'verified')
                if offers:
                    self.assertEqual(offers[name][0]['_legal_name'], 'ozgur iletisim limited sirketi')
                    self.assertEqual(offers[name][0]['price'], 5499)
                self.assertEqual(transport._read_document.call_args_list[0].args[0],
                                 f'https://{host}/mesafeli-satis')

    def test_pazarama_uses_the_scoped_site_reader_in_mixed_refresh(self):
        name, gtin = 'Anker Soundcore AeroFit 2', '0194644298876'
        url = 'https://www.pazarama.com/anker-aerofit-2-siyah-p-0194644298876'
        snapshot = {'checked_at': 'old', 'models': [{'name': name, 'gtin': gtin, 'offers': []}],
                    'sourceTargets': {name: [{'url': url, 'merchant': 'Mağaza'}]}}
        catalog = {'models': [{'name': name}]}
        with patch('tws_refresh.catalog_document_reader') as gateway, \
             patch('tws_refresh.reread', return_value=({}, {})) as reader:
            refresh(snapshot, catalog, collection='mixed')
        gateway.assert_called_once_with('earbud')
        self.assertEqual(reader.call_args.args[0], 'www.pazarama.com')
        self.assertIs(reader.call_args.kwargs['document_reader'], gateway.return_value)

    def test_teknosa_uses_the_existing_unavailable_robots_policy_for_exact_earbud_pages(self):
        host = 'www.teknosa.com'
        url = f'https://{host}/anker-aerofit-2-siyah-p-100000063496'
        with patch('tws_catalog_expansion.PriorityRetailerTransport') as factory:
            transport = factory.return_value
            transport.read_fresh_document.return_value = None, 'http_403', 403
            transport.resolved_url.return_value = url
            offers, reasons = reread(host, [('Anker Soundcore AeroFit 2', {'url': url})], discover=True)
        self.assertEqual(factory.call_args.kwargs['unavailable_robots_hosts'], (host,))
        self.assertFalse(factory.call_args.kwargs['browser_fallback'])
        self.assertFalse(offers)
        self.assertEqual(reasons, {'http_403': 1})

    def test_partial_refresh_replaces_offers_when_an_independent_pair_is_live(self):
        first, second, third = ('https://shop.example/' + suffix for suffix in ('one', 'two', 'three'))
        gtin = '0194644298876'
        a = {'url': first, 'merchant': 'Satıcı A', 'platform': 'Trendyol', 'price': 5000, 'observed_at': 'old'}
        b = {'url': second, 'merchant': 'Satıcı B', 'platform': 'n11', 'price': 5100, 'observed_at': 'old'}
        c = {'url': third, 'merchant': 'Satıcı C', 'platform': 'Teknosa', 'price': 5200, 'observed_at': 'old'}
        snapshot = {'checked_at': 'old', 'models': [
            {'name': 'Covered', 'gtin': gtin, 'offers': [a, b]},
            {'name': 'Unavailable', 'gtin': gtin, 'offers': [a, b, c]},
            {'name': 'Single', 'gtin': gtin, 'offers': [a]},
        ]}
        catalog = {'models': [{'name': row['name']} for row in snapshot['models']]}
        current = [{**row, 'price': row['price'] - 100, 'observed_at': 'new', 'gtin13': gtin,
                    '_legal_name': row['merchant']} for row in (a, b)]
        statuses = {first: 'verified', second: 'verified', third: 'unavailable'}
        observations = {'Covered': current, 'Unavailable': current, 'Single': current[:1]}
        updated, counts = merge_snapshot(snapshot, catalog, observations, statuses, 'new', partial=True)
        self.assertEqual(counts, {'refreshed': 2, 'deferred': 1})
        self.assertEqual(updated['models'][2], snapshot['models'][2])
        for row in updated['models'][:2]:
            self.assertEqual([offer['url'] for offer in row['offers']], [first, second])
            self.assertEqual([offer['price'] for offer in row['offers']], [4900, 5000])

    def test_vatan_store_inventory_requires_exact_sku_price_and_sellable_stock(self):
        url = 'https://www.vatanbilgisayar.com/sony-wf1000xm6s-kulaklik-gumus.html'
        name = 'Sony WF-1000XM6'
        title = 'SONY WF-1000XM6S Tam Kablosuz Kulaklık - Gümüş'
        sku, mpn, gtin = '159657', 'WF1000XM6S.CE7', '4548736171824'
        product = {'@type': 'Product', 'sku': sku, 'mpn': mpn, 'name': title, 'brand': {'name': 'SONY'}}
        event = {'ProductNumericId': int(sku), 'ProductId': mpn, 'ProductName': title,
                 'ProductBrand': 'SONY', 'ProductPrice': '25499'}
        html = f'''
          <link rel="canonical" href="{url}">
          <script type="application/ld+json">{json.dumps(product)}</script>
          <main class="product-detail">
            <h1 class="product-list__product-name">{title}</h1>
            <div class="product-id" data-productid="{sku}" data-productcode="{mpn}"></div>
            <button class="stockBTN" data-productid="{sku}">Stoktaki Mağazalar</button>
            <div class="product-detail-price"><span class="product-detail-price-big">25.499 TL</span></div>
          </main>
          <div class="detailPartialPage"><input class="flixinpage" ean="{gtin}" brand="SONY"></div>
          <script>UpdateProductDetayItem({json.dumps(event)})</script>
        '''
        store = {'I_ID': int(sku), 'StokStat': 1, 'Stok': 3, 'Sp_Title': 'Ankara (Söğütözü)'}
        stock = Mock(return_value=(json.dumps(json.dumps([store])), 'ready', 200))
        offer, reason = selected_vatan_store_tws(html, url, name, model_name, stock, sku)
        stock.assert_called_once_with(
            'https://www.vatanbilgisayar.com/ProductDetail/ShopStockByProductQ?productId=159657')
        self.assertEqual(reason, 'verified')
        self.assertEqual(offer['gtin13'], gtin)
        self.assertEqual(offer['storePickup'], {'sku': sku, 'name': store['Sp_Title']})
        self.assertEqual(offer['price'], 25499)
        for changed, rejection in (
            ({'I_ID': 159656}, 'store_inventory_unavailable_invalid_rows'),
            ({'StokStat': 3}, 'store_inventory_out_of_stock'),
            ({'Stok': 0}, 'store_inventory_out_of_stock'),
        ):
            with self.subTest(changed=changed):
                stock.return_value = json.dumps([{**store, **changed}]), 'ready', 200
                self.assertEqual(
                    selected_vatan_store_tws(html, url, name, model_name, stock, sku), (None, rejection))
        stock.reset_mock()
        self.assertEqual(
            selected_vatan_store_tws(html.replace('25.499 TL', '25.499 TL sepette'), url, name, model_name, stock, sku),
            (None, 'store_price_unverified'))
        stock.assert_not_called()

    def test_store_target_survives_stock_loss_for_later_refresh(self):
        name, gtin = 'Sony WF-1000XM6', '4548736171824'
        url = 'https://www.vatanbilgisayar.com/sony-wf1000xm6s-kulaklik-gumus.html'
        offer = {'url': url, 'merchant': 'Vatan Bilgisayar', 'platform': 'Vatan Bilgisayar',
                 'price': 25499, 'observed_at': 'old', 'storePickup': {'sku': '159657', 'name': 'Ankara'}}
        snapshot = {'checked_at': 'old', 'models': [{'name': name, 'gtin': gtin, 'offers': [offer]}]}
        catalog = {'models': [{'name': name}]}
        def collect(host, rows, statuses, document_reader=None):
            self.assertEqual(rows[0][1]['storePickupSku'], '159657')
            statuses[url] = 'unverified'
            return {}, {'store_inventory_out_of_stock': 1}
        with patch('tws_refresh.reread', side_effect=collect):
            empty, _ = refresh(snapshot, catalog)
            repeated, _ = refresh(empty, catalog)
        self.assertEqual(empty['models'][0]['offers'], [])
        self.assertEqual(repeated['sourceTargets'][name][0]['storePickupSku'], '159657')

    def test_hepsiburada_new_marketplace_seller_requires_legal_profile(self):
        name = 'Sony WF-1000XM6'
        host = 'www.hepsiburada.com'
        url = f'https://{host}/sony-wf-1000xm6-p-HBCV0001234'
        transport = Mock()
        transport.read_fresh_document.return_value = 'page', 'document_read', 200
        transport.resolved_url.return_value = url
        offer = {'gtin13': '4548736171770', 'url': url, 'merchant': 'Mağaza', 'merchant_id': 'selected-id',
                 'seller_url': f'https://{host}/magaza/magaza', 'platform': 'Hepsiburada', 'currency': 'TRY',
                 'price': 25000, 'availability': 'in_stock', 'condition': 'new'}
        for verified in (False, True):
            with self.subTest(verified=verified), \
                 patch('tws_catalog_expansion.PriorityRetailerTransport', return_value=transport), \
                 patch('tws_catalog_expansion.selected_hepsiburada_tws', return_value=(dict(offer), 'observed')), \
                 patch('tws_catalog_expansion.verify_seller', return_value=verified) as identity, \
                 patch('tws_catalog_expansion.hepsiburada_seller_targets', return_value=[]):
                observations, failures = reread(host, [(name, {'url': url})], discover=True)
                identity.assert_called_once()
                self.assertEqual(observations[name], [offer] if verified else [])
                self.assertEqual(failures['seller_identity_unverified'], not verified)

    def test_discovery_rereads_an_alternate_seller_for_the_original_barcode(self):
        name, gtin = 'Sony WF-1000XM6', '4548736171770'
        host = 'www.trendyol.com'
        original = f'https://{host}/sony/sony-wf-1000xm6-123456'
        other = original + '?merchantId=2'
        transport = Mock()
        transport.read_fresh_document.return_value = 'page', 'document_read', 200
        transport.resolved_url.side_effect = lambda url: url
        base = {'gtin13': gtin, 'merchant': 'Mağaza', 'merchant_id': 1, 'currency': 'TRY',
                'price': 25000, 'availability': 'in_stock', 'condition': 'new', '_legal_name': 'magaza'}
        for replacement in ({'merchant_id': 1}, {'gtin13': '4548736171824'}, {}):
            with self.subTest(replacement=replacement):
                def selected(_, url, model, *, seller_hints=None):
                    self.assertEqual(model, name)
                    if url == original:
                        seller_hints.append((other, 2))
                        return {**base, 'url': original}, 'observed'
                    return {**base, 'url': other, 'merchant_id': 2, **replacement}, 'observed'

                with patch('tws_catalog_expansion.PriorityRetailerTransport', return_value=transport), \
                     patch('tws_catalog_expansion.selected_tws_offer', side_effect=selected):
                    offers, failures = reread(host, [(name, {'url': original})], discover=True)
                self.assertEqual([o['url'] for o in offers[name]], [original] if replacement else [original, other])
                self.assertEqual(failures['selected_seller_or_gtin_changed'], bool(replacement))

    def test_earbud_reader_resumes_a_target_deferred_by_source_cooldown(self):
        name, gtin = 'Huawei FreeBuds 6i', '6942103123535'
        host = 'www.n11.com'
        first = f'https://{host}/urun/huawei-freebuds-6i-123456'
        waiting = f'https://{host}/urun/huawei-freebuds-6i-123457'
        transport = Mock()
        transport.read_fresh_document.side_effect = [
            (None, 'http_503', 503), (None, 'source_cooldown', None), ('page', 'document_read', 200),
        ]
        transport.resolved_url.side_effect = lambda url: url
        transport.wait_for_cooldown.return_value = True
        offer = {'gtin13': gtin, 'merchant': 'Mağaza', 'currency': 'TRY', 'price': 3000.0,
                 'availability': 'in_stock', 'condition': 'new', 'url': waiting}
        targets = [(name, {'url': url, 'gtin13': gtin, 'merchant': 'Mağaza'}) for url in (first, waiting)]
        with patch('tws_catalog_expansion.PriorityRetailerTransport', return_value=transport), \
             patch('tws_catalog_expansion.selected_n11_tws', return_value=(offer, 'observed')):
            observations, failures = reread(host, targets)
        transport.wait_for_cooldown.assert_called_once_with(waiting)
        self.assertEqual(observations[name], [offer])
        self.assertEqual(failures, {'http_503': 1})

    def test_color_variants_keep_separate_gtins_and_refresh_targets(self):
        name = 'Huawei FreeBuds 6i'
        white = '6942103123535'
        black = '6942103123542'
        catalog = {'models': [{'name': name}]}
        urls = [f'https://www.n11.com/urun/uyumlu-kulaklik-{123456 + i}' for i in range(3)]
        existing = [{'url': url, 'merchant': f'Satıcı {i}', 'price': 3000.0}
                    for i, url in enumerate(urls)]
        snapshot = {'checked_at': 'old', 'models': [
            {'name': name, 'gtin': white, 'offers': [existing[0]]},
            {'name': name, 'gtin': black, 'offers': existing[1:], 'requiresIndependentPair': True},
        ], 'sourceTargets': {name: [{k: existing[0][k] for k in ('url', 'merchant')}]}}

        def collect(host, rows, statuses, document_reader=None):
            self.assertEqual([target['gtin13'] for _, target in rows], [white, black, black])
            for _, target in rows:
                statuses[target['url']] = 'verified'
            return {name: [dict(target, price=3000.0, observed_at='new', platform='n11',
                                _legal_name=target['merchant']) for _, target in rows]}, {}

        with patch('tws_refresh.reread', side_effect=collect):
            updated, report = refresh(snapshot, catalog)
            repeated, _ = refresh(updated, catalog)
        self.assertEqual([[o['url'] for o in row['offers']] for row in updated['models']], [urls[:1], urls[1:]])
        self.assertEqual(updated['sourceTargets'], repeated['sourceTargets'])
        self.assertEqual(set(updated['sourceTargets']), {f'{name}/{white}', f'{name}/{black}'})
        self.assertEqual(report['sourceCoverage']['www.n11.com']['verifiedOffers'], 3)

    def test_unverified_offer_is_withdrawn_and_rechecked_on_next_run(self):
        name = 'Anker Soundcore AeroFit 2'
        gtin = '0194644298876'
        vatan = 'https://www.vatanbilgisayar.com/anker-soundcore-aerofit-2-kulak-ici-bluetooth-kulaklik-siyah.html'
        teknosa = 'https://www.teknosa.com/anker-soundcore-aerofit-2-a3874-siyah-kulaklik-p-100000063496'
        prior = [
            {'url': vatan, 'merchant': 'Vatan Bilgisayar', 'platform': 'Vatan Bilgisayar', 'price': 6999.0},
            {'url': teknosa, 'merchant': 'Teknosa', 'platform': 'Teknosa', 'price': 5799.0},
        ]
        catalog = {'models': [{'name': name, 'requiresIndependentPair': True}]}
        snapshot = {'checked_at': 'old', 'models': [{'name': name, 'gtin': gtin, 'offers': prior}]}

        def collect(host, rows, statuses, document_reader=None):
            self.assertEqual(rows[0][0], name)
            target = rows[0][1]
            self.assertEqual(target['gtin13'], gtin)
            url = target['url']
            statuses[url] = 'unverified' if url == teknosa else 'unavailable'
            return {}, {}

        with patch('tws_refresh.reread', side_effect=collect):
            updated, _ = refresh(snapshot, catalog)
        self.assertFalse(updated['models'][0]['offers'])
        self.assertEqual([t['url'] for t in updated['sourceTargets'][name]], [vatan, teknosa])

        def restored(host, rows, statuses, document_reader=None):
            url = rows[0][1]['url']
            statuses[url] = 'verified'
            offer = next(o for o in prior if o['url'] == url)
            return {name: [{**offer, 'observed_at': 'new', 'gtin13': gtin, '_legal_name': offer['merchant']}]}, {}

        with patch('tws_refresh.reread', side_effect=restored):
            updated, report = refresh(updated, catalog)
        self.assertEqual(len(updated['models'][0]['offers']), 2)
        self.assertEqual(report['refreshed'], 1)

    def test_transport_failure_preserves_original_timestamps(self):
        name = 'Model'
        offer = {'url': 'https://www.n11.com/urun/uyumlu-kulaklik-123456', 'merchant': 'Independent',
                 'observed_at': 'original', 'price': 1000.0}
        catalog = {'models': [{'name': name}]}
        snapshot = {'checked_at': 'old', 'models': [{'name': name, 'gtin': '0194644298876', 'offers': [offer]}]}
        with patch('tws_refresh.reread', return_value=({}, {'http_429': 1})):
            updated, report = refresh(snapshot, catalog)
        self.assertEqual(updated['checked_at'], 'old')
        self.assertEqual(updated['models'][0]['offers'], [offer])
        self.assertEqual(report['retained'], 1)
