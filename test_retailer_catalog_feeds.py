import unittest

from retailer_catalog_feeds import feed_candidates


HOST = 'www.sinerji.gen.tr'
URL = f'https://{HOST}/msi-mag-321upx-qd-oled-monitor-p-53664'


def item(url=URL, barcode='4711377150255', availability='in stock', kind='Görüntü ve Ses Sistemleri > Monitör'):
    return f'''<item><g:title>MSI MAG 321UPX QD-OLED Monitör</g:title>
      <g:link>{url}</g:link><g:gtin>{barcode}</g:gtin><g:availability>{availability}</g:availability>
      <g:product_type>{kind}</g:product_type><g:condition>new</g:condition><g:price>60000 TRY</g:price></item>'''


def feed(*items):
    return '<rss xmlns:g="http://base.google.com/ns/1.0"><channel>' + ''.join(items) + '</channel></rss>'


class RetailerCatalogFeedsTests(unittest.TestCase):
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
