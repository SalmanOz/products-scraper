"""Systematic existing-catalog recovery, with fresh proof and readback per offer.

No sampling, catalog creation, price guessing, or deferred stale artifact replay.
One manual invocation exhausts the bounded source queue; writing is opt-in.
"""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

from audit_priority_retailers import inspect_target
from offer_ingestion_client import IngestionClientError, OfferIngestionClient
from priority_retailer_transport import HOSTS, PriorityRetailerTransport, product_host
from publish_priority_candidates import CandidatePublicationError, refresh_candidates
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
    if host == 'www.n11.com':
        from n11_catalog_source import catalog_request_url
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


def sweep(*, client, publish=False, transport=None, reader=None, checkpoint=None, discover_n11=False):
    products, prices = client.get_published_products(), client.get_public_price_priority()
    if (not products or len(products) > 200 or len({p['id'] for p in products}) != len(products)
            or len({p['slug'] for p in products}) != len(products)):
        raise ValueError('invalid_or_oversized_catalog')
    collector = getattr(client, 'n11_document_get', None)
    transport = transport or PriorityRetailerTransport(maximum_per_host=200, n11_collector=collector)
    reader = reader or RetailerOfferRefresher(n11_document_get=collector)
    if publish:
        for source in (reader.hepsiburada_catalog_source, reader.trendyol_catalog_source,
                       reader.teknosa_catalog_source, reader.n11_catalog_source):
            source.configure_catalog_budget(200)
    targets = catalog_queue(products, prices)
    result = {'purpose': 'systematic_priority_catalog_recovery', 'catalog_count': len(products),
              'publish': publish, 'writes': False, 'status': 'running', 'queued': len(targets),
              'rows': [], 'accepted': 0, 'discovery': [], 'models_without_priority_hints': sorted(
                  {p['slug'] for p in products} - {p['slug'] for _, p in targets})}
    publication_failed = False
    def process_target(url, product):
        nonlocal publication_failed
        row = inspect_target(transport, url, product)
        row['expected_specs'] = product.get('attributes') and {
            key: value for key, value in PriceUpdater.get_expected_specs(
                product['attributes']).items() if key in ('ram_gb', 'storage_gb')}
        result['rows'].append(row)
        if row.get('prepared_offer') is not None and publish and not publication_failed:
            if product['slug'] == 'samsung-galaxy-a37-5g':
                row['publication'] = {'status': 'protected_paired_workflow_required'}
            else:
                audit = {'purpose': 'automatic_priority_retailer_diagnosis', 'writes': False,
                         'catalog_count': len(products), 'rows': [row]}
                # Re-read immediately, hydrate current complete seller targets, atomically
                # ingest only this observation, and verify the commercial readback.
                try:
                    refreshed = refresh_candidates(audit, client=client, reader=reader)
                except CandidatePublicationError as error:
                    # Stop ALL further price writes, but finish the independent
                    # GET-only diagnosis so the rest of the catalog is not lost.
                    refreshed = error.result
                    publication_failed = True
                except IngestionClientError:
                    # No mutation has started: the fresh catalog/proof read
                    # was unavailable even after bounded retries. Preserve
                    # the rest of the independent source diagnosis.
                    refreshed = {'status': 'publication_preflight_unavailable', 'writes': False}
                    publication_failed = True
                row['publication'] = refreshed
                result['accepted'] += refreshed.get('ingestion', {}).get('accepted', 0)
                result['writes'] |= refreshed['writes']
        result['sources'] = transport.report()
        if checkpoint:
            checkpoint(result)
    try:
        for url, product in targets:
            process_target(url, product)
        if discover_n11:
            from n11_catalog_source import discover_catalog_targets
            from reviewed_offer_targets import with_reviewed_radio
            from tr_price_scraper import TRPriceScraper
            inspected = {row['observed_url'] for row in result['rows']}
            for product in sorted(products, key=lambda p: (prices.get(p['id'], ('', True))[1], p['id'])):
                specs = with_reviewed_radio(product, PriceUpdater.get_expected_specs(product.get('attributes')))
                storage = specs.get('storage_gb')
                model = TRPriceScraper._catalog_model_name(product['name'], specs)
                if not model or not storage:
                    continue
                query = f'{model} {storage} GB'
                html, reason, status = transport.read_n11_search(query)
                diagnostic = {'slug': product['slug'], 'retailer': 'n11', 'query': query,
                              'reason': reason, 'http_status': status, 'new_targets': []}
                result['discovery'].append(diagnostic)
                if html is not None:
                    candidates = discover_catalog_targets(html, product['name'], specs)
                    for candidate in candidates:
                        url = candidate['source_url']
                        if url in inspected:
                            continue
                        inspected.add(url)
                        diagnostic['new_targets'].append(candidate)
                        result['queued'] += 1
                        process_target(url, product)
                    if diagnostic['new_targets']:
                        result['models_without_priority_hints'] = [slug for slug in
                            result['models_without_priority_hints'] if slug != product['slug']]
                if checkpoint:
                    checkpoint(result)
        result['status'] = 'publication_failed' if publication_failed else 'completed'
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
    if result.get('discovery'):
        lines += ['', 'Automatic n11 discovery: ' + json.dumps({
            'catalog_queries': len(result['discovery']),
            'search_documents_read': sum(row['reason'] == 'document_read' for row in result['discovery']),
            'new_targets': sum(len(row['new_targets']) for row in result['discovery'])}, ensure_ascii=False)]
    if result.get('public_coverage'):
        lines += ['', 'Public coverage: ' + json.dumps(result['public_coverage'], ensure_ascii=False)]
    Path('priority-catalog-sweep.md').write_text('\n'.join(lines))


