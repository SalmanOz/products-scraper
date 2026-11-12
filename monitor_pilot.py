"""Revalidate price-free monitor targets and export a publishable pilot."""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from itertools import combinations
import json
from pathlib import Path
import re
from time import sleep
from urllib.parse import urlparse

from curl_cffi.requests import RequestsError
from requests import RequestException

from laptop_discovery import Reader, _pdp
from laptop_pilot import IMAGE_HOSTS, _hepsiburada_public_get, _image, _public_offers, _target, verify_seller
from monitor_sources import PARSERS, WORDS
from priority_retailer_transport import PriorityRetailerTransport
from retailer_barcode import retailer_gtin13
from tws_offer_pilot import independent_sellers

PILOT_SIZE = 25
CROSS_PLATFORM_MINIMUM = 20
CORE = ('size', 'resolution', 'panel', 'refresh')
PHOTO_HOSTS = IMAGE_HOSTS | {'productimages.hepsiburada.net', 'www.gaming.gen.tr'}
MANUFACTURER_SPECS = json.loads(Path(__file__).with_name('monitor_manufacturer_specs.json').read_text())


def read_targets(host, rows, collector=None, *, retry_transient=True):
    get = collector if host in {'www.vatanbilgisayar.com', 'www.mediamarkt.com.tr', 'www.incehesap.com'} else None
    reader = Reader(host, len(rows), get=get)
    if host == 'www.n11.com' and collector:
        reader.transport = PriorityRetailerTransport(maximum_per_host=len(rows),
            n11_collector=collector, browser_fallback=False)
    elif host == 'www.hepsiburada.com':
        reader.transport = PriorityRetailerTransport(maximum_per_host=len(rows),
            get=_hepsiburada_public_get, browser_fallback=False)
    accepted, rejected = [], Counter()
    for index, row in enumerate(rows):
        if reader.stop:
            break
        try:
            raw, url = reader.read(row['url'])
            if not raw or not url:
                reason = reader.last_reason or 'selected_document_unavailable'
                rejected[reason] += 1
                # A retired PDP may redirect while other allowlisted products
                # on the same host remain healthy. Reject it without following.
                if reason == 'redirect' and reader.source is not None:
                    reader.stop = False
                if reader.stop and retry_transient and reason in {'transport_error', 'request_error'}:
                    sleep(8)
                    recovered, more_rejected = read_targets(host, rows[index:], collector, retry_transient=False)
                    accepted.extend(recovered)
                    rejected.update(more_rejected)
                    break
                continue
            if urlparse(row['url']).query != urlparse(url).query:
                rejected['listing_selector_changed'] += 1
                continue
            offer, reason = PARSERS[host](raw, url, row['code'])
            if not offer or offer.get('gtin13') != row['gtin']:
                rejected[reason if not offer else 'barcode_changed'] += 1
                continue
            if not verify_seller(offer, reader.source, reader.transport, reader.sellers):
                rejected['seller_identity_unproven'] += 1
                continue
            accepted.append({**offer, 'code': row['code']})
        except (RequestsError, RequestException, ValueError, TypeError, AttributeError, KeyError):
            rejected['read_or_parse_error'] += 1
    return accepted, rejected


def compatible(a, b):
    x, y = a['facts'], b['facts']
    # Retailers sometimes round 23.8 to 24 in the title. The exact GTIN,
    # code, brand and other hardware fields still have to agree.
    sizes_agree = x['size'] is None or y['size'] is None or abs(x['size'] - y['size']) <= .25
    return (a['gtin13'] == b['gtin13'] and a['code'] == b['code'] and x['brand'] == y['brand']
            and sizes_agree and all(x[key] is None or y[key] is None or x[key] == y[key]
                                    for key in ('resolution', 'panel', 'refresh')))


def independent_pair(a, b):
    return (a['platform'] != b['platform'] and compatible(a, b) and independent_sellers(a, b)
            and WORDS(a['merchant']).replace(' ', '') != WORDS(b['merchant']).replace(' ', '')
            and not all(re.match(r'^garaj online elektronik\b', o['_legal_name']) for o in (a, b))
            and min(a['price'], b['price']) >= max(a['price'], b['price']) * .62)


def candidates(group):
    yield from sorted((pair for pair in combinations(group, 2) if independent_pair(*pair)),
                      key=lambda pair: sum(o['price'] for o in pair))
    yield from ((offer,) for offer in sorted(group, key=lambda o: o['price']))


