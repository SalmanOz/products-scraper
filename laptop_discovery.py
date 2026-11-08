"""Discover laptop offer destinations from public retailer categories and selectors.

Category cards are URL hints only. This command emits manufacturer code/GTIN/URL
triples; it does not export prices or retailer page contents.
"""
import argparse
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from laptop_pilot import FIRST_PARTY, MAX_URLS, PARSERS, _target, compatible, select_offers, verify_seller
from laptop_retailer_sources import model_codes
from mediamarkt_source import MediaMarktSource, SourceBudgetExhausted, SourceCircuitOpen
from n11_catalog_source import discover_product_sellers
from priority_retailer_transport import PriorityRetailerTransport, compatible_get, product_host
from tws_retailer_sources import BOT, STOP_REASONS, _vatan_url
from vatan_category_source import VatanCategorySource

ROOTS = {
    'www.n11.com': 'https://www.n11.com/bilgisayar/dizustu-bilgisayar',
    'www.trendyol.com': 'https://www.trendyol.com/laptop-x-c103108',
    'www.vatanbilgisayar.com': 'https://www.vatanbilgisayar.com/notebook/',
    'www.mediamarkt.com.tr': 'https://www.mediamarkt.com.tr/tr/category/laptop-504926.html',
    'www.teknosa.com': 'https://www.teknosa.com/laptop-notebook-c-116004',
}
BRANDS = frozenset(('acer', 'apple', 'asus', 'casper', 'dell', 'gigabyte', 'hp',
                    'huawei', 'lenovo', 'monster', 'msi', 'samsung'))
STOP = STOP_REASONS | {'redirect', 'unexpected_final_url'}


class Reader:
    def __init__(self, host, maximum, *, get=None):
        self.host = host
        self.stop = False
        self.maximum = maximum
        self.pdp_count = 0
        self.source = self.transport = None
        self.sellers = {}
        if host in FIRST_PARTY:
            self.source = VatanCategorySource(fetch=get) if host.startswith('www.vatan') else MediaMarktSource(get=get or compatible_get)
            if host.startswith('www.mediamarkt'):
                self.source.configure_catalog_budget(maximum * 2 + 20)
            self.source._load_robots()
        else:
            self.transport = PriorityRetailerTransport(maximum_per_host=maximum, get=get, browser_fallback=not get)

    def read(self, url, category=False):
        if self.stop or not category and self.pdp_count >= self.maximum:
            return None, None
        if urlsplit(url).netloc != self.host:
            return None, None
        if not category:
            self.pdp_count += 1
        try:
            if self.host in FIRST_PARTY:
                if not self.source._rules or not self.source._rules.can_fetch(BOT, url):
                    return None, None
                if self.host.startswith('www.vatan'):
                    self.source._sleep(max(.75, self.source._delay))
                    raw, reason, _ = self.source._read(url, 5 * 1024 * 1024, 'text/html')
                else:
                    raw, reason, _ = self.source._direct(url, 5 * 1024 * 1024, 'text/html')
                html = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else raw
                final = url
            else:
                html, reason, _ = self.transport._read_document(url, self.host, category=category)
                final = url if category else self.transport.resolved_url(url)
        except (SourceBudgetExhausted, SourceCircuitOpen):
            self.stop = True
            return None, None
        if reason in STOP:
            self.stop = True
        return (html, final) if reason in ('ready', 'document_read') else (None, None)


def _pdp(host, url):
    p = _target(url)
    if not p or p.netloc != host:
        return False
    if host.startswith('www.vatan'):
        return _vatan_url(url)
    if host.startswith('www.mediamarkt'):
        return bool(re.fullmatch(r'/tr/product/_[a-z0-9-]+-\d{6,12}\.html', p.path))
    return product_host(url) == host


def _category_link(host, target):
    route = urlsplit(target)
    root = urlsplit(ROOTS[host])
    if route.scheme != 'https' or route.netloc != host or route.fragment or len(target) > 220:
        return False
    parts = route.path.strip('/').split('/')
    params = parse_qs(route.query)
    if host.startswith('www.trendyol'):
        return route.path == root.path and set(params) == {'pi'} and len(params['pi']) == 1 and params['pi'][0].isdigit()
    if host.startswith('www.n11'):
        return route.path == root.path and set(params) == {'m'} and len(params['m']) == 1 and params['m'][0].lower() in BRANDS
    if host.startswith('www.vatan'):
        return len(parts) == 2 and parts[0] in BRANDS and parts[1] == 'notebook' and not params
    if host == 'www.teknosa.com':
        return route.path == root.path and set(params) == {'page'} and len(params['page']) == 1 and params['page'][0].isdigit()
    return not params and bool(re.fullmatch(
        r'/tr/category/(?:' + '|'.join(sorted(BRANDS | {'macbook', 'gaming', 'notebook'})) + r')[-a-z]*-\d+\.html', route.path))


