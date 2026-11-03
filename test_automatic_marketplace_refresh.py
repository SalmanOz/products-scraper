import unittest
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import Mock
from mediamarkt_source import ROBOTS_URL
from retailer_offer_refresh import RetailerOfferRefresher
from update_prices import PriceUpdater
from offer_target_selection import select_offer_targets
from mediamarkt_marketplace_audit import generic_marketplace_offer
from test_mediamarkt_marketplace_audit import fixture, render, URL
from test_mediamarkt_source import Fetch, ROBOTS
from mediamarkt_marketplace_source import ALL_PROFILES
from offer_ingestion_client import IngestionClientError, TARGET_INVALID_REASONS


class AutomaticMarketplaceTests(unittest.TestCase):
    def test_all_raw_sellers_can_prove_an_unrelated_unsupported_refresh_hint_safe(self):
        url = URL.replace('166438395', '166438396')
        document = fixture()
        document['action']['object']['gtin13'] = '4006381333931'
        body = render(document).replace('166438395', '166438396').encode()
        reader = RetailerOfferRefresher(fetch=Fetch({ROBOTS_URL: ROBOTS, url: body}))
        specs = {'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g'}
        offers = reader.refresh_existing_offer_targets('Samsung Galaxy S25 Ultra',
                                                       [{'source_url': url}], specs)
        product = {'id': 999, 'slug': 'new-catalog-sku', 'name': 'Samsung Galaxy S25 Ultra',
                   'attributes': specs, 'offer_targets_truncated': True,
                   'offer_targets': [{'source_url': url, 'merchant_name': 'MediaMarkt',
                                      'seller_name': offers[0]['merchant']}]}
        reasons = {reason: 0 for reason in TARGET_INVALID_REASONS}
        reasons['unsupported_host'] = 1
        raw = {'purpose': 'read_only_raw_seller_collision_proof', 'writes': False,
               'product_id': 999, 'complete': True, 'blocked': False, 'reasons': [],
               'stored_rows_checked': 2, 'unknown_seller_rows': 0, 'invalid_url_rows': 0,
               'invalid_row_identity_rows': 0, 'proposed_seller_other_url_rows': 0,
               'refresh_targets_total': 1, 'refresh_targets_invalid_count': 1,
               'refresh_targets_duplicate_count': 0,
               'refresh_targets_invalid_reasons': reasons, 'refresh_targets_snapshot': 'b' * 64}
        inspection = {**product, 'offer_targets_total': 1, 'offer_targets_invalid_count': 1,
                      'offer_targets_duplicate_count': 0, 'offer_targets_invalid_reasons': reasons,
                      'offer_targets_snapshot': 'b' * 64}
        def make_updater(raw_body, inspected):
            client = Mock()
            client.get_complete_offer_targets.side_effect = IngestionClientError(
                'Complete target source has invalid rows (total=1, invalid=1, duplicate=0)')
            client.get_raw_seller_collision_proof.return_value = raw_body
            client.get_offer_target_inspection.return_value = inspected
            return PriceUpdater(ingestion_client=client, offer_refresher=reader), client
        updater, client = make_updater(raw, inspection)
        original = deepcopy(product)
        report = {}
        updater.hydrate_marketplace_target_proof(product, offers, report)
        self.assertEqual(product, original)
        self.assertTrue(updater.live_offer_is_valid(product, offers[0], specs))
        self.assertEqual(report['raw_target_proofs'][0]['raw_rows_checked'], 2)
        updater.hydrate_marketplace_target_proof(product, offers, report)
        client.get_raw_seller_collision_proof.assert_called_once_with(999, offers[0]['merchant'], url)
        changed_seller = {**offers[0], 'merchant': 'Other Seller', 'seller_name': 'Other Seller'}
        self.assertFalse(updater.live_offer_is_valid(product, changed_seller, specs))
        self.assertFalse(updater.live_offer_is_valid(product, {**offers[0], 'gtin13': '0000000000001'}, specs))
        key = updater._raw_target_key(product, offers[0])
        updater._raw_target_products[key]['checked_at'] = '2000-01-01T00:00:00Z'
        self.assertFalse(updater.live_offer_is_valid(product, offers[0], specs))
        # Duplicate hint rows do not hide an actual seller: the independent raw
        # proof must account for every row and match the complete inspection.
        for invalid_count in (0, 1):
            duplicate_reasons = {**reasons, 'unsupported_host': invalid_count}
            with_duplicates = {**raw, 'stored_rows_checked': 2 + invalid_count,
                'refresh_targets_invalid_count': invalid_count,
                'refresh_targets_duplicate_count': 1, 'refresh_targets_invalid_reasons': duplicate_reasons}
            duplicate_inspection = {**inspection, 'offer_targets_invalid_count': invalid_count,
                'offer_targets_duplicate_count': 1, 'offer_targets_invalid_reasons': duplicate_reasons}
            allowed, duplicate_client = make_updater(with_duplicates, duplicate_inspection)
            duplicate_client.get_complete_offer_targets.side_effect = IngestionClientError(
                f'Complete target source has duplicate rows (total=1, invalid={invalid_count}, duplicate=1)')
            duplicate_report = {}
            allowed.hydrate_marketplace_target_proof(product, offers, duplicate_report)
            self.assertTrue(allowed.live_offer_is_valid(product, offers[0], specs))
            self.assertEqual(duplicate_report['raw_target_proofs'][0]['duplicate_rows_checked'], 1)
            # A new URL for the proposed seller is still a collision, even if
            # all duplicate counts and snapshots agree.
            collided, _ = make_updater({**with_duplicates, 'proposed_seller_other_url_rows': 1}, duplicate_inspection)
            collided.hydrate_marketplace_target_proof(product, offers, {})
            self.assertFalse(collided.live_offer_is_valid(product, offers[0], specs))
        wide_targets = product['offer_targets'] + [
            {'merchant_name': 'MediaMarkt', 'seller_name': f'Other Seller {index}',
             'source_url': url.replace('166438396', str(266438396 + index))}
            for index in range(12)]
        wide_product = {**product, 'offer_targets': wide_targets[:12]}
        wide_raw = {**raw, 'stored_rows_checked': 15, 'refresh_targets_total': 13,
                    'refresh_targets_duplicate_count': 1}
        wide_inspection = {**inspection, 'offer_targets': wide_targets, 'offer_targets_total': 13,
                           'offer_targets_duplicate_count': 1}
        paginated, page_client = make_updater(wide_raw, wide_inspection)
        page_client.get_complete_offer_target_inspection.return_value = wide_inspection
        wide_report = {}
        paginated.hydrate_marketplace_target_proof(wide_product, offers, wide_report)
        self.assertTrue(paginated.live_offer_is_valid(wide_product, offers[0], specs))
        page_client.get_complete_offer_target_inspection.assert_called_once_with(999)
        page_client.get_offer_target_inspection.assert_not_called()
        self.assertEqual(wide_report['raw_target_proofs'][0]['raw_rows_checked'], 15)
        # A seller collision on page two remains blocking independently of the
        # aggregate proof. The first twelve rows alone cannot establish safety.
        changed_page = deepcopy(wide_inspection)
        changed_page['offer_targets'][-1]['seller_name'] = offers[0]['merchant']
        blocked_page, blocked_client = make_updater(wide_raw, changed_page)
        blocked_client.get_complete_offer_target_inspection.return_value = changed_page
        blocked_page.hydrate_marketplace_target_proof(wide_product, offers, {})
        self.assertFalse(blocked_page.live_offer_is_valid(wide_product, offers[0], specs))
        unsafe_reasons = {**reasons, 'unsupported_host': 0, 'sensitive_query': 1}
        for changes in ({'unknown_seller_rows': 1}, {'invalid_url_rows': 1},
                        {'invalid_row_identity_rows': 1}, {'proposed_seller_other_url_rows': 1},
                        {'refresh_targets_duplicate_count': 1}, {'complete': False},
                        {'blocked': True}, {'refresh_targets_invalid_reasons': unsafe_reasons},
                        {'refresh_targets_snapshot': 'c' * 64}):
            blocked, _ = make_updater({**raw, **changes}, inspection)
            blocked.hydrate_marketplace_target_proof(product, offers, {})
            self.assertFalse(blocked.live_offer_is_valid(product, offers[0], specs), changes)

    def test_truncated_catalog_fetches_complete_proof_and_detects_a_hidden_seller_collision(self):
        url = URL.replace('166438395', '166438396')
        document = fixture()
        document['action']['object']['gtin13'] = '4006381333931'
        body = render(document).replace('166438395', '166438396').encode()
        reader = RetailerOfferRefresher(fetch=Fetch({ROBOTS_URL: ROBOTS, url: body}))
        specs = {'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g'}
        offers = reader.refresh_existing_offer_targets('Samsung Galaxy S25 Ultra',
                                                       [{'source_url': url}], specs)
        targets = [{'seller_name': f'Other {i}', 'merchant_name': 'MediaMarkt',
                    'source_url': url.replace('166438396', str(266438396 + i))}
                   for i in range(13)]
        product = {'id': 999, 'slug': 'new-catalog-sku', 'name': 'Samsung Galaxy S25 Ultra',
                   'offer_targets': targets[:12], 'offer_targets_truncated': True}
        complete = {**product, 'offer_targets': targets, 'offer_targets_truncated': False,
                    'offer_targets_total': 13, 'offer_targets_snapshot': 'a' * 64}
        client = Mock()
        client.get_complete_offer_targets.return_value = complete
        updater = PriceUpdater(ingestion_client=client, offer_refresher=reader)
        hydrated = deepcopy(product)
        report = {}
        self.assertFalse(updater.live_offer_is_valid(hydrated, offers[0], specs))
        updater.hydrate_marketplace_target_proof(hydrated, offers, report)
        self.assertTrue(updater.live_offer_is_valid(hydrated, offers[0], specs))
        updater.hydrate_marketplace_target_proof(hydrated, offers, report)
        client.get_complete_offer_targets.assert_called_once_with(999)
        self.assertEqual(report['complete_target_proof'], {'status': 'ready', 'target_count': 13})
        known_product = deepcopy(product)
        updater.hydrate_marketplace_target_proof(known_product,
            [{**offers[0], 'url': next(iter(ALL_PROFILES))}], {})
        self.assertFalse(known_product['offer_targets_truncated'])

        hidden = deepcopy(complete)
        hidden['offer_targets'][-1] = {'seller_name': offers[0]['merchant'],
                                      'source_url': 'https://www.hepsiburada.com/phone-p-HBC123'}
        client.get_complete_offer_targets.return_value = hidden
        hydrated = deepcopy(product)
        updater.hydrate_marketplace_target_proof(hydrated, offers, {})
        self.assertFalse(updater.live_offer_is_valid(hydrated, offers[0], specs))

        for response in ({**complete, 'slug': 'different-catalog-product'}, None):
            client.get_complete_offer_targets.return_value = response
            hydrated = deepcopy(product)
            updater.hydrate_marketplace_target_proof(hydrated, offers, {})
            self.assertEqual(hydrated, product)
            self.assertFalse(updater.live_offer_is_valid(hydrated, offers[0], specs))

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
