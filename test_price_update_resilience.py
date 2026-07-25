"""Offline regression test: transient scrape misses must not erase live prices."""

import asyncio
from unittest.mock import patch

from update_prices import PriceUpdater


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append((statement, params))

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class FakeDatabase:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1

    def close(self):
        pass


class EmptyPriceScraper:
    def __init__(self):
        self.calls = []

    async def get_best_prices(self, product_name, expected_specs=None):
        self.calls.append((product_name, expected_specs))
        return []


async def no_delay(_seconds):
    return None


def test_scrape_miss_preserves_last_known_price():
    updater = PriceUpdater.__new__(PriceUpdater)
    updater.db = FakeDatabase()
    updater.cursor = FakeCursor([
        {
            'id': 7,
            'name': 'Samsung Galaxy A16 4G',
            'slug': 'samsung-galaxy-a16-4g',
            'base_price': 11499,
            'attributes': '{"ram_gb": 4, "storage_gb": 128}',
        }
    ])
    updater.price_scraper = EmptyPriceScraper()
    updater.ensure_connection = lambda *args, **kwargs: None

    with patch('update_prices.asyncio.sleep', new=no_delay):
        asyncio.run(updater.run_update())

    mutating_statements = [
        statement
        for statement, _params in updater.cursor.statements
        if statement.lstrip().upper().startswith(('UPDATE', 'DELETE', 'INSERT'))
    ]
    assert not mutating_statements
    assert updater.db.commits == 0
    assert updater.price_scraper.calls == [
        (
            'Samsung Galaxy A16 4G',
            {'ram_gb': 4, 'storage_gb': 128},
        )
    ]


if __name__ == '__main__':
    test_scrape_miss_preserves_last_known_price()
    print('✅ Transient scrape miss preserved the last known price')