def collect_cards(host, reader, max_pages):
    seen, cards = set(), {}
    queue = deque([ROOTS[host]])
    selectors = {
        'www.n11.com': 'a[href]:has(h2.product-item-title)',
        'www.trendyol.com': 'a.product-card[href]',
        'www.vatanbilgisayar.com': '#productsLoad a.product-list-link[href]',
        'www.mediamarkt.com.tr': 'a[data-test="mms-router-link"][href^="/tr/product/"]',
        'www.teknosa.com': 'a[href*="-p-"]:has(img)',
    }
    while queue and len(seen) < max_pages:
        url = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        html, _ = reader.read(url, category=True)
        if not html:
            if reader.stop:
                break
            continue
        soup = BeautifulSoup(html, 'html.parser')
        for card in soup.select(selectors[host]):
            target = urljoin(url, card['href']).split('?')[0]
            if _pdp(host, target) and target not in cards:
                cards[target] = model_codes(f"{card.get_text(' ', strip=True)} {target}")
        for card in soup.select('a[href]'):
            target = urljoin(url, card['href'])
            if target not in seen and target not in queue and _category_link(host, target):
                queue.append(target)
    return cards


def _collect_offers(host, reader, cards, count):
    offers, seen, secondary = [], set(), []
    def inspect(url, expected=None):
        if url in seen:
            return
        seen.add(url)
        html, resolved = reader.read(url)
        if not html or urlsplit(url).query != urlsplit(resolved).query:
            return
        offer, _ = PARSERS[host](html, resolved)
        if (not offer or offer['availability'] != 'in_stock'
                or expected and not compatible(offer, expected)
                or not verify_seller(offer, reader.source, reader.transport, reader.sellers)):
            return
        offers.append(offer)
        if host.startswith('www.n11') and expected is None:
            for hint in discover_product_sellers(html, resolved, maximum=3):
                target = hint['source_url']
                if _pdp(host, target) and hint['discovery_scope'] == 'public_other_seller_selector':
                    secondary.append((target, offer))

    for url in cards[:count]:
        if reader.stop:
            break
        inspect(url)
    for url, first in secondary:
        if reader.stop or reader.pdp_count >= reader.maximum:
            break
        inspect(url, first)
    return offers


def targets_from_observations(observations):
    grouped = defaultdict(list)
    for o in observations:
        grouped[o['facts']['mpn'], o['gtin13']].append(o)
    ready = []
    for identity, options in grouped.items():
        selected = select_offers(options)
        if selected:
            backups = [o for o in options if o not in selected and all(compatible(o, item) for item in selected)]
            ready.append((identity, selected, backups))
    ready.sort(key=lambda row: (-len({o['platform'] for o in row[1]}), -len(row[1]), row[0]))
    rows, seen = [], set()
    for phase in (1, 2):
        for item in ready:
            for o in item[phase]:
                if o['url'] in seen:
                    continue
                seen.add(o['url'])
                rows.append({'mpn': item[0][0], 'gtin': item[0][1], 'url': o['url']})
                if len(rows) >= MAX_URLS:
                    return rows
    return rows


def discover():
    budgets = {'www.n11.com': 240, 'www.trendyol.com': 230,
               'www.vatanbilgisayar.com': 130, 'www.mediamarkt.com.tr': 85, 'www.teknosa.com': 85}
    readers = {host: Reader(host, count) for host, count in budgets.items()}
    with ThreadPoolExecutor(max_workers=4) as pool:
        all_cards = {host: job.result() for host, job in
                     [(h, pool.submit(collect_cards, h, reader, 24 if h.startswith('www.trendyol') else 20))
                      for h, reader in readers.items()]}
        sources = defaultdict(set)
        for host, cards in all_cards.items():
            for codes in cards.values():
                for code in codes:
                    sources[code].add(host)
        jobs = []
        for host, cards in all_cards.items():
            rank = lambda url: max((len(sources[code]) for code in cards[url]), default=0)
            jobs.append(pool.submit(_collect_offers, host, readers[host], sorted(cards, key=rank, reverse=True),
                                    120 if host.startswith('www.n11') else budgets[host]))
        return targets_from_observations([o for job in jobs for o in job.result()])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    rows = discover()
    args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'targets': len(rows), 'models': len({o['mpn'] for o in rows}),
                      'sources': Counter(urlsplit(o['url']).netloc for o in rows)}))


if __name__ == '__main__':
    main()
