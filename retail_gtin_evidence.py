"""Collect GTIN-matched specification evidence from n11, Pazarama, Amazon Türkiye OCTO24 and Diyalog Bilgisayar.

Reads a local JSONL queue (gtin, kind, optionally source and offers[].url).
Writes private append-only observations to JSONL. The output includes misses
and can resume an interrupted scan. Use a new output file for a new snapshot.
"""
import argparse
import json
import math
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from lxml import html as lxml
from lxml.etree import ParserError

from n11_catalog_source import public_search_cards
from pazarama_offer_source import NUXT_IDENTIFIER, NUXT_STRING
from retail_html_catalog import (AMAZON, KIND_WORDS, N11, PAZARAMA, brand_name, catalog_gtin, document,
                                 jsonld, product_url, properties, read_product, select_one, text, words)



OCTO = 'www.octo24.com'
DIYALOG = 'diyalogbilgisayar.com'


def selected_diyalog_specs(body, url, gtin):
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.netloc != DIYALOG or '/prd-' not in parts.path or parts.query:
        return None
    doc = document(body)
    item = select_one([node for node in jsonld(doc) if node.get('@type') == 'Product' and node.get('url') == url])
    if (doc.xpath('//link[@rel="canonical"]/@href') != [url] or not item
            or catalog_gtin(item.get('gtin13')) != gtin):
        return None
    meta = doc.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," product_meta ")]/span[span]')
    visible = {catalog_gtin(text(child)) for row in meta if words(row.text or '') == 'barkodu'
               for child in row.xpath('./span')}
    if visible != {gtin}:
        return None
    fields = []
    panels = doc.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," woocommerce-Tabs-panel ")]')
    for panel in panels:
        for row in panel.xpath('.//table//tr'):
            cells = row.xpath('./td|./th')
            if len(cells) == 2:
                fields.append({'name': text(cells[0]), 'value': text(cells[1])})
        for li in panel.xpath('.//li[not(.//li)]'):
            label, sep, value = text(li).partition(':')
            if sep and ':' not in value and 0 < len(label.strip()) <= 60 and 0 < len(value.strip()) <= 260:
                fields.append({'name': label.strip(), 'value': value.strip()})
    for field in fields:
        if words(field['name']) in {'ean', 'barkod', 'gtin', 'ean barcode', 'gtin ean'}:
            codes = re.findall(r'(?<![0-9])[0-9]{12,14}(?![0-9])', field['value'])
            if any(catalog_gtin(c[1:] if len(c) == 14 and c.startswith('0') else c) != gtin for c in codes):
                return None
    return {'gtin': gtin, 'name': item['name'], 'brand': brand_name(item.get('brand')), 'source': url,
            'specs': properties(fields)}


def octo_url(url):
    parts = urlsplit(url)
    return (parts.scheme == 'https' and parts.netloc == OCTO and not parts.query and not parts.fragment
            and re.fullmatch(r'/[^/?#]+::[0-9]+\.html', parts.path))


def octo_document(body):
    # This store declares ISO-8859-15 in the document.
    return lxml.fromstring(body, parser=lxml.HTMLParser(no_network=True, recover=True))


def selected_octo_specs(body, url, gtin):
    doc = octo_document(body)
    product = select_one(doc.xpath('//*[@itemtype="https://schema.org/Product" or @itemtype="http://schema.org/Product"]'))
    if product is None or not octo_url(url) or doc.xpath('//link[@rel="canonical"]/@href') != [url]:
        return None
    codes = {catalog_gtin(text(node)) for node in product.xpath('.//*[@itemprop="gtin13"]')}
    title = select_one(product.xpath('.//h1[@itemprop="name"]'))
    brand = select_one(product.xpath('.//*[@itemprop="brand"]//*[@itemprop="name"]'))
    if codes != {gtin} or title is None or brand is None:
        return None
    fields = []
    for key in product.xpath('.//div[@class="ITSn"]'):
        value = select_one(key.xpath('../div[@class="ITSv"]'))
        if value is not None:
            fields.append({'name': text(key), 'value': text(value)})
    return {'source': url, 'name': text(title), 'brand': text(brand), 'gtin': gtin, 'specs': properties(fields)}


