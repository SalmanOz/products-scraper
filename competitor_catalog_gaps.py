"""Discover product candidates from public comparison categories without collecting offers."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from catalog_source_collector import catalog_document_reader
from laptop_retailer_sources import WORDS
from priority_retailer_transport import PriorityRetailerTransport, compatible_get, product_host

AKAKCE = 'www.akakce.com'
CIMRI = 'www.cimri.com'
EPEY = 'www.epey.com'
SOURCES = (AKAKCE, CIMRI, EPEY)
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
}
CARD_SELECTOR = {
    AKAKCE: '#CPL a.iC[href][title]',
    CIMRI: 'a[href][title]:has(h3)',
    EPEY: '#listele ul.metin.row[id] a.urunadi[href][title]',
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
        if target.scheme == 'https' and target.netloc == host and not target.fragment:
            links.add(unquote(target.geturl()))
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
        title = ' '.join(anchor['title'].split())
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
    ):
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


def comparison_reader(host, **options):
    gateway = catalog_document_reader('competitor') if host in (CIMRI, EPEY) else None
    return PriorityRetailerTransport(browser_fallback=False, get=gateway or compatible_get, **options)


def scan_source(host, kinds, indexes, start_page, pages, cursors=None):
    reader = comparison_reader(host)
    reports, observations = [], []
    stopped = None
    for kind in kinds:
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
    return reports, observations


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
            'completedPasses': previous.get('completedPasses', 0) + (row['nextPage'] is None),
        }
    for row in observations:
        key = f"{row['source']}/{row['category']}/{row['id']}"
        previous = state['products'].get(key, {})
        state['products'][key] = {**previous, **row, 'firstSeenAt': previous.get('firstSeenAt', now),
                                  'lastSeenAt': now}
    for row in state['products'].values():
        if row['category'] in indexes:
            row['catalogSlug'] = catalog_match(row['title'], indexes[row['category']])
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
    return url if product_host(url) or first_party and not selectors else None


def comparison_retailer_leads(html, host, source_url):
    soup = BeautifulSoup(html, 'html.parser')
    if host == EPEY:
        raw_links = [unquote(anchor['data-link']) for anchor in soup.select('a.git[data-link]:has(.urun_adi)')]
    else:
        raw_links = []
        expected = unquote(source_url)
        for node in soup.select('script[type="application/ld+json"]'):
            try:
                product = json.loads(node.get_text())
            except ValueError:
                continue
            if not isinstance(product, dict) or product.get('@type') not in {'Product', 'ProductGroup'}:
                continue
            canonical = product.get('url') or product.get('@id')
            if not isinstance(canonical, str) or unquote(canonical) != expected:
                continue
            aggregate = product.get('offers')
            if not isinstance(aggregate, dict) or aggregate.get('@type') != 'AggregateOffer':
                continue
            listings = aggregate.get('offers')
            if isinstance(listings, list):
                raw_links.extend(offer.get('url') for offer in listings if isinstance(offer, dict))
    return sorted({url for raw in raw_links if (url := retailer_destination(raw))})


def scan_retailer_leads(state, host, kinds, maximum, now):
    candidates = defaultdict(list)
    for row in state['products'].values():
        if row['source'] == host and row['category'] in kinds and row['catalogSlug'] is None:
            candidates[row['category']].append(row)
    for rows in candidates.values():
        rows.sort(key=lambda row: (row.get('lastLeadAttemptAt', ''), row['firstSeenAt'], row['id']))
    reader = comparison_reader(host, maximum_per_host=maximum)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site-data', required=True, type=Path)
    parser.add_argument('--phone-sitemap', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--category', action='append', choices=CATEGORIES)
    parser.add_argument('--source', action='append', choices=SOURCES)
    parser.add_argument('--state', type=Path, help='Resume category cursors and retain deduplicated candidate IDs')
    parser.add_argument('--start-page', type=int, default=1)
    parser.add_argument('--pages', type=int, default=2)
    parser.add_argument('--lead-limit', type=int, default=0, help='Maximum detail pages per source for direct retailer URLs')
    args = parser.parse_args()
    kinds = list(dict.fromkeys(args.category or CATEGORIES))
    if args.start_page < 1 or args.pages < 1 or len(kinds) * args.pages > 40:
        parser.error('Select a positive page number and up to 40 category requests per source')
    if not 0 <= args.lead_limit <= 40:
        parser.error('Select up to 40 detail pages per source')
    inventory = load_inventory(args.site_data, args.phone_sitemap)
    indexes = {kind: identity_index(inventory[kind]) for kind in kinds}
    state = read_state(args.state)
    hosts = list(dict.fromkeys(args.source or SOURCES))
    with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        tasks = [pool.submit(scan_source, host, kinds, indexes, args.start_page, args.pages, state['sources'])
                 for host in hosts]
        batches = [task.result() for task in tasks]
    sources = [row for reports, _ in batches for row in reports]
    observed = [row for _, rows in batches for row in rows]
    now = datetime.now(timezone.utc).isoformat()
    merge_state(state, sources, observed, indexes, now)
    if args.lead_limit:
        stopped = {row['source'] for row in sources if row['status'] in STOP_SOURCE}
        with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
            jobs = [pool.submit(scan_retailer_leads, state, host, kinds, args.lead_limit, now)
                    for host in hosts if host not in stopped]
            for job in jobs:
                job.result()
    if args.state is not None:
        write_json(args.state, state)
    candidates = [row for row in state['products'].values() if row['catalogSlug'] is None]
    report = {'checkedAt': now, 'inventory': {kind: len(inventory[kind]) for kind in kinds},
              'sources': sources, 'candidates': candidates}
    write_json(args.output, report)
    print(json.dumps({
        'pages': sum(row.get('pages', 0) for row in sources),
        'cards': sum(row.get('cards', 0) for row in sources),
        'matched': sum(row.get('matched', 0) for row in sources),
        'uniqueSourceProducts': len({(r['source'], r['id']) for r in state['products'].values()}),
        'candidateRows': len(candidates),
        'candidateRowsWithRetailerLinks': sum(bool(row.get('retailerLeads')) for row in candidates),
        'output': str(args.output),
    }, ensure_ascii=False))



if __name__ == '__main__':
    main()
