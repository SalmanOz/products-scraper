"""Offline contracts for the reviewed S25 FE 8/256 draft publication."""

import unittest
from unittest.mock import Mock, patch
from io import BytesIO

import requests

import publish_s25_fe_variant as variant


def source_records():
    values = {
        'ram_gb': 8, 'storage_gb': 256, 'performance_score': 8.0,
        'camera_score': 7.5, 'battery_score': 8.0, 'screen_score': 8.0,
        'battery_mah': 4900, 'screen_size_inch': 6.7,
        'Technical sheet': {'Release date': '2025'},
    }
    return [{'product_slug': variant.SLUG, 'attribute_key': key, 'value': value,
             'source_url': variant.SOURCE_URL, 'value_origin': 'spec_database',
             'observed_at': '2026-11-02T12:00:00Z', 'confidence': 'medium',
             'is_estimate': False} for key, value in values.items()]


class FakeScraper:
    def __init__(self, memory=(8, 256), image=False):
        self.memory = memory
        self.image = image
        self.calls = []

    def scrape_product_details(self, url, **kwargs):
        self.calls.append((url, kwargs))
        rows = source_records()
        rows[0]['value'], rows[1]['value'] = self.memory
        if self.memory != (8, 256):
            return False
        kwargs['record_sink'].extend(rows)
        if self.image:
            kwargs['image_sink'].append({'product_slug': variant.SLUG, 'urls': [
                f'https://cdn.teknoskor.com/products/{variant.SLUG}/{variant.SLUG}-0.webp']})
        return True


class FakeReader:
    def __init__(self, offers):
        self.offers = offers
        self.calls = []

    def refresh_existing_offer_targets(self, name, targets, specs):
        self.calls.append((name, targets, specs))
        return self.offers

    def target_report(self, *_):
        return [{'reason': 'observed_exact_offer', 'source_url': offer['url']}
                for offer in self.offers]


class FakeUpdater:
    def live_offer_is_valid(self, product, offer, specs):
        return product['name'] == variant.NAME and specs == variant.SPECS and offer.get('valid', True)

    def utc_timestamp(self):
        return '2026-11-02T12:00:00Z'

    def prepare_product_offers(self, product, offers, *_):
        return [{'product_id': product['id'], 'product_slug': product['slug'],
                 'seller': offer['merchant'], 'price': offer['price']}
                for offer in offers]


def offers():
    return [{'merchant': 'MediaMarkt', 'url': variant.MEDIAMARKT_URLS[0],
             'price': 38949, 'availability': 'in_stock'},
            {'merchant': 'Vatan Bilgisayar', 'url': variant.VATAN_URL,
             'price': 38949, 'availability': 'in_stock'}]


