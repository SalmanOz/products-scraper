"""Recheck exact GTINs regardless of their position in the discovery queue."""

import argparse
import asyncio
from collections import defaultdict
from contextlib import closing
import json
from pathlib import Path
from urllib.parse import urlsplit

from lxml.etree import ParserError
from requests import RequestException

from catalog_source_collector import catalog_document_reader

from retail_catalog_publish import served_image, valid_row
from retail_html_catalog import (
    Host, KIND_WORDS, TY, catalog_gtin, database, images, read_product, retail_listing_url, words,
)


WATCH = Path(__file__).with_name('retail_html_watch.json')


def targets():
    configured = json.loads(WATCH.read_text())
    for gtin, item in configured.items():
        if (catalog_gtin(gtin) != gtin or item['kind'] not in KIND_WORDS
                or not images(TY, item['photo']) or not item['urls'] or any(retail_listing_url(u, u) != u for u in item['urls'])):
            raise ValueError(f'Invalid watch target: {gtin}')
    return configured


async def read_watch(db, configured, site_reader=None, browser=None, browser_hosts=()):
    hosts = {}
    matches = defaultdict(list)
    counts = defaultdict(dict)

    async def probe(gtin, item, url):
        host = urlsplit(url).netloc
        status, raw = await hosts.setdefault(host, Host(.5)).get(url)
        transport = ''
        blocked = status in (301, 302, 307, 308, 403)
        access = [f'http_{status}']
        if not raw and site_reader and url in item['urls'] and status in (0, 301, 302, 307, 308, 403, 429, 503):
            try:
                witness = await asyncio.to_thread(site_reader, url)
                if witness.status_code == 200 and 'html' in witness.headers.get('Content-Type', ''):
                    raw = b''.join(witness.iter_content())
                    transport = 'site_'
                else:
                    blocked = blocked or witness.status_code in (301, 302, 307, 308, 403)
                    access.append(f'site_http_{witness.status_code}')
            except (RequestException, RuntimeError):
                access.append('site_unavailable')
        if browser and url in item['urls'] and (host in browser_hosts or (blocked and not raw)):
            reason, rendered = await browser.get(url)
            access.append(f'browser_{reason}')
            print(f'Browser {gtin} {url}: {reason}', flush=True)
            if rendered:
                raw = rendered
                transport = 'browser_'
        row = None
        if raw:
            final, body = raw if isinstance(raw, tuple) else (url, raw)
            try:
                row = await asyncio.to_thread(read_product, body, final, item['kind'], gtin)
            except (AttributeError, KeyError, TypeError, ValueError, ParserError):
                pass
        if not row:
            result = 'not_verified' if raw else '/'.join(access)
        elif row['gtin'] != gtin or words(row['brand']) != words(item['brand']):
            result = 'different_identity'
        else:
            matches[gtin].append(row)
            result = 'in_stock' if row['offer'] else 'no_verified_offer'
        counts[gtin][url] = transport + result

    jobs = []
    metadata = {}
    for gtin, item in configured.items():
        # Also check exact-GTIN listings found by the normal category crawl.
        recent = db.execute(
            """SELECT url,data FROM queue WHERE status='verified' AND json_extract(data, '$.gtin')=?
               AND kind=? ORDER BY checked_at DESC, url LIMIT 12""", (gtin, item['kind'])
        ).fetchall()
        metadata[gtin] = [row for _, raw in recent if words((row := json.loads(raw))['brand']) == words(item['brand'])]
        urls = dict.fromkeys([*item['urls'], *(url for url, _ in recent if retail_listing_url(url, url) == url)])
        jobs.extend(probe(gtin, item, url) for url in urls)
    await asyncio.gather(*jobs)

    staged = []
    for gtin, item in configured.items():
        rows = sorted(matches[gtin], key=lambda r: (-len(r['specs']), r['source']))
        offers = {}
        for row in sorted(rows, key=lambda r: (r['offer']['price'] if r['offer'] else float('inf'), r['source'])):
            offer = row['offer']
            if offer:
                offers.setdefault(offer['platform'], offer)
        # Pazarama's CDN may return image bytes but block embedded browser
        # requests. Prefer a current photo from another exact-GTIN document.
        photos = [r['image'] for r in sorted(rows, key=lambda r: (not bool(r['offer']), r['source']))
                  if urlsplit(r['image']).netloc != 'img.pzrmcdn.com']
        if rows:
            product = {k: v for k, v in rows[0].items() if k != 'offer'}
            specs = max([*rows, *metadata[gtin]], key=lambda r: len(r['specs']))['specs']
            product.update(model=item['model'], specs=specs, offers=list(offers.values()),
                           checkedSources={r['source']: r['checkedAt'] for r in rows})
            for photo in dict.fromkeys([*photos, item['photo']]):
                product['image'] = photo
                if valid_row(product) and await asyncio.to_thread(served_image, photo):
                    staged.append(product)
                    break
        available = sorted(offer['price'] for offer in offers.values())
        print(f'{gtin} {item["brand"]} {item["model"]}: {available or "no verified offer"}',
              json.dumps(counts[gtin], sort_keys=True), flush=True)
        if offers and not any(row['gtin'] == gtin for row in staged):
            print(f'{gtin}: awaiting a verified photo or sufficient product specs', flush=True)
    return staged


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--browser-fallback', action='store_true', help='Retry blocked watch URLs in Chromium')
    parser.add_argument('--browser-host', action='append', default=[],
                        help='Also use Chromium for this configured host when the direct request succeeds')
    parser.add_argument('--browser-executable', type=Path, help='Local Chromium executable override')
    args = parser.parse_args()
    configured = targets()
    reader = catalog_document_reader('retail-watch')

    async def run():
        with closing(database(args.db)) as db:
            if args.browser_fallback or args.browser_host:
                from retail_watch_browser import StockWatchBrowser
                async with StockWatchBrowser(args.browser_executable) as browser:
                    return await read_watch(db, configured, reader, browser, args.browser_host)
            return await read_watch(db, configured, reader)

    watched = asyncio.run(run())
    # Keep the targeted observation authoritative, including when the general
    # export still has an older offer that was unavailable on this run.
    general = json.loads(args.output.read_text()) if args.output.exists() else []
    rows = [row for row in general if row['gtin'] not in configured] + watched
    args.output.write_text(json.dumps(rows, ensure_ascii=False) + '\n')


if __name__ == '__main__':
    main()
