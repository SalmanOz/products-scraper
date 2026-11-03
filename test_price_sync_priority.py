"""Offline discovery-priority contract; no retailer or ingestion network."""

import asyncio
import unittest
from unittest.mock import Mock, patch

from offer_ingestion_client import OfferIngestionClient
from price_sync_priority import prioritize_unpriced_products, prioritize_discoverable_products
from update_prices import PriceUpdater


class _Response:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class _Session:
    def __init__(self, body):
        self.body = body
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return _Response(self.body)


class _Catalog:
    def __init__(self, products, status):
        self.products, self.status = products, status

    def get_published_products(self):
        return list(self.products)

    def get_public_price_priority(self):
        return self.status

    def ingest_offers(self, _rows):
        raise AssertionError('Priority audit must not write offers')


class _Discovery:
    async def get_best_prices(self, _name, _specs):
        return []


class _Refresher:
    def refresh_existing_offer_targets(self, _name, _targets, _specs):
        return []

    def target_report(self, _name, _targets, _specs):
        return []


def _phone(product_id):
    return {'id': product_id, 'slug': f'example-{product_id}',
            'name': f'Example {product_id}', 'attributes': {},
            'offer_targets': []}


class PriceSyncPriorityTests(unittest.TestCase):
    def test_real_unpriced_candidates_and_current_prices_precede_speculative_models(self):
        products = [_phone(i) for i in (1, 2, 3, 4)]
        status = {1: ('example-1', False), 2: ('example-2', True),
                  3: ('example-3', False), 4: ('changed-slug', False)}
        ordered = prioritize_discoverable_products(products, status, {'example-3', 'example-4'})
        self.assertEqual([p['id'] for p in ordered], [3, 2, 1, 4])
        self.assertEqual([p['id'] for p in products], [1, 2, 3, 4])

    def test_full_updater_uses_cached_sitemap_candidates_before_speculative_searches(self):
        from mediamarkt_source import MediaMarktSource
        products = [_phone(i) for i in (1, 2, 3)]
        client = _Catalog(products, {1: ('example-1', False),
                                     2: ('example-2', True), 3: ('example-3', False)})
        media = Mock(spec=MediaMarktSource)
        media.discover.side_effect = lambda name, specs: [{'source_url': 'https://www.mediamarkt.com.tr/tr/product/_phone-123456.html'}] if name == 'Example 3' else []
        media.report.return_value = {'sitemap_status': 'ready'}
        reader = _Refresher()
        reader.mediamarkt_source = media
        vatan = Mock()
        vatan.discover.return_value = []
        vatan.report.return_value = {}
        updater = PriceUpdater(client, _Discovery(), reader, vatan_sitemap_source=vatan)
        async def no_delay(_):
            return None
        with patch('update_prices.asyncio.sleep', new=no_delay):
            with self.assertRaisesRegex(RuntimeError, 'coverage 0.0%'):
                asyncio.run(updater.run_update(dry_run=True))
        self.assertEqual([p['slug'] for p in updater.last_report['products']],
                         ['example-3', 'example-2', 'example-1'])
        self.assertEqual(updater.last_report['discovery_priority']['retailer_candidate_models'], 1)
        media.configure_catalog_budget.assert_called_once_with(3)

    def test_public_snapshot_is_a_bounded_order_hint_without_bearer(self):
        session = _Session([
            {'id': 1, 'slug': 'a', 'fresh_lowest_price': None, 'fresh_offer_count': 0},
            {'id': 2, 'slug': 'b', 'fresh_lowest_price': 10000, 'fresh_offer_count': 1},
            {'id': 3, 'slug': 'c', 'fresh_lowest_price': -1, 'fresh_offer_count': 0},
            {'id': 4, 'slug': 'd', 'fresh_lowest_price': 10000},
        ])
        client = OfferIngestionClient('https://teknoskor.com', 's' * 32,
                                      session=session, sleep=lambda _: None)
        self.assertEqual(client.get_public_price_priority(),
                         {1: ('a', False), 2: ('b', True)})
        self.assertEqual(session.calls[0][1], 'https://teknoskor.com/api/products')
        self.assertEqual(session.calls[0][2]['params'], {'limit': '1000'})
        self.assertNotIn('Authorization', session.calls[0][2]['headers'])

    def test_stable_priority_requires_matching_id_and_slug(self):
        products = [_phone(i) for i in (1, 2, 3, 4, 5)]
        status = {1: ('example-1', True), 2: ('example-2', False),
                  3: ('other-slug', False), 4: ('example-4', False),
                  5: ('example-5', True)}
        ordered, counts = prioritize_unpriced_products(products, status)
        self.assertEqual([p['id'] for p in ordered], [2, 4, 1, 5, 3])
        self.assertEqual(counts, {'unpriced_first': 2,
                                  'priced_after': 2, 'unknown_last': 1})

    def test_full_discovery_uses_hint_but_scoped_audits_keep_request_order(self):
        products = [_phone(i) for i in (1, 2, 3)]
        client = _Catalog(products, {1: ('example-1', True),
                                     2: ('example-2', False),
                                     3: ('example-3', False)})
        source = Mock()
        source.discover.return_value = []
        source.report.return_value = {'status': 'unavailable'}
        updater = PriceUpdater(client, _Discovery(), _Refresher(),
                               vatan_sitemap_source=source)

        async def no_delay(_):
            return None

        with patch('update_prices.asyncio.sleep', new=no_delay):
            with self.assertRaisesRegex(RuntimeError, 'coverage 0.0%'):
                asyncio.run(updater.run_update(dry_run=True))
            self.assertEqual([p['slug'] for p in updater.last_report['products']],
                             ['example-2', 'example-3', 'example-1'])
            self.assertEqual(updater.last_report['discovery_priority']['unpriced_first'], 2)
            asyncio.run(updater.run_update(dry_run=True, known_pdp_only=True,
                                           product_slugs=['example-3', 'example-1']))
            self.assertEqual([p['slug'] for p in updater.last_report['products']],
                             ['example-1', 'example-3'])
            self.assertNotIn('discovery_priority', updater.last_report)

    def test_public_snapshot_outage_keeps_original_full_sync_order(self):
        products = [_phone(i) for i in (1, 2)]
        client = _Catalog(products, {})
        client.get_public_price_priority = Mock(side_effect=RuntimeError('public unavailable'))
        source = Mock()
        source.discover.return_value = []
        source.report.return_value = {'status': 'unavailable'}
        updater = PriceUpdater(client, _Discovery(), _Refresher(),
                               vatan_sitemap_source=source)

        async def no_delay(_):
            return None

        with patch('update_prices.asyncio.sleep', new=no_delay):
            with self.assertRaisesRegex(RuntimeError, 'coverage 0.0%'):
                asyncio.run(updater.run_update(dry_run=True))
        self.assertEqual([p['slug'] for p in updater.last_report['products']],
                         ['example-1', 'example-2'])
        self.assertEqual(updater.last_report['discovery_priority'],
                         {'source': 'original_catalog_order', 'reason': 'RuntimeError'})


if __name__ == '__main__':
    unittest.main()
