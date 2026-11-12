"""Stage new PC components using selected retail GTINs and manufacturer specifications."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from catalog_source_collector import catalog_document_reader
from competitor_catalog_gaps import read_state, retailer_destination, write_json
from laptop_pilot import _image
from pc_parts import CATEGORIES
from pc_parts_multistore import GAMING, INCEHESAP, PcRetailer, collect_targets, independent_offers, offer_host


SINERJI = 'www.sinerji.gen.tr'
PRIMARY_HOSTS = {INCEHESAP, SINERJI}
IMAGE_HOSTS = {INCEHESAP: {INCEHESAP}, SINERJI: {SINERJI, 'static.sinerji.gen.tr'}}


def candidate_groups(state, catalog, maximum, preferred=()):
    known = {url for row in catalog['parts'] for url in [row['sourceUrl'], *row.get('offerTargets', [])]}
    attempts = state.get('componentLeadAudits', {})
    groups = defaultdict(set)
    for row in state['products'].values():
        kind = row['category']
        if kind not in CATEGORIES:
            continue
        urls = {url for lead in row.get('retailerLeads', ())
                if (url := retailer_destination(lead)) and url not in known and offer_host(url)}
        if len({offer_host(url) for url in urls}) < 2:
            continue
        for url in urls:
            if offer_host(url) in PRIMARY_HOSTS:
                groups[kind, url].update(urls - {url})
    ordered = sorted(groups, key=lambda pair: (pair[1] not in preferred, attempts.get(pair[1], ''), -len(groups[pair]), pair))
    result = []
    counts = Counter()
    for kind, url in ordered:
        source = kind, offer_host(url)
        if counts[source] >= maximum:
            continue
        counts[source] += 1
        # Other URLs only discover possible offers. A selected GTIN from the
        # primary document must independently match each selected retailer SKU.
        links = sorted(groups[kind, url], key=lambda link: (bool(urlsplit(link).query), link))
        result.append((kind, url, links))
    return result


def selected_images(html, title):
    soup = BeautifulSoup(html, 'html.parser')
    paths = [image['src'] for image in soup.select('div.prod img[itemprop="image"][src]')
             if image.get('alt') == title and re.fullmatch(r'/resim/urun/\d{6}/[a-zA-Z0-9._-]+_500\.webp', image['src'])]
    if not paths:
        return []
    root = f'https://{INCEHESAP}'
    image = soup.select_one('meta[property="og:image"]')
    fallback = f'{root}/resim/og_resimler/{Path(paths[0]).name.removesuffix("_500.webp")}_og.jpg'
    urls = [urljoin(root, path) for path in dict.fromkeys(paths)]
    if image and image.get('content') == fallback:
        urls.insert(1, fallback)
    return urls[:3]


def stage(state, catalog, profiles, output, maximum):
    budget = min(50, len(CATEGORIES) * maximum + 2)
    site_reader = catalog_document_reader('component-discovery')
    readers = {INCEHESAP: PcRetailer(INCEHESAP, budget=budget, document_reader=site_reader),
               SINERJI: PcRetailer(SINERJI, budget=budget)}
    blocked_hosts, selected_hosts = set(), set()
    known = {part['gtin'] for part in catalog['parts']}
    leads = candidate_groups(state, catalog, maximum, {p['sourceUrl'] for p in profiles.values()})
    candidates, targets = {}, {}
    rejects = Counter()
    attempts = state.setdefault('componentLeadAudits', {})
    checked_at = datetime.now(timezone.utc).isoformat()
    for kind, url, links in leads:
        host = offer_host(url)
        if host in blocked_hosts:
            continue
        reader = readers[host]
        html, reason, _ = reader.transport._read_document(url, host)
        if not html and host == INCEHESAP and site_reader and host not in selected_hosts and reason in (
                'http_403', 'http_503', 'robots_http_403', 'robots_http_503', 'transport_error'):
            site_reader = None
            readers[host] = reader = PcRetailer(host, budget=budget)
            html, reason, _ = reader.transport._read_document(url, host)
        attempts[url] = checked_at
        if not html:
            rejects[reason] += 1
            if reason.startswith('robots_') or reason in ('http_403', 'http_429', 'source_circuit_open'):
                blocked_hosts.add(host)
            continue
        offer, reason = reader.parse(html, url, {'kind': kind})
        if not offer:
            rejects[reason] += 1
            continue
        selected_hosts.add(host)
        gtin = offer['gtin13']
        if gtin in known:
            continue
        part = {'kind': kind, 'gtin': gtin, 'brand': offer['brand'], 'name': offer['source_title']}
        if gtin not in candidates or profiles.get(gtin, {}).get('sourceUrl') == url:
            photo = selected_images(html, part['name']) if host == INCEHESAP else [offer.get('_image')]
            candidates[gtin] = (part, url, photo)
        per_host = Counter()
        for link in [url, *links]:
            host = offer_host(link)
            if host == GAMING or per_host[host] >= 2:
                continue
            per_host[host] += 1
            targets.setdefault(link, set()).add(gtin)
    routes = {url: [candidates[gtin][0] for gtin in sorted(barcodes)] for url, barcodes in targets.items()}
    verified = defaultdict(list)
    for offer in collect_targets(((url, parts[0]) for url, parts in routes.items()),
                                 alternatives_by_url={url: parts[1:] for url, parts in routes.items() if len(parts) > 1},
                                 document_readers={INCEHESAP: site_reader} if site_reader else None):
        verified[offer['gtin']].append(offer)
    photos = output / 'photos'
    photos.mkdir(parents=True, exist_ok=True)
    staged, paired = [], []
    for gtin, (part, url, photo) in candidates.items():
        offers = independent_offers(verified[gtin])
        if len({offer['platform'] for offer in offers}) < 2 or url not in {offer['url'] for offer in verified[gtin]}:
            rejects['same_gtin_second_platform_unverified'] += 1
            continue
        paired.append({'gtin': gtin, 'kind': part['kind'], 'name': part['name'], 'urls': [o['url'] for o in offers]})
        profile = profiles.get(gtin)
        if (not profile or profile['sourceUrl'] != url or profile['kind'] != part['kind']
                or profile['brand'].casefold() != part['brand'].casefold()):
            rejects['manufacturer_specifications_missing'] += 1
            continue
        image_hosts = IMAGE_HOSTS[offer_host(url)]
        asset = next((image for src in photo if src and (image := _image(src, allowed_hosts=image_hosts))), None)
        if not asset:
            rejects['selected_product_image_unavailable'] += 1
            continue
        (photos / f'{gtin}.webp').write_bytes(asset)
        specs = {'Marka': part['brand'], 'Üretici Kodu': profile['mpn'], 'EAN': gtin, **profile['specs']}
        slug = re.sub(r'[^a-z0-9]+', '-', part['name'].lower()).strip('-')[:100].rstrip('-')
        staged.append({**part, 'id': gtin, 'slug': f'{slug}-{gtin}', 'mpn': profile['mpn'],
                       'image': f'/media/pc-parts/{gtin}.webp', 'specs': specs,
                       'sourceUrl': url, 'specSourceUrl': profile['specSourceUrl'],
                       'offer': offers[0], 'offers': offers,
                       'offerTargets': list(dict.fromkeys([url, *(o['url'] for o in offers)]))})
    return {'checkedAt': checked_at, 'primaryReader': 'site' if site_reader else 'runner',
            'queued': len(leads), 'selected': len(candidates),
            'paired': paired, 'staged': staged, 'rejections': dict(rejects)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--specs', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--maximum-per-kind', type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.maximum_per_kind <= 5:
        parser.error('--maximum-per-kind must be between 1 and 5')
    payload = args.catalog.read_bytes()
    state = read_state(args.state)
    report = stage(state, json.loads(payload), json.loads(args.specs.read_bytes()), args.output, args.maximum_per_kind)
    report['baseSha256'] = sha256(payload).hexdigest()
    write_json(args.output / 'report.json', report)
    write_json(args.state, state)
    print(json.dumps({'primaryReader': report['primaryReader'], 'queued': report['queued'], 'selected': report['selected'], 'paired': len(report['paired']),
                      'staged': [r['gtin'] for r in report['staged']], 'rejections': report['rejections']},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
