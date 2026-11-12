"""Stage verified retailer additions for published earbud models."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

from catalog_source_collector import catalog_document_reader
from competitor_catalog_gaps import retailer_destination
from pttavm_offer_source import HOST as PTTAVM
from retailer_barcode import retailer_gtin13
from tws_catalog_expansion import common_color, exact_tws_sellers, public_tws_offers, reread, tws_source_target
from tws_refresh import MAX_CURRENT_OFFERS, selected_host


def stage(snapshot, catalog, report, selected_models=()):
    model_names = {row['name'] for row in catalog['models']}
    rows = {(row['name'], row['gtin']): row for row in snapshot['models']}
    model_counts = Counter(name for name, _ in rows)
    prior_targets = snapshot.get('sourceTargets', {})
    observed_targets, current_targets, url_owners, gtin_owners = (defaultdict(dict), defaultdict(dict),
                                                                defaultdict(set), defaultdict(set))
    for key, row in rows.items():
        name, gtin = key
        gtin_owners[gtin].add(name)
        target_key = f'{name}/{gtin}' if model_counts[name] > 1 else name
        for target in (*prior_targets.get(target_key, []), *row['offers']):
            url_owners[target['url']].add(key)
            current_targets[key][target['url']] = tws_source_target(
                target, current_targets[key].get(target['url']) or target)

    for record in report['verified']:
        name = record['name']
        if name not in model_names or (selected_models and name not in selected_models):
            continue
        for offer in record['observations']:
            gtin = offer.get('gtin13')
            url = retailer_destination(offer.get('url'))
            merchant = offer.get('merchant')
            if (not gtin or retailer_gtin13(gtin) != gtin or not url or not selected_host(url)
                    or not isinstance(merchant, str) or not merchant.strip() or len(merchant) > 100):
                continue
            key = (name, gtin)
            gtin_owners[gtin].add(name)
            url_owners[url].add(key)
            observed_targets[key][url] = tws_source_target({**offer, 'url': url})

    groups, attempted, reasons = defaultdict(list), [], Counter()
    for key, leads in observed_targets.items():
        name, gtin = key
        published = {o['url'] for o in rows.get(key, {}).get('offers', [])}
        if not (leads.keys() - published):
            continue
        if gtin_owners[gtin] != {name}:
            reasons['conflicting_barcode_owner'] += 1
            continue
        targets = {url: target for url, target in {**leads, **current_targets[key]}.items()
                   if url_owners[url] == {key} and selected_host(url)}
        if len(targets) < 2:
            reasons['insufficient_retailer_urls'] += 1
            continue
        attempted.append(key)
        for url, target in targets.items():
            groups[selected_host(url)].append((name, {**target, 'gtin13': gtin}))

    observed = defaultdict(list)
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = {host: pool.submit(reread, host, targets,
                                  document_reader=catalog_document_reader('earbud-discovery')
                                  if host in ('www.n11.com', PTTAVM) else None)
                for host, targets in groups.items()}
        for host, job in jobs.items():
            values, rejected = job.result()
            reasons.update({f'{host}:{reason}': count for reason, count in rejected.items()})
            for name, offers in values.items():
                for offer in offers:
                    observed[name, offer['gtin13']].append(offer)

    updated = deepcopy(snapshot)
    target_map = updated.setdefault('sourceTargets', {})
    staged, confirmed_colors = [], {}
    for name, gtin in attempted:
        key = (name, gtin)
        offers = exact_tws_sellers(observed[key], maximum=MAX_CURRENT_OFFERS)
        if len(offers) < 2:
            reasons['independent_pair_missing'] += 1
            continue
        previous = rows.get(key, {}).get('offers', [])
        if not ({o['url'] for o in offers} - {o['url'] for o in previous}):
            reasons['new_target_unverified'] += 1
            continue
        if (len(offers) < len(previous) or
                len({selected_host(o['url']) for o in offers}) < len({selected_host(o['url']) for o in previous})):
            reasons['current_offer_coverage_reduced'] += 1
            continue
        variants = [row for row in updated['models'] if row['name'] == name]
        row = next((row for row in variants if row['gtin'] == gtin), None)
        if row is None:
            if len(variants) == 1 and variants[0]['gtin'] is None and not variants[0]['offers']:
                row = variants[0]
                row['gtin'] = gtin
            else:
                if len(variants) == 1 and name in target_map:
                    target_map[f"{name}/{variants[0]['gtin']}"] = target_map.pop(name)
                row = {'name': name, 'gtin': gtin, 'requiresIndependentPair': True}
                updated['models'].append(row)
                variants.append(row)
        if 'variant' not in row and (color := common_color(offers)):
            confirmed_colors[key] = color
        row['offers'] = public_tws_offers(offers)
        target_key = f'{name}/{gtin}' if len(variants) > 1 else name
        targets = {o['url']: o for o in target_map.get(target_key, [])}
        for offer in offers:
            targets[offer['url']] = tws_source_target(offer, targets.get(offer['url']))
        target_map[target_key] = list(targets.values())
        updated['checked_at'] = max(updated['checked_at'], max(o['observed_at'] for o in offers))
        staged.append({'name': name, 'gtin': gtin, 'offers': row['offers']})
    variant_counts = Counter(row['name'] for row in updated['models'])
    for row in updated['models']:
        key = (row['name'], row['gtin'])
        if variant_counts[row['name']] > 1 and key in confirmed_colors:
            row['variant'] = confirmed_colors[key]
    return (updated if staged else snapshot), {'staged': staged, 'rejections': dict(reasons)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('catalog', 'snapshot', 'audit', 'output'):
        parser.add_argument(arg, type=Path)
    parser.add_argument('--model', action='append', default=[])
    args = parser.parse_args()
    if args.output.resolve() in {path.resolve() for path in (args.catalog, args.snapshot, args.audit)}:
        parser.error('Use a separate output path for the staged snapshot')
    original = args.snapshot.read_bytes()
    snapshot = json.loads(original)
    catalog, report = (json.loads(path.read_text(encoding='utf-8')) for path in (args.catalog, args.audit))
    updated, results = stage(snapshot, catalog, report, args.model)
    payload = (json.dumps(updated, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(payload)
    results.update(baseSnapshotSha256=sha256(original).hexdigest(), stagedSnapshotSha256=sha256(payload).hexdigest())
    print(json.dumps(results, ensure_ascii=False))


if __name__ == '__main__':
    main()
