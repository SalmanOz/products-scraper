"""Validate a retailer HTML snapshot and merge new products into the private site."""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
from threading import local
from urllib.parse import urlsplit

from curl_cffi import CurlError
from curl_cffi.requests import Session, RequestsError

from retail_html_catalog import (
    IMAGE_HOSTS, KIND_WORDS, STORES, amount, catalog_gtin, clean, images, product_url, technical_field, words,
)


def read(path):
    return json.loads(path.read_text())


def ready_for_publication(row, image_check, now):
    if not image_check(row['image']):
        return False
    for offer in row['offers']:
        try:
            age = now - datetime.fromisoformat(offer['checkedAt'])
        except (ValueError, TypeError, KeyError):
            continue
        if timedelta() <= age <= timedelta(hours=72):
            return True
    return False


def normalized_model(value):
    return words(value).replace(' ', '')


def inventory(site):
    codes, source_urls, known = {}, set(), set()
    for category, filename, key in (
        ('laptop', 'laptopPilot.json', 'models'),
        ('monitor', 'monitorPilot.json', 'models'),
        ('pc-parts', 'pcParts.json', 'parts'),
        ('earbuds', 'twsOfferSnapshot.json', 'models'),
    ):
        rows = read(site / filename)[key]
        known.update(p['gtin'] for p in rows if p.get('gtin'))
        codes[category] = {(words(p['brand']), normalized_model(p['mpn']))
                           for p in rows if p.get('brand') and p.get('mpn')}
        for p in rows:
            source_urls.update(o['url'].split('?')[0] for o in p.get('offers', []))
            source_urls.update(u.split('?')[0] for u in p.get('offerTargets', []))
    return known, codes, source_urls


def valid_row(row):
    barcode = row.get('gtin')
    if not barcode or catalog_gtin(barcode) != barcode or row.get('kind') not in KIND_WORDS:
        return False
    if not clean(row.get('name'), 350) or not clean(row.get('brand'), 80):
        return False
    source = row.get('source', '')
    host = urlsplit(source).netloc
    if host not in STORES or product_url(source, source) != source:
        return False
    image_host = urlsplit(row.get('image', '')).netloc
    if not any(image_host in allowed and images(h, [row['image']]) for h, allowed in IMAGE_HOSTS.items()):
        return False
    specs = row.get('specs')
    if not isinstance(specs, dict) or not 2 <= len(specs) <= 20 or not all(
            clean(key, 80) and clean(value, 110) for key, value in specs.items()):
        return False
    platforms = set()
    for offer in row['offers']:
        url = offer['url']
        host = urlsplit(url).netloc
        if (url != product_url(url, url) or STORES.get(host) != offer['platform'] or not amount(offer['price'])
                or not clean(offer['merchant'], 100) or offer['platform'] in platforms):
            return False
        platforms.add(offer['platform'])
    return True


