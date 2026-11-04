from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest

from bs4 import BeautifulSoup

from selected_sku_evidence import selected_sku_evidence
from tr_price_scraper import TRPriceScraper
from trendyol_basket_price import valid_basket_promotion, validity_deadline
from trendyol_catalog_source import parse_catalog_listing
from update_prices import PriceUpdater

URL = 'https://www.trendyol.com/xiaomi/redmi-note-14-pro-5g-8gb-256-beyaz-p-1190467372'
NAME = 'Xiaomi Redmi Note 14 Pro'
SPECS = {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'}


def document(change=None):
    soup = BeautifulSoup(Path(__file__).with_name('fixtures').joinpath(
        'trendyol-selected-public-basket.html').read_text(), 'html.parser')
    script = soup.select_one('script')
    state = json.loads(script.get_text().split('=', 1)[1].rstrip(';'))
    product = state['product']
    # Keep the observed fixture's identity/prices/conditions. Move only its
    # expiry clock forward so this regression remains meaningful after Dec 4.
    future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime('%Y-%m-%dT23:00:00')
    for row in product['merchantListing']['promotions'] + product['merchantListing']['filteredPromotions']:
        row['promotionEndDate'] = future
    if change:
        change(product, soup)
    script.string = 'window["__envoy__SHARED_PROPS"]=' + json.dumps(state, ensure_ascii=False) + ';'
    return str(soup)


class TrendyolBasketPriceTests(unittest.TestCase):
    def test_observed_public_applied_basket_survives_physical_sku_and_ingestion_preparation(self):
        offer, why = parse_catalog_listing(document(), URL, NAME, SPECS)
        self.assertEqual(why, 'selected_trendyol_public_basket_offer')
        self.assertEqual((offer['merchant'], offer['price'], offer['listed_price']), ('E-trade1', 24899, 24999))
        self.assertEqual(offer['source_title'], 'Xiaomi Redmi Note 14 Pro 5G 8GB+256 beyaz')
        self.assertTrue(TRPriceScraper().has_observed_offer_evidence(NAME, offer, SPECS))
        proof = selected_sku_evidence(offer)
        self.assertEqual(proof['price_kind'], 'basket')
        self.assertEqual(proof['basket_promotion']['id'], '31240051')
        records = PriceUpdater(ingestion_client=object()).prepare_product_offers(
            {'id': 999, 'slug': 'xiaomi-redmi-note-14-pro'}, [offer],
            PriceUpdater.utc_timestamp(), PriceUpdater.utc_timestamp())
        self.assertEqual(records[0]['source_variant_evidence'], proof)
        self.assertEqual(records[0]['price'], 24899)
        self.assertNotIn('registeredEmail', json.dumps(records))

    def test_member_region_usage_limit_unapplied_unknown_and_disagreeing_campaigns_are_held(self):
        for field, value in [('isTyPlus', True), ('isOnlyAz', True), ('mocPerUser', 1),
                             ('mocPerUser', False), ('isApplied', False), ('isLimitSatisfied', True),
                             ('discountType', 3), ('promotionDiscountType', 'Coupon'),
                             ('name', '2. ürüne 100 TL İndirim'), ('name', 'Sepette 200 TL İndirim')]:
            def change(p, s):
                for row in p['merchantListing']['promotions'] + p['merchantListing']['filteredPromotions']:
                    if row['id'] == 31240051:
                        row[field] = value
            self.assertIsNone(parse_catalog_listing(document(change), URL, NAME, SPECS)[0], (field, value))
        for field in ['isTyPlus', 'mocPerUser', 'isApplied', 'isOnlyAz']:
            def missing(p, s):
                for row in p['merchantListing']['promotions'] + p['merchantListing']['filteredPromotions']:
                    row.pop(field, None)
            self.assertIsNone(parse_catalog_listing(document(missing), URL, NAME, SPECS)[0], field)
        def disagreement(p, s):
            p['merchantListing']['filteredPromotions'][0]['isApplied'] = 1
        self.assertIsNone(parse_catalog_listing(document(disagreement), URL, NAME, SPECS)[0])
        def additional_discount(p, s):
            p['merchantListing']['promotions'].append({'id': 10, 'promotionDiscountType': 'SecondItem'})
        self.assertIsNone(parse_catalog_listing(document(additional_discount), URL, NAME, SPECS)[0])
        def unknown_condition(p, s):
            for row in p['merchantListing']['promotions'] + p['merchantListing']['filteredPromotions']:
                if row['id'] == 31240051:
                    row['minimumBasketAmount'] = 50000
        self.assertIsNone(parse_catalog_listing(document(unknown_condition), URL, NAME, SPECS)[0])

    def test_all_selected_price_stock_schema_visible_and_campaign_layers_must_still_agree(self):
        changes = [
            lambda p, s: p['merchantListing']['winnerVariant']['price']['couponApplicablePrice'].update(value=24000),
            lambda p, s: p['merchantListing']['winnerVariant']['price']['discountedPrice'].update(text='24.000 TL'),
            lambda p, s: p['merchantListing']['winnerVariant'].update(maxSaleLimit=0),
            lambda p, s: p['merchantListing']['winnerVariant'].update(inStock=False),
            lambda p, s: p['variants'][0]['price'].update(value=24999),
            lambda p, s: setattr(s.select_one('.new-price'), 'string', '24.000 TL'),
            lambda p, s: setattr(s.select_one('.old-price'), 'string', '25.999 TL'),
            lambda p, s: setattr(s.select_one('.info-text'), 'string', 'Üyelere özel'),
            lambda p, s: s.select_one('[data-testid="buy-now-button"]').attrs.update(disabled='disabled'),
        ]
        for change in changes:
            self.assertIsNone(parse_catalog_listing(document(change), URL, NAME, SPECS)[0])
        def schema_member(p, s):
            script = s.select_one('script[type="application/ld+json"]')
            data = json.loads(script.get_text())
            data['offers']['validForMemberTier'] = 'premium'
            script.string = json.dumps(data)
        self.assertIsNone(parse_catalog_listing(document(schema_member), URL, NAME, SPECS)[0])
        def schema_private_label(p, s):
            script = s.select_one('script[type="application/ld+json"]')
            data = json.loads(script.get_text()); data['offers']['description'] = 'Üyelere özel fiyat'
            script.string = json.dumps(data)
        self.assertIsNone(parse_catalog_listing(document(schema_private_label), URL, NAME, SPECS)[0])
        def wrong_schema_price(p, s):
            script = s.select_one('script[type="application/ld+json"]')
            data = json.loads(script.get_text()); data['offers']['price'] = '24999.00'
            script.string = json.dumps(data)
        self.assertIsNone(parse_catalog_listing(document(wrong_schema_price), URL, NAME, SPECS)[0])

    def test_expired_invalid_dates_and_forged_proof_deadlines_cannot_be_published(self):
        for date in ['2000-01-01T00:00:00', '2026-02-30T00:00:00', '0001-01-01T00:00:00', '2099-01-01T00:00:00Z']:
            def expired(p, s):
                for row in p['merchantListing']['promotions'] + p['merchantListing']['filteredPromotions']:
                    row['promotionEndDate'] = date
            self.assertIsNone(parse_catalog_listing(document(expired), URL, NAME, SPECS)[0], date)
        offer, _ = parse_catalog_listing(document(), URL, NAME, SPECS)
        for changed in [None, {}, {**offer['basket_promotion'], 'valid_until': '2099-01-01T00:00:00Z'},
                        {**offer['basket_promotion'], 'is_applied': False},
                        {**offer['basket_promotion'], 'discount_amount': 200}]:
            bad = deepcopy(offer); bad['basket_promotion'] = changed
            self.assertIsNone(selected_sku_evidence(bad))
        self.assertFalse(valid_basket_promotion(offer['basket_promotion'], 24999, 24898))
        self.assertEqual(validity_deadline('2026-12-04T23:00:00').isoformat(), '2026-12-03T00:00:00+00:00')


if __name__ == '__main__':
    unittest.main()
