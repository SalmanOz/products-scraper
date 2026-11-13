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

from bs4 import BeautifulSoup
from curl_cffi.requests import RequestsError
from requests import RequestException

from laptop_discovery import Reader, _pdp
from laptop_pilot import IMAGE_HOSTS, _hepsiburada_public_get, _image, _public_offers, _target, verify_seller
from pc_parts_multistore import DIRECT_PATHS, offer_host
from monitor_sources import (ASEMPI_HOST, ASEMPI_SELLER, AVANSAS_HOST, AVANSAS_SELLER,
                             COMPAZAR_HOST, COMPAZAR_SELLER, DIRECT_MONITOR_STORES,
                             ENSONU_HOST, ENSONU_SELLER,
                             GPN_HOST, GPN_SELLER, NETHOUSE_HOST, NETHOUSE_IMAGE_HOST, NETHOUSE_SELLER,
                             OVERGAME_HOST, OVERGAME_SELLER, OZAY_HOST, OZAY_SELLER, PARSERS,
                             PCKOLIK_HOST, PCKOLIK_LEGAL, PCKOLIK_SELLER, RAMPAGE_HOST, RAMPAGE_SELLER,
                             WORDS, asempi_product_id, asempi_seller_identity, avansas_seller_identity, compazar_seller_identity,
                             ensonu_seller_identity, gpn_seller_identity, nethouse_seller_identity, overgame_seller_identity,
                             ozay_seller_identity, rampage_seller_identity)
from n11_catalog_source import discover_product_sellers
from priority_retailer_transport import PriorityRetailerTransport
from retailer_barcode import retailer_gtin13
from tws_offer_pilot import independent_sellers

PILOT_SIZE = 25
CROSS_PLATFORM_MINIMUM = 20
MAX_TARGETS = 180
CORE = ('size', 'resolution', 'panel', 'refresh')
PHOTO_HOSTS = IMAGE_HOSTS | DIRECT_MONITOR_STORES | {
    'productimages.hepsiburada.net', 'static.sinerji.gen.tr', 'cdnsta.avansas.com',
    PCKOLIK_HOST, ASEMPI_HOST, OZAY_HOST, OVERGAME_HOST, GPN_HOST, ENSONU_HOST, RAMPAGE_HOST, COMPAZAR_HOST,
    NETHOUSE_IMAGE_HOST,
}
MANUFACTURER_SPECS = json.loads(Path(__file__).with_name('monitor_manufacturer_specs.json').read_text())
SELLER_CONTRACTS = {
    ASEMPI_HOST: (ASEMPI_SELLER, asempi_seller_identity), OZAY_HOST: (OZAY_SELLER, ozay_seller_identity),
    OVERGAME_HOST: (OVERGAME_SELLER, overgame_seller_identity),
    GPN_HOST: (GPN_SELLER, gpn_seller_identity),
    AVANSAS_HOST: (AVANSAS_SELLER, avansas_seller_identity),
    ENSONU_HOST: (ENSONU_SELLER, ensonu_seller_identity),
    RAMPAGE_HOST: (RAMPAGE_SELLER, rampage_seller_identity),
    COMPAZAR_HOST: (COMPAZAR_SELLER, compazar_seller_identity),
    NETHOUSE_HOST: (NETHOUSE_SELLER, nethouse_seller_identity),
}


def monitor_reader(host, maximum, collector=None):
    get = collector if host in {'www.vatanbilgisayar.com', 'www.mediamarkt.com.tr', 'www.incehesap.com'} else None
    reader = Reader(host, maximum, get=get, browser_fallback=False if host in
                    DIRECT_MONITOR_STORES | {'www.teknosa.com', PCKOLIK_HOST} | SELLER_CONTRACTS.keys() else None)
    if host == 'www.n11.com' and collector:
        reader.transport = PriorityRetailerTransport(maximum_per_host=maximum,
            n11_collector=collector, browser_fallback=False)
    elif host == 'www.hepsiburada.com':
        reader.transport = PriorityRetailerTransport(maximum_per_host=maximum,
            get=_hepsiburada_public_get, browser_fallback=False)
    return reader


def read_targets(host, rows, collector=None, *, retry_transient=True, audit=None):
    reader = monitor_reader(host, len(rows), collector)
    accepted, rejected = [], Counter()

    def reject(url, reason):
        rejected[reason] += 1
        if audit is not None:
            audit['outcomes'][url] = reason

    for index, row in enumerate(rows):
        if reader.stop:
            break
        url = row['url']
        if audit is not None:
            audit['attempted'] += 1
        try:
            raw, final = reader.read(url)
            if not raw or not final:
                reason = reader.last_reason or 'selected_document_unavailable'
                reject(url, reason)
                # A retired PDP may redirect while other allowlisted products
                # on the same host remain healthy. Reject it without following.
                if reason == 'redirect' and reader.source is not None:
                    reader.stop = False
                if reader.stop and retry_transient and audit is None and reason in {'transport_error', 'request_error'}:
                    sleep(8)
                    recovered, more_rejected = read_targets(host, rows[index:], collector, retry_transient=False)
                    accepted.extend(recovered)
                    rejected.update(more_rejected)
                    break
                continue
            offer, reason = verify_monitor_document(reader, host, row, raw, final)
            if offer is None:
                reject(url, reason)
                continue
            accepted.append(offer)
            if audit is not None:
                audit['outcomes'][url] = 'verified'
        except (RequestsError, RequestException, ValueError, TypeError, AttributeError, KeyError):
            reject(url, 'read_or_parse_error')
    if audit is not None:
        for row in rows:
            audit['outcomes'].setdefault(row['url'], 'not_attempted_source_stopped')
        if reader.transport:
            audit['transport'] = next(iter(reader.transport.report().values()), {})
        elif reader.source:
            audit['transport'] = reader.source.report()
    return accepted, rejected


