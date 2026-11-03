import unittest
from copy import deepcopy
from datetime import datetime, timezone
from mediamarkt_source import ROBOTS_URL
from retailer_offer_refresh import RetailerOfferRefresher
from update_prices import PriceUpdater
from offer_target_selection import select_offer_targets
from mediamarkt_marketplace_audit import generic_marketplace_offer
from test_mediamarkt_marketplace_audit import fixture, render, URL
from test_mediamarkt_source import Fetch, ROBOTS


class AutomaticMarketplaceTests(unittest.TestCase):
    def test_unlisted_sku_flows_from_live_document_to_existing_ingestion_contract(self):
        url = URL.replace('166438395', '166438396')
        document = fixture()
        document['action']['object']['gtin13'] = '4006381333931'
        body = render(document).replace('166438395', '166438396').encode()
        fetch = Fetch({ROBOTS_URL: ROBOTS, url: body})
        reader = RetailerOfferRefresher(fetch=fetch)
        specs = {'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g'}
        offers = reader.refresh_existing_offer_targets('Samsung Galaxy S25 Ultra',
                                                       [{'source_url': url}], specs)
        self.assertEqual(len(offers), 1)
        offer = offers[0]
        self.assertEqual(offer['merchant'], 'CepHane Teknoloji')
        self.assertEqual(offer['platform'], 'MediaMarkt')
        product = {'id': 999, 'slug': 'new-catalog-sku',
                   'name': 'Samsung Galaxy S25 Ultra', 'offer_targets': [],
                   'offer_targets_truncated': False}
        updater = PriceUpdater(ingestion_client=object(), offer_refresher=reader)
        self.assertTrue(updater.live_offer_is_valid(product, offer, specs))
        self.assertFalse(updater.live_offer_is_valid(
            {**product, 'offer_targets_truncated': True}, offer, specs))
        other_platform = {**product, 'offer_targets': [{'seller_name': offer['merchant'],
            'source_url': 'https://www.hepsiburada.com/another-p-HBC123'}]}
        self.assertFalse(updater.live_offer_is_valid(other_platform, offer, specs))
        self.assertFalse(generic_marketplace_offer(offer, product['name'],
                                                  {**specs, 'storage_gb': 512}))
        for change in ({'merchant': 'MediaMarkt'}, {'condition': 'refurbished'},
                       {'gtin13': '0000000000001'}, {'seller_id': ''}):
            self.assertFalse(generic_marketplace_offer({**offer, **change}, product['name'], specs))
        self.assertEqual(fetch.calls, [ROBOTS_URL, url])

    def test_distinct_seller_is_not_crowded_out_by_twelve_old_colors(self):
        prefix = 'https://www.mediamarkt.com.tr/tr/product/_phone-akilli-telefon-'
        targets = [{'source_url': prefix + str(123456 + i) + '.html',
                    'seller_name': 'Seller A'} for i in range(12)]
        second = {'source_url': prefix + '223456.html', 'seller_name': 'Seller B'}
        third = {'source_url': 'https://www.turkcell.com.tr/pasaj/phone', 'seller_name': 'Pasaj'}
        original = deepcopy(targets + [second, third])
        selected = select_offer_targets(original)
        self.assertEqual(len(selected), 12)
        self.assertIn(second['source_url'], [row['source_url'] for row in selected[:3]])
        self.assertIn(third['source_url'], [row['source_url'] for row in selected[:3]])
        self.assertEqual(original, targets + [second, third])


if __name__ == '__main__':
    unittest.main()