def materialize(offers, sources):
    first = offers[0]
    records = [o for o in sources if all(compatible(o, row) for row in offers)]
    brand = first['facts']['brand']
    code = first['code'].upper()
    manufacturer = MANUFACTURER_SPECS.get(first['gtin13'], {})
    official_facts = manufacturer.get('facts', {}) if manufacturer.get('code') == first['code'] else {}
    official_source = {'name': brand, 'url': manufacturer['url']} if official_facts else None
    if official_source:
        code = manufacturer.get('model', code)
    specs, facts = [], {}
    for key, label, unit in (('size', 'Ekran boyutu', ' inç'), ('resolution', 'Çözünürlük', ''),
                             ('panel', 'Panel tipi', ''), ('refresh', 'Ekran yenileme hızı', ' Hz'),
                             ('hdmi', 'HDMI', ''), ('dp', 'DisplayPort', ''), ('usb_c', 'USB-C', '')):
        if key in official_facts:
            value, source = official_facts[key], official_source
        else:
            observed = [o for o in records if o['facts'][key]]
            if not observed:
                if key in CORE:
                    return None
                facts[key] = False
                continue
            if key == 'size':
                observed.sort(key=lambda o: o['facts'][key] % 1 == 0)
            values = {o['facts'][key] for o in observed}
            if key in CORE and key != 'size' and len(values) > 1:
                return None
            value = observed[0]['facts'][key]
            source = {'name': observed[0]['platform'], 'url': observed[0]['url']}
        facts[key] = value
        if value is False:
            continue
        shown = ('Var' if value is True else f'{value:g}'.replace('.', ',') if key == 'size' else str(value))
        display_value = manufacturer.get('specValues', {}).get(key) if key in official_facts else None
        specs.append({'label': label, 'value': display_value or shown + unit, 'source': source})
    photo_offer = next((o for o in records if o.get('_image')
                        and (photo := _image(o['_image'], allowed_hosts=PHOTO_HOSTS))), None)
    if photo_offer is None:
        return None
    spec_sources = {spec['source']['url']: spec['source'] for spec in specs}
    name = f"{brand} {manufacturer.get('name', code) if official_source else code}"
    model = {'name': name, 'slug': f'{WORDS(brand).replace(" ", "-")}-{first["code"]}',
             'brand': brand, 'mpn': code, 'gtin': first['gtin13'], **facts,
             'photoSource': photo_offer['url'], 'specSources': list(spec_sources.values()),
             'specs': [{'label': 'Model kodu', 'value': code}, *specs],
             'offers': _public_offers(sorted(offers, key=lambda o: (o['price'], o['url'])))}
    return model, photo


def materialize_group(group):
    for offers in candidates(group):
        result = materialize(offers, group)
        if result:
            return result
    return None


def use_cases(model):
    flags = []
    if 23 <= model['size'] <= 25 and model['resolution'] == '1920 × 1080':
        flags.append('office')
    if 26.5 <= model['size'] <= 27 and model['resolution'] == '2560 × 1440' and model['refresh'] >= 144:
        flags.append('gaming')
    if model['usb_c']:
        flags.append('usb_c')
    return flags


def pick_pilot(records):
    remaining = sorted(records, key=lambda item: (-len(item[0]['offers']), item[0]['offers'][0]['price'], item[0]['slug']))
    output, slugs, gtins = [], set(), set()
    counts = Counter()
    while remaining and len(output) < PILOT_SIZE:
        # Keep the two-seller gate before adding single-offer models. Within
        # each band, prefer a use case that is still underrepresented.
        next_item = min(remaining, key=lambda item: (-len(item[0]['offers']),
            min((counts[key] for key in use_cases(item[0])), default=PILOT_SIZE), item[0]['offers'][0]['price']))
        remaining.remove(next_item)
        model = next_item[0]
        if model['slug'] in slugs or model['gtin'] in gtins:
            continue
        output.append(next_item)
        slugs.add(model['slug'])
        gtins.add(model['gtin'])
        counts.update(use_cases(model))
    return output


def collect_observations(targets, collector=None, *, direct_hosts=()):
    by_host = defaultdict(list)
    seen = set()
    if not isinstance(targets, list) or not 1 <= len(targets) <= 180:
        raise ValueError('Expected 1–180 price-free monitor targets')
    for row in targets:
        url, code, gtin = row['url'], row['code'], row['gtin']
        route = _target(url)
        if (not route or route.netloc not in PARSERS or url in seen or not re.fullmatch('[a-z0-9]{4,20}', code)
                or not any(c.isdigit() for c in code) or not gtin or retailer_gtin13(gtin) != gtin
                or (route.netloc not in {'www.incehesap.com', 'www.gaming.gen.tr'} and not _pdp(route.netloc, url))):
            raise ValueError('Invalid or duplicate monitor target')
        seen.add(url)
        by_host[route.netloc].append(row)
    groups, rejections = defaultdict(list), Counter()
    with ThreadPoolExecutor(max_workers=len(by_host)) as pool:
        jobs = {host: pool.submit(read_targets, host, rows, collector if host not in direct_hosts else None)
                for host, rows in by_host.items()}
        for host, job in jobs.items():
            rows = by_host[host]
            offers, rejected = job.result()
            for offer in offers:
                groups[offer['code'], offer['gtin13']].append(offer)
            rejections.update(rejected)
            print(json.dumps({'host': host, 'targets': len(rows), 'verified_offers': len(offers),
                              'rejected': rejected}), flush=True)
    return groups, rejections


def publish(targets, output, images):
    groups, rejections = collect_observations(targets)
    with ThreadPoolExecutor(max_workers=6) as pool:
        records = [row for row in pool.map(materialize_group, groups.values()) if row]
    result = pick_pilot(records)
    pairs = sum(len(m['offers']) >= 2 for m, _ in result)
    print(json.dumps({'models': len(result), 'cross_platform': pairs,
                      'observed_variants': len(groups), 'rejected': rejections}, ensure_ascii=False), flush=True)
    if len(result) != PILOT_SIZE or pairs < CROSS_PLATFORM_MINIMUM:
        raise ValueError('Pilot requires 25 photographed models and at least 20 independent platform pairs')
    images.mkdir(parents=True, exist_ok=True)
    for model, photo in result:
        (images / (model['slug'] + '.webp')).write_bytes(photo)
    snapshot = {'checkedAt': datetime.now(timezone.utc).isoformat(),
                'models': sorted((m for m, _ in result), key=lambda m: m['slug'])}
    output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('targets', type=Path)
    cli.add_argument('output', type=Path)
    cli.add_argument('images', type=Path)
    args = cli.parse_args()
    publish(json.loads(args.targets.read_text()), args.output, args.images)


if __name__ == '__main__':
    main()