def verify_monitor_document(reader, host, row, html, final, *, seller_links=None):
    if urlparse(row['url']).query != urlparse(final).query:
        return None, 'listing_selector_changed'
    options = {}
    if seller_links is not None:
        if host == 'www.n11.com':
            seller_links.extend(hint['source_url'] for hint in discover_product_sellers(html, final, maximum=2)
                                if hint['discovery_scope'] == 'public_other_seller_selector')
        elif host == 'www.trendyol.com':
            options = {'seller_links': seller_links, 'expected_gtin': row['gtin']}
    offer, reason = PARSERS[host](html, final, row['code'], **options)
    if not offer or offer.get('gtin13') != row['gtin']:
        return None, reason if not offer else 'barcode_changed'
    if host == PCKOLIK_HOST:
        if PCKOLIK_SELLER not in reader.sellers:
            raw, _, _ = reader.transport._read_document(PCKOLIK_SELLER, PCKOLIK_HOST, category=True)
            soup = BeautifulSoup(raw, 'html.parser') if raw else None
            body = WORDS(soup.main.get_text(' ', strip=True)) if soup and soup.main else ''
            verified = bool(re.search(r'\bsatici ' + PCKOLIK_LEGAL + r' ltd sti\b', body))
            reader.sellers[PCKOLIK_SELLER] = verified
        if not reader.sellers[PCKOLIK_SELLER]:
            return None, 'seller_identity_unproven'
        offer['_legal_name'] = PCKOLIK_LEGAL + ' limited sirketi'
    elif host in SELLER_CONTRACTS:
        seller_url, read_identity = SELLER_CONTRACTS[host]
        if seller_url not in reader.sellers:
            raw, _, _ = reader.transport._read_document(seller_url, host, category=True)
            reader.sellers[seller_url] = read_identity(raw) if raw else None
        identity = reader.sellers[seller_url]
        if not identity:
            return None, 'seller_identity_unproven'
        offer.update(identity)
    elif not verify_seller(offer, reader.source, reader.transport, reader.sellers):
        return None, 'seller_identity_unproven'
    return {**offer, 'code': row['code']}, 'verified'


def compatible(a, b):
    x, y = a['facts'], b['facts']
    if (a['gtin13'] != b['gtin13'] or a['code'] != b['code'] or x['brand'] != y['brand']):
        return False
    manufacturer = MANUFACTURER_SPECS.get(a['gtin13'], {})
    equivalents = manufacturer.get('equivalent_facts', {}) if (
        manufacturer.get('code') == a['code'] and manufacturer.get('brand') == x['brand']) else {}
    def agrees(key):
        first, second = x[key], y[key]
        if first is None or second is None or first == second:
            return True
        if key == 'size' and abs(first - second) <= .25:
            return True
        if key in equivalents:
            declared = [manufacturer['facts'][key], *equivalents[key]]
            return first in declared and second in declared
        return False
    # Retailers sometimes round 23.8 to 24 in the title. The exact GTIN,
    # code, brand and other hardware fields still have to agree.
    return all(agrees(key) for key in CORE)


def independent_pair(a, b):
    return pair_rejection(a, b) is None


def pair_rejection(a, b):
    if a['platform'] == b['platform']:
        return 'same_platform'
    if not compatible(a, b):
        return 'variant_facts_conflict'
    if (not independent_sellers(a, b)
            or WORDS(a['merchant']).replace(' ', '') == WORDS(b['merchant']).replace(' ', '')
            or all(re.match(r'^garaj online elektronik\b', o['_legal_name']) for o in (a, b))):
        return 'seller_not_independent'
    if min(a['price'], b['price']) < max(a['price'], b['price']) * .62:
        return 'price_spread_outlier'
    return None


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
    official_facts = manufacturer.get('facts', {}) if (
        manufacturer.get('code') == first['code'] and manufacturer.get('brand', brand) == brand) else {}
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


