"""Discover price-free retailer destinations for a fixed monitor cohort."""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from curl_cffi.requests import RequestsError
from requests import RequestException

from catalog_queue import LeaseLost, SourceDeferred
from monitor_pilot import MANUFACTURER_SPECS, monitor_reader, target_host
from monitor_scale import read_json, write_json
from monitor_sources import (ASEMPI_CATEGORY, ASEMPI_HOST, AVANSAS_HOST, COMPAZAR_HOST, ENSONU_CATEGORY, ENSONU_HOST,
                             GPN_HOST, NETHOUSE_HOST, NOT_MONITOR, OVERGAME_HOST, OZAY_HOST, RAMPAGE_HOST, WORDS,
                             asempi_product_id)
from tws_retailer_sources import STOP_REASONS


ROOTS = {
    'www.n11.com': ('https://www.n11.com/bilgisayar/cevre-birimleri/monitor-ve-ekran', 'pg', 8),
    'www.trendyol.com': ('https://www.trendyol.com/monitor-x-c103668', 'pi', 12),
    'www.vatanbilgisayar.com': ('https://www.vatanbilgisayar.com/bilgisayar-monitorleri/', None, 1),
    'www.mediamarkt.com.tr': ('https://www.mediamarkt.com.tr/tr/category/monitor-818179.html', 'page', 8),
    'www.teknosa.com': ('https://www.teknosa.com/monitor-ekran-c-116010011', 'page', 6),
    'www.hepsiburada.com': ('https://www.hepsiburada.com/monitorler-c-57', 'sayfa', 8),
    'www.gaming.gen.tr': ('https://www.gaming.gen.tr/kategori/monitor/', None, 1),
    'www.incehesap.com': ('https://www.incehesap.com/monitor-fiyatlari/', 'page', 5),
    'www.sinerji.gen.tr': ('https://www.sinerji.gen.tr/monitor-c-2029', 'px', 5),
    'pckolik.com.tr': ('https://pckolik.com.tr/monitorler', 'sayfa', 7),
    'pitrap.com': ('https://pitrap.com/kategori/monitorler', None, 1),
    'www.teknostim.com': ('https://www.teknostim.com/kategori/led-monitorler', 'tp', 12),
    'www.uzmanlarpc.com': ('https://www.uzmanlarpc.com/kategori/monitorler', None, 1),
    'www.netsiparis.com': ('https://www.netsiparis.com/monitor-modelleri', None, 1),
    ASEMPI_HOST: (ASEMPI_CATEGORY, 'sayfa', 27),
    OZAY_HOST: (f'https://{OZAY_HOST}/kategori/monitor', 'sayfa', 7),
    OVERGAME_HOST: (f'https://{OVERGAME_HOST}/pc-gaming/monitorler', 'ps', 7),
    GPN_HOST: (f'https://{GPN_HOST}/kategori/monitor-2', None, 1),
    AVANSAS_HOST: (f'https://{AVANSAS_HOST}/monitorler-c-267825', None, 1),
    ENSONU_HOST: (ENSONU_CATEGORY, None, 1),
    RAMPAGE_HOST: (f'https://{RAMPAGE_HOST}/en/collections/gaming-monitor', None, 1),
    COMPAZAR_HOST: (f'https://{COMPAZAR_HOST}/kategoriler/monitorler', 'page', 8),
    NETHOUSE_HOST: (f'https://{NETHOUSE_HOST}/monitor', None, 1),
}
EXTRA_CATEGORY_PATHS = {
    'www.netsiparis.com': ('led-monitor-fiyatlari', 'ips-ekran-fiyatlari', 'dokunmatik-monitorler-1'),
    'www.sinerji.gen.tr': ('msi/monitor-c-2029', 'aoc/monitor-c-2029', 'asus/monitor-c-2029'),
    'www.gaming.gen.tr': ('monitorler/',),
    'pckolik.com.tr': ('monitorler/asus', 'monitorler/msi', 'standart-monitorler'),
    GPN_HOST: ('kategori/monitor', 'kategori/monitor-1'),
    ENSONU_HOST: ('urun-kategori/bilgisayar/cevre-birimleri/monitor/page/2/',),
}
VATAN_BRANDS = ('asus', 'acer', 'aoc', 'benq', 'dell', 'gigabyte', 'hp', 'lenovo', 'lg', 'msi',
                'philips', 'samsung', 'viewsonic', 'xiaomi')
