"""Plan monitor candidates, verify selected listings, and stage a local catalog."""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from itertools import combinations
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from time import perf_counter
from urllib.parse import urlsplit

from monitor_pilot import (
    CROSS_PLATFORM_MINIMUM, MAX_TARGETS, PILOT_SIZE, candidates, export_verified_pilot, group_targets,
    pair_rejection, read_targets, target_host,
)
from monitor_sources import mentions


MAX_STORE_BUDGET = 60
MAX_RESCAN_TARGETS = 360
OFFER_AGE = timedelta(hours=1)
SPEC_TOKEN = re.compile(r'(?:\d+x\d+|\d+(?:hz|ms|r|w|nit|hdmi|dp)|(?:hdr|rj|usb|fhd|qhd|hdmi|typec)\d+)\Z')


def read_json(path):
    return json.loads(path.read_text())


def write_json(path, document, *, private=False):
    if private:
        handle = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w', encoding='utf-8')
    else:
        handle = path.open('x', encoding='utf-8')
    with handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write('\n')


def safe_target(code, gtin, url):
    row = {'code': code, 'gtin': gtin, 'url': url}
    return row if target_host(row) else None


def historical_target(row, known_codes):
    gtin, title = row.get('gtin13'), row.get('source_title')
    if not isinstance(title, str):
        return None
    code = known_codes.get(gtin)
    if code:
        if not mentions(title, code):
            return None
    else:
        codes = {
            code for code in row.get('_codes', [])
            if isinstance(code, str) and re.search('[a-z]', code) and not SPEC_TOKEN.fullmatch(code)
            and mentions(title, code)
        }
        if len(codes) != 1:
            return None
        code = codes.pop()
    # Keep the selected seller URL for n11. Other observations retain the
    # original PDP destination and must prove the current selected seller again.
    url = row.get('url') if row.get('platform') == 'n11' else row.get('input')
    return safe_target(code, gtin, url)


def make_plan(seeds, historical, published, *, model_count, store_budget):
    if not 1 <= model_count <= MAX_TARGETS or not 1 <= store_budget <= MAX_STORE_BUDGET:
        raise ValueError('Invalid model count or per-store PDP budget')
    grouped = defaultdict(dict)
    rejected = Counter()
    codes, urls = {}, {}
    published_urls = {offer['url'] for model in published for offer in model['offers']}

    def add(row):
        if row is None:
            rejected['invalid_or_ambiguous_target'] += 1
            return
        gtin, code, url = row['gtin'], row['code'], row['url']
        if (gtin in codes and codes[gtin] != code) or (url in urls and urls[url] != gtin):
            rejected['conflicting_target_identity'] += 1
            return
        if url in urls:
            rejected['duplicate_url'] += 1
            return
        codes[gtin], urls[url] = code, gtin
        host = urlsplit(url).hostname
        current = grouped[gtin].get(host)
        if current is None or url in published_urls and current['url'] not in published_urls:
            grouped[gtin][host] = row

    for row in seeds:
        add(safe_target(row.get('code'), row.get('gtin'), row.get('url')))
    for row in historical:
        add(historical_target(row, codes))

    published_gtins = {model['gtin'] for model in published}
    ordered = sorted(grouped, key=lambda gtin: (gtin not in published_gtins, -len(grouped[gtin]), gtin))
    if len(ordered) < model_count:
        raise ValueError(f'Only {len(ordered)} distinct candidate GTINs for {model_count} requested models')
    selected = ordered[:model_count]
    if published_gtins - set(selected):
        raise ValueError('The plan must include all published monitor GTINs')

    used = Counter()
    targets = defaultdict(list)
    # Give every model one read before assigning second platforms. Within
    # that pass, fill the most constrained models first.
    allocation_order = sorted(selected, key=lambda gtin: len(grouped[gtin]))
    for gtin in allocation_order + selected:
        if sum(used.values()) == MAX_TARGETS:
            break
        options = [
            (host, row) for host, row in grouped[gtin].items()
            if used[host] < store_budget and row not in targets[gtin]
        ]
        if options:
            host, row = min(options, key=lambda item: (item[1]['url'] not in published_urls, used[item[0]], item[0]))
            targets[gtin].append(row)
            used[host] += 1
    if any(not targets[gtin] for gtin in selected):
        raise ValueError('Store budgets could not allocate a PDP read to every selected model')

    return {
        'model_count': model_count,
        'store_budget': store_budget,
        'total_budget': MAX_TARGETS,
        'discovery': {'eligible_gtins': len(grouped), 'rejected': dict(rejected)},
        'candidates': [
            {'gtin': gtin, 'code': codes[gtin], 'published': gtin in published_gtins,
             'available_platforms': len(grouped[gtin]), 'targets': targets[gtin]}
            for gtin in selected
        ],
    }


