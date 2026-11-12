"""Discover product candidates from public comparison categories without collecting offers."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import gzip
import json
from itertools import zip_longest
from pathlib import Path
import re
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from ayaz_phone_source import AYAZ_HOST, ayaz_phone_url
from catalog_source_collector import catalog_document_reader
from laptop_retailer_sources import WORDS
from ozgur_phone_source import OZGUR_HOST, ozgur_phone_url
from pazarama_category_source import pazarama_product_url
from pc_parts_multistore import DIRECT_PATHS, offer_host
from public_retailer_sources import public_source
import requests
from priority_retailer_transport import PriorityRetailerTransport, compatible_get, product_host

AKAKCE = 'www.akakce.com'
CIMRI = 'www.cimri.com'
EPEY = 'www.epey.com'
URUNAI = 'urunai.com'
KARSILASTIRICI = 'karsilastirici.com'
PAHALIMI = 'pahalimi.com'
AMAHANGISI = 'amahangisi.com'
INDIRIMLI = 'indirimli.com'
TUBERKARAS = 'tuberkaras.com'
UCUZYUM = 'www.ucuzyum.com'
SOURCES = (AKAKCE, CIMRI, EPEY, URUNAI, KARSILASTIRICI, PAHALIMI, AMAHANGISI, INDIRIMLI,
           TUBERKARAS, OZGUR_HOST, UCUZYUM, AYAZ_HOST)
URUNAI_CATEGORIES = {
    'telefon': 'phone', 'kulaklik': 'tws', 'laptop': 'laptop', 'monitor': 'monitor',
    'islemci': 'cpu', 'anakart': 'motherboard', 'ram-bellek': 'ram', 'ekran-karti': 'gpu',
    'ssd': 'ssd', 'bilgisayar-kasasi': 'case', 'guc-kaynagi': 'psu', 'sivi-sogutucu': 'cooler',
}
KARSILASTIRICI_CATEGORIES = {'phone': '/kategori/cep-telefonu', 'laptop': '/kategori/laptop'}
URUNAI_PRODUCT = re.compile(r'/([a-z0-9-]+)/([a-z0-9-]+)/([a-z0-9-]{1,220})')
KARSILASTIRICI_PRODUCT = re.compile(r'/urun/([a-z0-9-]{1,220})')
ADDITIONAL_COMPARISON_CATEGORIES = {
    INDIRIMLI: {
        'phone': '/kategori/cep-telefonu', 'tws': '/kategori/bluetooth-kulaklik',
        'laptop': '/kategori/laptop-notebook', 'monitor': '/kategori/monitor',
        'cpu': '/kategori/islemci', 'motherboard': '/kategori/anakart',
        'ram': '/kategori/ram', 'gpu': '/kategori/ekran-karti', 'ssd': '/kategori/ssd',
        'case': '/kategori/bilgisayar-kasasi', 'psu': '/kategori/power-supply',
        'cooler': '/kategori/islemci-sogutucu',
    },
    TUBERKARAS: {'phone': '/kategori/telefon', 'tws': '/kategori/kulaklik'},
    OZGUR_HOST: {'phone': '/telefon'},
    AYAZ_HOST: {'phone': '/kategori/cep-telefonu'},
}
PAHALIMI_CATEGORIES = {'phone': '/kategori/cep-telefonu', 'laptop': '/kategori/dizustu-bilgisayar',
                       'monitor': '/kategori/monitor', 'tws': '/kategori/kulaklik'}
PAHALIMI_PRODUCT = re.compile(r'/kategori/[a-z0-9-]+/[a-z0-9-]{1,220}-p-([a-f0-9]{8})')
AMAHANGISI_CATEGORIES = {
    'akilli-telefon/android': 'phone', 'akilli-telefon/iphone': 'phone', 'akilli-telefon/harmonyos': 'phone',
    'kulaklik': 'tws', 'laptop': 'laptop', 'monitor': 'monitor', 'islemci': 'cpu',
    'anakart': 'motherboard', 'ram-bellek': 'ram', 'ekran-karti': 'gpu', 'ssd': 'ssd',
    'bilgisayar-kasasi': 'case',
}
AMAHANGISI_PRODUCT = re.compile(
    r'/(' + '|'.join(map(re.escape, AMAHANGISI_CATEGORIES)) + r')/([a-z0-9-]{3,220})')
ADDITIONAL_RETAILER_PATHS = {
    'www.idefix.com': r'/[a-z0-9-]+-p-[1-9]\d{4,11}',
    'www.pttavm.com': r'/[a-z0-9-]+-p-[1-9]\d{5,11}',
    'www.amazon.com.tr': r'/(?:[\w%-]+/)?(?:dp|gp/product)/[A-Z0-9]{10}',
    'www.a101.com.tr': r'/elektronik/[a-z0-9-]+_p-[1-9]\d{5,11}',
    'www.gurgencler.com.tr': r'/iphone-[a-z0-9-]+',
}
PRODUCT_SITEMAPS = {
    CIMRI: ('/sitemaps/product.xml', '/sitemaps/product_'),
    EPEY: ('/sitemap/urun.xml', '/sitemap/urun_'),
    AMAHANGISI: ('/sitemap.xml', '/sitemap/urunler-'),
}
PRODUCT_SITEMAP_LIMIT = 50 * 1024 * 1024
PHONE_MEMORY = re.compile(r'(?: [1-9]\d? gb(?: ram)?)? (?:[1-9]\d{1,3} gb|[1-9] tb)$')
PHONE_STORAGE_THEN_RAM = re.compile(r' (?:[1-9]\d{2,3} gb|[1-9] tb) [1-9]\d? gb(?: ram)?$')
PHONE_COLOR_AFTER_MEMORY = re.compile(
    r'( [1-9]\d{0,3} (?:gb(?: ram)?|tb)) (?:[a-z]{2,16} ){0,2}'
    r'(?:siyah|siyahi|beyaz|beyazi|gri|grisi|mavi|mavisi|mor|moru|yesil|yesili|kirmizi|pembe|'
    r'turuncu|sari|gumus|altin|lacivert|bej|grafit|titanyum)$'
)
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
EPEY_CATEGORIES = {paths[2].strip('/'): kind for kind, paths in CATEGORIES.items()}
CIMRI_CATEGORIES = {paths[1].rsplit('/', 1)[-1]: kind for kind, paths in CATEGORIES.items()}
CIMRI_CATEGORIES['bluetooth-kulaklik'] = 'tws'
AKAKCE_CATEGORIES = {paths[0].split('/')[1].removesuffix('.html'): kind for kind, paths in CATEGORIES.items()}
SNAPSHOT_CATEGORIES = {AKAKCE: AKAKCE_CATEGORIES, CIMRI: CIMRI_CATEGORIES, EPEY: EPEY_CATEGORIES}
CARD = {
    AKAKCE: re.compile(r'/[a-z0-9-]+/en-ucuz-[a-z0-9-]+-fiyati,(\d{4,12})\.html'),
    CIMRI: re.compile(r'/[a-z0-9-]+/en-ucuz-[a-z0-9-]+-fiyatlari,(a?\d{4,12})'),
    EPEY: re.compile(r'/[a-z0-9-]+/[a-z0-9-]+\.html'),
    KARSILASTIRICI: KARSILASTIRICI_PRODUCT,
    PAHALIMI: PAHALIMI_PRODUCT,
    AMAHANGISI: AMAHANGISI_PRODUCT,
    INDIRIMLI: KARSILASTIRICI_PRODUCT,
    TUBERKARAS: KARSILASTIRICI_PRODUCT,
    OZGUR_HOST: re.compile(r'/urun/([a-z0-9-]{6,250})'),
    AYAZ_HOST: re.compile(r'/urun/([a-z0-9-]{6,250})'),
    UCUZYUM: re.compile(r'/[a-z0-9-]{3,220}-cep-telefonu-([1-9]\d{3,9})'),
}
CARD_SELECTOR = {
    AKAKCE: '#CPL a.iC[href][title]',
    CIMRI: 'a[href][title]:has(h3)',
    EPEY: '#listele ul.metin.row[id] a.urunadi[href][title]',
    KARSILASTIRICI: 'a.product-card-link[href]:has(h3)',
    PAHALIMI: 'a.kat-urun-ort[href]',
    INDIRIMLI: 'a.product-card-refresh[href]:has(.product-card-title)',
    TUBERKARAS: 'article.urun-satir .satir-bilgi h3 > a[href]',
    OZGUR_HOST: 'h2.oi-card-title > a[href]',
    AYAZ_HOST: '.showcase-title > a[href][title]',
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
    if host in (PAHALIMI, INDIRIMLI):
        return f'https://{host}{path}?sayfa={page}'
    if host == AYAZ_HOST:
        return f'https://{host}{path}?tp={page}'
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
        if host == INDIRIMLI:
            title = anchor.select_one('.product-card-title').get_text(' ', strip=True)
        elif host == KARSILASTIRICI:
            title = anchor.select_one('h3').get_text(' ', strip=True)
        elif host in (PAHALIMI, TUBERKARAS, OZGUR_HOST):
            title = anchor.get_text(' ', strip=True)
        else:
            title = anchor['title']
        title = ' '.join(title.split())
        if host == OZGUR_HOST:
            title = re.sub(r'\s+\([\w ]{1,70} Türkiye Garantili\)$', '', title, flags=re.I)
        if host == AYAZ_HOST:
            title = re.sub(r'\s*\(\s*[\w ]{1,70} Türkiye Garantili\s*\)', '', title, flags=re.I)
            title = re.sub(r'(\b(\d{2,4})\s*GB)(\s+[^\d]{2,30})\s+\2\s*GB$', r'\1\3', title, flags=re.I)
        if host in ADDITIONAL_COMPARISON_CATEGORIES and re.match(r'iphone\s+\d', title, re.I):
            title = 'Apple ' + title
        if 3 <= len(title) <= 260:
            card = cards.setdefault(source_id, {
                'id': source_id, 'title': title,
                'url': target._replace(path=path, query='', fragment='').geturl(),
            })
            if host == TUBERKARAS:
                raw = anchor.find_parent('article', class_='urun-satir').select('a[rel~=sponsored][href]')
                leads = {store_url for link in raw if (store_url := retailer_destination(link['href']))}
                if leads:
                    card['retailerLeads'] = sorted(leads | set(card.get('retailerLeads', ())))
            elif host == OZGUR_HOST and ozgur_phone_url(card['url']):
                card['retailerLeads'] = [card['url']]
            elif host == AYAZ_HOST and ayaz_phone_url(card['url']):
                card['retailerLeads'] = [card['url']]
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
    label = PHONE_COLOR_AFTER_MEMORY.sub(r'\1', label)
    label = PHONE_STORAGE_THEN_RAM.sub('', label)
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


def additional_comparison_in_scope(kind, title):
    title = WORDS(title)
    if kind == 'phone':
        return not re.search(r'\b(?:uyumlu|dokunmatik ekran|ekran koruyucu|yedek parca|kapak|kilif|maket|tuslu cep telefonu)\b', title)
    return kind != 'tws' or bool(re.search(r'\b(?:tws|kulak ici|airpods|tam kablosuz|acik kulak)\b', title))


def comparison_reader(host, *, collection='runner', **options):
    gateway = catalog_document_reader('competitor') if collection == 'site' and host in (CIMRI, EPEY) else None
    unavailable = (EPEY,) if host == EPEY and collection == 'runner' else ()
    return PriorityRetailerTransport(browser_fallback=False, get=gateway or compatible_get,
                                     unavailable_robots_hosts=unavailable, **options)


def scan_source(host, kinds, indexes, start_page, pages, cursors=None, *, collection='runner',
                cimri_sitemap=False, sitemap_only=0):
    reader = comparison_reader(host, collection=collection)
    reports, observations = [], []
    if host == URUNAI:
        return scan_urunai(reader, kinds, indexes)
    if host == UCUZYUM:
        return scan_ucuzyum(reader, indexes['phone']) if 'phone' in kinds else ([], [])
    if host == AMAHANGISI:
        return scan_product_sitemap(host, kinds, indexes, cursors or {}, reader=reader, maximum=pages)
    stopped = None
    if host in PRODUCT_SITEMAPS and sitemap_only:
        return scan_product_sitemap(host, kinds, indexes, cursors or {}, reader=reader, maximum=sitemap_only)
    if host == CIMRI and cimri_sitemap:
        reports, observations = scan_product_sitemap(host, kinds, indexes, cursors or {}, reader=reader)
    if host in (KARSILASTIRICI, PAHALIMI) and 'phone' in kinds:
        state, observations = known_phones_from_sitemap(reader, host, indexes['phone'])
        reports.append({'source': host, 'category': 'phone_sitemap', 'status': state,
                        'nextPage': None, 'pages': int(state == 'sitemap_read'), 'cards': len(observations)})
        if state in STOP_SOURCE or state.startswith('robots_') and state != 'robots_disallowed':
            stopped = state
    for kind in kinds:
        if host in ADDITIONAL_COMPARISON_CATEGORIES:
            path = ADDITIONAL_COMPARISON_CATEGORIES[host].get(kind)
            if path is None:
                continue
        elif host in (KARSILASTIRICI, PAHALIMI):
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
                if (not in_scope(kind, card['title']) or
                        host in ADDITIONAL_COMPARISON_CATEGORIES and not additional_comparison_in_scope(kind, card['title']) or
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


def scan_ucuzyum(reader, index):
    root = f'https://{UCUZYUM}'
    xml, reason, _ = reader._read_document(root + '/sitemap.xml', UCUZYUM, category=True, sitemap=True)
    cards = {}
    if xml:
        try:
            for url in sitemap_locations(xml, 'urlset'):
                if not url.startswith(root + '/'):
                    continue
                route = urlsplit(url)
                match = CARD[UCUZYUM].fullmatch(route.path)
                if not match or route.query or route.fragment:
                    continue
                title = route.path[1:].rsplit('-cep-telefonu-', 1)[0].replace('-', ' ')
                if not in_scope('phone', title) or not additional_comparison_in_scope('phone', title):
                    continue
                cards.setdefault(match[1], {'id': match[1], 'title': title, 'url': url, 'source': UCUZYUM,
                    'category': 'phone', 'catalogSlug': catalog_match(title, index), 'titleFromSitemap': True})
            reason = 'sitemap_read'
        except (ElementTree.ParseError, ValueError):
            return [{'source': UCUZYUM, 'category': 'phone', 'status': 'invalid_product_sitemap',
                     'pages': 0, 'nextPage': None}], []
    matched = sum(bool(card['catalogSlug']) for card in cards.values())
    return [{'source': UCUZYUM, 'category': 'phone', 'status': reason, 'pages': int(reason == 'sitemap_read'),
             'nextPage': None, 'cards': len(cards), 'matched': matched, 'unmatched': len(cards) - matched}], list(cards.values())


def cimri_sitemap_cards(xml, kinds, indexes):
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
        kind = CIMRI_CATEGORIES.get(category)
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


def epey_sitemap_cards(xml, kinds, indexes):
    cards = {}
    for url in sitemap_locations(xml, 'urlset'):
        if not url.startswith(f'https://{EPEY}/'):
            continue
        route = urlsplit(url)
        if not CARD[EPEY].fullmatch(route.path) or route.query or route.fragment:
            continue
        category, slug = route.path.removesuffix('.html').split('/')[1:]
        kind = EPEY_CATEGORIES.get(category)
        title = slug.replace('-', ' ')
        if kind not in kinds or not in_scope(kind, title):
            continue
        cards.setdefault((kind, slug), {'id': slug, 'title': title, 'url': url, 'source': EPEY,
            'category': kind, 'catalogSlug': catalog_match(title, indexes[kind]), 'titleFromSitemap': True})
    return list(cards.values())


def amahangisi_sitemap_cards(xml, kinds, indexes):
    cards = {}
    for url in sitemap_locations(xml, 'urlset'):
        if not url.startswith(f'https://{AMAHANGISI}/'):
            continue
        route = urlsplit(url)
        match = AMAHANGISI_PRODUCT.fullmatch(route.path)
        if not match or route.query or route.fragment:
            continue
        category, slug = match.groups()
        kind, title = AMAHANGISI_CATEGORIES[category], slug.replace('-', ' ')
        if kind not in kinds or not in_scope(kind, title):
            continue
        cards.setdefault((kind, slug), {'id': slug, 'title': title, 'url': url, 'source': AMAHANGISI,
            'category': kind, 'catalogSlug': catalog_match(title, indexes[kind]), 'titleFromSitemap': True})
    return list(cards.values())


def scan_product_sitemap(host, kinds, indexes, cursors, *, reader=None, maximum=1):
    reader = reader or comparison_reader(host)
    index_path, shard_prefix = PRODUCT_SITEMAPS[host]
    stem = f'https://{host}{shard_prefix}'
    shard_url = re.compile(re.escape(stem) + r'([1-9]\d{0,3})\.xml')
    previous = cursors.get(f'{host}/product_sitemap', {})
    report = {'source': host, 'category': 'product_sitemap', 'pages': 0, 'nextPage': previous.get('nextPage')}
    index, reason, _ = reader._read_document(f'https://{host}{index_path}', host, category=True, sitemap=True)
    if not index:
        return [{**report, 'status': reason}], []
    try:
        shards = sorted({int(match[1]) for url in sitemap_locations(index, 'sitemapindex')
                         if (match := shard_url.fullmatch(url))}, reverse=host == CIMRI)
    except (ValueError, ElementTree.ParseError):
        return [{**report, 'status': 'invalid_product_sitemap_index'}], []
    if not shards:
        return [{**report, 'status': 'product_sitemap_shards_missing'}], []
    start = shards.index(report['nextPage']) if report['nextPage'] in shards else 0
    parse_cards = {CIMRI: cimri_sitemap_cards, EPEY: epey_sitemap_cards,
                   AMAHANGISI: amahangisi_sitemap_cards}[host]
    rows = {}
    report.update(status='sitemap_read', pages=1, shardsRead=[])
    for offset in range(start, min(len(shards), start + maximum)):
        shard = shards[offset]
        report.update(shard=shard, nextPage=shard)
        url = f'{stem}{shard}.xml'
        xml, reason, _ = reader._read_document(url, host, category=True, sitemap=True, sitemap_limit=PRODUCT_SITEMAP_LIMIT)
        if not xml:
            report['status'] = reason
            break
        try:
            cards = parse_cards(xml, kinds, indexes)
        except (ValueError, ElementTree.ParseError):
            report['status'] = 'invalid_product_sitemap'
            break
        rows.update({(card['category'], card['id']): card for card in cards})
        report['pages'] += 1
        report['shardsRead'].append(shard)
        report['nextPage'] = shards[offset + 1] if offset + 1 < len(shards) else None
    matched = sum(bool(card['catalogSlug']) for card in rows.values())
    report.update(cards=len(rows), matched=matched, unmatched=len(rows) - matched)
    return [report], list(rows.values())


def read_state(path):
    if path is None or not path.exists():
        return {'version': 1, 'sources': {}, 'products': {}}
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('version') != 1 or not isinstance(data.get('sources'), dict) or not isinstance(data.get('products'), dict):
        raise ValueError('Unsupported competitor discovery state')
    data['products'] = {product_state_key(row): row for row in data['products'].values()}
    return data


def product_state_key(row):
    identity = row['url'] if row['source'] == EPEY else row['id']
    return f"{row['source']}/{row['category']}/{identity}"


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
        key = product_state_key(row)
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
    if (product_host(url) or first_party and not selectors or pazarama_product_url(url)
            or ozgur_phone_url(url) or ayaz_phone_url(url)):
        return url
    if route.netloc in DIRECT_PATHS and offer_host(url):
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
    elif host in (AMAHANGISI, INDIRIMLI, TUBERKARAS, UCUZYUM):
        canonical = [node.get('href') for node in soup.select('link[rel=canonical]')]
        if canonical != [source_url]:
            return []
        if host == AMAHANGISI:
            raw_links = [anchor['href'] for anchor in soup.select(
                'section#offers #price-panel-offers a[target="_blank"][rel~=noopener][href]')]
        elif host == UCUZYUM:
            raw_links = [anchor['href'] for anchor in soup.select('#t1List a[rel~=sponsored][href]')]
        else:
            raw_links = [anchor['href'] for anchor in soup.select('a[rel~=sponsored][href]')]
            if host == INDIRIMLI:
                raw_links = [target for raw in raw_links if (target := indirimli_retailer_link(raw, source_url))]
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
    return sorted({url for raw in raw_links if (url := comparison_lead_destination(raw))})


def comparison_lead_destination(raw):
    direct = retailer_destination(raw)
    if direct or not isinstance(raw, str) or len(raw) > 600 or re.search(r'[\x00-\x20\\]', raw):
        return direct
    try:
        route = urlsplit(raw)
        if (route.netloc != 'www.pazarama.com' or route.fragment
                or not re.fullmatch(r'magaza=[a-z0-9]+(?:-[a-z0-9]+)*', route.query)):
            return None
        # Treat the canonical PDP as a new lead. It must prove its own seller.
        return retailer_destination(route._replace(query='').geturl())
    except ValueError:
        return None


def indirimli_retailer_link(raw, source_url):
    try:
        route = urlsplit(urljoin(source_url, raw))
        if (route.scheme != 'https' or route.netloc != INDIRIMLI or route.fragment
                or route.path != '/api/affiliate/redirect'):
            return None
        targets = [value for key, value in parse_qsl(route.query, max_num_fields=8) if key == 'product_url']
        return targets[0] if len(targets) == 1 else None
    except ValueError:
        return None


def epey_earbud_title(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    canonical = [node.get('href') for node in soup.select('link[rel=canonical]')]
    titles = [node.get_text(' ', strip=True) for node in soup.select('h1 > a[href]') if node['href'] == url]
    if canonical != [url] or len(titles) != 1:
        return None, 'epey_product_identity_unverified'
    filter_url = f'https://{EPEY}/kulaklik/baglanti-tipi/tam-kablosuz/'
    if ('tam kablosuz' in WORDS(titles[0]) and
            any(node['href'] == filter_url for node in soup.select('#oncelikli a[href]'))):
        return titles[0], None
    return None, 'epey_tws_category_unverified'


def lead_group(row):
    # Match known catalog families or exact normalized names across comparison
    # sites. This is a discovery hint; SKU and seller checks still read the store.
    identity = row.get('catalogFamily') or WORDS(row['title'])
    return row['category'], identity


def lead_inventory(state):
    groups = defaultdict(lambda: defaultdict(set))
    for row in state['products'].values():
        if not row.get('retailerLeads'):
            continue
        for url in row['retailerLeads']:
            groups[lead_group(row)][row['source']].add(urlsplit(url).netloc)
    return groups


def scan_retailer_leads(state, host, kinds, maximum, now, priority=(), pinned=(), *,
                        collection='runner', known_leads=None):
    candidates = defaultdict(list)
    needs_second_store = {group for group, sources in (known_leads or {}).items()
                          if host not in sources and len(set().union(*sources.values())) == 1}
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
        first = [row for row in rows if row.get('catalogFamily') in pinned or row['id'] in pinned]
        others = [row for row in rows if row.get('catalogFamily') not in pinned and row['id'] not in pinned]
        gaps, complementary, rest = [], [], []
        targeted_groups = set()
        for row in others:
            group = lead_group(row)
            if row.get('catalogFamily') in priority:
                gaps.append(row)
            elif group in needs_second_store and group not in targeted_groups:
                complementary.append(row)
                targeted_groups.add(group)
            else:
                rest.append(row)
        candidates[kind] = first + [row for cycle in zip_longest(gaps, complementary, rest)
                                    for row in cycle if row is not None]
    reader = comparison_reader(host, collection=collection, maximum_per_host=maximum)
    offsets = Counter()
    outcomes = Counter()
    # Give priority to the phone and earbud queues after every category has
    # had one turn. Repeated cycles still service the other catalog sections.
    slots = [*kinds, *(kind for _ in range(3) for kind in kinds if kind in ('phone', 'tws'))]
    turn = 0
    while sum(offsets.values()) < maximum and slots:
        position = turn % len(slots)
        kind = slots[position]
        if offsets[kind] >= len(candidates[kind]):
            slots.pop(position)
            continue
        turn += 1
        row = candidates[kind][offsets[kind]]
        offsets[kind] += 1
        html, reason, _ = reader._read_document(row['url'], host)
        row['lastLeadAttemptAt'] = now
        if html is None:
            row['leadStatus'] = reason
            outcomes[reason] += 1
            if reason in STOP_SOURCE or reason.startswith('robots_') and reason != 'robots_disallowed':
                break
        else:
            if host == EPEY and kind == 'tws' and row.get('titleFromSitemap'):
                title, reason = epey_earbud_title(html, row['url'])
                if not title:
                    row['leadStatus'] = reason
                    outcomes[reason] += 1
                    continue
                row['title'] = title
                row.pop('titleFromSitemap')
            leads = comparison_retailer_leads(html, host, row['url'])
            if leads or host != TUBERKARAS:
                row['retailerLeads'] = leads
            row['leadStatus'] = 'direct_retailer_links' if leads else 'direct_retailer_links_missing'
            outcomes[row['leadStatus']] += 1
    return {'source': host, 'requests': sum(offsets.values()), 'outcomes': dict(outcomes)}


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + '.tmp')
    pending.write_text(json.dumps(payload, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    pending.replace(path)


def source_snapshot_row(row, host):
    if not isinstance(row, dict) or row.get('source') != host or not isinstance(row.get('url'), str):
        return None
    url = row['url']
    if not url.startswith(f'https://{host}/') or len(url) > 600:
        return None
    route = urlsplit(url)
    match = CARD[host].fullmatch(route.path)
    if not match or route.query or route.fragment:
        return None
    if host == UCUZYUM:
        kind, identity = 'phone', match[1]
    else:
        path_category, product = route.path.split('/')[1:]
        kind = SNAPSHOT_CATEGORIES[host].get(path_category)
        identity = product.removesuffix('.html') if host == EPEY else match[1]
    title = row.get('title')
    if (not kind or row.get('category') != kind or not isinstance(title, str) or
            not 3 <= len(title) <= 260 or not in_scope(kind, title) or
            host == UCUZYUM and not additional_comparison_in_scope(kind, title)):
        return None
    try:
        first = datetime.fromisoformat(row.get('firstSeenAt', ''))
        last = datetime.fromisoformat(row.get('lastSeenAt', ''))
        if first.tzinfo is None or last.tzinfo is None or first > last:
            return None
    except (TypeError, ValueError):
        return None
    result = {'source': host, 'category': kind, 'id': identity, 'url': url, 'title': title,
              'firstSeenAt': first.astimezone(timezone.utc).isoformat(),
              'lastSeenAt': last.astimezone(timezone.utc).isoformat()}
    if host == CIMRI:
        result['hasMarketPage'] = True
    if row.get('titleFromSitemap') is True:
        result['titleFromSitemap'] = True
    leads = row.get('retailerLeads')
    if isinstance(leads, list):
        links = sorted({url for raw in leads[:100] if (url := retailer_destination(raw))})
        stamp = row.get('lastLeadAttemptAt', '')
        try:
            attempted = datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            return result
        if links and attempted.tzinfo is not None:
            result.update(retailerLeads=links, leadStatus='direct_retailer_links',
                          lastLeadAttemptAt=attempted.astimezone(timezone.utc).isoformat())
    return result


def export_source_snapshot(path, state, host):
    rows = [clean for row in state['products'].values() if (clean := source_snapshot_row(row, host))]
    if not rows:
        raise ValueError(f'No verified {host} discovery rows to export')
    payload = json.dumps({'version': 1, 'source': host, 'products': rows},
                         ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + '.tmp')
    pending.write_bytes(gzip.compress(payload, mtime=0))
    pending.replace(path)
    return len(rows)


def import_source_snapshot(path, state, host):
    limit = 64 * 1024 * 1024
    with gzip.open(path, 'rb') as source:
        body = source.read(limit + 1)
    if len(body) > limit:
        raise ValueError(f'{host} snapshot exceeds the discovery input limit')
    data = json.loads(body)
    if (not isinstance(data, dict) or data.get('version') != 1 or data.get('source') != host
            or not isinstance(data.get('products'), list)):
        raise ValueError(f'Invalid {host} discovery snapshot')
    seen, linked = set(), 0
    for record in data['products']:
        row = source_snapshot_row(record, host)
        if not row:
            continue
        key = product_state_key(row)
        if key in seen:
            continue
        seen.add(key)
        linked += bool(row.get('retailerLeads'))
        previous = state['products'].get(key)
        if not previous:
            state['products'][key] = row
            continue
        if previous.get('titleFromSitemap') and not row.get('titleFromSitemap'):
            previous['title'] = row['title']
            previous.pop('titleFromSitemap')
        previous['lastSeenAt'] = max(previous['lastSeenAt'], row['lastSeenAt'])
        if row.get('retailerLeads') and not previous.get('retailerLeads'):
            previous['retailerLeads'] = row['retailerLeads']
        if row.get('lastLeadAttemptAt', '') > previous.get('lastLeadAttemptAt', ''):
            previous.update({k: row[k] for k in ('lastLeadAttemptAt', 'leadStatus', 'retailerLeads')})
    return {'source': host, 'status': 'imported', 'rows': len(seen), 'withRetailerLinks': linked}


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


def akakce_limits(budget, category_count, pages, leads):
    if not budget:
        return pages, leads
    category_budget = budget - min(leads, budget // 2)
    pages = min(pages, max(1, category_budget // category_count))
    return pages, min(leads, max(0, budget - pages * category_count))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site-data', type=Path)
    parser.add_argument('--phone-sitemap', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--category', action='append', choices=CATEGORIES)
    parser.add_argument('--source', action='append', choices=SOURCES)
    parser.add_argument('--collection', choices=('runner', 'site'), default='runner')
    cimri = parser.add_mutually_exclusive_group()
    cimri.add_argument('--cimri-sitemap', action='store_true', help='Read one Cimri sitemap shard before the categories')
    cimri.add_argument('--cimri-sitemap-only', type=int, choices=range(1, 9), default=0,
                       metavar='SHARDS', help='Resume up to eight Cimri sitemap shards and skip category pages')
    parser.add_argument('--epey-sitemap-only', type=int, choices=range(1, 9), default=0,
                        metavar='SHARDS', help='Resume up to eight Epey sitemap shards and skip category pages')
    parser.add_argument('--import-akakce-snapshot', type=Path, help='Import Akakçe discovery links from a verified source run')
    parser.add_argument('--import-epey-snapshot', type=Path, help='Import Epey discovery links from a verified source run')
    parser.add_argument('--export-epey-snapshot', type=Path, help='Export Epey discovery links without prices')
    parser.add_argument('--import-cimri-snapshot', type=Path, help='Import Cimri discovery links from a verified source run')
    parser.add_argument('--import-ucuzyum-snapshot', type=Path, help='Import Ucuzyum discovery links from a verified source run')
    parser.add_argument('--export-cimri-snapshot', type=Path, help='Export Cimri discovery links without prices')
    parser.add_argument('--export-akakce-snapshot', type=Path, help='Export Akakçe discovery links without prices')
    parser.add_argument('--export-ucuzyum-snapshot', type=Path, help='Export Ucuzyum discovery links without prices')
    parser.add_argument('--lead-only', action='store_true', help='Continue retailer-link discovery from saved candidates')
    parser.add_argument('--state', type=Path, help='Resume category cursors and retain deduplicated candidate IDs')
    parser.add_argument('--prioritize-model', action='append', default=[])
    parser.add_argument('--start-page', type=int, default=1)
    parser.add_argument('--pages', type=int, default=2)
    parser.add_argument('--lead-limit', type=int, default=0, help='Maximum detail pages per source for direct retailer URLs')
    parser.add_argument('--akakce-budget', type=int, default=0,
                        help='Share a request budget between Akakçe category pages and retailer-link discovery')
    args = parser.parse_args()
    kinds = list(dict.fromkeys(args.category or CATEGORIES))
    if args.site_data is None and kinds != ['phone']:
        parser.error('--site-data is required when scanning categories beyond phones')
    if args.start_page < 1 or args.pages < 1 or len(kinds) * args.pages > 40:
        parser.error('Select a positive page number and up to 40 category requests per source')
    if not 0 <= args.lead_limit <= 40:
        parser.error('Select up to 40 detail pages per source')
    if args.lead_only and (not args.state or not args.state.is_file() or not args.lead_limit):
        parser.error('Lead-only mode needs a saved state and a positive lead limit')
    if args.akakce_budget and not len(kinds) <= args.akakce_budget <= 40:
        parser.error('Akakçe budget must allow at least one page per category and no more than 40 requests')
    akakce_pages, akakce_leads = akakce_limits(args.akakce_budget, len(kinds), args.pages, args.lead_limit)
    sitemap_limits = {CIMRI: args.cimri_sitemap_only, EPEY: args.epey_sitemap_only}
    if any(sitemap_limits.values()) and args.collection != 'runner':
        parser.error('Sitemap-only discovery requires direct collection')
    inventory = load_inventory(args.site_data, args.phone_sitemap)
    indexes = {kind: identity_index(inventory[kind]) for kind in kinds}
    state = read_state(args.state)
    source_snapshots = {}
    for host, path in ((AKAKCE, args.import_akakce_snapshot), (EPEY, args.import_epey_snapshot),
                       (CIMRI, args.import_cimri_snapshot), (UCUZYUM, args.import_ucuzyum_snapshot)):
        if path is None:
            continue
        try:
            source_snapshots[host] = import_source_snapshot(path, state, host)
        except (EOFError, OSError, ValueError):
            source_snapshots[host] = {'source': host, 'status': 'snapshot_unavailable_or_invalid'}
    priority = set(args.prioritize_model)
    if args.lead_limit and 'phone' in kinds:
        priority.update(row['slug'] for row in public_phone_catalog()
                        if row.get('fresh_offer_count') in (0, '0'))
    hosts = list(dict.fromkeys(args.source or SOURCES))
    batches = []
    if not args.lead_only:
        with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
            tasks = [pool.submit(scan_source, host, kinds, indexes, args.start_page,
                                 akakce_pages if host == AKAKCE else args.pages, state['sources'],
                                 collection=args.collection,
                                 cimri_sitemap=args.cimri_sitemap and args.collection == 'runner',
                                 sitemap_only=sitemap_limits.get(host, 0)) for host in hosts]
            batches = [task.result() for task in tasks]
    sources = [row for reports, _ in batches for row in reports]
    observed = [row for _, rows in batches for row in rows]
    now = datetime.now(timezone.utc).isoformat()
    merge_state(state, sources, observed, indexes, now)
    lead_scans = []
    if args.lead_limit:
        stopped = {row['source'] for row in sources if row['status'] in STOP_SOURCE}
        if args.lead_only and args.akakce_budget:
            akakce_leads = min(args.lead_limit, args.akakce_budget)
        limits = {host: akakce_leads if host == AKAKCE else min(args.lead_limit, 16)
                  if host in ADDITIONAL_COMPARISON_CATEGORIES or host == UCUZYUM else args.lead_limit for host in hosts}
        known_leads = lead_inventory(state)
        with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
            jobs = [pool.submit(scan_retailer_leads, state, host, kinds, limits[host], now,
                                priority, set(args.prioritize_model), collection=args.collection,
                                known_leads=known_leads)
                    for host in hosts if host not in (OZGUR_HOST, AYAZ_HOST) and host not in stopped and limits[host]]
            lead_scans = [job.result() for job in jobs]
    if args.state is not None:
        write_json(args.state, state)
    for host, path in ((AKAKCE, args.export_akakce_snapshot), (EPEY, args.export_epey_snapshot),
                       (CIMRI, args.export_cimri_snapshot), (UCUZYUM, args.export_ucuzyum_snapshot)):
        if path is not None:
            export_source_snapshot(path, state, host)
    candidates = [row for row in state['products'].values() if row['catalogSlug'] is None]
    existing_with_leads = [row for row in state['products'].values()
                           if row.get('catalogFamily') and row.get('retailerLeads')]
    report = {'checkedAt': now, 'inventory': {kind: len(inventory[kind]) for kind in kinds},
              'sources': sources, 'sourceSnapshot': source_snapshots.get(EPEY),
              'sourceSnapshots': list(source_snapshots.values()), 'retailerLeadScans': lead_scans,
              'candidates': candidates, 'existingWithRetailerLeads': existing_with_leads}
    write_json(args.output, report)
    print(json.dumps({
        'pages': sum(row.get('pages', 0) for row in sources),
        'cards': sum(row.get('cards', 0) for row in sources),
        'matched': sum(row.get('matched', 0) for row in sources),
        'uniqueSourceProducts': len({(r['source'], r['id']) for r in state['products'].values()}),
        'candidateRows': len(candidates),
        'candidateRowsWithRetailerLinks': sum(bool(row.get('retailerLeads')) for row in candidates),
        'existingRowsWithRetailerLinks': len(existing_with_leads),
        'importedProductUrls': {host: result.get('rows', 0) for host, result in source_snapshots.items()},
        'output': str(args.output),
    }, ensure_ascii=False))



if __name__ == '__main__':
    main()