def nuxt_code(script):
    """Read the selected product's Nuxt code from literal or positional JSON primitives."""
    if script.count('UI_PRODUCT_DETAIL:{') != 1:
        return None
    header = re.match(r'window\.__NUXT__=\(function\(([^)]{0,10000})\)\{', script)
    detail = script.split('UI_PRODUCT_DETAIL:{', 1)[1].split('attributes:', 1)[0][:3000]
    code = re.search(r'(?:^|,)code:(' + NUXT_STRING + '|' + NUXT_IDENTIFIER + r')(?=,)', detail)
    if not header or not code:
        return None
    token = code[1]
    if token.startswith('"'):
        return catalog_gtin(json.loads(token))
    keys = header[1].split(',')
    start = script.rfind('}(')
    if (token not in keys or len(keys) != len(set(keys)) or start < header.end()
            or not script.endswith('));') or len(script) - start > 256000):
        return None
    # A reassignment would invalidate positional argument identity.
    before = script[header.end():script.index('UI_PRODUCT_DETAIL:{')]
    if re.search(r'(?<![\w$])' + re.escape(token) + r'\s*(?:[+*/%-]?=(?!=)|\+\+|--)', before):
        return None
    pos, decoder = start + 2, json.JSONDecoder()
    for i in range(keys.index(token) + 1):
        if script.startswith('void 0', pos):
            value, pos = None, pos + 6
        else:
            value, pos = decoder.raw_decode(script, pos)
        if i == keys.index(token):
            return catalog_gtin(value)
        if script[pos:pos + 1] != ',':
            return None
        pos += 1


def selected_pazarama_specs(body, gtin):
    doc = document(body)
    candidates = [p for p in jsonld(doc) if p.get('@type') == 'Product'
                  and product_url(p.get('url'), f'https://{PAZARAMA}') == p.get('url')
                  and str(p.get('url')).endswith('-p-' + gtin)]
    selected = select_one(candidates)
    heading = select_one(doc.xpath('//h1[normalize-space()]'))
    script = select_one(doc.xpath('//script[starts-with(text(),"window.__NUXT__=(function(")]/text()'))
    if (not selected or heading is None or not script or nuxt_code(script) != gtin
            or words(text(heading)) != words(selected.get('name'))):
        return None
    fields = []
    for pair in doc.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," feature ")]/div[span]'):
        cells = pair.xpath('./span')
        if len(cells) == 2:
            fields.append({'name': text(cells[0]), 'value': text(cells[1])})
    return {'source': selected['url'], 'name': text(heading), 'brand': brand_name(selected.get('brand')),
            'gtin': gtin, 'specs': properties(fields)}


def candidates(host, body):
    if host == OCTO:
        links = octo_document(body).xpath('//div[@class="prod_image"]/a/@href')
        return list(dict.fromkeys(url for url in links if octo_url(url)))
    if host == N11:
        return [r['source_url'] for r in public_search_cards(body.decode('utf8'))]
    doc = document(body)
    ids = doc.xpath('//*[@data-component-type="s-search-result" and .//h2]/@data-asin')
    return list(dict.fromkeys(f'https://{AMAZON}/dp/{i}' for i in ids if re.fullmatch(r'[A-Z0-9]{10}', i)))


class AccessPaused(Exception):
    pass


