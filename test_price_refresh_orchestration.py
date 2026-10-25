"""Offline PDP-refresh/discovery orchestration tests; no production credentials."""

import asyncio
from copy import deepcopy
import os
import socket
import unittest
from unittest.mock import patch

import requests

from update_prices import PriceUpdater


NOW = "2026-10-19T12:00:00Z"
PDP_OBSERVED_AT = "2026-10-19T11:58:00Z"
TITLE = "Samsung Galaxy S25 12GB RAM 256GB"
MERCHANTS = [
    ("Teknosa", "www.teknosa.com"),
    ("Trendyol", "www.trendyol.com"),
    ("Hepsiburada", "www.hepsiburada.com"),
    ("Amazon TR", "www.amazon.com.tr"),
]


def offer(index=0, *, title=TITLE, availability="in_stock", observed=True):
    merchant, host = MERCHANTS[index]
    row = {
        "merchant": merchant,
        "price": 20000 + index * 500,
        "currency": "TRY",
        "availability": availability,
        "source_title": title,
        "url": f"https://{host}/samsung-galaxy-s25-12gb-ram-256gb",
    }
    if observed:
        row["observed_at"] = PDP_OBSERVED_AT
    return row


def product():
    return {
        "id": 42,
        "name": "Samsung Galaxy S25",
        "slug": "samsung-galaxy-s25",
        "attributes": {
            "ram_gb": 12,
            "storage_gb": 256,
            "Connectivity": {"5G": "n1 (2100), n78 (3500)"},
        },
        "offer_targets": [
            {
                "merchant_name": merchant,
                "source_url": offer(index)["url"],
                "checked_at": "2001-01-01T00:00:00Z",
            }
            for index, (merchant, _host) in enumerate(MERCHANTS)
        ],
    }


class FakeIngestionClient:
    def __init__(self, catalog):
        self.catalog = deepcopy(catalog)
        self.submissions = []

    def get_published_products(self):
        return deepcopy(self.catalog)

    def ingest_offers(self, rows):
        self.submissions.append(deepcopy(rows))
        return {
            "accepted": len(rows), "stale_ignored": 0,
            "variant_rejected": 0, "variant_rejections": [],
            "changed_paths": [], "committed": True,
        }


class FakeRefresher:
    def __init__(self, rows=(), error=None, discovery_rows=()):
        self.rows = deepcopy(list(rows))
        self.discovery_rows = deepcopy(list(discovery_rows))
        self.error = error
        self.calls = []

    def refresh_existing_offer_targets(self, name, targets, expected_specs):
        self.calls.append((name, deepcopy(targets), deepcopy(expected_specs)))
        if self.error is not None and len(self.calls) == 1:
            raise self.error
        return deepcopy(self.rows if len(self.calls) == 1 else self.discovery_rows)


class FakeDiscovery:
    def __init__(self, rows=()):
        self.rows = deepcopy(list(rows))
        self.calls = []

    async def get_best_prices(self, name, expected_specs=None):
        self.calls.append((name, deepcopy(expected_specs)))
        return deepcopy(self.rows)


async def no_delay(_seconds):
    return None


def network_forbidden(*_args, **_kwargs):
    raise AssertionError("Offline orchestration test attempted real network access")


