"""Validate a retailer HTML snapshot and merge new products into the private site."""
import argparse
import asyncio
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
from threading import local
from urllib.parse import urlsplit

from curl_cffi import CurlError
from curl_cffi.requests import Session, RequestsError

from retail_html_catalog import (
    IMAGE_HOSTS, KIND_WORDS, STORES, amount, catalog_gtin, clean, images, merge_specifications,
    retail_listing_url, technical_field, words,
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
        if timedelta() <= age <= timedelta(hours=26 if offer.get('storeOnly') else 72):
            return True
    return False


def normalized_model(value):
    return words(value).replace(' ', '')


def valid_row(row, *, require_specs=True):
    barcode = row.get('gtin')
    if not barcode or catalog_gtin(barcode) != barcode or row.get('kind') not in KIND_WORDS:
        return False
    if not clean(row.get('name'), 350) or not clean(row.get('brand'), 80):
        return False
    source = row.get('source', '')
    host = urlsplit(source).netloc
    if host not in STORES or retail_listing_url(source, source) != source:
        return False
    image_host = urlsplit(row.get('image', '')).netloc
    if not any(image_host in allowed and images(h, [row['image']]) for h, allowed in IMAGE_HOSTS.items()):
        return False
    specs = row.get('specs')
    conflicts = row.get('specConflicts', {})
    if (not isinstance(specs, dict) or not isinstance(conflicts, dict)
            or not (2 if require_specs else 0) <= len(specs) + len(conflicts) <= 256 or not all(
                clean(key, 80) and clean(value, 2048) for key, value in specs.items())):
        return False
    sources = row.get('specSources', {})
    if not isinstance(sources, dict) or set(sources) - set(specs):
        return False
    evidence = list(sources.values())
    for label, values in conflicts.items():
        if (label in specs or not clean(label, 80) or not isinstance(values, list) or len(values) < 2
                or any(not isinstance(item, dict) or not clean(item.get('value'), 2048) for item in values)):
            return False
        evidence.extend(values)
    for item in evidence:
        if not isinstance(item, dict):
            return False
        url = item.get('source', '')
        if urlsplit(url).netloc not in STORES or retail_listing_url(url, url) != url:
            return False
        try:
            if 'checkedAt' in item and datetime.fromisoformat(item['checkedAt']).tzinfo is None:
                return False
        except (ValueError, TypeError, KeyError):
            return False
    rejections = row.get('offerRejections', {})
    if not isinstance(rejections, dict):
        return False
    for url, checked in rejections.items():
        if (not isinstance(url, str) or urlsplit(url).netloc not in STORES
                or retail_listing_url(url, url) != url):
            return False
        try:
            if datetime.fromisoformat(checked).tzinfo is None:
                return False
        except (TypeError, ValueError):
            return False
    platforms = set()
    for offer in row['offers']:
        url = offer['url']
        host = urlsplit(url).netloc
        if (url != retail_listing_url(url, url) or STORES.get(host) != offer['platform'] or not amount(offer['price'])
                or not clean(offer['merchant'], 100) or offer['platform'] in platforms):
            return False
        platforms.add(offer['platform'])
    return True


def merge(rows, site, image_check, catalog):
    counters = Counter()
    output = {}
    name_owners = {}
    for item in read(catalog) if catalog.exists() else ():
        model_key = words(item['brand']), normalized_model(item['model'])
        item['specs'] = {k: v for k, v in item['specs'].items() if technical_field(k, v)}
        if 'specSources' in item:
            item['specSources'] = {k: v for k, v in item['specSources'].items() if k in item['specs']}
        if valid_row(item, require_specs=False):
            # Persisted rows need the same conflict rules even without a new listing.
            item.update(merge_specifications(item, {}, legacy_existing=True))
            name_key = item['kind'], model_key[0], words(item['name'])
            if name_key in name_owners:
                counters['removed_duplicate_name'] += 1
                continue
            name_owners[name_key] = item['gtin']
            output[item['gtin']] = apply_offer_rejections(item)
        else:
            counters['removed_invalid'] += 1
    source_owners = {p['source']: p['gtin'] for p in output.values()}
    for row in rows:
        row = {**row, 'specs': {k: v for k, v in row['specs'].items() if technical_field(k, v)}}
        if 'specSources' in row:
            row['specSources'] = {k: v for k, v in row['specSources'].items() if k in row['specs']}
        checked_sources = row.pop('checkedSources', {})
        barcode = row['gtin']
        if not valid_row(row, require_specs=barcode not in output):
            counters['invalid_or_insufficient_specs'] += 1
            continue
        model_key = words(row['brand']), normalized_model(row['model'])
        name_key = row['kind'], model_key[0], words(row['name'])
        if (source_owners.get(row['source'], barcode) != barcode
                or name_owners.get(name_key, barcode) != barcode):
            counters['existing_identity'] += 1
            continue
        existing = output.get(barcode)
        if existing and (words(existing['brand']) != words(row['brand']) or existing['kind'] != row['kind']
                         or existing.get('model') and row.get('model')
                         and normalized_model(existing['model']) != normalized_model(row['model'])):
            counters['identity_conflict'] += 1
            continue
        if not image_check(row['image']):
            counters['image_unavailable'] += 1
            continue
        source_owners[row['source']] = barcode
        name_owners[name_key] = barcode
        if existing:
            facts = merge_specifications(existing, row, legacy_existing=True)
            if datetime.fromisoformat(existing['checkedAt']) > datetime.fromisoformat(row['checkedAt']):
                row = {**row, **{key: existing[key] for key in
                                ('name', 'brand', 'kind', 'model', 'image', 'source', 'checkedAt')}}
            row = {**row, **facts}
            old = {offer['platform']: offer for offer in existing['offers']
                   if checked_sources.get(offer['url'], '') < offer['checkedAt']}
            for offer in row['offers']:
                platform = offer['platform']
                if platform not in old or offer['checkedAt'] >= old[platform]['checkedAt']:
                    old[platform] = offer
            row = {**row, 'offers': sorted(old.values(), key=lambda o: (o['price'], o['url']))}
        else:
            row = {**row, **merge_specifications({}, row)}
        row = apply_offer_rejections(row, existing.get('offerRejections', {}) if existing else {})
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
    existing = {url for url, in db.execute(
        "SELECT url FROM images WHERE verified=1 AND checked_at >= datetime('now', '-3 days')"
    )}
    # Revalidate older image URLs in bounded batches and keep the remaining
    # previously verified URLs eligible until they get their turn.
    urls = set(urls)
    stale = [url for url, in db.execute(
        "SELECT url FROM images WHERE verified=1 AND checked_at < datetime('now', '-3 days') "
        "ORDER BY checked_at, url"
    ) if url in urls]
    cache = existing | set(stale)
    new = sorted(urls - cache) + stale[:600]
    limits = {host: asyncio.Semaphore(3) for host in {urlsplit(url).netloc for url in new}}

    async def probe(url):
        async with limits[urlsplit(url).netloc]:
            if await asyncio.to_thread(served_image, url):
                cache.add(url)
                db.execute('INSERT OR REPLACE INTO images (url, verified, checked_at) VALUES (?, 1, CURRENT_TIMESTAMP)',
                           (url,))
            else:
                cache.discard(url)
                db.execute('DELETE FROM images WHERE url=?', (url,))
            db.commit()
            await asyncio.sleep(.15)

    await asyncio.gather(*(probe(url) for url in new))
    db.commit()
    return cache


def image_cache_schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS images (url TEXT PRIMARY KEY, verified INT NOT NULL, checked_at TEXT)')
    if 'checked_at' not in {column[1] for column in db.execute('PRAGMA table_info(images)')}:
        db.execute('ALTER TABLE images ADD COLUMN checked_at TEXT')
    db.execute('UPDATE images SET checked_at=CURRENT_TIMESTAMP WHERE checked_at IS NULL')
    db.commit()




def apply_offer_rejections(row, extra=None):
    """Keep identity rejection evidence across runs with older crawler state."""
    rejections = dict(row.get('offerRejections', {}))
    for url, checked in (extra or {}).items():
        if url not in rejections or datetime.fromisoformat(checked) > datetime.fromisoformat(rejections[url]):
            rejections[url] = checked
    if not rejections:
        return row
    offers = [offer for offer in row['offers'] if offer['url'] not in rejections
              or datetime.fromisoformat(offer['checkedAt']) > datetime.fromisoformat(rejections[offer['url']])]
    return {**row, 'offers': offers, 'offerRejections': rejections}


def invalidate_changed_identities(rows, queue_path, scope=None):
    """Apply confirmed URL identity changes even when that product cannot export."""
    rejected = {}
    with closing(sqlite3.connect(queue_path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        observations = []
        if 'identity_rejections' in tables:
            observations.extend((url, gtin, checked) for url, gtin, checked in db.execute(
                'SELECT url,gtin,checked_at FROM identity_rejections'))
        if 'refresh_attempts' in tables:
            expected = 't.gtin' if 'catalog_targets' in tables else 'NULL'
            join = 'LEFT JOIN catalog_targets t ON t.url=queue.url' if 'catalog_targets' in tables else ''
            attempts = db.execute(
                f"SELECT queue.url,queue.data,{expected},refresh_attempts.attempted_at,refresh_attempts.outcome "
                f"FROM refresh_attempts JOIN queue USING(url) {join} "
                "WHERE refresh_attempts.outcome='identity_changed' "
                "OR json_extract(queue.data,'$.offerCheckedAt') IS NOT NULL"
            )
            for url, raw, expected_gtin, attempted, outcome in attempts:
                data = json.loads(raw) if raw else {}
                checked = data.get('offerCheckedAt')
                gtin = data.get('gtin') if checked else expected_gtin or data.get('gtin')
                if not checked and outcome == 'identity_changed':
                    # SQLite CURRENT_TIMESTAMP is the actual UTC attempt time.
                    checked = datetime.fromisoformat(attempted).replace(tzinfo=timezone.utc).isoformat()
                if not gtin or not checked:
                    continue
                observations.append((url, gtin, checked))
                if data.get('source') and data.get('gtin') == gtin:
                    observations.append((data['source'], gtin, checked))
        for url, gtin, checked in observations:
            if scope is not None and gtin not in scope:
                continue
            observed = datetime.fromisoformat(checked)
            if observed.tzinfo is None:
                continue
            key = gtin, url
            rejected[key] = max(rejected.get(key, observed), observed)
    output, removed = [], 0
    for row in rows:
        if scope is not None and row['gtin'] not in scope:
            output.append(row)
            continue
        rejections = {url: checked.isoformat() for (gtin, url), checked in rejected.items() if gtin == row['gtin']}
        updated = apply_offer_rejections(row, rejections)
        removed += len(row['offers']) - len(updated['offers'])
        output.append(updated)
    return output, removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('site', type=Path)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--image-cache', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--scope', type=Path, help='Modify only the GTINs listed in this JSON object')
    parser.add_argument('--queue', type=Path, help='Apply confirmed identity changes from the persistent crawl queue')
    args = parser.parse_args()
    scope = set(read(args.scope)) if args.scope else None
    rows = read(args.report)
    if scope is not None and any(row['gtin'] not in scope for row in rows):
        parser.error('Report is outside the requested GTIN scope')
    current = read(args.catalog) if args.catalog.exists() else []
    existing_barcodes = {row['gtin'] for row in current}
    eligible = [p for p in rows if valid_row(p, require_specs=p['gtin'] not in existing_barcodes)]
    with closing(sqlite3.connect(args.image_cache, timeout=30)) as db, db:
        db.execute('PRAGMA journal_mode=WAL')
        image_cache_schema(db)
        verified = asyncio.run(verify_images(
            (row['image'] for batch in (eligible, current) for row in batch
             if (scope is None or row['gtin'] in scope) and valid_row(row, require_specs=row['gtin'] not in existing_barcodes)), db
        ))
    merged, counters = merge(rows, args.site, verified.__contains__, args.catalog)
    if args.queue:
        merged, counters['invalidated_identity_offers'] = invalidate_changed_identities(merged, args.queue, scope)
    now = datetime.now(timezone.utc)
    candidates = [row for row in merged if scope is None or row['gtin'] in scope]
    merged = [row for row in candidates if row['gtin'] in existing_barcodes
              or ready_for_publication(row, verified.__contains__, now)]
    counters['awaiting_current_price_or_image'] = len(candidates) - len(merged)
    counters['specification_conflicts'] = sum(len(row.get('specConflicts', {})) for row in merged)
    counters['retained_without_current_price'] = sum(not ready_for_publication(row, lambda _: True, now)
                                                    for row in merged)
    if scope is not None:
        # The scoped stock job must not expire, reformat or repair other products.
        scoped = {row['gtin']: row for row in merged}
        before = {row['gtin']: row for row in current if row['gtin'] in scope}
        if before == scoped and args.catalog.exists():
            args.output.write_bytes(args.catalog.read_bytes())
            print('No watched product changed')
            return
        merged = sorted([row for row in current if row['gtin'] not in scope] + list(scoped.values()),
                        key=lambda row: row['gtin'])
    args.output.write_text('[\n' + ',\n'.join(json.dumps(p, ensure_ascii=False, separators=(',', ':')) for p in merged) + '\n]\n')
    print(json.dumps({'counts': counters, 'total': len(merged),
                      'byKind': Counter(p['kind'] for p in merged)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
