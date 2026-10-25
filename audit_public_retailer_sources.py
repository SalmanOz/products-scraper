"""GET-only runner proof of daily public-source discovery on a real catalog SKU.

No old targets, saved prices, legacy aggregator/browser fetches or ingestion
writes. The same PriceUpdater discovery and quality gates used daily are tested.
"""

import asyncio
import re

from offer_ingestion_client import OfferIngestionClient
from update_prices import PriceUpdater


class NoLegacyDiscovery:
    async def get_best_prices(self, *_args):
        return []


class ReadOnlyCatalog:
    def __init__(self, products):
        self.products = products

    def get_published_products(self):
        return self.products

    def ingest_offers(self, _rows):
        raise AssertionError('Public-source audit cannot write offers')


async def audit():
    catalog = OfferIngestionClient.from_env().get_published_products()
    selected = [p for p in catalog
                if re.fullmatch(r'(?:Apple\s+)?iPhone\s+16', p['name'], re.I)
                and PriceUpdater.get_expected_specs(p.get('attributes')).get('storage_gb') == 128]
    if len(selected) != 1:
        raise RuntimeError('Audit requires exactly one published iPhone 16 128GB catalog profile')
    # Only this audit suppresses old destinations, never the normal daily job.
    selected = [{**p, 'offer_targets': []} for p in selected]
    updater = PriceUpdater(ReadOnlyCatalog(selected), NoLegacyDiscovery())
    await updater.run_update(dry_run=True, product_slugs=[selected[0]['slug']])
    merchants = set(updater.last_report['products'][0]['purchasable_merchants'])
    expected = {'Troy', 'Pozitif Teknoloji'}
    if not expected.issubset(merchants):
        raise RuntimeError('Public sources not both verified in stock on runner: ' + ', '.join(sorted(expected - merchants)))
    print('Verified two new public retailers on actual published SKU; zero writes.')


if __name__ == '__main__':
    asyncio.run(audit())