def expand_plan(base, hints, previous_report, *, store_budget=MAX_STORE_BUDGET):
    """Retain the original GTIN cohort and spend extra reads on distinct sources first."""
    if not 1 <= store_budget <= MAX_STORE_BUDGET:
        raise ValueError('Invalid per-store PDP budget')
    models = {model['gtin']: model for model in base['candidates']}
    options = defaultdict(dict)
    identities = defaultdict(set)
    for row in hints:
        model = models.get(row.get('gtin'))
        if model and model['code'] == row.get('code') and target_host(row):
            identities[row['url']].add(row['gtin'])
            options[row['gtin']][row['url']] = row
    old_urls = {row['url'] for model in models.values() for row in model['targets']}
    for model in models.values():
        for row in model['targets']:
            options[model['gtin']][row['url']] = row
    outcomes = {row['url']: row['outcome'] for model in previous_report['models'] for row in model['targets']}
    store_reports = previous_report['stores']
    used, selected = Counter(), defaultdict(list)
    used_urls = set()
    planned_hosts = defaultdict(set)

    def eligible(row):
        url = row['url']
        return (url not in used_urls and used[target_host(row)] < store_budget
                and (url in old_urls or len(identities[url]) == 1))

    def rank(row, gtin):
        host = target_host(row)
        status = outcomes.get(row['url'])
        store = store_reports.get(host, {})
        parent = host == 'www.hepsiburada.com' and '-pm-' in urlsplit(row['url']).path
        return (host in planned_hosts[gtin], status != 'verified',
                bool(store.get('transport', {}).get('blocked')), status not in (None, 'verified'),
                used[host] / store_budget, row['url'] not in old_urls, parent, len(row['url']), row['url'])

    # Put the least flexible candidates first on every pass, then distribute
    # alternate seller listings within the remaining request allowance.
    order = sorted(models, key=lambda gtin: len({target_host(row) for row in options[gtin].values()}))
    while len(used_urls) < MAX_RESCAN_TARGETS:
        before = len(used_urls)
        for gtin in order:
            if len(used_urls) == MAX_RESCAN_TARGETS:
                break
            choices = [row for row in options[gtin].values() if eligible(row)]
            if not choices:
                continue
            row = min(choices, key=lambda row: rank(row, gtin))
            host = target_host(row)
            selected[gtin].append(row)
            used[host] += 1
            used_urls.add(row['url'])
            planned_hosts[gtin].add(host)
        if before == len(used_urls):
            break
    if any(not selected[gtin] for gtin in models):
        raise ValueError('Store budgets could not retain the fixed cohort')
    return {
        'model_count': base['model_count'], 'store_budget': store_budget, 'total_budget': MAX_RESCAN_TARGETS,
        'discovery': {**base['discovery'], 'fixed_cohort': True,
                      'additional_target_urls': len(used_urls - old_urls)},
        'previously_verified_urls': sorted(url for url in used_urls if outcomes.get(url) == 'verified'),
        'candidates': [dict(model, available_platforms=len(planned_hosts[gtin]), targets=selected[gtin])
                       for gtin, model in models.items()],
    }


