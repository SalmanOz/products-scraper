"""Read public retailer RSS product links for discovery and later PDP verification."""

from collections import Counter
import re
from xml.etree import ElementTree

import requests

from laptop_retailer_sources import WORDS
from pc_parts_multistore import offer_host
from priority_retailer_transport import PriorityRetailerTransport
from retailer_barcode import retailer_gtin13
from tebilon_component_source import TEBILON


FEEDS = {
    'www.gaming.gen.tr': 'https://www.gaming.gen.tr/wp-content/uploads/beta-feeds/stoktaki-urunler.xml',
    'www.sinerji.gen.tr': 'https://www.sinerji.gen.tr/Content/Docs/Merchants/Google/products.xml',
    TEBILON: 'https://www.tebilon.com/export/2025110406001022.xml',
}
NS = '{http://base.google.com/ns/1.0}'
MAX_FEED_BYTES = 12 * 1024 * 1024
LEAF_CATEGORIES = {
    'monitor': 'monitor', 'gaming monitor': 'monitor', 'tasinabilir monitor': 'monitor',
    'notebook': 'laptop', 'laptop': 'laptop', 'gaming laptop': 'laptop',
    'islemci': 'cpu', 'anakart': 'motherboard', 'bellek ram': 'ram', 'ram bellek': 'ram',
    'ekran karti': 'gpu', 'ai ekran karti': 'gpu', 'ssd disk': 'ssd', 'ssd': 'ssd',
    'bilgisayar kasasi': 'case', 'kasa': 'case', 'guc kaynagi': 'psu', 'guc kaynagi psu': 'psu',
    'islemci sogutucu': 'cooler', 'sivi sogutma': 'cooler', 'hava sogutma': 'cooler',
}
TEBILON_COMPONENT_CATEGORIES = {
    'islemci': 'cpu', 'anakart': 'motherboard', 'bellek ram': 'ram', 'ekran karti': 'gpu',
    'ssd': 'ssd', 'bilgisayar kasalari': 'case', 'guc kaynaklari psu': 'psu',
}


def _component_kind(host, raw):
    path = [WORDS(label) for label in raw.split('>')]
    if host != TEBILON:
        return LEAF_CATEGORIES.get(path[-1])
    if len(path) < 2 or path[0] != 'bilgisayar parcalari':
        return None
    if path[1] == 'sogutma sistemleri' and len(path) >= 3 and path[2] in ('sivi sogutma', 'hava sogutma'):
        return 'cooler'
    return TEBILON_COMPONENT_CATEGORIES.get(path[1])


def feed_get(url, **options):
    headers = options['headers']
    if headers.get('Accept') == 'application/xml':
        options['headers'] = {**headers, 'Accept': 'application/xml,text/xml'}
    return requests.get(url, **options)


def feed_candidates(payload, host, categories):
    if not isinstance(payload, str) or len(payload.encode()) > MAX_FEED_BYTES or re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', payload, re.I):
        raise ValueError('Unexpected public RSS feed')
    root = ElementTree.fromstring(payload)
    if root.tag != 'rss' or len(root.findall('./channel')) != 1:
        raise ValueError('Invalid retailer RSS feed')
    candidates, rejected, ambiguous = {}, Counter(), set()
    for item in root.findall('./channel/item'):
        kind = _component_kind(host, item.findtext(NS + 'product_type', ''))
        if kind not in categories:
            continue
        def field(name):
            values = item.findall(NS + name)
            return (values[0].text or '').strip() if len(values) == 1 else ''

        title, url, gtin = field('title'), field('link'), retailer_gtin13(field('gtin'))
        if offer_host(url) != host or not gtin or not 3 <= len(title) <= 260:
            rejected[kind] += 1
            continue
        allowed_condition = ('yeni',) if host == TEBILON else ('', 'new')
        if WORDS(field('availability')) != 'in stock' or WORDS(field('condition')) not in allowed_condition:
            rejected[kind] += 1
            continue
        key = kind, gtin
        row = {'source': host, 'category': kind, 'id': gtin, 'title': title,
               'url': url, 'feedGtin': gtin, 'retailerLeads': [url]}
        if host == TEBILON:
            mpn = field('mpn')
            if not 3 <= len(mpn) <= 120 or field('id') != mpn:
                rejected[kind] += 1
                continue
            row['feedMpn'] = mpn
            if key in candidates and candidates[key] != row:
                ambiguous.add(key)
        candidates.setdefault(key, row)
    if host == TEBILON:
        route_counts = Counter(row['url'] for row in candidates.values())
        ambiguous.update(key for key, row in candidates.items() if route_counts[row['url']] != 1)
    for key in ambiguous:
        rejected[key[0]] += 1
        del candidates[key]
    return list(candidates.values()), rejected


def scan_feed(host, categories):
    supported = {*TEBILON_COMPONENT_CATEGORIES.values(), 'cooler'} if host == TEBILON else LEAF_CATEGORIES.values()
    categories = [kind for kind in categories if kind in supported]
    if not categories:
        return [], []
    reader = PriorityRetailerTransport(maximum_per_host=1, browser_fallback=False, get=feed_get)
    html, status, _ = reader._read_document(FEEDS[host], host, sitemap=True, sitemap_limit=MAX_FEED_BYTES)
    rows, rejected = [], {}
    if html:
        try:
            rows, rejected = feed_candidates(html, host, categories)
            status = 'feed_read'
        except (ElementTree.ParseError, ValueError):
            status = 'invalid_feed'
    counts = Counter(row['category'] for row in rows)
    reports = [{'source': host, 'category': kind, 'nextPage': None, 'status': status,
                'pages': int(status == 'feed_read'), 'cards': counts[kind], 'excluded': rejected.get(kind, 0)}
               for kind in categories]
    return reports, rows