class PriceRefreshOrchestrationTests(unittest.TestCase):
    def setUp(self):
        for patcher in [
            patch.dict(os.environ, {}, clear=True),
            patch.object(socket.socket, "connect", network_forbidden),
            patch.object(requests.sessions.Session, "request", network_forbidden),
            patch("update_prices.OfferIngestionClient.from_env", side_effect=network_forbidden),
            patch("update_prices.submit_urls", side_effect=network_forbidden),
            patch("update_prices.asyncio.sleep", new=no_delay),
            patch("update_prices.PriceUpdater.utc_timestamp", return_value=NOW),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def updater(self, refreshed=(), discovered=(), error=None):
        client = FakeIngestionClient([product()])
        verified_discovery = [{**row, 'observed_at': PDP_OBSERVED_AT} for row in discovered]
        refresher = FakeRefresher(refreshed, error, verified_discovery)
        discovery = FakeDiscovery(discovered)
        return PriceUpdater(client, discovery, refresher), client, refresher, discovery

    def test_four_valid_fresh_merchants_skip_discovery(self):
        updater, client, refresher, discovery = self.updater([offer(i) for i in range(4)])
        result = asyncio.run(updater.run_update())
        self.assertEqual(result["accepted"], 4)
        self.assertEqual(len(refresher.calls), 1)
        self.assertEqual(discovery.calls, [])
        self.assertEqual(refresher.calls[0][2], {
            "ram_gb": 12, "storage_gb": 256, "network_generation": "5g",
        })
        rows = client.submissions[0]
        self.assertEqual(len({row["seller"] for row in rows}), 4)
        self.assertTrue(all(row["source_title"] == TITLE for row in rows))
        self.assertTrue(all(row["checked_at"] == PDP_OBSERVED_AT for row in rows))

    def test_fewer_refreshed_merchants_fall_back_to_discovery(self):
        updater, client, _refresher, discovery = self.updater(
            [offer(0), offer(1)], [offer(2, observed=False)],
        )
        asyncio.run(updater.run_update())
        self.assertEqual(len(discovery.calls), 1)
        self.assertEqual({row["seller"] for row in client.submissions[0]},
                         {"Teknosa", "Trendyol", "Hepsiburada"})
        discovered = next(row for row in client.submissions[0] if row["seller"] == "Hepsiburada")
        self.assertEqual(discovered["checked_at"], PDP_OBSERVED_AT)
        self.assertEqual(len(_refresher.calls), 2)

    def test_refresher_exception_does_not_suppress_discovery(self):
        updater, client, refresher, discovery = self.updater(
            discovered=[offer(0, observed=False)], error=RuntimeError("Offline fixture failure"),
        )
        result = asyncio.run(updater.run_update())
        self.assertEqual(len(refresher.calls), 2)
        self.assertEqual(len(discovery.calls), 1)
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(client.submissions[0][0]["source_title"], TITLE)

    def test_direct_out_of_stock_overrides_same_url_search_and_does_not_count_as_coverage(self):
        direct = offer(0, availability="out_of_stock")
        search = {**offer(0, observed=False), "price": 19000}
        updater, client, _refresher, discovery = self.updater([direct], [search])
        with self.assertRaisesRegex(RuntimeError, "coverage 0.0% is below the required 50%"):
            asyncio.run(updater.run_update())
        self.assertEqual(len(discovery.calls), 1)
        self.assertEqual(len(client.submissions), 1)
        self.assertEqual(len(client.submissions[0]), 1)
        record = client.submissions[0][0]
        self.assertEqual(record["availability"], "out_of_stock")
        self.assertEqual(record["price"], direct["price"])
        self.assertEqual(record["source_url"], search["url"])
        self.assertEqual(record["checked_at"], PDP_OBSERVED_AT)

    def test_stale_url_seed_alone_cannot_create_price_or_renew_checked_at(self):
        updater, client, refresher, discovery = self.updater()
        catalog_before = deepcopy(client.catalog)
        with self.assertRaisesRegex(RuntimeError, "coverage 0.0% is below the required 50%"):
            asyncio.run(updater.run_update())
        self.assertEqual(len(refresher.calls), 1)
        self.assertEqual(len(discovery.calls), 1)
        self.assertEqual(client.submissions, [])
        self.assertEqual(client.catalog, catalog_before)
        self.assertEqual([r['source_url'] for r in refresher.calls[0][1]],
                         [r['source_url'] for r in catalog_before[0]['offer_targets']])

    def test_search_price_alone_cannot_renew_or_replace_a_real_offer(self):
        updater, client, refresher, _discovery = self.updater([offer(0)], [offer(1, observed=False)])
        refresher.discovery_rows = []  # blocked/missing merchant PDP
        result = asyncio.run(updater.run_update())
        self.assertEqual(result['accepted'], 1)
        self.assertEqual([r['seller'] for r in client.submissions[0]], ['Teknosa'])

    def test_unknown_stock_and_preorder_do_not_count_toward_four_merchants(self):
        for state in ['unknown', 'preorder']:
            with self.subTest(state=state):
                updater, client, _refresher, discovery = self.updater(
                    [offer(i, availability=state) for i in range(4)], [offer(0, observed=False)])
                if state == 'preorder':
                    with self.assertRaisesRegex(RuntimeError, 'coverage 0.0%'):
                        asyncio.run(updater.run_update())
                else:
                    asyncio.run(updater.run_update())
                self.assertEqual(len(discovery.calls), 1)
                self.assertEqual(updater.last_report['coverage']['with_two_merchants'], 0)
                self.assertEqual(updater.last_report['coverage']['with_purchasable_offer'], 0 if state == 'preorder' else 1)

    def test_dry_run_and_known_pdp_mode_never_write_or_run_discovery(self):
        updater, client, _refresher, discovery = self.updater([offer(0), offer(1)])
        result = asyncio.run(updater.run_update(dry_run=True, known_pdp_only=True))
        self.assertEqual(client.submissions, [])
        self.assertEqual(result['accepted'], 0)
        self.assertEqual(discovery.calls, [])
        self.assertEqual(updater.last_report['coverage']['with_two_merchants'], 1)
        self.assertEqual(updater.last_report['mode'], 'audit')

    def test_stale_missing_or_future_observations_never_receive_a_new_timestamp(self):
        for timestamp in [None, '2001-01-01T00:00:00Z', '2026-10-20T12:00:00Z', 'invalid']:
            with self.subTest(timestamp=timestamp):
                row = {**offer(0), 'observed_at': timestamp}
                updater, client, _refresher, _discovery = self.updater([row])
                asyncio.run(updater.run_update(dry_run=True, product_slugs=['samsung-galaxy-s25'], known_pdp_only=True))
                self.assertEqual(client.submissions, [])
                self.assertEqual(updater.last_report['coverage']['with_purchasable_offer'], 0)

    def test_unknown_scoped_slug_fails_before_any_fetch_or_write(self):
        updater, client, refresher, discovery = self.updater([offer(0)])
        with self.assertRaisesRegex(RuntimeError, 'not found'):
            asyncio.run(updater.run_update(dry_run=True, product_slugs=['not-in-catalog']))
        self.assertEqual(client.submissions, [])
        self.assertEqual(refresher.calls, [])
        self.assertEqual(discovery.calls, [])

    def test_protected_budget_pair_loss_is_reported_even_when_one_store_coverage_is_100_percent(self):
        p = {'id': 279, 'slug': 'samsung-galaxy-a27-5g', 'name': 'Samsung Galaxy A27 5G',
             'attributes': {'ram_gb': 6, 'storage_gb': 128, 'Connectivity': {'5G': True}},
             'offer_targets': []}
        row = {**offer(0), 'source_title': 'Samsung Galaxy A27 5G 6GB RAM 128GB',
               'url': 'https://www.teknosa.com/samsung-galaxy-a27-5g-6gb-ram-128gb'}
        client = FakeIngestionClient([p])
        updater = PriceUpdater(client, FakeDiscovery(), FakeRefresher([row]))
        with self.assertRaisesRegex(RuntimeError, 'Protected budget models'):
            asyncio.run(updater.run_update(dry_run=True, known_pdp_only=True))
        self.assertEqual(client.submissions, [])
        self.assertEqual(updater.last_report['coverage']['with_purchasable_offer'], 1)
        self.assertEqual(updater.last_report['coverage']['protected_pair_failures'], [p['slug']])
        self.assertEqual(updater.last_report['status'], 'failed')

    def test_one_domain_cannot_count_as_two_merchants_by_forged_merchant_name(self):
        forged = {**offer(1), 'url': offer(0)['url']}
        updater, client, _refresher, _discovery = self.updater([offer(0), forged])
        asyncio.run(updater.run_update(dry_run=True, known_pdp_only=True))
        self.assertEqual(updater.last_report['coverage']['with_two_merchants'], 0)
        self.assertEqual(updater.last_report['products'][0]['rejected_live_observations'], 1)
        self.assertEqual(client.submissions, [])

    def test_wrong_model_or_ram_refreshes_do_not_count_or_bypass_discovery(self):
        for wrong_title, wrong_path in [
            ("Samsung Galaxy S24 12GB RAM 256GB", "samsung-galaxy-s24-12gb-ram-256gb"),
            ("Samsung Galaxy S25 8GB RAM 256GB", "samsung-galaxy-s25-8gb-ram-256gb"),
        ]:
            with self.subTest(title=wrong_title):
                invalid = [
                    {**offer(i, title=wrong_title), "url": f"https://{MERCHANTS[i][1]}/{wrong_path}"}
                    for i in range(4)
                ]
                updater, client, _refresher, discovery = self.updater(invalid, [offer(0, observed=False)])
                result = asyncio.run(updater.run_update())
                self.assertEqual(len(discovery.calls), 1)
                self.assertEqual(result["accepted"], 1)
                self.assertEqual(len(client.submissions[0]), 1)
                self.assertEqual(client.submissions[0][0]["source_title"], TITLE)
                self.assertEqual(client.submissions[0][0]["source_url"], offer(0)["url"])


if __name__ == "__main__":
    unittest.main()