def verify_plan(plan):
    planned_models = plan['candidates']
    gtins = {model['gtin'] for model in planned_models}
    budget = plan['store_budget']
    if len(gtins) != plan['model_count'] or len(planned_models) != len(gtins) or not 1 <= budget <= MAX_STORE_BUDGET:
        raise ValueError('Invalid candidate plan')
    rows = [row for model in planned_models for row in model['targets']]
    if any(
        not model['targets'] or any(row['gtin'] != model['gtin'] or row['code'] != model['code'] for row in model['targets'])
        for model in planned_models
    ):
        raise ValueError('Targets must belong to their planned model')
    total_budget = plan.get('total_budget', MAX_TARGETS)
    if total_budget not in {MAX_TARGETS, MAX_RESCAN_TARGETS}:
        raise ValueError('Invalid overall PDP budget')
    by_host = group_targets(rows, total_budget)
    if any(len(targets) > budget for targets in by_host.values()):
        raise ValueError('The plan exceeds its per-store PDP budget')

    def verify_host(item):
        host, targets = item
        if host == 'www.incehesap.com':
            known = set(plan.get('previously_verified_urls', []))
            targets = sorted(targets, key=lambda row: row['url'] not in known)
        audit = {'attempted': 0, 'outcomes': {}}
        started = perf_counter()
        offers, rejections = read_targets(host, targets, retry_transient=False, audit=audit)
        report = {
            'pdp_budget': budget, 'planned': len(targets), 'attempted': audit['attempted'],
            'verified': len(offers), 'not_attempted': len(targets) - audit['attempted'],
            'rejected': dict(rejections), 'transport': audit.get('transport', {}),
            'seconds': round(perf_counter() - started, 2),
        }
        print(json.dumps({'host': host, **report}, ensure_ascii=False), flush=True)
        return host, report, offers, audit['outcomes']

    started = perf_counter()
    stores, grouped, outcomes = {}, defaultdict(list), {}
    with ThreadPoolExecutor(max_workers=min(8, len(by_host))) as pool:
        for host, report, offers, reasons in pool.map(verify_host, by_host.items()):
            stores[host] = report
            outcomes.update(reasons)
            for offer in offers:
                grouped[offer['gtin13']].append(offer)
    models = []
    model_reasons = Counter()
    pair_failures = Counter()
    for model in planned_models:
        offers = grouped[model['gtin']]
        cohort = next(candidates(offers), ())
        reason = 'independent_pair' if len(cohort) == 2 else 'verified_without_independent_pair' if offers else 'no_verified_offer'
        model_reasons[reason] += 1
        pair_reasons = []
        if len(cohort) == 1:
            pair_reasons = sorted({pair_rejection(a, b) for a, b in combinations(offers, 2)
                                   if a['platform'] != b['platform']}) or ['no_second_verified_platform']
            pair_failures.update(pair_reasons)
        models.append({
            'gtin': model['gtin'], 'code': model['code'], 'published': model['published'],
            'outcome': reason, 'available_platforms': model['available_platforms'],
            'pair_rejections': pair_reasons,
            'targets': [{'url': row['url'], 'outcome': outcomes[row['url']]} for row in model['targets']],
        })
    new_models = [model for model in models if not model['published']]
    count = len(models)
    priced = count - model_reasons['no_verified_offer']
    pairs = model_reasons['independent_pair']
    rejections = Counter(reason for reason in outcomes.values()
                         if reason not in {'verified', 'not_attempted_source_stopped'})
    checked_at = datetime.now(timezone.utc).isoformat()
    summary = {
        'checked_at': checked_at,
        'discovery': plan['discovery'],
        'pdp_budget': total_budget,
        'pdp_planned': len(rows),
        'pdp_attempted': sum(store['attempted'] for store in stores.values()),
        'pdp_not_attempted': sum(store['not_attempted'] for store in stores.values()),
        'candidate_models': count,
        'verified_offer_count': sum(store['verified'] for store in stores.values()),
        'models_with_offer': priced,
        'offer_coverage': priced / count,
        'models_with_independent_pair': pairs,
        'pair_coverage': pairs / count,
        'new_candidates': len(new_models),
        'new_candidates_with_offer': sum(model['outcome'] != 'no_verified_offer' for model in new_models),
        'new_candidates_with_pair': sum(model['outcome'] == 'independent_pair' for model in new_models),
        'model_outcomes': dict(model_reasons),
        'pair_rejections': dict(pair_failures),
        'rejected': dict(rejections),
        'stores': stores,
        'seconds': round(perf_counter() - started, 2),
        'models': models,
    }
    artifact = {'checked_at': checked_at, 'model_count': count,
                'groups': [offers for offers in grouped.values() if offers]}
    return summary, artifact


