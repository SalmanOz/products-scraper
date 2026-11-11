"""Discover product candidates from public comparison categories without collecting offers."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from itertools import zip_longest
from pathlib import Path
import re
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from catalog_source_collector import catalog_document_reader
from laptop_retailer_sources import WORDS
from pazarama_category_source import pazarama_product_url
from public_retailer_sources import public_source
import requests
from priority_retailer_transport import PriorityRetailerTransport, compatible_get, product_host

AKAKCE = 'www.akakce.com'
CIMRI = 'www.cimri.com'
EPEY = 'www.epey.com'
URUNAI = 'urunai.com'
KARSILASTIRICI = 'karsilastirici.com'
PAHALIMI = 'pahalimi.com'
SOURCES = (AKAKCE, CIMRI, EPEY, URUNAI, KARSILASTIRICI, PAHALIMI)
URUNAI_CATEGORIES = {
    'telefon': 'phone', 'kulaklik': 'tws', 'laptop': 'laptop', 'monitor': 'monitor',
    'islemci': 'cpu', 'anakart': 'motherboard', 'ram-bellek': 'ram', 'ekran-karti': 'gpu',
    'ssd': 'ssd', 'bilgisayar-kasasi': 'case', 'guc-kaynagi': 'psu', 'sivi-sogutucu': 'cooler',
}
KARSILASTIRICI_CATEGORIES = {'phone': '/kategori/cep-telefonu', 'laptop': '/kategori/laptop'}
URUNAI_PRODUCT = re.compile(r'/([a-z0-9-]+)/([a-z0-9-]+)/([a-z0-9-]{1,220})')
KARSILASTIRICI_PRODUCT = re.compile(r'/urun/([a-z0-9-]{1,220})')
PAHALIMI_CATEGORIES = {'phone': '/kategori/cep-telefonu', 'laptop': '/kategori/dizustu-bilgisayar',
                       'monitor': '/kategori/monitor', 'tws': '/kategori/kulaklik'}
PAHALIMI_PRODUCT = re.compile(r'/kategori/[a-z0-9-]+/[a-z0-9-]{1,220}-p-([a-f0-9]{8})')
ADDITIONAL_RETAILER_PATHS = {
    'www.idefix.com': r'/[a-z0-9-]+-p-[1-9]\d{4,11}',
    'www.pttavm.com': r'/[a-z0-9-]+-p-[1-9]\d{5,11}',
    'www.amazon.com.tr': r'/(?:[\w%-]+/)?(?:dp|gp/product)/[A-Z0-9]{10}',
    'www.a101.com.tr': r'/elektronik/[a-z0-9-]+_p-[1-9]\d{5,11}',
    'www.gurgencler.com.tr': r'/iphone-[a-z0-9-]+',
}
CIMRI_SITEMAP_INDEX = f'https://{CIMRI}/sitemaps/product.xml'
CIMRI_SITEMAP_PATH = re.compile(r'/sitemaps/product_([1-9]\d{0,3})\.xml')
CIMRI_SITEMAP_LIMIT = 50 * 1024 * 1024
PHONE_MEMORY = re.compile(r'(?: [1-9]\d? gb)? (?:[1-9]\d{1,3} gb|[1-9] tb)$')
CATEGORIES = {
    'phone': ('/cep-telefonu.html', '/cep-telefonlari', '/akilli-telefonlar/'),
    'tws': ('/bluetooth-kulaklik/tws.html', '/kulaklik/kulak-ici-bluetooth-kulaklik', '/kulaklik/'),
    'laptop': ('/laptop-notebook.html', '/dizustu-bilgisayar', '/laptop/'),
    'monitor': ('/monitor.html', '/monitor', '/monitor/'),
    'cpu': ('/islemci.html', '/islemci', '/islemci/'),
    'motherboard': ('/anakart.html', '/anakart', '/anakart/'),
    'ram': ('/ram.html', '/ram-bellekler', '/bellek-ram/'),
    'gpu': ('/ekran-karti.html', '/ekran-kartlari', '/ekran-karti/'),
    'ssd': ('/ssd.html', '/ssd-solid-state-drive', '/sabit-disk/'),
    'case': ('/bilgisayar-kasasi.html', '/pc-kasalari', '/bilgisayar-kasasi/'),
    'psu': ('/power-supply.html', '/power-supply', '/power-supply-psu/'),
    'cooler': ('/islemci-sogutucu.html', '/islemci-sogutucu', '/islemci-sogutucu/'),
}
CARD = {
    AKAKCE: re.compile(r'/[a-z0-9-]+/en-ucuz-[a-z0-9-]+-fiyati,(\d{4,12})\.html'),
    CIMRI: re.compile(r'/[a-z0-9-]+/en-ucuz-[a-z0-9-]+-fiyatlari,(a?\d{4,12})'),
    EPEY: re.compile(r'/[a-z0-9-]+/[a-z0-9-]+\.html'),
    KARSILASTIRICI: KARSILASTIRICI_PRODUCT,
    PAHALIMI: PAHALIMI_PRODUCT,
}
CARD_SELECTOR = {
    AKAKCE: '#CPL a.iC[href][title]',
    CIMRI: 'a[href][title]:has(h3)',
    EPEY: '#listele ul.metin.row[id] a.urunadi[href][title]',
    KARSILASTIRICI: 'a.product-card-link[href]:has(h3)',
    PAHALIMI: 'a.kat-urun-ort[href]',
}
MODEL_TOKEN = re.compile(r'[a-zA-Z0-9]+(?:[-./_][a-zA-Z0-9]+)*')
STOP_SOURCE = {'http_403', 'http_429', 'challenge_page', 'source_circuit_open',
               'source_cooldown', 'transport_error', 'category_budget_exhausted'}


def category_url(host, path, page):
    if page == 1:
        return 'https://' + host + path
    if host == AKAKCE:
        return f'https://{host}{path.removesuffix(".html")},{page}.html'
    if host == EPEY:
        return f'https://{host}{path}{page}/'
    if host == PAHALIMI:
        return f'https://{host}{path}?sayfa={page}'
    return f'https://{host}{path}?page={page}'


def comparison_cards(html, host, url):
    soup = BeautifulSoup(html, 'html.parser')
    cards = {}
    links = set()
    for anchor in soup.select('a[href], link[rel=next][href]'):
        try:
            target = urlsplit(urljoin(url, anchor['href']))
        except ValueError:
            continue
        if target.scheme == 'https' and target.netloc == host and (not target.fragment or
                host == PAHALIMI and target.fragment == 'devam'):
            links.add(unquote(target._replace(fragment='').geturl()))
    for anchor in soup.select(CARD_SELECTOR[host]):
        try:
            target = urlsplit(urljoin(url, anchor['href']))
        except ValueError:
            continue
        path = unquote(target.path)
        if (target.scheme != 'https' or target.netloc != host or not CARD[host].fullmatch(path)
                or target.fragment):
            continue
        if host == EPEY:
            if path.split('/')[1] != urlsplit(url).path.split('/')[1]:
                continue
            source_id = anchor.find_parent('ul', class_='metin')['id']
            if not re.fullmatch(r'[1-9]\d{2,11}', source_id):
                continue
        else:
            source_id = CARD[host].fullmatch(path)[1]
        if host == KARSILASTIRICI:
            title = anchor.select_one('h3').get_text(' ', strip=True)
        elif host == PAHALIMI:
            title = anchor.get_text(' ', strip=True)
        else:
            title = anchor['title']
        title = ' '.join(title.split())
        if 3 <= len(title) <= 260:
            cards.setdefault(source_id, {
                'id': source_id, 'title': title,
                'url': target._replace(path=path, query='', fragment='').geturl(),
            })
    return cards.values(), links


def load_inventory(data, phone_sitemap):
    inventory = defaultdict(list)
    for name, kind, key in (
        ('pcParts.json', None, 'parts'), ('laptopPilot.json', 'laptop', 'models'),
        ('monitorPilot.json', 'monitor', 'models'), ('twsProductCatalog.json', 'tws', 'models'),
    ) if data else ():
        for part in json.loads((data / name).read_text(encoding='utf-8'))[key]:
            inventory[kind or part['kind']].append(part)
    for node in ElementTree.parse(phone_sitemap).iter():
        if node.tag.rsplit('}', 1)[-1] != 'loc' or not node.text:
            continue
        path = urlsplit(node.text)
        if path.netloc == 'teknoskor.com' and re.fullmatch(r'/product/[a-z0-9-]+', path.path):
            slug = path.path.split('/')[-1]
            inventory['phone'].append({'name': slug, 'slug': slug})
    return inventory


def identity_index(parts):
    by_label, by_mpn = defaultdict(set), defaultdict(set)
    for part in parts:
        for label in {part['name'], part['slug']}:
            by_label[WORDS(label)].add(part['slug'])
        mpn = WORDS(part.get('mpn', '')).replace(' ', '')
        if len(mpn) >= 6 and any(char.isdigit() for char in mpn) and any(char.isalpha() for char in mpn):
            by_mpn[mpn].add(part['slug'])
    return by_label, by_mpn


def catalog_match(title, index):
    by_label, by_mpn = index
    label = WORDS(title)
    matches = by_label.get(label, set())
    if not matches:
        matches = set()
        for token in MODEL_TOKEN.findall(title):
            matches.update(by_mpn.get(WORDS(token).replace(' ', ''), ()))
    return next(iter(matches)) if len(matches) == 1 else None


def phone_family(title):
    label = WORDS(re.sub(r'(\d+)(gb|tb)\b', r'\1 \2', title, flags=re.I))
    label = re.sub(r' [1-9]\d? gb ram$', '', label)
    return PHONE_MEMORY.sub('', label)


def candidate_family(kind, title, index):
    if kind != 'phone':
        return None
    family = phone_family(title)
    return catalog_match(family, index) if family != WORDS(title) else None


def urunai_sitemap_cards(xml, kinds, indexes):
    # Sitemap names provide hints for retailer searches. Use no offer or
    # specification data from the comparison site.
    root = ElementTree.fromstring(xml)
    if root.tag.rsplit('}', 1)[-1] != 'urlset':
        raise ValueError('Expected product URL set')
    for entry in root:
        url = next((node.text for node in entry if node.tag.rsplit('}', 1)[-1] == 'loc'), None)
        if not url or not url.startswith('https://' + URUNAI + '/'):
            continue
        route = urlsplit(url)
        match = URUNAI_PRODUCT.fullmatch(route.path)
        if not match or route.query or route.fragment:
            continue
        section, brand, model = match.groups()
        kind = URUNAI_CATEGORIES.get(section)
        title = model if model == brand or model.startswith(brand + '-') else f'{brand}-{model}'
        title = title.replace('-', ' ')
        if kind not in kinds or not in_scope(kind, title):
            continue
        yield {'id': f'{brand}/{model}', 'title': title, 'url': url, 'source': URUNAI,
               'category': kind, 'catalogSlug': catalog_match(title, indexes[kind])}


def urunai_market_slugs(xml):
    root = ElementTree.fromstring(xml)
    if root.tag.rsplit('}', 1)[-1] != 'urlset':
        raise ValueError('Expected market URL set')
    pattern = re.compile(r'https://urunai\.com/en-ucuz/([a-z0-9-]{1,220})')
    return {match[1] for entry in root for node in entry
            if node.tag.rsplit('}', 1)[-1] == 'loc' and node.text
            and (match := pattern.fullmatch(node.text))}


def scan_urunai(reader, kinds, indexes):
    url = f'https://{URUNAI}/sitemap-products.xml'
    body, reason, _ = reader._read_document(url, URUNAI, category=True, sitemap=True)
    try:
        rows = list(urunai_sitemap_cards(body, kinds, indexes)) if body is not None else []
    except (ValueError, ElementTree.ParseError):
        reason, rows = 'invalid_product_sitemap', []
    market_counts = Counter()
    if rows:
        market_xml, _, _ = reader._read_document(f'https://{URUNAI}/sitemap-en-ucuz.xml',
                                                URUNAI, category=True, sitemap=True)
        if market_xml:
            try:
                market = urunai_market_slugs(market_xml)
                for row in rows:
                    brand, model = row['id'].split('/', 1)
                    slug = model if model.startswith(brand + '-') else f'{brand}-{model}'
                    row['hasMarketPage'] = slug in market
                    market_counts[row['category']] += int(row['hasMarketPage'])
            except (ValueError, ElementTree.ParseError):
                pass
    counts = Counter(row['category'] for row in rows)
    matched = Counter(row['category'] for row in rows if row['catalogSlug'])
    return ([{'source': URUNAI, 'category': kind, 'status': 'sitemap_read' if counts[kind] else reason,
              'nextPage': None, 'pages': int(bool(counts[kind])), 'cards': counts[kind],
              'marketPages': market_counts[kind],
              'matched': matched[kind], 'unmatched': counts[kind] - matched[kind]} for kind in kinds], rows)


def known_phones_from_sitemap(reader, host, index):
    path = '/sitemap-products.xml' if host == KARSILASTIRICI else '/sitemap-urun-cep-telefonu.xml'
    url = f'https://{host}{path}'
    body, reason, _ = reader._read_document(url, host, category=True, sitemap=True)
    if body is None:
        return reason, []
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return 'invalid_product_sitemap', []
    if root.tag.rsplit('}', 1)[-1] != 'urlset':
        return 'invalid_product_sitemap', []
    rows = []
    for entry in root:
        url = next((node.text for node in entry if node.tag.rsplit('}', 1)[-1] == 'loc'), None)
        if not url or not url.startswith(f'https://{host}/'):
            continue
        route = urlsplit(url)
        match = CARD[host].fullmatch(route.path)
        if not match or route.query or route.fragment:
            continue
        slug = match[1] if host == KARSILASTIRICI else route.path.rsplit('/', 1)[-1].rsplit('-p-', 1)[0]
        title = slug.replace('-', ' ')
        known = catalog_match(title, index)
        if known or candidate_family('phone', title, index):
            rows.append({'id': match[1], 'title': title, 'url': url, 'source': host,
                         'category': 'phone', 'catalogSlug': known})
    return 'sitemap_read', rows


def in_scope(kind, title):
    title = WORDS(title)
    if re.search(r'\b(?:yenilenmis|ikinci el|teshir)\b', title):
        return False
    if kind == 'ram' and re.search(r'\b(?:sodimm|so dimm|notebook|laptop)\b', title):
        return False
    if kind == 'ssd' and re.search(r'\b(?:tasinabilir|harici|portable|external|hdd)\b', title):
        return False
    if kind == 'tws' and re.search(r'\b(?:intercom|kulak ustu|yedek|kilif|kablolu)\b', title):
        return False
    return True


def comparison_reader(host, *, collection='runner', **options):
    gateway = catalog_document_reader('competitor') if collection == 'site' and host in (CIMRI, EPEY) else None
    return PriorityRetailerTransport(browser_fallback=False, get=gateway or compatible_get, **options)


def scan_source(host, kinds, indexes, start_page, pages, cursors=None, *, collection='runner',
                cimri_sitemap=False):
    reader = comparison_reader(host, collection=collection)
    reports, observations = [], []
    if host == URUNAI:
        return scan_urunai(reader, kinds, indexes)
    stopped = None
    if host in (KARSILASTIRICI, PAHALIMI) and 'phone' in kinds:
        state, observations = known_phones_from_sitemap(reader, host, indexes['phone'])
        reports.append({'source': host, 'category': 'phone_sitemap', 'status': state,
                        'nextPage': None, 'pages': int(state == 'sitemap_read'), 'cards': len(observations)})
        if state in STOP_SOURCE or state.startswith('robots_') and state != 'robots_disallowed':
            stopped = state
    for kind in kinds:
        if host in (KARSILASTIRICI, PAHALIMI):
            paths = KARSILASTIRICI_CATEGORIES if host == KARSILASTIRICI else PAHALIMI_CATEGORIES
            path = paths.get(kind)
            if path is None:
                continue
        else:
            path = CATEGORIES[kind][SOURCES.index(host)]
        previous = (cursors or {}).get(f'{host}/{kind}', {})
        first_page = previous.get('nextPage') or start_page
        stats = Counter()
        seen = set()
        state = 'page_limit'
        next_page = first_page
        for page in range(first_page, first_page + pages):
            if stopped:
                state = stopped
                break
            url = category_url(host, path, page)
            body, reason, _ = reader._read_document(url, host, category=True)
            if body is None:
                state = reason
                if reason in STOP_SOURCE or reason.startswith('robots_') and reason != 'robots_disallowed':
                    stopped = reason
                break
            cards, links = comparison_cards(body, host, url)
            if not cards:
                # A changed template or an empty response is not proof of the end.
                state = 'product_cards_missing'
                break
            stats['pages'] += 1
            distinct = [card for card in cards if card['id'] not in seen]
            if not distinct:
                state = 'pagination_repeated_cards'
                break
            seen.update(card['id'] for card in distinct)
            stats['cards'] += len(distinct)
            for card in distinct:
                if not in_scope(kind, card['title']) or (
                        host == EPEY and kind == 'tws' and 'tam kablosuz' not in WORDS(card['title'])):
                    stats['excluded'] += 1
                    continue
                known = catalog_match(card['title'], indexes[kind])
                stats['matched' if known else 'unmatched'] += 1
                observations.append({**card, 'source': host, 'category': kind, 'catalogSlug': known})
            next_page = page + 1
            if category_url(host, path, next_page) not in links:
                state = 'no_next_page_link'
                next_page = None
                break
        reports.append({'source': host, 'category': kind, 'status': state, 'nextPage': next_page, **stats})
    if host == CIMRI and cimri_sitemap and not stopped:
        sitemap_reports, sitemap_rows = scan_cimri_sitemap(kinds, indexes, cursors or {}, reader=reader)
        reports.extend(sitemap_reports)
        observations = sitemap_rows + observations
    return reports, observations


def sitemap_locations(xml, expected):
    root = ElementTree.fromstring(xml)
    if root.tag.rsplit('}', 1)[-1] != expected:
        raise ValueError('Unexpected sitemap root')
    namespace = root.tag.removesuffix(expected)
    entry_tag = namespace + ('url' if expected == 'urlset' else 'sitemap')
    for entry in root:
        if entry.tag == entry_tag:
            location = entry.findtext(namespace + 'loc', '').strip()
            if location:
                yield location


def cimri_sitemap_cards(xml, kinds, indexes):
    categories = {paths[1].strip('/').rsplit('/', 1)[-1]: kind for kind, paths in CATEGORIES.items()}
    categories['bluetooth-kulaklik'] = 'tws'
    cards = {}
    for url in sitemap_locations(xml, 'urlset'):
        if not url.startswith(f'https://{CIMRI}/'):
            continue
        route = urlsplit(url)
        path = unquote(route.path)
        match = CARD[CIMRI].fullmatch(path)
        if not match or route.query or route.fragment:
            continue
        category, product = path.split('/')[1:]
        kind = categories.get(category)
        if kind not in kinds:
            continue
        title = product.split('-fiyatlari,', 1)[0].removeprefix('en-ucuz-').replace('-', ' ')
        if kind == 'tws' and not re.search(r'\b(?:tws|kulak ici|tam kablosuz|airpods|freebuds|galaxy buds|aerofit|wf \w+)\b', title):
            continue
        if not in_scope(kind, title):
            continue
        cards.setdefault((kind, match[1]), {'id': match[1], 'title': title, 'url': route._replace(path=path).geturl(),
            'source': CIMRI, 'category': kind, 'catalogSlug': catalog_match(title, indexes[kind]),
            'hasMarketPage': True, 'titleFromSitemap': True})
    return list(cards.values())


def scan_cimri_sitemap(kinds, indexes, cursors, *, reader=None):
    reader = reader or comparison_reader(CIMRI)
    previous = cursors.get(f'{CIMRI}/product_sitemap', {})
    report = {'source': CIMRI, 'category': 'product_sitemap', 'pages': 0, 'nextPage': previous.get('nextPage')}
    index, reason, _ = reader._read_document(CIMRI_SITEMAP_INDEX, CIMRI, category=True, sitemap=True)
    if not index:
        return [{**report, 'status': reason}], []
    try:
        shards = sorted({int(match[1]) for url in sitemap_locations(index, 'sitemapindex')
                         if url.startswith(f'https://{CIMRI}') and
                         (match := CIMRI_SITEMAP_PATH.fullmatch(url.removeprefix(f'https://{CIMRI}')))}, reverse=True)
    except (ValueError, ElementTree.ParseError):
        return [{**report, 'status': 'invalid_product_sitemap_index'}], []
    if not shards:
        return [{**report, 'status': 'product_sitemap_shards_missing'}], []
    shard = report['nextPage'] if report['nextPage'] in shards else shards[0]
    report['nextPage'] = shard
    url = f'https://{CIMRI}/sitemaps/product_{shard}.xml'
    xml, reason, _ = reader._read_document(url, CIMRI, category=True, sitemap=True, sitemap_limit=CIMRI_SITEMAP_LIMIT)
    if not xml:
        return [{**report, 'status': reason, 'shard': shard}], []
    try:
        rows = cimri_sitemap_cards(xml, kinds, indexes)
    except (ValueError, ElementTree.ParseError):
        return [{**report, 'status': 'invalid_product_sitemap', 'shard': shard}], []
    index = shards.index(shard) + 1
    return [{**report, 'status': 'sitemap_read', 'pages': 2, 'cards': len(rows), 'shard': shard,
             'nextPage': shards[index] if index < len(shards) else None}], rows


def read_state(path):
    if path is None or not path.exists():
        return {'version': 1, 'sources': {}, 'products': {}}
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('version') != 1 or not isinstance(data.get('sources'), dict) or not isinstance(data.get('products'), dict):
        raise ValueError('Unsupported competitor discovery state')
    return data


def merge_state(state, reports, observations, indexes, now):
    for row in reports:
        key = f"{row['source']}/{row['category']}"
        previous = state['sources'].get(key, {})
        state['sources'][key] = {
            **row,
            'lastAttemptAt': now,
            'completedPasses': previous.get('completedPasses', 0) + (
                row['nextPage'] is None and row['status'] in ('sitemap_read', 'no_next_page_link')),
        }
    for row in observations:
        key = f"{row['source']}/{row['category']}/{row['id']}"
        previous = state['products'].get(key, {})
        product = {**previous, **row, 'firstSeenAt': previous.get('firstSeenAt', now), 'lastSeenAt': now}
        if row.get('titleFromSitemap') and previous and not previous.get('titleFromSitemap'):
            product['title'] = previous['title']
            product.pop('titleFromSitemap')
        elif not row.get('titleFromSitemap'):
            product.pop('titleFromSitemap', None)
        state['products'][key] = product
    for row in state['products'].values():
        if row['category'] in indexes:
            kind = row['category']
            row['catalogSlug'] = catalog_match(row['title'], indexes[kind])
            row['catalogFamily'] = row['catalogSlug'] or candidate_family(kind, row['title'], indexes[kind])
    return state


def retailer_destination(raw):
    if not isinstance(raw, str) or len(raw) > 1000 or re.search(r'[\x00-\x20\\]', raw):
        return None
    try:
        route = urlsplit(raw)
        if route.scheme != 'https' or route.fragment:
            return None
        query = parse_qsl(route.query, keep_blank_values=True, max_num_fields=10)
    except ValueError:
        return None
    selectors = [(key, value) for key, value in query if not key.startswith('utm_')]
    if route.netloc == 'www.teknosa.com' and selectors == [('shopId', 'teknosa')]:
        # This is a comparison-site marker. The canonical shop must be
        # verified from the retailer's own selected listing.
        selectors = []
    url = route._replace(query=urlencode(selectors)).geturl()
    first_party = (
        route.netloc == 'www.vatanbilgisayar.com' and re.fullmatch(r'/[a-z0-9-]+\.html', route.path)
        or route.netloc == 'www.mediamarkt.com.tr' and re.fullmatch(
            r'/tr/product/_[a-z0-9-]+-[1-9]\d{5,11}\.html', route.path)
    )
    if product_host(url) or first_party and not selectors or pazarama_product_url(url):
        return url
    if public_source(url) and re.fullmatch(r'/[a-z0-9_-]+', route.path):
        return url
    pattern = ADDITIONAL_RETAILER_PATHS.get(route.netloc)
    if not pattern or not re.fullmatch(pattern, route.path):
        return None
    if route.netloc == 'www.idefix.com' and len(selectors) == 1 and selectors[0][0] == 'vendorId':
        return url if re.fullmatch(r'[1-9]\d{0,9}', selectors[0][1]) else None
    return url if not selectors else None


def comparison_retailer_leads(html, host, source_url):
    soup = BeautifulSoup(html, 'html.parser')
    if host == EPEY:
        raw_links = [unquote(anchor['data-link']) for anchor in soup.select('a.git[data-link]:has(.urun_adi)')]
    else:
        selectors = {URUNAI: 'a[rel~=sponsored][href]',
                     KARSILASTIRICI: '#fiyatlar a.store-link[rel~=sponsored][href]',
                     PAHALIMI: 'a.kat-teklif-satir[rel~=sponsored][href]'}
        raw_links = [anchor['href'] for anchor in soup.select(selectors[host])] if host in selectors else []
        expected = unquote(source_url)
        for node in soup.select('script[type="application/ld+json"]'):
            try:
                product = json.loads(node.get_text())
            except ValueError:
                continue
            if not isinstance(product, dict) or product.get('@type') not in {'Product', 'ProductGroup'}:
                continue
            canonical = product.get('url') or product.get('@id')
            if isinstance(canonical, str):
                canonical = canonical.removesuffix('#product')
            if not isinstance(canonical, str) or unquote(canonical) != expected:
                continue
            aggregate = product.get('offers')
            if not isinstance(aggregate, dict) or aggregate.get('@type') != 'AggregateOffer':
                continue
            listings = aggregate.get('offers')
            if isinstance(listings, list):
                raw_links.extend(offer.get('url') for offer in listings if isinstance(offer, dict))
    return sorted({url for raw in raw_links if (url := retailer_destination(raw))})


def scan_retailer_leads(state, host, kinds, maximum, now, priority=(), pinned=(), *, collection='runner'):
    candidates = defaultdict(list)
    for row in state['products'].values():
        if row['source'] != host or row['category'] not in kinds:
            continue
        route = urlsplit(row['url'])
        product_path = URUNAI_PRODUCT if host == URUNAI else CARD[host]
        if (route.scheme == 'https' and route.netloc == host and not route.query and not route.fragment
                and product_path.fullmatch(unquote(route.path))):
            candidates[row['category']].append(row)
    for kind, rows in candidates.items():
        rows.sort(key=lambda row: (row.get('lastLeadAttemptAt', ''), not row.get('hasMarketPage'),
                                   row['firstSeenAt'], row['id']))
        first = [row for row in rows if row.get('catalogFamily') in pinned]
        others = [row for row in rows if row.get('catalogFamily') not in pinned]
        gaps = (row for row in others if row.get('catalogFamily') in priority)
        rest = (row for row in others if row.get('catalogFamily') not in priority)
        candidates[kind] = first + [row for pair in zip_longest(gaps, rest) for row in pair if row is not None]
    reader = comparison_reader(host, collection=collection, maximum_per_host=maximum)
    offsets = Counter()
    for turn in range(maximum):
        active = [kind for kind in kinds if offsets[kind] < len(candidates[kind])]
        if not active:
            break
        kind = active[turn % len(active)]
        row = candidates[kind][offsets[kind]]
        offsets[kind] += 1
        html, reason, _ = reader._read_document(row['url'], host)
        row['lastLeadAttemptAt'] = now
        if html is None:
            row['leadStatus'] = reason
            if reason in STOP_SOURCE or reason.startswith('robots_') and reason != 'robots_disallowed':
                break
        else:
            row['retailerLeads'] = comparison_retailer_leads(html, host, row['url'])
            row['leadStatus'] = 'direct_retailer_links' if row['retailerLeads'] else 'direct_retailer_links_missing'


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + '.tmp')
    pending.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    pending.replace(path)


def public_phone_catalog():
    try:
        with requests.get('https://teknoskor.com/api/products', params={'limit': 1000},
                          timeout=(5, 10), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                return []
            data = bytearray()
            for chunk in response.iter_content(65536):
                if len(data) + len(chunk) > 8 * 1024 * 1024:
                    return []
                data.extend(chunk)
        rows = json.loads(data)
        if isinstance(rows, list) and len(rows) <= 1000:
            return [row for row in rows if isinstance(row, dict) and
                    isinstance(row.get('slug'), str) and re.fullmatch(r'[a-z0-9-]{1,160}', row['slug'])]
    except (ValueError, requests.RequestException):
        pass
    return []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site-data', type=Path)
    parser.add_argument('--phone-sitemap', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--category', action='append', choices=CATEGORIES)
    parser.add_argument('--source', action='append', choices=SOURCES)
    parser.add_argument('--collection', choices=('runner', 'site'), default='runner')
    parser.add_argument('--cimri-sitemap', action='store_true', help='Read one bounded Cimri product sitemap shard')
    parser.add_argument('--state', type=Path, help='Resume category cursors and retain deduplicated candidate IDs')
    parser.add_argument('--prioritize-model', action='append', default=[])
    parser.add_argument('--start-page', type=int, default=1)
    parser.add_argument('--pages', type=int, default=2)
    parser.add_argument('--lead-limit', type=int, default=0, help='Maximum detail pages per source for direct retailer URLs')
    args = parser.parse_args()
    kinds = list(dict.fromkeys(args.category or CATEGORIES))
    if args.site_data is None and kinds != ['phone']:
        parser.error('--site-data is required when scanning categories beyond phones')
    if args.start_page < 1 or args.pages < 1 or len(kinds) * args.pages > 40:
        parser.error('Select a positive page number and up to 40 category requests per source')
    if not 0 <= args.lead_limit <= 40:
        parser.error('Select up to 40 detail pages per source')
    inventory = load_inventory(args.site_data, args.phone_sitemap)
    indexes = {kind: identity_index(inventory[kind]) for kind in kinds}
    state = read_state(args.state)
    priority = set(args.prioritize_model)
    if args.lead_limit and 'phone' in kinds:
        priority.update(row['slug'] for row in public_phone_catalog()
                        if row.get('fresh_offer_count') in (0, '0'))
    hosts = list(dict.fromkeys(args.source or SOURCES))
    with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        tasks = [pool.submit(scan_source, host, kinds, indexes, args.start_page, args.pages, state['sources'],
                             collection=args.collection,
                             cimri_sitemap=args.cimri_sitemap and args.collection == 'runner') for host in hosts]
        batches = [task.result() for task in tasks]
    sources = [row for reports, _ in batches for row in reports]
    observed = [row for _, rows in batches for row in rows]
    now = datetime.now(timezone.utc).isoformat()
    merge_state(state, sources, observed, indexes, now)
    if args.lead_limit:
        stopped = {row['source'] for row in sources if row['status'] in STOP_SOURCE}
        with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
            jobs = [pool.submit(scan_retailer_leads, state, host, kinds, args.lead_limit, now,
                                priority, set(args.prioritize_model), collection=args.collection)
                    for host in hosts if host not in stopped]
            for job in jobs:
                job.result()
    if args.state is not None:
        write_json(args.state, state)
    candidates = [row for row in state['products'].values() if row['catalogSlug'] is None]
    existing_with_leads = [row for row in state['products'].values()
                           if row.get('catalogFamily') and row.get('retailerLeads')]
    report = {'checkedAt': now, 'inventory': {kind: len(inventory[kind]) for kind in kinds},
              'sources': sources, 'candidates': candidates, 'existingWithRetailerLeads': existing_with_leads}
    write_json(args.output, report)
    print(json.dumps({
        'pages': sum(row.get('pages', 0) for row in sources),
        'cards': sum(row.get('cards', 0) for row in sources),
        'matched': sum(row.get('matched', 0) for row in sources),
        'uniqueSourceProducts': len({(r['source'], r['id']) for r in state['products'].values()}),
        'candidateRows': len(candidates),
        'candidateRowsWithRetailerLinks': sum(bool(row.get('retailerLeads')) for row in candidates),
        'existingRowsWithRetailerLinks': len(existing_with_leads),
        'output': str(args.output),
    }, ensure_ascii=False))



if __name__ == '__main__':
    main()
