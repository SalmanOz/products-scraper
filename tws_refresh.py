"""Refresh published earbud prices from their previously verified retailer URLs."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import urlsplit

from catalog_source_collector import catalog_document_reader
from nethouse_offer_source import HOST as NETHOUSE, nethouse_product_url
from pazarama_category_source import HOST as PAZARAMA, pazarama_product_url
from pttavm_offer_source import HOST as PTTAVM, ptt_product_url
from priority_retailer_transport import n11_product_selectors, product_host
from retailer_barcode import retailer_gtin13
from tws_catalog_expansion import MARKETPLACES, exact_tws_sellers, public_tws_offers, reread, tws_source_target
from tws_offer_pilot import _mediamarkt_tws_url, select_sku_offers
from tws_retailer_sources import _vatan_url

SITE_DOCUMENT_HOSTS = {
    'www.n11.com', 'www.vatanbilgisayar.com', 'www.mediamarkt.com.tr',
    'www.hepsiburada.com', 'www.teknosa.com', PAZARAMA, PTTAVM,
}
MIXED_SITE_DOCUMENT_HOSTS = {'www.n11.com', 'www.vatanbilgisayar.com', PAZARAMA, PTTAVM}
MAX_CURRENT_OFFERS = 4


def selected_host(url):
    host = urlsplit(url).netloc
    if host == 'www.n11.com':
        selectors = n11_product_selectors(url)
        return host if len(url) <= 500 and selectors is not None and 'dahili-hafiza' not in selectors else None
    if host in MARKETPLACES and product_host(url) == host:
        return host
    if host == PAZARAMA and pazarama_product_url(url):
        return host
    if host == PTTAVM and ptt_product_url(url):
        return host
    if host == NETHOUSE and nethouse_product_url(url):
        return host
    if host == 'www.mediamarkt.com.tr' and _mediamarkt_tws_url(url):
        return host
    if host == 'www.vatanbilgisayar.com' and _vatan_url(url):
        return host
    return None


def merge_snapshot(snapshot, catalog, observations, statuses, now, *, partial=False):
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
        fresh = (exact_tws_sellers(current, maximum=MAX_CURRENT_OFFERS) if required or partial
                 else select_sku_offers(current, maximum=MAX_CURRENT_OFFERS))
        if partial and not fresh:
            # A partial run can replace a variant only with a live pair.
            # All saved retailer URLs remain in sourceTargets for retry.
            output.append(row)
            counts['deferred'] += 1
            continue
        if fresh:
            offers = public_tws_offers(fresh)
            counts['refreshed'] += 1
        else:
            # A selected listing that failed verification is removed. For a
            # transport failure, keep its original timestamp and let the site's
            # existing freshness limit expire it.
            checked = {offer['url']: (offer['price'], offer.get('storePickup')) for offer in current}
            offers = [o for o in previous if statuses.get(o['url']) != 'unverified'
                      and checked.get(o['url'], (o['price'], o.get('storePickup'))) ==
                      (o['price'], o.get('storePickup'))]
            if required and len(offers) < 2:
                offers = []
            counts['retained' if offers == previous else 'withdrawn'] += 1
        output.append({**row, 'offers': offers})
    changed = output != snapshot['models']
    return ({**snapshot, 'checked_at': now, 'models': output} if changed else snapshot), counts


def refresh(snapshot, catalog, *, collection='site', allow_partial=False):
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
            known[offer['url']] = tws_source_target(offer, known.get(offer['url']))
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
    configured = sum(map(len, groups.values()))
    completed = sum(status != 'unavailable' for status in statuses.values())
    sufficient = configured > 0 and completed * 5 >= configured * 4
    partial = allow_partial and not sufficient
    now = datetime.now(timezone.utc).isoformat()
    updated, counts = merge_snapshot(snapshot, catalog, observations, statuses, now, partial=partial)
    if targets != snapshot.get('sourceTargets', {}) and (not partial or counts['refreshed']):
        updated = {**updated, 'sourceTargets': targets}
    publication_mode = ('full' if sufficient else 'verified_variants_only'
                        if partial and counts['refreshed'] else 'held')
    return updated, {**counts, 'rejections': dict(rejected), 'sourceCoverage': source_coverage,
                     'verifiedOffers': sum(map(len, observations.values())),
                     'completedTargetReads': completed, 'configuredTargets': configured,
                     'publicationMode': publication_mode,
                     'changed': updated is not snapshot}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('catalog', 'snapshot', 'output'):
        parser.add_argument(arg, type=Path)
    parser.add_argument('--collection', choices=('site', 'runner', 'mixed'), default='site')
    parser.add_argument('--allow-partial', action='store_true')
    args = parser.parse_args()
    catalog = json.loads(args.catalog.read_text(encoding='utf-8'))
    snapshot = json.loads(args.snapshot.read_text(encoding='utf-8'))
    updated, report = refresh(snapshot, catalog, collection=args.collection, allow_partial=args.allow_partial)
    args.output.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
