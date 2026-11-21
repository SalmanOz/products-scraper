"""Match a local GTIN queue to Gaming.gen.tr's public product specifications.

Input is JSONL with a gtin and kind per product. Output is private JSONL, one
selected product per matched GTIN, with raw retailer specification labels and
units. Example: python gaming_catalog_evidence.py queue.jsonl evidence.jsonl
"""
import argparse
import json
import math
import os
import re
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests

from retail_html_catalog import catalog_gtin, clean, properties

HOST = 'www.gaming.gen.tr'
PRODUCTS = f'https://{HOST}/wp-json/wc/store/v1/products'
FIELDS = 'id,type,name,permalink,attributes,variations'
PAGE_SIZE = 50


def attributes(product, taxonomy):
    return {term.get('name') for attr in product.get('attributes', [])
            if attr.get('taxonomy') == taxonomy
            for term in attr.get('terms', []) if isinstance(term.get('name'), str)}


def identity(product):
    """Require a single GS1 retail barcode and a product-specific native permalink."""
    number, url = product.get('id'), product.get('permalink')
    if type(number) is not int or not isinstance(url, str) or product.get('type') != 'simple' or product.get('variations'):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if (parts.scheme != 'https' or parts.netloc != HOST or parts.query or parts.fragment
            or not re.fullmatch(rf'/urun/{number}/[A-Za-z0-9%-]+/', parts.path)):
        return None
    declared = attributes(product, 'pa_ean')
    codes = {catalog_gtin(v[1:] if re.fullmatch(r'0[0-9]{13}', v) else v) for v in declared}
    return codes.pop() if len(codes) == 1 else None


def selected_evidence(product, target, number):
    expected = target['gtin']
    if product.get('id') != number or identity(product) != expected:
        return None
    brand = attributes(product, 'pa_marka')
    name = clean(product.get('name'), 350)
    if len(brand) != 1 or not name:
        return None
    rows = []
    for attr in product.get('attributes', []):
        if attr.get('has_variations') or attr.get('taxonomy') in {'pa_ean', 'pa_marka'}:
            continue
        labels = [v.get('name') for v in attr.get('terms', [])]
        if labels and all(isinstance(v, str) for v in labels):
            rows.append({'name': attr.get('name'), 'value': ', '.join(labels)})
    specs = properties(rows)
    if not specs:
        return None
    return {'kind': target['kind'], 'gtin': expected, 'brand': brand.pop(), 'name': name,
            'source': product['permalink'], 'checkedAt': datetime.now(timezone.utc).isoformat(), 'specs': specs}


class Store:
    def __init__(self, spacing):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'TeknoSkor catalog verification/1.0 (+https://teknoskor.com)'})
        self.spacing = spacing
        self.last_request = 0

    def get(self, url, params):
        time.sleep(max(0, self.last_request + self.spacing - time.monotonic()))
        self.last_request = time.monotonic()
        with self.session.get(url, params=params, stream=True, allow_redirects=False, timeout=(6, 25)) as response:
            response.raise_for_status()
            if response.status_code != 200 or 'application/json' not in response.headers.get('Content-Type', ''):
                raise ValueError(f'Unexpected product API response ({response.status_code})')
            body = bytearray()
            for block in response.iter_content(65536):
                body.extend(block)
                if len(body) > 8_000_000:
                    raise ValueError('Unexpectedly large product API response')
            return json.loads(body), response.headers


def matching_products(targets, store):
    matches = defaultdict(set)
    seen = set()
    page, pages = 1, 1
    while page <= pages:
        products, headers = store.get(PRODUCTS, {'page': page, 'per_page': PAGE_SIZE, 'orderby': 'id', 'order': 'asc', '_fields': FIELDS})
        if not isinstance(products, list):
            raise ValueError('Unexpected product collection')
        if page == 1:
            pages = int(headers['X-WP-TotalPages'])
        for product in products:
            gtin = identity(product)
            if gtin in targets:
                matches[gtin].add(product['id'])
            seen.add(product['id'])
        if page % 10 == 0 or page == pages:
            print(f'Catalog pages {page}/{pages}, unique products {len(seen)}, candidate GTINs {len(matches)}', file=sys.stderr, flush=True)
        page += 1
    counts = Counter({'catalog_products': len(seen), 'queried_gtins': len(targets), 'absent_gtins': len(targets) - len(matches)})
    for gtin, numbers in sorted(matches.items()):
        if len(numbers) != 1:
            counts['ambiguous_product_ids'] += 1
            continue
        number = next(iter(numbers))
        try:
            product, _ = store.get(f'{PRODUCTS}/{number}', {'_fields': FIELDS})
        except requests.HTTPError as error:
            if error.response.status_code != 404:
                raise
            counts['product_removed'] += 1
            continue
        result = selected_evidence(product, targets[gtin], number)
        if result:
            counts['verified'] += 1
            yield result
        else:
            counts['selected_product_unverified'] += 1
    print(json.dumps(counts, sort_keys=True), file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('queue', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--spacing', type=float, default=0.8, help='Minimum seconds between requests')
    args = parser.parse_args()
    if not math.isfinite(args.spacing) or args.spacing < 0.5:
        parser.error('spacing must be at least 0.5 seconds')
    targets = {}
    for row in map(json.loads, args.queue.read_text().splitlines()):
        code = catalog_gtin(row.get('gtin'))
        if code and isinstance(row.get('kind'), str) and row['kind']:
            if code in targets and targets[code]['kind'] != row['kind']:
                parser.error(f'Conflicting product kinds for GTIN {code}')
            targets[code] = {'gtin': code, 'kind': row['kind']}
    store = Store(args.spacing)
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf8', dir=args.output.parent, delete=False) as output:
            temp = Path(output.name)
            for product in matching_products(targets, store):
                output.write(json.dumps(product, ensure_ascii=False) + '\n')
        os.replace(temp, args.output)
    finally:
        store.session.close()
        if 'temp' in locals():
            temp.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