class Transport:
    def __init__(self, spacing):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'TeknoSkor catalog verification/1.0 (+https://teknoskor.com)'})
        self.spacing = spacing
        self.previous = {}

    def get(self, host, url):
        for _ in range(4):
            time.sleep(max(0, self.previous.get(host, 0) + self.spacing - time.monotonic()))
            self.previous[host] = time.monotonic()
            with self.session.get(url, allow_redirects=False, stream=True, timeout=(5, 16)) as response:
                if response.status_code in (301, 302, 307, 308):
                    url = urljoin(url, response.headers.get('Location', ''))
                    if urlsplit(url).netloc != host or urlsplit(url).scheme != 'https':
                        return None
                    continue
                if response.status_code in (403, 429, 503):
                    raise AccessPaused(f'HTTP {response.status_code}')
                if response.status_code != 200 or 'html' not in response.headers.get('Content-Type', ''):
                    return None
                body = bytearray()
                for block in response.iter_content(65536):
                    body.extend(block)
                    if len(body) > 6_000_000:
                        return None
                if b'validateCaptcha' in body or b'<title>Just a moment...' in body:
                    raise AccessPaused('Verification page')
                return url, bytes(body)
        return None


def verify(host, gtin, kind, transport):
    if host == DIYALOG:
        page = transport.get(host, f'https://{host}/arama/?src={gtin}')
        return selected_diyalog_specs(page[1], page[0], gtin) if page else None
    if host == PAZARAMA:
        page = transport.get(host, f'https://{host}/urun-p-{gtin}')
        return selected_pazarama_specs(page[1], gtin) if page else None
    query = {N11: '/arama?q=', AMAZON: '/s?k=', OCTO: '/result.php?keywords='}[host]
    page = transport.get(host, f'https://{host}{query}{gtin}')
    if not page:
        return None
    links = candidates(host, page[1])
    if not 1 <= len(links) <= 8:
        return None
    for url in links[:2]:
        page = transport.get(host, url)
        if host == OCTO and page:
            row = selected_octo_specs(page[1], page[0], gtin)
            if row:
                return row
        elif page and product_url(page[0], url):
            row = read_product(page[1], page[0], kind, gtin)
            if row and row['gtin'] == gtin:
                return row
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('queue', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--store', choices=(N11, PAZARAMA, AMAZON, OCTO, DIYALOG), action='append',
                        help='Select one or more stores. Defaults to the three Turkish stores.')
    parser.add_argument('--spacing', type=float, default=1.0)
    args = parser.parse_args()
    if not math.isfinite(args.spacing) or args.spacing < 0.5:
        parser.error('spacing must be at least 0.5 seconds')
    completed = set()
    if args.output.exists():
        for row in map(json.loads, args.output.read_text().splitlines()):
            if row.get('outcome') != 'error':
                completed.add((row['store'], row['gtin']))
    counts, paused, transport = Counter(), set(), Transport(args.spacing)
    try:
        with args.queue.open() as queue, args.output.open('a') as output:
            for target in map(json.loads, queue):
                code, kind = catalog_gtin(target.get('gtin')), target.get('kind')
                if not code or kind not in KIND_WORDS:
                    continue
                known = {urlsplit(url).hostname for url in [target.get('source') or '',
                         *(v.get('url') or '' for v in target.get('offers', []) if isinstance(v, dict))]}
                for host in dict.fromkeys(args.store or (N11, PAZARAMA, AMAZON)):
                    key = host, code
                    if key in completed or host in known or host in paused:
                        continue
                    row = {'gtin': code, 'kind': kind, 'store': host}
                    try:
                        product = verify(host, code, kind, transport)
                        if product and product['specs']:
                            row.update({k: product[k] for k in ('source', 'name', 'brand', 'specs')})
                            row['outcome'] = 'verified'
                        else:
                            row['outcome'] = 'no_verified_specs'
                    except AccessPaused as error:
                        print(f'{host} paused: {error}', file=sys.stderr, flush=True)
                        paused.add(host)
                        continue
                    except (requests.RequestException, KeyError, ValueError, TypeError, ParserError):
                        row['outcome'] = 'error'
                    row['checkedAt'] = datetime.now(timezone.utc).isoformat()
                    output.write(json.dumps(row, ensure_ascii=False) + '\n')
                    output.flush()
                    completed.add(key)
                    counts[row['outcome']] += 1
    finally:
        transport.session.close()
    print(json.dumps(counts, sort_keys=True), file=sys.stderr)


if __name__ == '__main__':
    main()
