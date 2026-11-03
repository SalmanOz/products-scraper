from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from bs4 import BeautifulSoup
from teknosa_seller_cards import canonical_document_url, public_seller_offers, parse_seller_destination
from teknosa_reviewed_source import TeknosaCatalogSource
from retailer_offer_refresh import RetailerOfferRefresher
from update_prices import PriceUpdater

URL = 'https://www.teknosa.com/samsung-galaxy-s26-ultra-5g-12256gb-siyah-akilli-telefon-p-100000061482'
DESTINATION = URL + '?shopId=2544'
PROFILE = {'name': 'Samsung Galaxy S26 Ultra', 'specs': {'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g'}}


def document():
    return Path(__file__).with_name('fixtures').joinpath('teknosa-public-other-seller.html').read_text()


class TeknosaSellerCardTests(unittest.TestCase):
    def test_actual_public_listing_price_is_not_the_repeated_global_cart_analytics(self):
        offers = public_seller_offers(document(), URL, PROFILE)
        self.assertEqual(len(offers), 1)
        offer = offers[0]
        self.assertEqual((offer['merchant'], offer['merchant_id'], offer['price']), ('ÇARŞI MOBİLE', '2544', 88649))
        self.assertEqual((offer['sku'], offer['gtin13'], offer['listing_id']),
                         ('100000061482', '8806097866633', '1644913'))
        self.assertEqual(offer['url'], DESTINATION)
        self.assertEqual(offer['source_document_url'], URL)
        self.assertNotIn('CSRF', str(offer))
        self.assertNotIn('Token', str(offer))
        parsed = parse_seller_destination(document(), DESTINATION, PROFILE)[0]
        self.assertEqual(parsed, offers[0] | {'observed_at': parsed['observed_at']})

    def test_independent_sku_gtin_seller_listing_price_stock_and_destination_layers_are_required(self):
        changes = [
            ('data-product-id', '100000061483'), ('data-product-ean', '8806097866634'),
            ('data-product-name', 'Samsung Galaxy S26 Ultra 12GB RAM 512GB'),
            ('data-shop-name', 'Other Seller'), ('data-product-shop-id', '2522'),
            ('data-shop-id', 'shop_offer_1644914'), ('data-product-url', '/other-p-100000061482?shopId=2544'),
            ('aria-disabled', 'true'), ('class', ['js-add-to-cart', 'js-shop_offer_1644913']),
        ]
        for key, value in changes:
            soup = BeautifulSoup(document(), 'html.parser')
            card = soup.select_one('.pds.active[data-prod-seller-url]')
            card.select_one('button#addToCartButton')[key] = value
            self.assertEqual(public_seller_offers(str(soup), URL, PROFILE), [], (key, value))
        for selector, value in [('.prc-last', '1 TL'), ('.pds-delivery-detail > span', 'Stok tükendi'),
                                ('.pds-title > a', 'Other Seller')]:
            soup = BeautifulSoup(document(), 'html.parser')
            soup.select_one('.pds.active[data-prod-seller-url]').select_one(selector).string = value
            self.assertEqual(public_seller_offers(str(soup), URL, PROFILE), [], selector)
        soup = BeautifulSoup(document(), 'html.parser')
        soup.select_one('input[name="productCodePost"]')['value'] = 'offer_1644914'
        self.assertEqual(public_seller_offers(str(soup), URL, PROFILE), [])
        soup = BeautifulSoup(document(), 'html.parser')
        card = soup.select_one('.pds.active[data-prod-seller-url]')
        card.append(BeautifulSoup('<span>Kuponla 88.649 TL</span>', 'html.parser'))
        self.assertEqual(public_seller_offers(str(soup), URL, PROFILE), [])
        # Duplicate or conflicting rows cannot be silently deduplicated into proof.
        soup = BeautifulSoup(document(), 'html.parser')
        soup.append(deepcopy(soup.select_one('.pds.active[data-prod-seller-url]')))
        self.assertEqual(public_seller_offers(str(soup), URL, PROFILE), [])
        for key, value in [('ram_gb', 8), ('storage_gb', 512), ('network_generation', '4g')]:
            p = deepcopy(PROFILE); p['specs'][key] = value
            self.assertEqual(public_seller_offers(document(), URL, p), [])
        self.assertIsNone(parse_seller_destination(document(), URL + '?shopId=2522', PROFILE)[0])
        self.assertIsNone(canonical_document_url(DESTINATION + '&token=private'))

    def test_full_sweep_rereads_canonical_document_then_publishes_and_reads_back_each_real_seller(self):
        from test_priority_candidates import Client
        from sweep_priority_catalog import sweep, coverage_readback
        client = Client()
        client.product.update(name=PROFILE['name'], slug='samsung-galaxy-s26-ultra',
            attributes={**PROFILE['specs'], 'Connectivity': {'5G': True}},
            offer_targets=[{'source_url': URL, 'merchant_name': 'Teknosa'}])
        client.get_public_price_priority = lambda: {999: (client.product['slug'], False)}
        class Transport:
            def __init__(self): self.calls = []
            def configure_catalog_budget(self, count): pass
            def read_document(self, url):
                self.calls.append(url)
                if url != URL: raise AssertionError('Never fetch the shop selector')
                return document(), 'document_read', 200
            def report(self): return {}
        audit_transport, live_transport = Transport(), Transport()
        source = TeknosaCatalogSource(transport=live_transport)
        reader = RetailerOfferRefresher(teknosa_catalog_source=source)
        report = sweep(client=client, publish=True, transport=audit_transport, reader=reader)
        self.assertEqual((report['status'], report['accepted'], report['queued']), ('completed', 2, 2))
        self.assertEqual(audit_transport.calls, [URL])
        self.assertEqual(live_transport.calls, [URL])
        self.assertEqual([(batch[0]['seller'], batch[0]['price']) for batch in client.writes],
                         [('hepsi10line', 88648), ('ÇARŞI MOBİLE', 88649)])
        self.assertEqual(client.writes[1][0]['source_url'], DESTINATION)
        self.assertTrue(all(row['publication']['commercial_readback_verified'] for row in report['rows']))
        self.assertTrue(report['rows'][1]['document_reused'])
        self.assertEqual(coverage_readback(report, client.get_public_price_priority())['documents_read'], 1)

    def test_reader_fetches_only_the_canonical_document_and_preserves_its_real_observation_time(self):
        calls = []
        class Transport:
            def read_document(self, url):
                calls.append(url)
                if url != URL:
                    raise AssertionError('No shop-selector or filter URL may be fetched')
                return document(), 'document_read', 200
        source = TeknosaCatalogSource(transport=Transport())
        reader = RetailerOfferRefresher(teknosa_catalog_source=source)
        offers = reader.refresh_existing_offer_targets(PROFILE['name'], [{'source_url': DESTINATION}], PROFILE['specs'])
        self.assertEqual(len(offers), 1)
        self.assertEqual(calls, [URL])
        updater = PriceUpdater(ingestion_client=object(), offer_refresher=reader)
        product = {'id': 999, 'slug': 'samsung-galaxy-s26-ultra', 'name': PROFILE['name'],
                   'offer_targets': [{'source_url': URL, 'merchant_name': 'Teknosa', 'seller_name': 'hepsi10line'}],
                   'offer_targets_truncated': False}
        self.assertTrue(updater.live_offer_is_valid(product, offers[0], PROFILE['specs']))
        record = updater.prepare_product_offers(product, offers, updater.utc_timestamp(), updater.utc_timestamp())[0]
        self.assertEqual((record['seller'], record['source_url'], record['price']), ('ÇARŞI MOBİLE', DESTINATION, 88649))
        again = source.read_offer(DESTINATION)[0]
        self.assertEqual(again['observed_at'], offers[0]['observed_at'])
        self.assertEqual(calls, [URL])
        html, _, status = source._documents[URL]
        source._documents[URL] = (html, (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat(), status)
        self.assertEqual(source.read_offer(DESTINATION)[1], 'source_document_observation_expired')
        self.assertEqual(calls, [URL])