class ReviewedVariantPublicationTests(unittest.TestCase):
    def test_official_pdf_requires_selected_sku_on_same_8_256_row(self):
        self.assertTrue(variant.official_sku_line(
            'Other model 8+256\nGalaxy S25FE 8+256 SM-S731BDBGTUR 2,500 10'))
        self.assertTrue(variant.official_sku_line('Galaxy S25FE 8+256\nSM-S731BDBGTUR'))
        for text in ('Galaxy S25FE 8+128 SM-S731BDBGTUR',
                     'Galaxy S25FE 8+256 SM-S731BDBGTUX',
                     'Galaxy S25FE 8+256\nOther Phone SM-S731BDBGTUR'):
            self.assertFalse(variant.official_sku_line(text))

    def test_bounded_official_pdf_reader_rejects_mismatched_document(self):
        response = requests.Response()
        response.status_code = 200
        response.url = variant.SAMSUNG_PDF
        response._content = b'%PDF-synthetic'
        response.raw = BytesIO(response._content)
        response.headers['Content-Length'] = str(len(response._content))
        page = Mock()
        page.extract_text.return_value = 'Galaxy S25FE 8+256 SM-S731BDBGTUR 2,500 10'
        reader = Mock()
        reader.pages = [page]
        result = variant.fetch_official_sku_pdf(fetch=lambda *_a, **_k: response,
                                                reader_factory=lambda _stream: reader)
        self.assertEqual(result['sku'], variant.SKU)
        page.extract_text.return_value = 'Galaxy S25FE 8+128 SM-S731BDBGTUR'
        with self.assertRaisesRegex(variant.PublicationPreflightError, 'selected_sku_memory_missing'):
            variant.fetch_official_sku_pdf(fetch=lambda *_a, **_k: response,
                                           reader_factory=lambda _stream: reader)

    def test_source_preflight_is_read_only_and_exact_memory(self):
        scraper = FakeScraper()
        records, images = variant.source_records(scraper)
        self.assertEqual(images, [])
        self.assertEqual({r['attribute_key'] for r in records}.__len__(), 9)
        call = scraper.calls[0][1]
        self.assertEqual(call['expected_memory'], {'ram_gb': 8, 'storage_gb': 256})
        self.assertEqual(call['product_slug'], variant.SLUG)
        with self.assertRaisesRegex(variant.PublicationPreflightError, 'exact_technical_variant_unavailable'):
            variant.source_records(FakeScraper(memory=(8, 128)))

    def test_official_capacity_sources_replace_only_ram_and_storage(self):
        staged = variant.add_official_capacity_sources(source_records())
        for row in staged:
            if row['attribute_key'] in {'ram_gb', 'storage_gb'}:
                self.assertEqual((row['source_url'], row['value_origin'], row['confidence']),
                                 (variant.SAMSUNG_PDF, 'manufacturer', 'high'))
            else:
                self.assertEqual(row['source_url'], variant.SOURCE_URL)

    def test_preflight_needs_two_independent_exact_live_merchants(self):
        checked = variant.preflight(pdf_check=lambda: {'observed': True},
                                    scraper=FakeScraper(), reader=FakeReader(offers()),
                                    updater=FakeUpdater())
        self.assertEqual({row['merchant'] for row in checked.offers},
                         {'MediaMarkt', 'Vatan Bilgisayar'})
        with self.assertRaisesRegex(variant.PublicationPreflightError, 'two_exact_live_merchants_missing'):
            variant.preflight(pdf_check=lambda: {'observed': True},
                              scraper=FakeScraper(), reader=FakeReader(offers()[:1]),
                              updater=FakeUpdater())

    def test_published_rerun_skips_source_and_publish_writes(self):
        preflight = variant.Preflight(source_records(), offers(), [], [])
        source_client = Mock()
        offer_client = Mock()
        offer_client.ingest_offers.return_value = {'committed': True, 'accepted': 2,
                                                   'stale_ignored': 0, 'variant_rejected': 0}
        result = variant.publish(
            preflight, source_client=source_client, offer_client=offer_client,
            catalog_expander=lambda _batch: {'products': [{
                'id': 309, 'name': variant.NAME, 'slug': variant.SLUG, 'status': 'published'}]},
            reader=FakeReader(offers()), updater=FakeUpdater(),
        )
        self.assertTrue(result['published'])
        source_client.submit_sources.assert_not_called()
        self.assertEqual(result['offers']['merchants'], ['MediaMarkt', 'Vatan Bilgisayar'])

    def test_draft_requires_sources_image_and_server_publication_gate(self):
        preflight = variant.Preflight(source_records(), offers(), [], [])
        source_client = Mock()
        source_client.submit_sources.return_value = {'committed': True, 'accepted': 9,
                                                     'stale_ignored': 0}
        offer_client = Mock()
        offer_client.ingest_offers.return_value = {'committed': True, 'accepted': 2,
                                                   'stale_ignored': 0, 'variant_rejected': 0}
        with patch.object(variant, 'publish_draft', return_value={
            'committed': True, 'scores_recalculated': True}) as server_publish:
            result = variant.publish(
                preflight, source_client=source_client, offer_client=offer_client,
                catalog_expander=lambda _batch: {'products': [{
                    'id': 309, 'name': variant.NAME, 'slug': variant.SLUG, 'status': 'draft'}]},
                source_scraper=FakeScraper(image=True), reader=FakeReader(offers()),
                updater=FakeUpdater(),
            )
        self.assertTrue(result['published'])
        uploaded = source_client.submit_sources.call_args.args[0]
        self.assertEqual({r['source_url'] for r in uploaded
                          if r['attribute_key'] in {'ram_gb', 'storage_gb'}},
                         {variant.SAMSUNG_PDF})
        self.assertEqual(server_publish.call_args.args[1], 309)
        self.assertEqual(offer_client.ingest_offers.call_count, 1)

    def test_missing_image_cannot_promote_draft(self):
        with self.assertRaisesRegex(variant.PublicationPreflightError, 'exact_variant_image_missing'):
            variant.source_records(FakeScraper(), upload_images=True)


if __name__ == '__main__':
    unittest.main()