SEARCH = {'www.n11.com': 'read_n11_search', 'www.hepsiburada.com': 'read_hepsiburada_search'}
SEARCH_BUDGET = 48


def page_cards(html, page):
    host = urlsplit(page).netloc
    selector = {
        ASEMPI_HOST: 'a.urun-kart[href]:has(.urun-kart-stok.stok-var)',
        OZAY_HOST: 'a.pcard[href]:has(.chip-stock)',
        OVERGAME_HOST: 'a.ProductName[href]',
        AVANSAS_HOST: 'a.product-card-detail-link[href]',
        NETHOUSE_HOST: '.ProductList .productName > a[href]',
    }.get(host, 'a[href]')
    for anchor in BeautifulSoup(html, 'html.parser').select(selector):
        try:
            url = urljoin(page, anchor['href'])
            if host != ASEMPI_HOST:
                url = url.split('?', 1)[0].split('#', 1)[0]
            elif not asempi_product_id(url):
                continue
            target = urlsplit(url)
        except ValueError:
            continue
        if target.scheme != 'https' or target.netloc != host:
            continue
        image = anchor.select_one('img[alt]')
        title = ' '.join((anchor.get('title', ''), image.get('alt', '') if image else '',
                          anchor.get_text(' ', strip=True)))
        if len(title) <= 650:
            yield url, title


def routing_code(model):
    manufacturer = MANUFACTURER_SPECS.get(model['gtin'], {})
    return (manufacturer.get('discovery_code') if manufacturer.get('code') == model['code'] else None) or model['code']


def match_cards(models, cards):
    """A matching title/slug only schedules a read. It supplies no live facts."""
    hints, seen = [], set()
    identities = defaultdict(list)
    for model in models:
        identities[routing_code(model)].append(model)
    for url, title in cards:
        if url in seen:
            continue
        try:
            slug = urlsplit(url).path
        except ValueError:
            continue
        if NOT_MONITOR.search(WORDS(title)) or NOT_MONITOR.search(WORDS(slug.replace('-', ' '))):
            continue
        codes = set()
        for value in (title, slug):
            words = re.findall(r'[a-z0-9]+', WORDS(value))
            codes.update(''.join(words[i:i+width]) for i in range(len(words)) for width in range(1, 5))
        identity = [model for code in codes & identities.keys() for model in identities[code]]
        if len(identity) != 1:
            continue
        model = identity[0]
        row = {'code': model['code'], 'gtin': model['gtin'], 'url': url}
        if target_host(row):
            hints.append(row)
            seen.add(url)
    return hints


