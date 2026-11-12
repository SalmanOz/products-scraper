import unittest

from retailer_catalog_feeds import feed_candidates
from tebilon_component_source import TEBILON, selected_tebilon_component


HOST = 'www.sinerji.gen.tr'
URL = f'https://{HOST}/msi-mag-321upx-qd-oled-monitor-p-53664'


def item(url=URL, barcode='4711377150255', availability='in stock', kind='Görüntü ve Ses Sistemleri > Monitör'):
    return f'''<item><g:title>MSI MAG 321UPX QD-OLED Monitör</g:title>
      <g:link>{url}</g:link><g:gtin>{barcode}</g:gtin><g:availability>{availability}</g:availability>
      <g:product_type>{kind}</g:product_type><g:condition>new</g:condition><g:price>60000 TRY</g:price></item>'''


def feed(*items):
    return '<rss xmlns:g="http://base.google.com/ns/1.0"><channel>' + ''.join(items) + '</channel></rss>'


class RetailerCatalogFeedsTests(unittest.TestCase):
    def test_tebilon_requires_same_feed_mpn_and_live_purchasable_checkout(self):
        url = f'https://{TEBILON}/amd-ryzen-5-5600-mpk/'
        gtin = '8592978402266'
        title = 'AMD Ryzen 5 5600 MPK Soket AM4 Fanlı Kutusuz İşlemci'
        xml = item(url, gtin, kind='Bilgisayar Parçaları > İşlemci > Ryzen İşlemci').replace(
            'MSI MAG 321UPX QD-OLED Monitör', title).replace('<g:condition>new', '<g:condition>Yeni').replace(
            '</item>', '<g:id>100-100000927MPK</g:id><g:mpn>100-100000927MPK</g:mpn></item>')
        rows, rejected = feed_candidates(feed(xml), TEBILON, {'cpu'})
        self.assertFalse(rejected)
        row, = rows
        self.assertNotIn('price', row)
        html = f'''<link rel="canonical" href="{url}">
          <div class="productDetail__productName"><h1>{title}</h1><h2>100-100000927MPK</h2></div>
          <div class="productDetail__topPrice"><div class="price"><span class="disc">7.590 TL</span></div></div>
          <a id="sepeteEkleTagMng" class="sepeteEkleAdet" role="button" data-id="4189">Sepete Ekle</a>
          <script type="application/ld+json">{{"@type":"Product","name":"{title}",
            "brand":{{"name":"AMD"}},"offers":{{"@type":"Offer","url":"{url}",
            "priceCurrency":"TRY","price":"7.590"}}}}</script>'''
        offer, reason = selected_tebilon_component(html, url, row, 'cpu')
        self.assertEqual(reason, 'verified')
        self.assertEqual((offer['gtin13'], offer['price']), (gtin, 7590))
        for wrong in (html.replace('100-100000927MPK', '100-100000927BOX'),
                      html.replace('7.590 TL', '7.890 TL'), html.replace('role="button"', 'role="button" disabled'),
                      html.replace('Sepete Ekle', 'Stoğa Gelince Haber Ver')):
            self.assertIsNone(selected_tebilon_component(wrong, url, row, 'cpu')[0])
        self.assertIsNone(selected_tebilon_component(html, url, row, 'gpu')[0])
        self.assertFalse(feed_candidates(feed(xml, xml.replace(url, url.replace('mpk/', 'tray/'))), TEBILON, {'cpu'})[0])

    def test_uses_only_own_canonical_barcode_destinations_and_never_stores_feed_prices(self):
        rows, rejected = feed_candidates(feed(
            item(), item(), item(URL.replace(HOST, 'www.gaming.gen.tr')),
            item(barcode='4711377150256'), item(availability='out of stock'),
            item(kind='Bilgisayar Bileşenleri > Kasa > Kasa Aksesuarları'),
        ), HOST, {'monitor', 'case'})
        self.assertEqual(rows, [{'source': HOST, 'category': 'monitor', 'id': '4711377150255',
                                 'title': 'MSI MAG 321UPX QD-OLED Monitör', 'url': URL,
                                 'feedGtin': '4711377150255', 'retailerLeads': [URL]}])
        self.assertEqual(rejected, {'monitor': 3})

    def test_rejects_unsupported_root_and_dtd(self):
        for payload in ('<html/>', '<!DOCTYPE rss [<!ENTITY x "remote">]>' + feed(item())):
            with self.assertRaises(ValueError):
                feed_candidates(payload, HOST, {'monitor'})