def merge(rows, site, image_check, catalog):
    known, codes, source_urls = inventory(site)
    counters = Counter()
    output = {}
    name_owners = {}
    for item in read(catalog) if catalog.exists() else ():
        model_key = words(item['brand']), normalized_model(item['model'])
        if (item['gtin'] in known or model_key in codes.get(item['kind'], set())
                or item['source'] in source_urls):
            counters['removed_existing_identity'] += 1
            continue
        item['specs'] = {k: v for k, v in item['specs'].items() if technical_field(k, v)}
        if valid_row(item):
            name_key = item['kind'], model_key[0], words(item['name'])
            if name_key in name_owners:
                counters['removed_duplicate_name'] += 1
                continue
            name_owners[name_key] = item['gtin']
            output[item['gtin']] = item
        else:
            counters['removed_invalid'] += 1
    source_owners = {p['source']: p['gtin'] for p in output.values()}
    for row in rows:
        row = {**row, 'specs': {k: v for k, v in row['specs'].items() if technical_field(k, v)}}
        checked_sources = row.pop('checkedSources', {})
        barcode = row['gtin']
        if not valid_row(row):
            counters['invalid_or_insufficient_specs'] += 1
            continue
        model_key = words(row['brand']), normalized_model(row['model'])
        name_key = row['kind'], model_key[0], words(row['name'])
        if (barcode in known or model_key in codes.get(row['kind'], set())
                or row['source'] in source_urls or source_owners.get(row['source'], barcode) != barcode
                or name_owners.get(name_key, barcode) != barcode):
            counters['existing_identity'] += 1
            continue
        existing = output.get(barcode)
        if existing and (words(existing['brand']) != words(row['brand']) or existing['kind'] != row['kind']):
            counters['identity_conflict'] += 1
            continue
        if not image_check(row['image']):
            counters['image_unavailable'] += 1
            continue
        source_owners[row['source']] = barcode
        name_owners[name_key] = barcode
        if existing:
            if len(existing['specs']) > len(row['specs']):
                row = {**row, 'specs': existing['specs'], 'source': existing['source']}
            old = {offer['platform']: offer for offer in existing['offers']
                   if checked_sources.get(offer['url'], '') < offer['checkedAt']}
            for offer in row['offers']:
                platform = offer['platform']
                if platform not in old or offer['checkedAt'] >= old[platform]['checkedAt']:
                    old[platform] = offer
            row = {**row, 'offers': sorted(old.values(), key=lambda o: (o['price'], o['url']))}
        if existing != row:
            counters['updated' if existing else 'added'] += 1
        output[barcode] = row
    return [output[key] for key in sorted(output)], counters


clients = local()


def served_image(url):
    if not hasattr(clients, 'session'):
        clients.session = Session(impersonate='chrome', discard_cookies=True)
    try:
        with clients.session.stream('GET', url, headers={'Range': 'bytes=0-2047', 'Accept': 'image/*'},
                                    timeout=12, allow_redirects=False) as response:
            if response.status_code not in (200, 206) or not response.headers.get('content-type', '').startswith('image/'):
                return False
            data = next(response.iter_content(), b'')
            return data[:3] == b'\xff\xd8\xff' or data.startswith(b'\x89PNG\r\n\x1a\n') \
                or data.startswith((b'GIF87a', b'GIF89a')) or data.startswith(b'RIFF') and data[8:12] == b'WEBP' \
                or data[4:12] in (b'ftypavif', b'ftypavis')
    except (CurlError, RequestsError, OSError, TimeoutError):
        return False


async def verify_images(urls, db):
    existing = {url for url, in db.execute('SELECT url FROM images WHERE verified=1')}
    new = sorted(set(urls) - existing)
    limits = {host: asyncio.Semaphore(3) for host in {urlsplit(url).netloc for url in new}}
    cache = set(existing)

    async def probe(url):
        async with limits[urlsplit(url).netloc]:
            if await asyncio.to_thread(served_image, url):
                cache.add(url)
                db.execute('INSERT OR REPLACE INTO images (url, verified) VALUES (?, 1)', (url,))
                db.commit()
            await asyncio.sleep(.15)

    await asyncio.gather(*(probe(url) for url in new))
    db.commit()
    return cache


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('site', type=Path)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--image-cache', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    rows = read(args.report)
    eligible = [p for p in rows if valid_row(p)]
    with sqlite3.connect(args.image_cache, timeout=30) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE IF NOT EXISTS images (url TEXT PRIMARY KEY, verified INT NOT NULL)')
        verified = asyncio.run(verify_images((row['image'] for row in eligible), db))
    merged, counters = merge(rows, args.site, verified.__contains__, args.catalog)
    now = datetime.now(timezone.utc)
    ready = [row for row in merged if ready_for_publication(row, verified.__contains__, now)]
    counters['awaiting_current_price_or_image'] = len(merged) - len(ready)
    merged = ready
    args.output.write_text('[\n' + ',\n'.join(json.dumps(p, ensure_ascii=False, separators=(',', ':')) for p in merged) + '\n]\n')
    print(json.dumps({'counts': counters, 'total': len(merged),
                      'byKind': Counter(p['kind'] for p in merged)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