def discover_host(host, models, search_budget, *, reader=None, on_hints=None):
    reader = reader or monitor_reader(host, max(1, search_budget))
    root, page_key, count = ROOTS[host]
    report, rejected, hints = Counter(), Counter(), []
    def add_cards(html, page):
        additions = match_cards(models, page_cards(html, page))
        hints.extend(additions)
        if additions and on_hints is not None:
            on_hints(additions)

    separator = '&' if '?' in root else '?'
    pages = [root, *(f'{root}{separator}{page_key}={index if host == "www.teknosa.com" else index + 1}'
                     for index in range(1, count))]
    if host == 'www.vatanbilgisayar.com':
        pages.extend(f'https://{host}/{brand}/bilgisayar-monitorleri/' for brand in VATAN_BRANDS)
    pages.extend(f'https://{host}/{path}' for path in EXTRA_CATEGORY_PATHS.get(host, ()))
    if host == ASEMPI_HOST:
        pages = [root]
    for page in pages:
        report['category_attempts'] += 1
        try:
            html, _ = reader.read(page, category=True)
        except (LeaseLost, SourceDeferred) as error:
            rejected[error.reason if isinstance(error, SourceDeferred) else 'lease_lost'] += 1
            reader.stop = True
            break
        except (RequestsError, RequestException):
            rejected['transport_error'] += 1
            reader.stop = True
            break
        if html:
            add_cards(html, page)
            if host == ASEMPI_HOST and page == root:
                prefix = urlsplit(root).query + '&sayfa='
                numbers = {int(m[1]) for anchor in BeautifulSoup(html, 'html.parser').select('a[href]')
                           if (m := re.fullmatch(r'\?' + re.escape(prefix) + r'([1-9][0-9]?)', anchor['href']))}
                pages.extend(f'{root}&sayfa={n}' for n in sorted(numbers) if 2 <= n <= count)
        else:
            rejected[reader.last_reason or 'category_unavailable'] += 1
        if reader.stop:
            break
    if host in SEARCH and not reader.stop:
        existing = {model['gtin'] for model in models
                    if any(urlsplit(target['url']).netloc == host for target in model['targets'])}
        new = {hint['gtin'] for hint in hints} - existing
        missing = sorted((model for model in models if model['gtin'] not in existing | new),
                         key=lambda model: model['available_platforms'])
        search = getattr(reader.transport, SEARCH[host])
        for code in dict.fromkeys(routing_code(model) for model in missing):
            if report['search_attempts'] >= search_budget:
                break
            report['search_attempts'] += 1
            try:
                html, reason, _ = search(code)
            except (LeaseLost, SourceDeferred) as error:
                rejected[error.reason if isinstance(error, SourceDeferred) else 'lease_lost'] += 1
                break
            except (RequestsError, RequestException):
                rejected['transport_error'] += 1
                break
            if html:
                add_cards(html, root)
            else:
                rejected[reason] += 1
            if reason in STOP_REASONS or reason.startswith('robots_http_'):
                break
    unique = {row['url']: row for row in hints}
    report['target_hints'] = len(unique)
    report['candidate_gtins'] = len({row['gtin'] for row in unique.values()})
    return list(unique.values()), {**report, 'rejected': dict(rejected),
        'transport': reader.transport.report() if reader.transport else reader.source.report()}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('plan', type=Path)
    cli.add_argument('hints', type=Path)
    cli.add_argument('report', type=Path)
    cli.add_argument('--cards', type=Path, nargs='*', default=[])
    cli.add_argument('--hosts', nargs='+', choices=ROOTS, default=list(ROOTS))
    cli.add_argument('--search-budget', type=int, default=SEARCH_BUDGET, choices=range(SEARCH_BUDGET + 1), metavar='0–48')
    args = cli.parse_args()
    models = read_json(args.plan)['candidates']
    if args.hints.exists() or args.report.exists() or args.hints.resolve() == args.report.resolve():
        cli.error('Choose two new, distinct discovery output files')
    hints, reports = [], {}
    with ThreadPoolExecutor(max_workers=len(args.hosts)) as pool:
        jobs = {host: pool.submit(discover_host, host, models, args.search_budget) for host in args.hosts}
        for host, job in jobs.items():
            rows, reports[host] = job.result()
            hints.extend(rows)
    archived = []
    for path in args.cards:
        document = read_json(path)
        decks = document.values() if all(isinstance(value, dict) for value in document.values()) else [document]
        for deck in decks:
            archived.extend(match_cards(models, deck.items()))
    hints = list({row['url']: row for row in [*archived, *hints]}.values())
    covered = defaultdict(set)
    for row in hints:
        covered[row['gtin']].add(urlsplit(row['url']).netloc)
    missing = [model for model in models if model['available_platforms'] == 1]
    added = sum(bool(covered[model['gtin']] - {urlsplit(row['url']).netloc for row in model['targets']}) for model in missing)
    report = {'candidates': len(models), 'single_platform_candidates': len(missing),
              'single_platform_candidates_with_new_hint': added, 'target_hints': len(hints),
              'archive_target_hints': len({row['url'] for row in archived}), 'stores': reports}
    write_json(args.hints, hints)
    write_json(args.report, report)
    print({key: value for key, value in report.items() if key != 'stores'}, flush=True)


if __name__ == '__main__':
    main()