def coverage_readback(result, current):
    """An exhausted queue is operational completion, not sufficient coverage."""
    if not isinstance(current, dict) or len(current) != result['catalog_count']:
        return {'passed': False, 'reason': 'public_catalog_readback_changed_or_incomplete'}
    priced = sum(row[1] is True for row in current.values())
    return {'catalog': len(current), 'priced': priced, 'ratio': priced / len(current),
            'minimum_ratio': 0.5, 'passed': priced / len(current) >= 0.5,
            'documents_read': sum(row.get('reason') == 'document_read' for row in result['rows']),
            'source_cooldown_skips': sum(row.get('reason') == 'source_cooldown' for row in result['rows']),
            'source_circuit_skips': sum(row.get('reason') == 'source_circuit_open' for row in result['rows'])}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--n11-preflight', action='store_true')
    args = parser.parse_args()
    if args.n11_preflight:
        if args.publish:
            raise ValueError('The collector preflight is read-only')
        client = OfferIngestionClient.from_env()
        products = client.get_published_products()
        candidate = next((row for row in catalog_queue(products, {})
                          if product_host(row[0]) == 'www.n11.com'), None)
        if candidate is None:
            raise ValueError('No catalog n11 destination available')
        transport = PriorityRetailerTransport(maximum_per_host=1,
                                             n11_collector=client.n11_document_get)
        row = inspect_target(transport, candidate[0], candidate[1])
        result = {'purpose': 'n11_collector_readonly_preflight', 'catalog_count': len(products),
                  'status': 'completed' if row['reason'] == 'document_read' else 'failed',
                  'publish': False, 'writes': False, 'accepted': 0, 'queued': 1, 'rows': [row],
                  'models_without_priority_hints': [], 'sources': transport.report()}
        save_report(result)
        print(Path('priority-catalog-sweep.md').read_text())
        if result['status'] != 'completed':
            raise SystemExit('Dedicated source collector preflight failed; no writes')
        return
    visited = 0
    def checkpoint(result):
        nonlocal visited
        save_report(result)
        if len(result['rows']) > visited:
            visited = len(result['rows'])
            row = result['rows'][-1]
            print(json.dumps({'visited': visited, 'queued': result['queued'],
                'accepted': result['accepted'], 'slug': row['slug'], 'retailer': row['retailer'],
                'reason': row.get('reader_reason', row['reason']), 'http_status': row['http_status'],
                'expected_specs': row.get('expected_specs'), 'heading': row.get('heading'),
                'physical_facts': row.get('physical_facts'), 'publication': row.get('publication')},
                ensure_ascii=False), flush=True)
    client = OfferIngestionClient.from_env()
    result = sweep(client=client, publish=args.publish, checkpoint=checkpoint, discover_n11=True)
    result['public_coverage'] = coverage_readback(result, client.get_public_price_priority())
    save_report(result)
    print(Path('priority-catalog-sweep.md').read_text())
    if result['status'] != 'completed':
        raise SystemExit('Publication verification failed; remaining queue inspected without writes')
    if not result['public_coverage']['passed']:
        raise SystemExit('Public catalog coverage gate failed; queue completion alone is insufficient')


if __name__ == '__main__':
    main()
