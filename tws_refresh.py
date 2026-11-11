"""Refresh published earbud prices from their previously verified retailer URLs."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import urlsplit

from catalog_source_collector import catalog_document_reader
from laptop_pilot import _public_offers
from priority_retailer_transport import product_host
from retailer_barcode import retailer_gtin13
from tws_catalog_expansion import MARKETPLACES, exact_tws_sellers, reread
from tws_offer_pilot import _mediamarkt_tws_url, select_sku_offers
from tws_retailer_sources import _vatan_url

SITE_DOCUMENT_HOSTS = {
    'www.n11.com', 'www.vatanbilgisayar.com', 'www.mediamarkt.com.tr',
    'www.hepsiburada.com', 'www.teknosa.com',
}
MIXED_SITE_DOCUMENT_HOSTS = {'www.n11.com', 'www.vatanbilgisayar.com'}
MAX_CURRENT_OFFERS = 4


def selected_host(url):
    host = urlsplit(url).netloc
    if host in MARKETPLACES and product_host(url) == host:
        return host
    if host == 'www.mediamarkt.com.tr' and _mediamarkt_tws_url(url):
        return host
    if host == 'www.vatanbilgisayar.com' and _vatan_url(url):
        return host
    return None


def merge_snapshot(snapshot, catalog, observations, statuses, now):
    models = {row['name']: row for row in catalog['models']}
    output = []
    counts = Counter()
    for row in snapshot['models']:
        name = row['name']
        previous = row['offers']
        if name not in models:
            raise ValueError(f'Missing detail page for {name}')
        current = [offer for offer in observations.get(name, []) if offer['gtin13'] == row['gtin']]
        required = row.get('requiresIndependentPair') is True or models[name].get('requiresIndependentPair') is True
        fresh = (exact_tws_sellers(current, maximum=MAX_CURRENT_OFFERS) if required
                 else select_sku_offers(current, maximum=MAX_CURRENT_OFFERS))
        if fresh:
            offers = _public_offers(fresh)
            counts['refreshed'] += 1
        else:
            # A selected listing that failed verification is removed. For a
            # transport failure, keep its original timestamp and let the site's
            # existing freshness limit expire it.
            prices = {offer['url']: offer['price'] for offer in current}
            offers = [o for o in previous if statuses.get(o['url']) != 'unverified'
                      and prices.get(o['url'], o['price']) == o['price']]
            if required and len(offers) < 2:
                offers = []
            counts['retained' if offers == previous else 'withdrawn'] += 1
        output.append({**row, 'offers': offers})
    changed = output != snapshot['models']
    return ({**snapshot, 'checked_at': now, 'models': output} if changed else snapshot), counts


def refresh(snapshot, catalog, *, collection='site'):
    groups = defaultdict(list)
    targets = {}
    previous_targets = dict(snapshot.get('sourceTargets', {}))
    model_counts = Counter(row['name'] for row in snapshot['models'])
    for row in snapshot['models']:
        gtin = row['gtin']
        name = row['name']
        target_key = f'{name}/{gtin}' if model_counts[name] > 1 else name
        legacy = previous_targets.pop(name, [])
        known = {o['url']: o for o in previous_targets.get(target_key, legacy)}
        for offer in row['offers']:
            known[offer['url']] = {key: offer[key] for key in ('url', 'merchant')}
        if known and retailer_gtin13(gtin) != gtin:
            raise ValueError(f'Invalid GTIN for {name}')
        if known:
            targets[target_key] = list(known.values())
        for url, offer in known.items():
            host = selected_host(url)
            if host is None:
                raise ValueError(f'Unrecognized public retailer URL: {url}')
            groups[host].append((name, {**offer, 'gtin13': gtin}))
    observations, statuses, rejected, source_coverage = defaultdict(list), {}, Counter(), {}
    gateway_hosts = {
        'site': SITE_DOCUMENT_HOSTS, 'mixed': MIXED_SITE_DOCUMENT_HOSTS, 'runner': set(),
    }[collection]

    def collect(host, targets):
        states = {}
        gateway = catalog_document_reader('earbud') if host in gateway_hosts else None
        found, failures = reread(host, targets, states, document_reader=gateway)
        return found, failures, states

    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = {host: pool.submit(collect, host, rows) for host, rows in groups.items()}
        for host, job in jobs.items():
            found, failures, states = job.result()
            rejected.update(failures)
            statuses.update(states)
            source_coverage[host] = {
                'configuredTargets': len(groups[host]),
                'completedTargetReads': sum(status != 'unavailable' for status in states.values()),
                'verifiedOffers': sum(map(len, found.values())),
                'rejections': dict(failures),
            }
            for name, offers in found.items():
                observations[name].extend(offers)
    now = datetime.now(timezone.utc).isoformat()
    updated, counts = merge_snapshot(snapshot, catalog, observations, statuses, now)
    if targets != snapshot.get('sourceTargets', {}):
        updated = {**updated, 'sourceTargets': targets}
    return updated, {**counts, 'rejections': dict(rejected), 'sourceCoverage': source_coverage,
                     'verifiedOffers': sum(map(len, observations.values())),
                     'completedTargetReads': sum(status != 'unavailable' for status in statuses.values()),
                     'configuredTargets': sum(map(len, groups.values())),
                     'changed': updated is not snapshot}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('catalog', 'snapshot', 'output'):
        parser.add_argument(arg, type=Path)
    parser.add_argument('--collection', choices=('site', 'runner', 'mixed'), default='site')
    args = parser.parse_args()
    catalog = json.loads(args.catalog.read_text(encoding='utf-8'))
    snapshot = json.loads(args.snapshot.read_text(encoding='utf-8'))
    updated, report = refresh(snapshot, catalog, collection=args.collection)
    args.output.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
