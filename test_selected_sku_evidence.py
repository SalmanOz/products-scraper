import copy
import unittest

from selected_sku_evidence import selected_sku_evidence
from tr_price_scraper import TRPriceScraper
from trendyol_catalog_source import parse_catalog_listing
from update_prices import PriceUpdater
from test_trendyol_catalog_source import document, evidence


def a16_offer():
    title = 'Samsung Galaxy A16 128 GB 4 GB Ram Siyah Cep Telefonu'
    data = evidence()
    p = ('state', 'product')
    changes = {('title',): title, p + ('name',): title.removeprefix('Samsung '),
               ('schema', 'name'): title, p + ('attributes', 0, 'value', 'name'): '4 GB',
               p + ('attributes', 2, 'value', 'name'): '4.5G'}
    url = 'https://www.trendyol.com/samsung/galaxy-a16-128-gb-4-gb-ram-siyah-p-676507062?merchantId=657799&v=tek-ebat'
    specs = {'ram_gb': 4, 'storage_gb': 128, 'network_generation': '4g'}
    return parse_catalog_listing(document(changes), url, 'Samsung Galaxy A16', specs)[0], specs



class SelectedSkuEvidenceTests(unittest.TestCase):
    def test_selected_retailer_radio_never_fills_an_unresolved_dual_radio_catalog_identity(self):
        from mediamarkt_marketplace_audit import catalog_variant_matches
        matcher = TRPriceScraper()
        for name, ram, storage in [('Samsung Galaxy A07', 4, 64),
                                   ('Tecno Spark 20 Pro', 8, 256), ('Infinix Note 40', 8, 256)]:
            unknown = {'ram_gb': ram, 'storage_gb': storage}
            for radio in ['4g', '5g']:
                offer = {'source_title': f'{name} {radio} RAM {ram} GB; ROM {storage} GB',
                         'url': 'https://www.teknosa.com/' + name.lower().replace(' ', '-') +
                                f'-{radio}-{ram}gb-ram-{storage}gb-p-100000060715',
                         'ram_gb': ram, 'storage_gb': storage, 'network_generation': radio}
                self.assertFalse(matcher.has_observed_offer_evidence(name, offer, unknown), name)
                self.assertFalse(catalog_variant_matches(offer, name, unknown), name)
                resolved = {**unknown, 'network_generation': radio}
                self.assertTrue(matcher.has_observed_offer_evidence(name, offer, resolved), name)
                self.assertTrue(catalog_variant_matches(offer, name, resolved), name)
                other = {**unknown, 'network_generation': '4g' if radio == '5g' else '5g'}
                self.assertFalse(matcher.has_observed_offer_evidence(name, offer, other), name)
                self.assertFalse(catalog_variant_matches(offer, name, other), name)
            self.assertNotIn('network_generation', unknown)

    def test_plus_capacity_requires_actual_selected_sku_and_preserves_raw_title(self):
        offer, specs = a16_offer()
        matcher = TRPriceScraper()
        for title in ['Samsung Galaxy A16 4+128 GB Siyah Cep Telefonu',
                      'Samsung Galaxy A16 4GB+128GB ROM Siyah Cep Telefonu']:
            changed = dict(offer, source_title=title)
            self.assertTrue(matcher.has_observed_offer_evidence('Samsung Galaxy A16', changed, specs))
            self.assertEqual(changed['source_title'], title)
            changed.pop('selected_trendyol_variant_verified')
            self.assertFalse(matcher.has_observed_offer_evidence('Samsung Galaxy A16', changed, specs))
        for title in ['Samsung Galaxy A16 4+4 GB RAM 128 GB', 'Samsung Galaxy A16 4+256 GB',
                      'Samsung Galaxy A16 8+128 GB', 'Samsung Galaxy A16 RAM 4+128 GB',
                      'Samsung Galaxy A16 4+128 GB RAM', 'Samsung Galaxy A16 4+128 GB 8 GB RAM',
                      'Samsung Galaxy A16 4+128 GB Sanal RAM', 'Samsung Galaxy A16 Plus 4+128 GB']:
            self.assertFalse(matcher.has_observed_offer_evidence('Samsung Galaxy A16', dict(offer, source_title=title), specs), title)
        self.assertFalse(matcher.is_strict_match('Apple iPhone 16', 'Apple iPhone 16+ 256GB'))
        self.assertFalse(matcher.is_strict_match('Samsung Galaxy S26', 'Samsung Galaxy S26+ 12+256GB'))

    def test_selected_actual_attributes_establish_omitted_radio_without_rewriting_title(self):
        offer, specs = a16_offer()
        assert offer is not None
        raw_title = offer['source_title']
        matcher = TRPriceScraper()
        assert matcher.has_observed_offer_evidence('Samsung Galaxy A16', offer, specs)
        assert not matcher.has_observed_offer_evidence('Samsung Galaxy A16',
            {k: v for k, v in offer.items() if k != 'selected_trendyol_variant_verified'}, specs)
        proof = selected_sku_evidence(offer)
        assert proof['network_generation'] == '4g'
        assert (proof['ram_gb'], proof['storage_gb']) == (4, 128)
        product = {'id': 999, 'slug': 'samsung-galaxy-a16-4g'}
        updater = PriceUpdater(ingestion_client=object())
        record = updater.prepare_product_offers(product, [offer], updater.utc_timestamp(), updater.utc_timestamp())[0]
        assert record['source_variant_evidence'] == proof
        assert record['source_title'] == raw_title
        for changed in [dict(specs, ram_gb=8), dict(specs, storage_gb=256),
                        dict(specs, network_generation='5g')]:
            assert not matcher.has_observed_offer_evidence('Samsung Galaxy A16', offer, changed)
        for title in [raw_title + ' 5G', raw_title.replace('4 GB Ram', '8 GB Ram'),
                      raw_title.replace('128 GB', '256 GB'), raw_title.replace('A16', 'A15'),
                      raw_title.replace('A16', 'A16 Plus')]:
            assert not matcher.has_observed_offer_evidence('Samsung Galaxy A16', dict(offer, source_title=title), specs)


    def test_selected_evidence_never_fills_missing_facts_or_crosses_url_seller_identity(self):
        offer, _ = a16_offer()
        assert selected_sku_evidence(offer)
        for field in ['ram_gb', 'storage_gb', 'network_generation', 'gtin13', 'item_number',
                      'selected_trendyol_variant_verified', 'selected_trendyol_seller',
                      'selected_trendyol_merchant_id']:
            changed = copy.deepcopy(offer)
            changed.pop(field)
            assert selected_sku_evidence(changed) is None, field
        for patch in [{'gtin13': '8806094821438'}, {'merchant': 'Other'}, {'item_number': True},
                      {'url': offer['url'] + '&token=private'},
                      {'url': offer['url'].replace('676507062', '676507063')},
                      {'url': offer['url'].replace('657799', '123')}, {'ram_gb': '4'},
                      {'url': offer['url'].replace('.com/', '.com.evil.test/')},
                      {'availability': 'out_of_stock'}, {'condition': 'refurbished'}]:
            assert selected_sku_evidence(dict(offer, **patch)) is None, patch
