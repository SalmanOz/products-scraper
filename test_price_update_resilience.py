"""Offline regression test: transient scrape misses must not erase live prices."""

import asyncio
from unittest.mock import patch

from update_prices import PriceUpdater


class FakeIngestionClient:
    def __init__(self, rows):
        self.rows = rows
        self.submissions = []

    def get_published_products(self):
        return self.rows

    def ingest_offers(self, offers):
        self.submissions.append(offers)
        return {
            "accepted": 0,
            "stale_ignored": 0,
            "changed_paths": [],
            "committed": True,
        }


class EmptyPriceScraper:
    def __init__(self):
        self.calls = []

    async def get_best_prices(self, product_name, expected_specs=None):
        self.calls.append((product_name, expected_specs))
        return []


async def no_delay(_seconds):
    return None


def test_scrape_miss_preserves_last_known_price():
    ingestion_client = FakeIngestionClient([
        {
            'id': 7,
            'name': 'Samsung Galaxy A16 4G',
            'slug': 'samsung-galaxy-a16-4g',
            'attributes': {'ram_gb': 4, 'storage_gb': 128},
        }
    ])
    price_scraper = EmptyPriceScraper()
    updater = PriceUpdater(ingestion_client, price_scraper)

    with patch('update_prices.asyncio.sleep', new=no_delay):
        asyncio.run(updater.run_update(7))

    assert ingestion_client.submissions == []
    assert price_scraper.calls == [
        (
            'Samsung Galaxy A16 4G',
            {'ram_gb': 4, 'storage_gb': 128},
        )
    ]


if __name__ == '__main__':
    test_scrape_miss_preserves_last_known_price()
    print('✅ Transient scrape miss preserved the last known price')
