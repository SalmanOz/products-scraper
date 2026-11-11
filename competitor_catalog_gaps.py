"""Discover product candidates from public comparison categories without collecting offers."""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import unquote, urljoin, urlsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from laptop_retailer_sources import WORDS
from priority_retailer_transport import PriorityRetailerTransport, compatible_get

AKAKCE = 'www.akakce.com'
CIMRI = 'www.cimri.com'
CATEGORIES = {
    'phone': ('/cep-telefonu.html', '/cep-telefonlari'),
    'laptop': ('/laptop-notebook.html', '/dizustu-bilgisayar'),
    'monitor': ('/monitor.html', '/monitor'),
    'tws': ('/bluetooth-kulaklik/tws.html', '/kulaklik/kulak-ici-bluetooth-kulaklik'),
    'cpu': ('/islemci.html', '/islemci'),
    'motherboard': ('/anakart.html', '/anakart'),
    'ram': ('/ram.html', '/ram-bellekler'),
    'gpu': ('/ekran-karti.html', '/ekran-kartlari'),
    'ssd': ('/ssd.html', '/ssd-solid-state-drive'),
    'case': ('/bilgisayar-kasasi.html', '/pc-kasalari'),
    'psu': ('/power-supply.html', '/power-supply'),
    'cooler': ('/islemci-sogutucu.html', '/islemci-sogutucu'),
}
CARD = {
    AKAKCE: re.compile(r'/[a-z0-9-]+/en-ucuz-[a-z0-9-]+-fiyati,(\d{4,12})\.html'),
    CIMRI: re.compile(r'/[a-z0-9-]+/en-ucuz-[a-z0-9-]+-fiyatlari,(\d{4,12})'),
}
MODEL_TOKEN = re.compile(r'[a-zA-Z0-9]+(?:[-./_][a-zA-Z0-9]+)*')
STOP_SOURCE = {'http_403', 'http_429', 'challenge_page', 'source_circuit_open',
               'source_cooldown', 'transport_error', 'category_budget_exhausted'}


def category_url(host, path, page):
    if page == 1:
        return 'https://' + host + path
    if host == AKAKCE:
        return f'https://{host}{path.removesuffix(".html")},{page}.html'
    return f'https://{host}{path}?page={page}'


def comparison_cards(html, host, url):
    soup = BeautifulSoup(html, 'html.parser')
    cards = {}
    links = set()
    for anchor in soup.select('a[href]'):
        try:
            target = urlsplit(urljoin(url, anchor['href']))
        except ValueError:
            continue
        if target.scheme != 'https' or target.netloc != host:
            continue
        links.add(unquote(target.geturl()))
        match = CARD[host].fullmatch(unquote(target.path))
        title = ' '.join(anchor.get('title', '').split())
        if not match or not 3 <= len(title) <= 260 or host == CIMRI and not anchor.find('h3'):
            continue
        cards.setdefault(match[1], {
            'id': match[1], 'title': title,
            'url': target._replace(path=unquote(target.path), query='', fragment='').geturl(),
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
    if kind == 'ssd' and re.search(r'\b(?:tasinabilir|harici|portable|external)\b', title):
        return False
    return True


def scan_source(host, kinds, indexes, start_page, pages):
    reader = PriorityRetailerTransport(browser_fallback=False, get=compatible_get)
    reports, candidates = [], []
    stopped = None
    for kind in kinds:
        akakce_path, cimri_path = CATEGORIES[kind]
        path = cimri_path if host == CIMRI else akakce_path
        stats = Counter()
        seen = set()
        state = 'page_limit'
        next_page = start_page
        for page in range(start_page, start_page + pages):
            if stopped:
                state = stopped
                break
            url = category_url(host, path, page)
            body, reason, _ = reader._read_document(url, host, category=True)
            if body is None:
                state = reason
                if reason in STOP_SOURCE or reason.startswith('robots_'):
                    stopped = reason
                break
            stats['pages'] += 1
            cards, links = comparison_cards(body, host, url)
            distinct = [card for card in cards if card['id'] not in seen]
            if not distinct:
                state = 'no_new_product_cards'
                next_page = None
                break
            seen.update(card['id'] for card in distinct)
            stats['cards'] += len(distinct)
            for card in distinct:
                if not in_scope(kind, card['title']):
                    stats['excluded'] += 1
                    continue
                known = catalog_match(card['title'], indexes[kind])
                stats['matched' if known else 'unmatched'] += 1
                if not known:
                    candidates.append({**card, 'source': host, 'category': kind})
            if category_url(host, path, page + 1) not in links:
                state = 'no_next_page_link'
                next_page = None
                break
            next_page = page + 1
        reports.append({'source': host, 'category': kind, 'status': state, 'nextPage': next_page, **stats})
    return reports, candidates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site-data', required=True, type=Path)
    parser.add_argument('--phone-sitemap', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--category', action='append', choices=CATEGORIES)
    parser.add_argument('--start-page', type=int, default=1)
    parser.add_argument('--pages', type=int, default=2)
    args = parser.parse_args()
    kinds = list(dict.fromkeys(args.category or CATEGORIES))
    if args.start_page < 1 or args.pages < 1 or len(kinds) * args.pages > 40:
        parser.error('Select a positive page number and up to 40 category requests per source')
    inventory = load_inventory(args.site_data, args.phone_sitemap)
    indexes = {kind: identity_index(inventory[kind]) for kind in kinds}
    with ThreadPoolExecutor(2) as pool:
        tasks = [pool.submit(scan_source, host, kinds, indexes, args.start_page, args.pages)
                 for host in (AKAKCE, CIMRI)]
        batches = [task.result() for task in tasks]
    sources = [row for reports, _ in batches for row in reports]
    candidates = [row for _, rows in batches for row in rows]
    report = {'checkedAt': datetime.now(timezone.utc).isoformat(),
              'inventory': {kind: len(inventory[kind]) for kind in kinds},
              'sources': sources, 'candidates': candidates}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({
        'pages': sum(row.get('pages', 0) for row in sources),
        'cards': sum(row.get('cards', 0) for row in sources),
        'matched': sum(row.get('matched', 0) for row in sources),
        'candidateRows': len(candidates),
        'output': str(args.output),
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
