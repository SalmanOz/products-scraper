"""Read original retailer listings discovered on comparison sites for known earbuds."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from urllib.parse import urlsplit

from catalog_source_collector import catalog_document_reader
from competitor_catalog_gaps import read_state, retailer_destination, write_json
from retailer_barcode import retailer_gtin13
from tr_price_scraper import TRPriceScraper
from tws_catalog_expansion import exact_tws_sellers, reread
from tws_offer_pilot import COHORT, EXPANSION_COHORT, NEW_MODELS, model_name
from tws_refresh import selected_host


def identify(title):
    words = TRPriceScraper._normalize_words(title).split()
    if not words:
        return None
    brand = {'redmi': 'xiaomi', 'airpods': 'apple', 'soundcore': 'anker'}.get(words[0], words[0])
    if brand not in {row[0].casefold() for row in (*COHORT, *EXPANSION_COHORT, *NEW_MODELS)}:
        return None
    return model_name(title, brand)


def collect_leads(state, models=(), maximum=16):
    candidates = defaultdict(set)
    for row in state['products'].values():
        if row.get('category') != 'tws' or not row.get('retailerLeads'):
            continue
        name = identify(row['title'])
        if name is None or models and name not in models:
            continue
        for raw in row['retailerLeads']:
            url = retailer_destination(raw)
            if url and selected_host(url):
                candidates[url].add(name)
    groups = defaultdict(list)
    previous = state.get('earbudLeadAudits', {})
    for url, names in sorted(candidates.items(), key=lambda item: (previous.get(item[0], ''), item[0])):
        if len(names) == 1:
            groups[urlsplit(url).netloc].append((next(iter(names)), {'url': url}))
    return {host: rows[:maximum] for host, rows in groups.items()}


def recent_observations(state, now):
    observed = {}
    for raw, row in state.get('earbudLeadObservations', {}).items():
        url = retailer_destination(raw)
        if not url or url != raw or not selected_host(url) or not isinstance(row, dict):
            continue
        name, gtin = row.get('name'), row.get('gtin13')
        if not isinstance(name, str) or not isinstance(gtin, str) or retailer_gtin13(gtin) != gtin:
            continue
        try:
            checked = datetime.fromisoformat(row.get('checkedAt', ''))
            if checked.tzinfo is not None and now - timedelta(days=30) <= checked <= now:
                observed[url] = {'name': name, 'gtin13': gtin, 'checkedAt': checked.isoformat()}
        except (TypeError, ValueError):
            continue
    return observed


def add_catalog_targets(groups, snapshot, observations, maximum=8):
    # Re-read current and withdrawn targets for the models selected by this
    # run. Snapshot prices cannot supply one side of a verified pair.
    names = {name for rows in groups.values() for name, _ in rows}
    owners = defaultdict(set)
    for rows in groups.values():
        for name, target in rows:
            owners[target['url']].add(name)
    selected_gtins = defaultdict(set)
    for url, row in observations.items():
        if row['name'] in names and owners[url] == {row['name']}:
            selected_gtins[row['name']].add(row['gtin13'])
    known = defaultdict(set)
    targets = snapshot.get('sourceTargets', {})
    for model in snapshot.get('models', []):
        name = model['name']
        if name not in names:
            continue
        prior = targets.get(f"{name}/{model['gtin']}", targets.get(name, []))
        for target in (*model['offers'], *prior):
            url = retailer_destination(target['url'])
            if url and selected_host(url):
                known[name].add(url)
                owners[url].add(name)
    historical = defaultdict(set)
    for url, row in observations.items():
        name = row['name']
        if name in names:
            historical[name].add(url)
            owners[url].add(name)
    for host, rows in groups.items():
        groups[host] = [(name, target) for name, target in rows if owners[target['url']] == {name}]
    existing = {target['url'] for rows in groups.values() for _, target in rows}
    extra = defaultdict(list)
    for name in sorted(names):
        for url in sorted((known[name] | historical[name]) - existing):
            if owners[url] == {name}:
                same_gtin = observations.get(url, {}).get('gtin13') in selected_gtins[name]
                extra[selected_host(url)].append((not same_gtin, url not in known[name], name, url))
    for host, candidates in extra.items():
        by_model = Counter()
        ranked = []
        for same_gtin_missing, is_unpublished, name, url in sorted(candidates):
            ranked.append((same_gtin_missing, by_model[name], is_unpublished, name, url))
            by_model[name] += 1
        groups.setdefault(host, []).extend((name, {'url': url}) for *_, name, url in sorted(ranked)[:maximum])
    return known


def audit(state, models=(), snapshot=None):
    started = datetime.now(timezone.utc)
    history = recent_observations(state, started)
    groups = collect_leads(state, models)
    known = add_catalog_targets(groups, snapshot or {}, history)
    observations, failures = defaultdict(list), {}
    statuses = {host: {} for host in groups}
    n11_reader = catalog_document_reader('earbud-discovery')
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {host: pool.submit(reread, host, rows, statuses[host], discover=True,
                                  document_reader=n11_reader if host == 'www.n11.com' else None)
                for host, rows in groups.items()}
        for host, job in jobs.items():
            rows, rejects = job.result()
            failures[host] = dict(rejects)
            for model, offers in rows.items():
                observations[model].extend(offers)
    verified = []
    for name, offers in sorted(observations.items()):
        pair = exact_tws_sellers(offers)
        fields = ('url', 'platform', 'merchant', 'gtin13', 'price')
        by_gtin = defaultdict(list)
        for offer in offers:
            by_gtin[offer['gtin13']].append(offer)
        variants = []
        for gtin, current in sorted(by_gtin.items()):
            variant_pair = exact_tws_sellers(current)
            if variant_pair:
                variants.append({'gtin13': gtin, 'hasNewTarget': any(o['url'] not in known[name] for o in variant_pair),
                                 'offers': [{key: offer[key] for key in fields} for offer in variant_pair]})
        verified.append({'name': name, 'observations': [{key: offer[key] for key in fields} for offer in offers],
                         'verifiedPair': [{key: offer[key] for key in fields} for offer in pair],
                         'verifiedVariants': variants})
    now = datetime.now(timezone.utc).isoformat()
    for name, offers in observations.items():
        for offer in offers:
            history[offer['url']] = {'name': name, 'gtin13': offer['gtin13'], 'checkedAt': now}
    state['earbudLeadObservations'] = history
    state.setdefault('earbudLeadAudits', {}).update({url: now for rows in statuses.values() for url in rows})
    return {'sourceRequests': {host: len(rows) for host, rows in statuses.items()},
            'rejections': failures, 'verified': verified}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', action='append', default=[])
    parser.add_argument('--snapshot', type=Path, help='Use published retailer URLs as additional leads')
    args = parser.parse_args()
    state = read_state(args.state)
    snapshot = json.loads(args.snapshot.read_text(encoding='utf-8')) if args.snapshot else None
    result = audit(state, args.model, snapshot)
    write_json(args.state, state)
    write_json(args.output, result)
    print(json.dumps({'earbudLeadRequests': sum(result['sourceRequests'].values()),
                      'earbudModelsVerified': len(result['verified']),
                      'earbudPairsVerified': sum(bool(row['verifiedPair']) for row in result['verified']),
                      'earbudVariantsWithNewSellers': sum(variant['hasNewTarget'] for row in result['verified']
                                                         for variant in row['verifiedVariants'])}))


if __name__ == '__main__':
    main()