def pick_pilot(records, size=PILOT_SIZE):
    remaining = sorted(records, key=lambda item: (-len(item[0]['offers']), item[0]['offers'][0]['price'], item[0]['slug']))
    output, slugs, gtins = [], set(), set()
    counts = Counter()
    while remaining and len(output) < size:
        # Keep the two-seller gate before adding single-offer models. Within
        # each band, prefer a use case that is still underrepresented.
        next_item = min(remaining, key=lambda item: (-len(item[0]['offers']),
                                     min((counts[key] for key in use_cases(item[0])), default=size), item[0]['offers'][0]['price']))
        remaining.remove(next_item)
        model = next_item[0]
        if model['slug'] in slugs or model['gtin'] in gtins:
            continue
        output.append(next_item)
        slugs.add(model['slug'])
        gtins.add(model['gtin'])
        counts.update(use_cases(model))
    return output


def target_host(row):
    url, code, gtin = (row.get(key) for key in ('url', 'code', 'gtin'))
    if (not isinstance(url, str) or not isinstance(code, str)
            or not re.fullmatch('[a-z0-9]{4,20}', code) or not any(c.isdigit() for c in code)
            or not gtin or retailer_gtin13(gtin) != gtin):
        return None
    try:
        if asempi_product_id(url):
            return ASEMPI_HOST
        if re.fullmatch(r'https://ozaybilgisayar\.com/urun/[a-z0-9-]{6,360}', url):
            return OZAY_HOST
        if re.fullmatch(r'https://www\.overgameweb\.com/[a-z0-9-]{12,360}-monitor', url):
            return OVERGAME_HOST
        if re.fullmatch(r'https://www\.gpnteknoloji\.com/urun/[a-z0-9-]{6,360}', url):
            return GPN_HOST
        if re.fullmatch(r'https://www\.avansas\.com/[a-z0-9-]{6,360}-p-[1-9][0-9]{4,8}', url):
            return AVANSAS_HOST
        if re.fullmatch(r'https://www\.ensonu\.com/urun/[a-z0-9-]{6,360}/', url):
            return ENSONU_HOST
        if re.fullmatch(r'https://www\.rampage\.com\.tr/en/products/[a-z0-9-]{6,360}', url):
            return RAMPAGE_HOST
        if re.fullmatch(r'https://www\.compazar\.com/urun/[a-z0-9-]{6,360}', url):
            return COMPAZAR_HOST
        if re.fullmatch(r'https://www\.uzmanlarpc\.com/urun/[a-z0-9()-]{6,360}', url):
            return 'www.uzmanlarpc.com'
        if re.fullmatch(r'https://www\.nethouse\.com\.tr/(?=[a-z0-9.-]{8,360}$)[a-z0-9-]+(?:\.[a-z0-9-]+)*', url):
            return NETHOUSE_HOST
        if re.fullmatch(r'https://pckolik\.com\.tr/urun/[a-z0-9-]{6,360}', url):
            return PCKOLIK_HOST
        route = _target(url)
        if (route and route.netloc in PARSERS
                and ((offer_host(url) == route.netloc if route.netloc in DIRECT_PATHS else _pdp(route.netloc, url))
                     or route.netloc == 'www.gaming.gen.tr' and re.fullmatch(r'/urun/\d+/[a-z0-9-]{6,360}/?', route.path)
                     or route.netloc == 'www.netsiparis.com' and re.fullmatch(r'/[a-z0-9-]{6,360}', route.path)
                     or route.netloc in {'pitrap.com', 'www.teknostim.com'}
                     and re.fullmatch(r'/urun/[a-z0-9-]{6,360}', route.path)
                     or route.netloc == 'www.sinerji.gen.tr' and re.fullmatch(r'/[a-z0-9-]+-p-\d{4,8}', route.path))):
            return route.netloc
    except ValueError:
        return None
    return None


def group_targets(targets, maximum=MAX_TARGETS):
    by_host = defaultdict(list)
    seen = set()
    if not isinstance(targets, list) or not 1 <= len(targets) <= maximum:
        raise ValueError(f'Expected 1–{maximum} price-free monitor targets')
    for row in targets:
        host = target_host(row)
        if not host or row['url'] in seen:
            raise ValueError('Invalid or duplicate monitor target')
        seen.add(row['url'])
        by_host[host].append(row)
    return by_host


def collect_observations(targets, collector=None, *, direct_hosts=()):
    by_host = group_targets(targets)
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
    export_verified_pilot(groups, output, images, rejections=rejections)


def export_verified_pilot(groups, output, images, *, rejections=None, size=PILOT_SIZE,
                          minimum_pairs=CROSS_PLATFORM_MINIMUM):
    with ThreadPoolExecutor(max_workers=6) as pool:
        records = [row for row in pool.map(materialize_group, groups.values()) if row]
    result = pick_pilot(records, size)
    pairs = sum(len(m['offers']) >= 2 for m, _ in result)
    print(json.dumps({'models': len(result), 'cross_platform': pairs,
                      'observed_variants': len(groups), 'rejected': rejections or {}}, ensure_ascii=False), flush=True)
    if len(result) != size or pairs < minimum_pairs:
        raise ValueError(f'Pilot requires {size} photographed models and at least {minimum_pairs} independent platform pairs')
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
