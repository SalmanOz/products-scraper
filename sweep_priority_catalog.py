"""Systematic existing-catalog recovery, with fresh proof and readback per offer.

No sampling, catalog creation, price guessing, or deferred stale artifact replay.
One manual invocation exhausts the bounded source queue; writing is opt-in.
"""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

from audit_priority_retailers import inspect_target
from offer_ingestion_client import OfferIngestionClient
from priority_retailer_transport import HOSTS, PriorityRetailerTransport, product_host
from publish_priority_candidates import refresh_candidates
from retailer_offer_refresh import RetailerOfferRefresher
from update_prices import PriceUpdater


def observed_url(url):
    host = product_host(url)
    if host == 'www.hepsiburada.com':
        from hepsiburada_catalog_source import catalog_request_url
        return catalog_request_url(url)
    if host == 'www.trendyol.com':
        from trendyol_catalog_source import catalog_request_url
        return catalog_request_url(url)
    return url


def catalog_queue(products, prices):
    """Visit every model before additional colors, excluding ambiguous owners."""
    owners, groups = defaultdict(dict), defaultdict(list)
    for product in products:
        for target in product.get('offer_targets', []):
            url = target.get('source_url') if isinstance(target, dict) else None
            if product_host(url):
                owners[observed_url(url)][product['id']] = product
    for url, matches in owners.items():
        if len(matches) == 1:
            product = next(iter(matches.values()))
            groups[(product_host(url), product['id'])].append((url, product))
    rows = []
    for group in groups.values():
        for rank, (url, product) in enumerate(sorted(group, key=lambda row: row[0])):
            rows.append((rank, url, product))
    # The stable ID order does not shift models out of the queue when prices change.
    rows.sort(key=lambda row: (row[0], prices.get(row[2]['id'], ('', True))[1],
                              row[2]['id'], list(HOSTS).index(product_host(row[1])), row[1]))
    return [(url, product) for _, url, product in rows]


def sweep(*, client, publish=False, transport=None, reader=None, checkpoint=None):
    products, prices = client.get_published_products(), client.get_public_price_priority()
    if (not products or len(products) > 200 or len({p['id'] for p in products}) != len(products)
            or len({p['slug'] for p in products}) != len(products)):
        raise ValueError('invalid_or_oversized_catalog')
    transport = transport or PriorityRetailerTransport(maximum_per_host=200)
    reader = reader or RetailerOfferRefresher()
    if publish:
        for source in (reader.hepsiburada_catalog_source, reader.trendyol_catalog_source,
                       reader.teknosa_catalog_source):
            source.configure_catalog_budget(200)
    targets = catalog_queue(products, prices)
    result = {'purpose': 'systematic_priority_catalog_recovery', 'catalog_count': len(products),
              'publish': publish, 'writes': False, 'status': 'running', 'queued': len(targets),
              'rows': [], 'accepted': 0, 'models_without_priority_hints': sorted(
                  {p['slug'] for p in products} - {p['slug'] for _, p in targets})}
    try:
        for url, product in targets:
            row = inspect_target(transport, url, product)
            row['expected_specs'] = product.get('attributes') and {
                key: value for key, value in PriceUpdater.get_expected_specs(
                    product['attributes']).items() if key in ('ram_gb', 'storage_gb')}
            result['rows'].append(row)
            if row.get('prepared_offer') is not None and publish:
                if product['slug'] == 'samsung-galaxy-a37-5g':
                    row['publication'] = {'status': 'protected_paired_workflow_required'}
                else:
                    audit = {'purpose': 'automatic_priority_retailer_diagnosis', 'writes': False,
                             'catalog_count': len(products), 'rows': [row]}
                    # Re-read immediately, hydrate current complete seller targets, atomically
                    # ingest only this observation, and verify the commercial readback.
                    refreshed = refresh_candidates(audit, client=client, reader=reader)
                    row['publication'] = refreshed
                    result['accepted'] += refreshed.get('ingestion', {}).get('accepted', 0)
                    result['writes'] |= refreshed['writes']
            result['sources'] = transport.report()
            if checkpoint:
                checkpoint(result)
        result['status'] = 'completed'
    except Exception as error:
        result.update(status='failed', error_type=type(error).__name__)
        if checkpoint:
            checkpoint(result)
        raise
    finally:
        result['sources'] = transport.report()
    if checkpoint:
        checkpoint(result)
    return result


def save_report(result):
    Path('priority-catalog-sweep.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    counts = Counter(row.get('reader_reason', row.get('reason')) for row in result['rows'])
    lines = ['## Systematic priority retailer recovery',
             f"Status: {result['status']}; catalog: {result['catalog_count']}; "
             f"visited: {len(result['rows'])}/{result['queued']}; accepted: {result['accepted']}.",
             '', '| Model | Retailer | HTTP | Parser | Publication |', '|---|---|---:|---|---|']
    for row in result['rows']:
        lines.append(f"| {row['slug']} | {row['retailer']} | {row['http_status']} | "
                     f"{row.get('reader_reason', row['reason'])} | "
                     f"{row.get('publication', {}).get('status', '')} |")
    lines += ['', 'Reasons: ' + json.dumps(counts, ensure_ascii=False), '',
              'Sources: ' + json.dumps(result.get('sources'), ensure_ascii=False), '',
              'No-hint models: ' + ', '.join(result['models_without_priority_hints'])]
    Path('priority-catalog-sweep.md').write_text('\n'.join(lines))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    result = sweep(client=OfferIngestionClient.from_env(), publish=args.publish, checkpoint=save_report)
    print(Path('priority-catalog-sweep.md').read_text())


if __name__ == '__main__':
    main()
