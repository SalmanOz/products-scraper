import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from retail_html_catalog import product_intent_rejection, valid_title
from retail_catalog_publish import merge, valid_row
import test_retail_html_catalog as fixtures
from test_retail_html_catalog import URL, GTIN


class RetailProductIntentTest(TestCase):
    def test_explicit_incompatible_product_types_are_rejected(self):
        examples = [
            ('network', 'TP-Link UA430D USB SD/microSD Kart Okuyucu'),
            ('earbuds', 'BEATS Kulaklık Yastığı + Kafa Bandı'),
            ('monitor', 'ARCTIC MX-6 (2 g) Ultimate Performance ısı iletken macun CPU konsol'),
            ('earbuds', 'BEATS 360 Kulaklık Yastığı Seti Kulaklıklar ve Kafa Bandı için'),
            ('air-purifier', 'XIAOMI Smart Air Purifier 4 Pro Filtre'),
            ('air-purifier', 'PURMED Levoit Core 300 Uyumlu Hepa + Karbon Hava Temizleyici Filtresi Beyaz'),
            ('air-purifier', 'BLUEAIR Prefilter Blue 221 Hava Temizleyici Ön Filtresi Sarı'),
            ('camera', 'Trendfone Gopro Aksiyon Kameraları İçin Aksesuar Seti'),
            ('camera', 'Gplus GP525 Kask Bağlantı Aparatı 5 Parça'),
            ('camera', 'DJI Osmo Action Extreme Battery Plus 1950 Mah Batarya'),
            ('camera', 'Vothoon Çocuk Kamera Baskı Kağıdı 5li Rulo'),
            ('camera', 'Insta360 Invisible Selfie Stick 114cm'),
            ('camera', 'Gplus GP76M Araç Cam Tutucu Kalın Mini Vantuz'),
            ('keyboard', 'ASUS ROG PBT Doubleshot 124 Adet Keycap Set Siyah'),
            ('keyboard', 'M2M Dekoratif Klavye Tuş Sticker Seti'),
            ('keyboard', 'SPELT Mekanik Klavye Switch Ve Tuş Kapağı Sökme Aparatı'),
            ('microphone', 'FIFINE BM88 Mikrofon Standı Beyaz'),
            ('projector', 'XGIMI Portable Outdoor Perde 70 inç'),
            ('projector', 'WANBO Projeksiyon Masaüstü Taban Standı'),
            ('mouse', 'Trust 20429 Bigfoot Jel Mousepad Kırmızı'),
            ('drone', 'DJI Mini 5 Pro Akıllı Uçuş Bataryası Plus'),
            ('television', 'Xvive Xv-u45 Kablosuz In-ear Monitor Sistemi'),
        ]
        for kind, title in examples:
            with self.subTest(kind=kind, title=title):
                self.assertIsNotNone(product_intent_rejection(title, kind))
                self.assertFalse(valid_title(title, kind))

    def test_actual_devices_and_explicit_bundles_keep_their_category(self):
        examples = [
            ('network', 'TP-Link Router Ethernet SD Kart Okuyucu ile'),
            ('laptop', 'Lenovo IdeaPad 16GB RAM 512GB SSD Laptop + Çanta'),
            ('camera', 'Akaso EK7000 Pro 4K Wi-Fi Aksiyon Kamera ve Aksesuar Seti'),
            ('camera', 'GoPro Hero13 Black Pack Aksesuar Kit Aksiyon Kamerası'),
            ('camera', 'Canon EOS R8 Fotoğraf Makinesi + Lens + Batarya'),
            ('camera', 'Torima Dijital Fotoğraf Makinesi Çocuk Termal Kağıt Baskılı Kamera'),
            ('camera', 'Juo JH830 Aksiyon Kamera Sabitleme Aparatı ile'),
            ('earbuds', 'RAMPAGE Retsu Wireless 2.4G+ Bluetooth Kumaş ve Deri Protein Earpad Mikrofonlu Oyuncu Kulaklığı'),
            ('earbuds', 'Philips SHB1803 Bluetooth Kulaklık + Araç İçi Şarj Adaptörü'),
            ('air-purifier', 'ELECTROLUX Pure 500 XL 4 Aşamalı HEPA Filtre Sessiz Mod Hava Temizleyici'),
            ('air-purifier', 'PURMED Pure Compact Yedek Hepa Filtre Hediyeli Hava Temizleyici'),
            ('air-purifier', 'DREO 338S Akıllı HEPA Filtre PM2.5 Sensör Hava Temizleyici'),
            ('robot-vacuum', 'DJI Romo P Paspas Pedleri 25000 Pa Emiş Gücü Robot Süpürge'),
            ('security-camera', 'HIKVISION Termal Bullet Kamera'),
            ('television', 'ONVO 75VQ90F3UA 75 QLED TV Duvar Askı Aparatlı'),
            ('mouse', 'Logitech M196 Mouse + Notebook Sırt Çantası'),
            ('mouse', 'Logitech G305 Mouse + Mousepad'),
            ('microphone', 'FIFINE AM8PROT Mikrofon Sehpası ile Kablo Mikrofon Seti'),
            ('projector', 'XGIMI Mogo 4 1080P FHD Bundle Projeksiyon Seti'),
            ('pc-parts', 'ARCTIC MX6 Isı İletken Macun'),
        ]
        for kind, title in examples:
            with self.subTest(kind=kind, title=title):
                self.assertIsNone(product_intent_rejection(title, kind))
                self.assertTrue(valid_title(title, kind))

    def test_publisher_quarantines_cached_and_incoming_false_categories_with_reasons(self):
        factory = fixtures.RetailHtmlCatalogTest()
        from retail_html_catalog import read_product
        row = read_product(factory.listing(), URL, 'laptop')
        row['offers'] = [row.pop('offer')]
        wrong = {**row, 'gtin': '4560907452075', 'kind': 'monitor',
                 'name': 'ARCTIC MX-6 Isı İletken Macun',
                 'specs': {'Ekran Yenileme Hızı': '250 Hz', 'Ekran Boyutu': '13.3 inç'}, 'specSources': {}}
        with TemporaryDirectory() as root:
            folder = Path(root)
            catalog = folder / 'catalog.json'
            catalog.write_text(json.dumps([row, wrong]))
            quarantine = []
            rows, counts = merge([wrong], folder, lambda _: True, catalog, quarantine)
        self.assertEqual([p['gtin'] for p in rows], [GTIN])
        self.assertFalse(valid_row(wrong))
        self.assertEqual(len(quarantine), 2)
        self.assertEqual({p['stage'] for p in quarantine}, {'existing', 'incoming'})
        self.assertEqual({p['reason'] for p in quarantine}, {'monitor_thermal_compound'})
        self.assertEqual({p['source'] for p in quarantine}, {URL})
        self.assertEqual(counts['quarantined_monitor_thermal_compound'], 2)
