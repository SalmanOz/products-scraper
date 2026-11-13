"""Verify comparison-site laptop and monitor leads on original retailer pages."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from itertools import islice
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit

from curl_cffi.requests import RequestsError
from requests import RequestException

from catalog_source_collector import catalog_document_reader
from competitor_catalog_gaps import MODEL_TOKEN, read_state, retailer_destination, write_json
from hepsiburada_catalog_discovery import organic_search_cards
from laptop_discovery import Reader, _pdp
from laptop_pilot import (PARSERS as LAPTOP_PARSERS, _hepsiburada_public_get, _image,
                          build_model, compatible as laptop_compatible, select_offers, verify_seller)
from laptop_retailer_sources import model_codes
from monitor_pilot import PHOTO_HOSTS, candidates as monitor_candidates, compatible as monitor_compatible, materialize
from monitor_sources import PARSERS as MONITOR_PARSERS, code_key, mentions, selected_feed_monitor
from n11_catalog_source import public_search_cards
from pc_parts_multistore import DIRECT_PATHS, offer_host
from priority_retailer_transport import PriorityRetailerTransport
from retailer_barcode import retailer_gtin13
from retailer_catalog_feeds import FEEDS


CATALOG_FILES = {'laptop': 'laptopPilot.json', 'monitor': 'monitorPilot.json'}
PARSERS = {'laptop': LAPTOP_PARSERS, 'monitor': MONITOR_PARSERS}
NATIVE_SEARCH_HOSTS = ('www.hepsiburada.com', 'www.n11.com')
FEATURE_TOKEN = re.compile(r'(?:\d+(?:hz|ms|k|inc|inch)|[a-z]*hdmi\d*|usb\d*|freesync\d*|gsync\d*|\d+x\d+)')
SEARCH_CODE = re.compile(r'(?=[a-z0-9]*[a-z])(?=[a-z0-9]*[0-9])[a-z0-9]{5,20}')


def monitor_codes(title):
    title = re.sub(r'\b(mp\d{3,4})[\s-]+(e\d{2}[a-z]{1,3})\b', r'\1\2', title, flags=re.I)
    title = re.sub(r'\bMAG[\s-]+(\d{3}QPF)[\s-]+(X\d{2}[A-Z]{1,3})\b', r'MAG \1\2', title, flags=re.I)
    return {code for token in MODEL_TOKEN.findall(title) if 5 <= len(code := code_key(token)) <= 20
            and any(c.isdigit() for c in code) and any(c.isalpha() for c in code)
            and not FEATURE_TOKEN.fullmatch(code)}


def accessory_retailer_url(raw, kind):
    if url := retailer_destination(raw):
        host = urlsplit(url).netloc
        if host in PARSERS[kind] and (offer_host(url) if host in DIRECT_PATHS else _pdp(host, url)):
            return url
    return None


def search_accessory_retailer(host, models):
    hepsiburada = host == 'www.hepsiburada.com'
    get = _hepsiburada_public_get if hepsiburada else catalog_document_reader('accessory-discovery')
    reader = PriorityRetailerTransport(maximum_per_host=len(models), browser_fallback=False, get=get)
    search = reader.read_hepsiburada_search if hepsiburada else reader.read_n11_search
    results, reports = {}, []
    for kind, code in models:
        html, reason, status = search(code)
        cards, parser = (organic_search_cards(html, code) if hepsiburada else
                         (public_search_cards(html), 'parsed' if html else None))
        urls = [url for row in cards if mentions(row['discovery_title'], code)
                and (url := accessory_retailer_url(row['source_url'], kind))
                and urlsplit(url).netloc == host and mentions(urlsplit(url).path, code)]
        results[kind, code] = list(dict.fromkeys(urls))[:2]
        reports.append({'category': kind, 'code': code, 'reason': reason, 'httpStatus': status,
                        'parser': parser, 'destinations': len(results[kind, code])})
    return results, reports


def discover_accessory_leads(state, model_links, comparison_sources, maximum):
    saved = state.setdefault('accessoryNativeLeads', {})
    audits = state.setdefault('accessoryNativeSearchAudits', {})
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=24)).isoformat()
    options = []
    for (kind, code), sources in comparison_sources.items():
        urls = model_links[kind, code, True]
        urls.update(url for raw in saved.get(f'{kind}/{code}', [])
                    if (url := accessory_retailer_url(raw, kind)) and mentions(urlsplit(url).path, code))
        retailers = {urlsplit(url).netloc for url in urls}
        if len(retailers) < 2 and (retailers or len(sources) >= 2):
            options.append((kind, code, retailers, len(sources)))
    by_host = {}
    for host in NATIVE_SEARCH_HOSTS:
        prior = audits.get(host, {})
        pending = defaultdict(list)
        for kind, code, retailers, source_count in options:
            checked = prior.get(f'{kind}/{code}', '')
            if host in PARSERS[kind] and host not in retailers and checked < cutoff:
                pending[kind].append((not retailers, checked, -source_count, code))
        # Give both categories an independent share of the search allowance.
        by_host[host] = []
        for kind, rows in pending.items():
            ordered = sorted(rows)
            selected = ordered[:maximum]
            if maximum > 1 and not selected[-1][0]:
                unlinked = next((row for row in ordered[maximum:] if row[0]), None)
                if unlinked:
                    selected[-1] = unlinked
            by_host[host].extend((kind, code) for _, _, _, code in selected)
    reports, discovered = {}, set()
    with ThreadPoolExecutor(max_workers=len(NATIVE_SEARCH_HOSTS)) as pool:
        jobs = {host: pool.submit(search_accessory_retailer, host, models) for host, models in by_host.items() if models}
        for host, job in jobs.items():
            targets, reports[host] = job.result()
            prior = audits.setdefault(host, {})
            for row in reports[host]:
                if row['httpStatus'] == 200 and row['parser'] == 'parsed':
                    prior[f"{row['category']}/{row['code']}"] = now.isoformat()
            for (kind, code), urls in targets.items():
                if urls:
                    key = f'{kind}/{code}'
                    saved[key] = list(dict.fromkeys([*urls, *saved.get(key, [])]))[:8]
                    model_links[kind, code, True].update(urls)
                    discovered.update(urls)
    return reports, discovered


def candidate_targets(state, categories, maximum, diagnostics=None, *, existing_codes=None, native_search=0,
                      native_reports=None, existing_barcodes=None, allow_single=False):
    prior = state.get('accessoryLeadAudits', {})
    existing_codes = existing_codes or {}
    existing_barcodes = existing_barcodes or {}
    targets = defaultdict(dict)
    feeds = defaultdict(dict)
    model_links = defaultdict(set)
    comparison_sources = defaultdict(set)
    for key, row in state['products'].items():
        kind = row['category']
        if kind not in categories:
            continue
        urls = [url for raw in row.get('retailerLeads', []) if (url := accessory_retailer_url(raw, kind))]
        if not urls and not native_search:
            continue
        codes = (monitor_codes(row['title']) if kind == 'monitor'
                 else {code_key(code) for code in model_codes(row['title'])})
        known = existing_codes.get(kind, set())
        gtin = row.get('feedGtin')
        feed_url = row.get('url')
        if (gtin and retailer_gtin13(gtin) == gtin and feed_url in urls
                and row.get('source') in FEEDS and urlsplit(feed_url).netloc == row['source']):
            if gtin in existing_barcodes.get(kind, set()):
                continue
            if not codes.intersection(known):
                feeds[kind, gtin][feed_url] = {code for code in codes if mentions(urlsplit(feed_url).path, code)}
        unknown_codes = codes - known
        for code in unknown_codes:
            model_links[kind, code, True].update(url for url in urls if mentions(urlsplit(url).path, code))
            if native_search and SEARCH_CODE.fullmatch(code) and row.get('source'):
                comparison_sources[kind, code].add(row['source'])
        if kind == 'laptop' and (unknown_codes or not codes):
            # Some retailer URLs omit the MPN. A single comparison page can
            # still supply leads, and the selected PDP has to prove the MPN.
            model_links[kind, key, False].update(urls)
    # A feed GTIN groups retailers even when their URL slugs differ. Product
    # documents still have to prove the selected variant and live offer.
    feed_routes = {}
    for (kind, gtin), routes in feeds.items():
        code_sets = [codes for codes in routes.values() if codes]
        if kind == 'monitor' and not code_sets:
            continue
        shared = set.intersection(*code_sets) if code_sets else set()
        if kind == 'monitor' and not shared:
            continue
        code = max(shared, key=lambda value: (len(value), value)) if shared else gtin
        model_links[kind, code, kind == 'monitor' or bool(shared)].update(routes)
        for url in routes:
            feed_routes[kind, url] = gtin
    new_urls = set()
    if native_search:
        reports, new_urls = discover_accessory_leads(state, model_links, comparison_sources, native_search)
        if native_reports is not None:
            native_reports.update(reports)
    for (kind, model, exact_code), urls in model_links.items():
        sources = len({urlsplit(url).netloc for url in urls})
        for url in urls:
            if sources < 2 and not (allow_single and (kind, url) in feed_routes):
                continue
            candidates = targets[kind, urlsplit(url).netloc]
            identity = (len(model) if kind == 'monitor' else 0, sources, exact_code, model)
            if identity > candidates.get(url, (0, 0, False, '')):
                candidates[url] = identity
    models = defaultdict(dict)
    for (kind, host), targets_by_url in targets.items():
        for url, (_, _, _, model) in targets_by_url.items():
            key = (url not in new_urls, prior.get(f'{kind}/{url}', ''), url)
            options = models[kind, model]
            options[host] = min(key, options.get(host, key))

    # Reserve part of each source's allowance for reading both sides of the
    # same model in one run. Other listings keep their usual per-host rotation.
    selected = defaultdict(list)
    reserve = max(1, maximum // 2)
    matched = [(kind, model, options) for (kind, model), options in models.items() if len(options) >= 2]
    matched.sort(key=lambda row: (all(key[0] for key in row[2].values()), sorted(row[2].values())[1][1],
                                  -len(row[2]), row[0], row[1]))
    for kind, model, options in matched:
        available = [(host, hint) for host, hint in options.items() if len(selected[kind, host]) < reserve]
        if len(available) < 2:
            continue
        available.sort(key=lambda row: (len(selected[kind, row[0]]), row[1], row[0]))
        for host, (_, _, url) in available[:2]:
            selected[kind, host].append((url, model))

    result = defaultdict(list)
    planned = defaultdict(set)
    for (kind, host), targets_by_url in targets.items():
        paired = selected[kind, host]
        chosen = {url for url, _ in paired}
        ordered = sorted(targets_by_url.items(), key=lambda row: (prior.get(f'{kind}/{row[0]}', ''),
                                                                  (kind, row[0]) not in feed_routes,
                                                                  -row[1][1], row[1][3], row[0]))
        counts = Counter(model for _, model in paired)
        ranked = []
        for url, (_, _, _, model) in ordered:
            if url in chosen:
                continue
            ranked.append((counts[model], url, model))
            counts[model] += 1
        ranked.sort(key=lambda row: row[0])
        routes = paired + [(url, model) for _, url, model in ranked[:maximum - len(paired)]]
        result[host].extend((kind, url, model if kind == 'monitor' else '', feed_routes.get((kind, url)))
                            if allow_single else (kind, url, model if kind == 'monitor' else '')
                            for url, model in routes)
        for _, model in routes:
            planned[kind, model].add(host)
    if diagnostics is not None:
        for kind in categories:
            counts = [len(hosts) for (category, _), hosts in planned.items() if category == kind]
            diagnostics[kind] = {'modelGroupsQueued': len(counts),
                                 'modelGroupsQueuedOnTwoHosts': sum(n >= 2 for n in counts)}
    return result


def read_host(host, targets):
    reader = Reader(host, len(targets))
    if host == 'www.hepsiburada.com':
        reader.transport = PriorityRetailerTransport(maximum_per_host=len(targets),
            get=_hepsiburada_public_get, browser_fallback=False)
    elif reader.transport is not None:
        reader.transport = PriorityRetailerTransport(maximum_per_host=len(targets), browser_fallback=False,
            get=catalog_document_reader('accessory-discovery') if host == 'www.n11.com' else None)
    offers, rejections, attempted = [], Counter(), set()
    for kind, url, code, feed_gtin in targets:
        if reader.stop:
            break
        attempted.add(f'{kind}/{url}')
        try:
            html, final = reader.read(url)
            if not html or not final:
                rejections[reader.last_reason or 'selected_document_unavailable'] += 1
                if reader.source is not None and reader.last_reason == 'redirect':
                    reader.stop = False
                continue
            if urlsplit(url).query != urlsplit(final).query:
                rejections['seller_selector_changed'] += 1
                continue
            parser = PARSERS[kind][host]
            manufacturer_code = None
            if kind == 'monitor' and host == 'www.gaming.gen.tr' and feed_gtin:
                offer, reason, manufacturer_code = selected_feed_monitor(html, final, code, feed_gtin)
                code = manufacturer_code or code
            else:
                offer, reason = parser(html, final, code) if code else parser(html, final)
            if not offer:
                rejections[reason or 'selected_offer_unverified'] += 1
                continue
            gtin, price = offer.get('gtin13'), offer.get('price')
            if (not gtin or retailer_gtin13(gtin) != gtin or offer.get('currency') != 'TRY' or
                    offer.get('availability') != 'in_stock' or offer.get('condition') != 'new' or
                    not isinstance(price, (int, float)) or not math.isfinite(price) or
                    not (3000 if kind == 'laptop' else 1) <= price <= 1_000_000):
                rejections['barcode_stock_or_condition_unverified'] += 1
                continue
            if feed_gtin and gtin != feed_gtin:
                rejections['feed_barcode_does_not_match_product_page'] += 1
                continue
            if not verify_seller(offer, reader.source, reader.transport, reader.sellers):
                rejections['seller_identity_unverified'] += 1
                continue
            offers.append((kind, {**offer, **({'code': code} if code else {}),
                                  **({'_feed_gtin': feed_gtin} if feed_gtin else {}),
                                  **({'_manufacturer_code': manufacturer_code} if manufacturer_code else {})}))
        except (RequestsError, RequestException, OSError, ValueError, TypeError, KeyError, AttributeError):
            rejections['read_or_parse_error'] += 1
    return offers, dict(rejections), attempted


def materialize_candidate(kind, offers):
    # Only a matching first-party feed and live seller document can
    # establish a new model with a single offer.
    single = [offer for offer in offers if offer.get('_feed_gtin') == offer['gtin13']]
    if kind == 'laptop':
        selected = select_offers(offers)
        choices = [selected] if len({offer['platform'] for offer in selected}) >= 2 else []
        choices.extend([offer] for offer in single)
        for cohort in choices:
            for offer in cohort:
                if offer.get('_image') and (photo := _image(offer['_image'], allowed_hosts=PHOTO_HOSTS)):
                    return build_model(cohort, offer), photo
        return None
    for cohort in monitor_candidates(offers):
        if len(cohort) == 1 and cohort[0] not in single:
            continue
        if result := materialize(cohort, offers if len(cohort) > 1 else cohort):
            return result
    return None


def stage_category(kind, catalog, grouped, output, maximum_models):
    known_barcodes = {row['gtin'] for row in catalog['models']}
    known_codes = {code_key(row['mpn']) for row in catalog['models']}
    known_slugs = {row['slug'] for row in catalog['models']}
    rows, rejected = [], Counter()
    candidates = []
    for (code, gtin), offers in sorted(grouped.items(), key=lambda row: (-len(row[1]), row[0])):
        if gtin in known_barcodes or code_key(code) in known_codes:
            rejected['already_in_catalog'] += 1
            continue
        if len({offer['platform'] for offer in offers}) < 2 and not any(o.get('_feed_gtin') for o in offers):
            rejected['same_barcode_second_platform_missing'] += 1
            continue
        candidates.append((code, gtin, offers))
    with ThreadPoolExecutor(max_workers=6) as pool:
        pending = iter(candidates)
        while len(rows) < maximum_models and (batch := list(islice(pending, min(12, maximum_models - len(rows))))):
            materials = pool.map(lambda row: materialize_candidate(kind, row[2]), batch)
            for (code, gtin, offers), result in zip(batch, materials):
                if not result:
                    rejected['verified_offers_specs_or_photo_missing'] += 1
                    continue
                model, image = result
                model_code = code_key(model['mpn'])
                if gtin in known_barcodes or {code_key(code), model_code} & known_codes or model['slug'] in known_slugs:
                    rejected['already_in_catalog'] += 1
                    continue
                known_barcodes.add(gtin)
                known_codes.update((code_key(code), model_code))
                known_slugs.add(model['slug'])
                (output / f"{model['slug']}.webp").write_bytes(image)
                selected_urls = {offer['url'] for offer in model['offers']}
                selected = [offer for offer in offers if offer['url'] in selected_urls]
                compatible = monitor_compatible if kind == 'monitor' else laptop_compatible
                targets = {offer['url']: {'url': offer['url'], 'gtin': gtin,
                                         'code' if kind == 'monitor' else 'mpn': code}
                           for offer in offers if all(compatible(offer, chosen) for chosen in selected)}
                row = {'model': model, 'targets': list(targets.values())}
                if len(selected_urls) == 1:
                    row['feedGtin'] = gtin
                rows.append(row)
    return rows, dict(rejected)


def run(state, catalogs, output, maximum_requests, maximum_models, *, native_search=0):
    diagnostics, native_reports = {}, {}
    inventory = {kind: json.loads(payload) for kind, payload in catalogs.items()}
    existing_codes = {kind: {code_key(row['mpn']) for row in catalog['models']}
                      for kind, catalog in inventory.items()}
    groups = candidate_targets(state, catalogs, maximum_requests, diagnostics, existing_codes=existing_codes,
                               native_search=native_search, native_reports=native_reports, allow_single=True,
                               existing_barcodes={kind: {row['gtin'] for row in cat['models']}
                                                  for kind, cat in inventory.items()})
    observed, sources = defaultdict(lambda: defaultdict(list)), {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = {host: pool.submit(read_host, host, rows) for host, rows in groups.items()}
        for host, job in jobs.items():
            offers, rejections, attempted = job.result()
            sources[host] = {'queued': len(groups[host]), 'read': len(attempted), 'verified': len(offers), 'rejections': rejections}
            now = datetime.now(timezone.utc).isoformat()
            state.setdefault('accessoryLeadAudits', {}).update(dict.fromkeys(attempted, now))
            for kind, offer in offers:
                code = offer['code'] if kind == 'monitor' else offer['facts']['mpn']
                observed[kind][code, offer['gtin13']].append(offer)
    monitor_groups = observed['monitor']
    exact_codes = defaultdict(set)
    for (code, gtin), offers in monitor_groups.items():
        if any(o.get('_manufacturer_code') for o in offers):
            exact_codes[gtin].add(code)
    for (alias, gtin), matches in list(monitor_groups.items()):
        alternatives = exact_codes[gtin]
        if len(alternatives) == 1:
            code = next(iter(alternatives))
            if alias != code and code.startswith(alias):
                monitor_groups[code, gtin].extend({**offer, 'code': code} for offer in matches)
                del monitor_groups[alias, gtin]
    report = {'sources': sources, 'nativeSearch': native_reports, 'categories': {}}
    for kind, payload in catalogs.items():
        directory = output / kind
        directory.mkdir(parents=True, exist_ok=True)
        rows, rejected = stage_category(kind, inventory[kind], observed[kind], directory, maximum_models)
        report['categories'][kind] = {'baseSha256': sha256(payload).hexdigest(), 'staged': rows,
                                      'observedVariants': len(observed[kind]), 'rejections': rejected,
                                      **diagnostics[kind]}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ('state', 'site-data', 'output'):
        parser.add_argument(f'--{option}', type=Path, required=True)
    parser.add_argument('--category', choices=CATALOG_FILES, action='append')
    parser.add_argument('--requests-per-host', type=int, default=60, choices=range(1, 101))
    parser.add_argument('--maximum-models', type=int, default=100, choices=range(1, 201))
    parser.add_argument('--native-search', type=int, default=0, choices=range(0, 21),
                        help='Maximum exact model searches per category and retailer')
    args = parser.parse_args()
    if not args.state.is_file():
        parser.error('A saved comparison discovery state is required')
    state = read_state(args.state)
    catalogs = {kind: (args.site_data / CATALOG_FILES[kind]).read_bytes()
                for kind in dict.fromkeys(args.category or CATALOG_FILES)}
    report = run(state, catalogs, args.output, args.requests_per_host, args.maximum_models,
                 native_search=args.native_search)
    write_json(args.output / 'report.json', report)
    write_json(args.state, state)
    print(json.dumps({'sources': report['sources'], 'nativeSearch': report['nativeSearch'], 'categories': {kind: {
        'observedVariants': row['observedVariants'], 'modelGroupsQueuedOnTwoHosts': row['modelGroupsQueuedOnTwoHosts'],
        'staged': [item['model']['name'] for item in row['staged']],
        'rejections': row['rejections']} for kind, row in report['categories'].items()}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