def stage_catalog(artifact, destination, now=None):
    now = now or datetime.now(timezone.utc)
    size = artifact['model_count']
    minimum_pairs = (size * CROSS_PLATFORM_MINIMUM + PILOT_SIZE - 1) // PILOT_SIZE
    if destination.exists():
        raise ValueError('Choose a new staging directory')
    if not 1 <= size <= MAX_TARGETS or not timedelta() <= now - datetime.fromisoformat(artifact['checked_at']) <= OFFER_AGE:
        raise ValueError('Invalid or expired verification artifact')
    groups, seen_gtins = {}, set()
    for offers in artifact['groups']:
        first = offers[0]
        identity = first['code'], first['gtin13']
        if first['gtin13'] in seen_gtins or any((o['code'], o['gtin13']) != identity for o in offers):
            raise ValueError('Invalid verification group identity')
        seen_gtins.add(first['gtin13'])
        fresh = [o for o in offers if timedelta() <= now - datetime.fromisoformat(o['observed_at']) <= OFFER_AGE]
        if fresh:
            groups[identity] = fresh
    pairs = sum(len(next(candidates(offers), ())) == 2 for offers in groups.values())
    decision = {'eligible_models': len(groups), 'required_models': size,
                'eligible_pairs': pairs, 'required_pairs': minimum_pairs}
    if len(groups) < size or pairs < minimum_pairs:
        return {'status': 'blocked', **decision}
    # Write the complete local bundle before exposing it at its final path.
    with TemporaryDirectory(prefix='.monitor-stage-', dir=destination.parent) as temporary:
        bundle = Path(temporary) / 'bundle'
        bundle.mkdir()
        export_verified_pilot(groups, bundle / 'monitorPilot.json', bundle / 'images',
                              size=size, minimum_pairs=minimum_pairs)
        bundle.rename(destination)
    return {'status': 'staged', **decision}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest='command', required=True)
    discover = commands.add_parser('discover', help='Select a price-free candidate plan')
    discover.add_argument('--seeds', type=Path, nargs='+', required=True)
    discover.add_argument('--observations', type=Path, nargs='*', default=[])
    discover.add_argument('--catalog', type=Path, required=True)
    discover.add_argument('--models', type=int, default=100)
    discover.add_argument('--store-budget', type=int, default=48)
    discover.add_argument('plan', type=Path)
    expand = commands.add_parser('expand', help='Allocate additional destinations without replacing candidates')
    expand.add_argument('base', type=Path)
    expand.add_argument('previous_report', type=Path)
    expand.add_argument('plan', type=Path)
    expand.add_argument('--hints', type=Path, nargs='+', required=True)
    expand.add_argument('--store-budget', type=int, default=MAX_STORE_BUDGET)
    verify = commands.add_parser('verify', help='Read live offers and save coverage and an independent verification artifact')
    verify.add_argument('plan', type=Path)
    verify.add_argument('report', type=Path)
    verify.add_argument('artifact', type=Path)
    stage = commands.add_parser('stage', help='Apply the publication gate and write a local bundle if it passes')
    stage.add_argument('artifact', type=Path)
    stage.add_argument('directory', type=Path)
    args = cli.parse_args()
    if args.command == 'discover':
        seeds = (row for path in args.seeds for row in read_json(path))
        historical = (json.loads(line) for path in args.observations for line in path.read_text().splitlines() if line.strip())
        plan = make_plan(seeds, historical, read_json(args.catalog)['models'],
                         model_count=args.models, store_budget=args.store_budget)
        write_json(args.plan, plan)
        print(json.dumps({'models': len(plan['candidates']), **plan['discovery']}))
    elif args.command == 'expand':
        hints = (row for path in args.hints for row in read_json(path))
        plan = expand_plan(read_json(args.base), hints, read_json(args.previous_report), store_budget=args.store_budget)
        write_json(args.plan, plan)
        print(json.dumps({'models': len(plan['candidates']), **plan['discovery']}))
    elif args.command == 'verify':
        if args.report.exists() or args.artifact.exists() or args.report.resolve() == args.artifact.resolve():
            cli.error('Choose two new, distinct verification output files')
        summary, artifact = verify_plan(read_json(args.plan))
        write_json(args.report, summary)
        # The verification artifact contains seller identity evidence. Keep it
        # out of the public report and restrict the local file permissions.
        write_json(args.artifact, artifact, private=True)
        print(json.dumps({key: summary[key] for key in ('models_with_offer', 'models_with_independent_pair', 'seconds')}))
    else:
        decision = stage_catalog(read_json(args.artifact), args.directory)
        print(json.dumps(decision))
        if decision['status'] != 'staged':
            raise SystemExit(2)


if __name__ == '__main__':
    main()
