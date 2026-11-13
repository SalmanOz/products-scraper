"""Queue, reverify and export exact catalog offers without production writes."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from email.utils import parsedate_to_datetime
import json
from itertools import combinations, groupby
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
import time
from urllib.parse import urlsplit

from curl_cffi.requests import RequestsError
from requests import RequestException

from catalog_inventory import (
    FEED_FORMATS, MAX_SITEMAPS, MAX_XML, InventoryError, StoreInventory, compare_feed_offer, feed_routes, sitemap_hints,
    store_url, xml_root,
)
from catalog_queue import CatalogQueue, DAY, LeaseLost, RETRY_SECONDS, SourceDeferred
from laptop_discovery import Reader, _pdp
from laptop_direct_source import DIRECT_STORES
from laptop_pilot import PARSERS as LAPTOP_PARSERS, _hepsiburada_public_get, _target, valid_pair, verify_seller
from monitor_pilot import independent_pair, target_host, verify_monitor_document
from monitor_scale_discovery import ROOTS, SEARCH_BUDGET, discover_host
from monitor_sources import ASEMPI_HOST, DIRECT_MONITOR_STORES, PCKOLIK_HOST
from priority_retailer_transport import compatible_get
from retailer_barcode import retailer_gtin13
from tws_catalog_expansion import MARKETPLACES, STORES, exact_tws_sellers, verify_media
from tws_offer_pilot import _mediamarkt_tws_url, model_name, selected_tws_offer
from tws_retailer_sources import _vatan_url, selected_n11_tws, selected_vatan_tws


CATEGORIES = {'monitor': 'code', 'laptop': 'mpn', 'earbuds': 'model'}
DIRECT_FEED_AUDIT_HOSTS = set(DIRECT_STORES) | DIRECT_MONITOR_STORES | {
    ASEMPI_HOST, PCKOLIK_HOST, 'www.vatanbilgisayar.com',
}


def retry_after(raw, now, default):
    if isinstance(raw, str):
        try:
            if re.fullmatch(r'[0-9]{1,10}', raw.strip()):
                return max(default, int(raw))
            deadline = parsedate_to_datetime(raw)
            if deadline.tzinfo is not None:
                return max(default, int(deadline.timestamp()) - now)
        except (ValueError, TypeError, OverflowError):
            pass
    return default


class BudgetedGet:
    def __init__(self, queue, host, lease, *, get=None, sleep=time.sleep):
        self.queue, self.host, self.lease = queue, host, lease
        self.get = get or (_hepsiburada_public_get if host == 'www.hepsiburada.com' else compatible_get)
        self.sleep = sleep
        self.rejection = None

    def __call__(self, url, **kwargs):
        if urlsplit(url).scheme != 'https' or urlsplit(url).netloc != self.host:
            raise SourceDeferred('cross_store_evidence_unavailable')
        while True:
            reason, until = self.queue.before_request(self.host, self.lease)
            if reason == 'ready':
                break
            if reason != 'wait':
                if until:
                    self.queue.block(self.host, self.lease, reason, max(1, until - int(self.queue.clock())))
                raise SourceDeferred(reason, until)
            self.sleep(max(0, until - self.queue.clock()))
        try:
            response = self.get(url, **dict(kwargs, allow_redirects=False))
        except (RequestException, RequestsError):
            self._stop('transport_error', RETRY_SECONDS)
            raise
        status = response.status_code
        if status == 403:
            self._stop('http_403', DAY)
        elif status == 429 or status in (500, 502, 503, 504):
            seconds = retry_after(response.headers.get('Retry-After'), int(self.queue.clock()),
                                  60 * 60 if status == 429 else RETRY_SECONDS)
            self._stop(f'http_{status}', seconds)
        return response

    def _stop(self, reason, seconds):
        self.queue.block(self.host, self.lease, reason, seconds)
        self.rejection = (reason, int(self.queue.clock()) + seconds)


def normalize_route(category, row, *, priority=0):
    key = CATEGORIES[category]
    route = {k: row.get(k) for k in ('url', 'gtin', key)}
    url, gtin, code = (route[k] for k in ('url', 'gtin', key))
    if not all(isinstance(x, str) for x in (url, gtin, code)) or retailer_gtin13(gtin) != gtin:
        raise ValueError('Expected a URL, an exact GTIN and a category model code')
    if not 0 <= priority <= 100:
        raise ValueError('Priority must be between 0 and 100')
    if category == 'monitor':
        host = target_host(route)
    elif category == 'laptop':
        parsed = _target(url)
        host = parsed.netloc if parsed and re.fullmatch(r'[A-Z0-9.\-/+#]{5,36}', code) and (
            _pdp(parsed.netloc, url) or parsed.netloc in DIRECT_STORES) else None
    else:
        parsed = urlsplit(url)
        host = parsed.netloc
        allowed = (host in MARKETPLACES and _pdp(host, url) or host in STORES and (
            _vatan_url(url) if host.startswith('www.vatan') else _mediamarkt_tws_url(url)))
        if not allowed or not code.strip() or len(code) > 160:
            host = None
    if host is None:
        raise ValueError(f'Unsupported {category} product destination')
    if category == 'earbuds' and row.get('merchant'):
        merchant, merchant_id = row['merchant'], row.get('merchant_id')
        if not isinstance(merchant, str) or len(merchant) > 150 or not isinstance(merchant_id, (str, int, type(None))):
            raise ValueError('Invalid selected earbud seller')
        route['merchant'] = merchant
        if merchant_id is not None:
            route['merchant_id'] = merchant_id
    return host, category, route, priority


def verify_target(reader, host, category, route, *, seller_links=None):
    html, final = reader.read(route['url'])
    if not html or not final:
        return None, reader.last_reason or 'selected_document_unavailable'
    if category == 'monitor':
        return verify_monitor_document(reader, host, route, html, final, seller_links=seller_links)
    if urlsplit(route['url']).query != urlsplit(final).query:
        return None, 'listing_selector_changed'
    if category == 'laptop':
        offer, reason = LAPTOP_PARSERS[host](html, final)
        if not offer:
            return None, reason
        if (offer['facts']['mpn'] != route['mpn'] or offer['gtin13'] != route['gtin']
                or not 3_000 <= offer['price'] <= 1_000_000):
            return None, 'laptop_variant_changed'
        if not verify_seller(offer, reader.source, reader.transport, reader.sellers):
            return None, 'seller_identity_unproven'
    else:
        name = route['model']
        if host == 'www.n11.com':
            offer, reason = selected_n11_tws(html, final, name, model_name)
        elif host == 'www.trendyol.com':
            offer, reason = selected_tws_offer(html, final, name)
        else:
            offer = (selected_vatan_tws(html, final, name, model_name) if host.startswith('www.vatan')
                     else verify_media(html, final, name))
            reason = 'first_party_evidence_missing'
        if not offer:
            return None, reason
        if (offer.get('gtin13') != route['gtin'] or not 100 <= offer['price'] <= 150_000
                or 'merchant' in route and offer['merchant'] != route['merchant']
                or 'merchant_id' in route and offer.get('merchant_id') != route['merchant_id']):
            return None, 'earbud_variant_or_seller_changed'
        if not offer.get('_legal_name'):
            return None, 'seller_identity_unproven'
    return offer, 'verified'


def verify_store(path, host, limit, *, get=None, category=None, focus_gtins=()):
    outcomes = Counter()
    with closing(CatalogQueue(path)) as queue:
        lease = queue.acquire(host, category=category)
        if lease is None:
            return {}
        transport = BudgetedGet(queue, host, lease, get=get)
        reader = None
        try:
            for _ in range(limit):
                job = queue.next_target(host, lease, category=category, focus_gtins=focus_gtins)
                if job is None:
                    break
                target_category, route = job
                retry_at = None
                keep = stop = False
                offer = None
                seller_links = None
                if target_category == 'monitor' and host in {'www.n11.com', 'www.trendyol.com'}:
                    offers = queue.fresh_offers('monitor', gtin=route['gtin'])
                    if not any(independent_pair(a, b) for a, b in combinations(offers, 2)):
                        seller_links = []
                try:
                    if reader is None:
                        reader = Reader(host, limit, get=transport)
                    offer, outcome = verify_target(reader, host, target_category, route, seller_links=seller_links)
                except SourceDeferred as error:
                    outcome = error.reason
                    stop = outcome != 'cross_store_evidence_unavailable'
                    keep = True
                    retry_at = error.retry_at or int(queue.clock()) + RETRY_SECONDS
                except (RequestException, RequestsError):
                    outcome = 'transport_error'
                except (TypeError, ValueError, KeyError, AttributeError):
                    outcome = 'selected_document_invalid'
                if offer is None and transport.rejection:
                    reason, retry_at = transport.rejection
                    if outcome in {'seller_identity_unproven', 'transport_error'}:
                        outcome = reason
                    keep = stop = True
                elif offer is None and outcome in {'source_circuit_open', 'challenge_page', 'source_cooldown',
                                                   'transport_error', 'request_error', 'source_budget_or_circuit'}:
                    stop = keep = True
                    retry_at = int(queue.clock()) + RETRY_SECONDS
                    queue.block(host, lease, outcome, RETRY_SECONDS)
                # A first-party redirect to another product may mean a single retired route.
                if reader and reader.stop and outcome == 'redirect' and reader.source:
                    reader.stop = False
                try:
                    queue.complete(host, lease, route, outcome, offer, keep=keep, retry_at=retry_at)
                except (ValueError, TypeError, KeyError):
                    outcome = 'offer_evidence_contract_failed'
                    queue.complete(host, lease, route, outcome)
                outcomes[outcome] += 1
                if seller_links:
                    queue.enqueue((normalize_route('monitor', {**route, 'url': url})
                                   for url in dict.fromkeys(seller_links)), conflicts=Counter())
                if stop or reader and reader.stop:
                    break
        except LeaseLost:
            outcomes['lease_lost'] += 1
        finally:
            queue.release(host, lease)
    return dict(outcomes)


def discover_store(path, host, models, search_budget):
    with closing(CatalogQueue(path)) as queue:
        lease = queue.acquire(host, discovery=True)
        if lease is None:
            return {'status': 'busy_or_deferred'}
        get = BudgetedGet(queue, host, lease)
        conflicts = Counter()
        added = 0

        def checkpoint(hints):
            nonlocal added
            added += queue.enqueue((normalize_route('monitor', row) for row in hints), conflicts=conflicts)

        try:
            reader = Reader(host, max(1, search_budget), get=get)
            _, report = discover_host(host, models, search_budget, reader=reader, on_hints=checkpoint)
            return {**report, 'enqueued': added, 'identity_conflicts': dict(conflicts)}
        except SourceDeferred as error:
            return {'status': error.reason}
        except LeaseLost:
            return {'status': 'lease_lost', 'enqueued': added}
        except (RequestsError, RequestException):
            return {'status': 'transport_error'}
        finally:
            queue.release(host, lease)


def discover_inventory(queue, host, consume, *, get=None, sleep=time.sleep):
    lease = queue.acquire(host, discovery=True)
    if lease is None:
        return {'status': 'busy_or_deferred'}
    try:
        inventory = StoreInventory(host, BudgetedGet(queue, host, lease, get=get, sleep=sleep))
        return {**consume(inventory), 'http_requests': inventory.requests}
    except InventoryError as error:
        return {'status': str(error)}
    except SourceDeferred as error:
        return {'status': error.reason}
    except LeaseLost:
        return {'status': 'lease_lost'}
    except (RequestsError, RequestException):
        return {'status': 'transport_error'}
    finally:
        queue.release(host, lease)


def discover_sitemaps(path, host, models, maximum, roots=()):
    with closing(CatalogQueue(path)) as queue:
        def consume(inventory):
            hints, stats = sitemap_hints(inventory, models, maximum, roots=roots)
            conflicts = Counter()
            stats['enqueued'] = queue.enqueue((normalize_route('monitor', row) for row in hints), conflicts=conflicts)
            return {**stats, 'identity_conflicts': dict(conflicts)}
        return discover_inventory(queue, host, consume)


def import_inventory(queue, root, feed_format, host, category, models, *, commerce=None):
    code = CATEGORIES[category]
    identities = {}
    for model in models:
        gtin = model.get('gtin')
        if not isinstance(gtin, str) or retailer_gtin13(gtin) != gtin or not isinstance(model.get(code), str):
            raise InventoryError('invalid_catalog_identity')
        identity = {code: model[code], 'gtin': gtin}
        if gtin in identities and identities[gtin] != identity:
            raise InventoryError('ambiguous_catalog_identity')
        identities[gtin] = identity
    candidates, report = feed_routes(root, feed_format, host, identities, lambda row: normalize_route(category, row),
                                     commerce=commerce)
    conflicts = Counter()
    report['enqueued'] = queue.enqueue(candidates, conflicts=conflicts)
    return {**report, 'identity_conflicts': dict(conflicts)}


def audit_inventory_offers(queue, host, category, commerce, maximum):
    """Compare only selected, freshly read offers with in-memory feed claims."""
    counts, rejected = Counter(), Counter()
    candidates = [item for item in commerce.values() if item is not None and None not in item[1]]
    counts['feed_commerce_unproven'] = len(commerce) - len(candidates)
    if not candidates:
        return dict(counts)
    lease = queue.acquire(host, discovery=True)
    if lease is None:
        return {**counts, 'status': 'busy_or_deferred'}
    transport = BudgetedGet(queue, host, lease)
    try:
        reader = Reader(host, maximum, get=transport)
        for route, claim in candidates[:maximum]:
            counts['attempted'] += 1
            offer, reason = verify_target(reader, host, category, route)
            if offer is None:
                rejected[reason] += 1
            else:
                counts[compare_feed_offer(claim, offer)] += 1
            if reader.stop or transport.rejection:
                break
    except SourceDeferred as error:
        rejected[error.reason] += 1
    except LeaseLost:
        rejected['lease_lost'] += 1
    except (RequestsError, RequestException):
        rejected['transport_error'] += 1
    finally:
        queue.release(host, lease)
    return {**counts, 'live_rejected': dict(rejected), 'not_attempted': len(candidates) - counts['attempted']}


def rows(path, category):
    if path.suffix == '.jsonl':
        with path.open(encoding='utf-8') as stream:
            for line in stream:
                if line.strip():
                    yield json.loads(line), 0
        return
    document = json.loads(path.read_text(encoding='utf-8'))
    if isinstance(document, list):
        for row in document:
            yield row, 0
    elif 'candidates' in document:
        for model in document['candidates']:
            for row in model['targets']:
                yield row, 50 if model['published'] else 0
    elif category == 'earbuds':
        for model in document['models']:
            if model.get('gtin'):
                for offer in model['offers']:
                    yield {**offer, 'gtin': model['gtin'], 'model': model['name']}, 25
    else:
        raise ValueError('Expected a price-free target list or discovery plan')


def coverage_report(queue):
    report = queue.report()
    pair_checks = {
        'monitor': independent_pair,
        'laptop': valid_pair,
        'earbuds': lambda a, b: a['model'] == b['model'] and bool(exact_tws_sellers((a, b))),
    }
    for category in report['categories']:
        name = category['category']
        pair_check = pair_checks[name]
        category['fresh_independent_pairs'] = sum(
            any(a['platform'] != b['platform'] and pair_check(a, b) for a, b in combinations(group, 2))
            for _, group in groupby(queue.fresh_offers(name), key=lambda row: row['gtin13']))
    return report


def export_offers(queue, category, destination):
    """Expose an owner-only snapshot only after the full export succeeds."""
    with NamedTemporaryFile(mode='w', encoding='utf-8', dir=destination.parent) as output:
        for offer in queue.fresh_offers(category):
            output.write(json.dumps(offer, ensure_ascii=False, allow_nan=False) + '\n')
        output.flush()
        os.fsync(output.fileno())
        os.link(output.name, destination)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('db', type=Path)
    commands = cli.add_subparsers(dest='command', required=True)
    enqueue = commands.add_parser('enqueue')
    enqueue.add_argument('category', choices=CATEGORIES)
    enqueue.add_argument('targets', nargs='+', type=Path)
    discover = commands.add_parser('discover', help='Find additional monitor destinations within the shared HTTP budget')
    discover.add_argument('plan', type=Path)
    discover.add_argument('--search-budget', type=int, default=12)
    discover.add_argument('--store', action='append', choices=ROOTS, default=[])
    sitemap = commands.add_parser('sitemap', help='Find monitor destinations in retailer sitemaps')
    sitemap.add_argument('plan', type=Path)
    sitemap.add_argument('--store', action='append', required=True, choices=ROOTS)
    sitemap.add_argument('--sitemap-url', action='append', default=[],
                         help='Use a store-owned HTTPS sitemap URL for the selected stores')
    sitemap.add_argument('--documents-per-store', type=int, default=8, choices=range(1, MAX_SITEMAPS + 1), metavar='1–16')
    feed = commands.add_parser('feed', help='Queue exact GTIN matches from an authorized merchant feed')
    feed.add_argument('category', choices=CATEGORIES)
    feed.add_argument('catalog', type=Path, help='JSON list of identities or a monitor discovery plan')
    feed.add_argument('host', choices=sorted(set(ROOTS) | set(LAPTOP_PARSERS) | set(MARKETPLACES) | set(STORES)))
    feed.add_argument('--format', required=True, choices=FEED_FORMATS)
    feed.add_argument('--audit-live', type=int, choices=range(1, 61), metavar='1–60',
                      help='Compare that many feed claims to fresh live offers without publishing prices')
    source = feed.add_mutually_exclusive_group(required=True)
    source.add_argument('--file', type=Path)
    source.add_argument('--url-env', metavar='VARIABLE', help='Read a store-owned HTTPS feed URL from an environment variable')
    policy = commands.add_parser('configure-store', help='Persist a provisioned per-store HTTP quota and request interval')
    policy.add_argument('host')
    policy.add_argument('--budget', type=int, required=True)
    policy.add_argument('--interval', type=int, default=1)
    verify = commands.add_parser('verify')
    verify.add_argument('--routes-per-store', type=int, default=45)
    verify.add_argument('--store', action='append', default=[])
    verify.add_argument('--category', choices=CATEGORIES)
    verify.add_argument('--focus-gtins', type=Path, metavar='JSON',
                        help='Prioritize a list of monitor GTINs after refreshing offers close to expiry')
    commands.add_parser('report')
    export = commands.add_parser('export')
    export.add_argument('category', choices=CATEGORIES)
    export.add_argument('output', type=Path)
    args = cli.parse_args()
    with closing(CatalogQueue(args.db)) as queue:
        if args.command == 'enqueue':
            new = queue.enqueue(normalize_route(args.category, row, priority=priority)
                                for path in args.targets for row, priority in rows(path, args.category))
            print(json.dumps({'queued': new}))
        elif args.command == 'configure-store':
            queue.configure(args.host, args.budget, args.interval)
        elif args.command == 'discover':
            if not 0 <= args.search_budget <= SEARCH_BUDGET:
                cli.error('Choose 0–48 search pages per store')
            models = json.loads(args.plan.read_text(encoding='utf-8'))['candidates']
            with ThreadPoolExecutor(max_workers=8) as pool:
                jobs = {host: pool.submit(discover_store, args.db, host, models, args.search_budget)
                        for host in dict.fromkeys(args.store or ROOTS)}
                print(json.dumps({host: job.result() for host, job in jobs.items()}, ensure_ascii=False))
        elif args.command == 'sitemap':
            roots = {host: [url for url in args.sitemap_url if store_url(url, host)]
                     for host in dict.fromkeys(args.store)}
            if any(not any(store_url(url, host) for host in roots) for url in args.sitemap_url):
                cli.error('Each sitemap URL must use HTTPS and a selected store host')
            models = json.loads(args.plan.read_text(encoding='utf-8'))['candidates']
            with ThreadPoolExecutor(max_workers=8) as pool:
                jobs = {host: pool.submit(discover_sitemaps, args.db, host, models,
                                          args.documents_per_store, roots[host])
                        for host in roots}
                print(json.dumps({host: job.result() for host, job in jobs.items()}, ensure_ascii=False))
        elif args.command == 'feed':
            if args.audit_live and args.host not in DIRECT_FEED_AUDIT_HOSTS:
                cli.error('Live feed auditing requires a direct retailer with a verified seller identity')
            catalog = json.loads(args.catalog.read_text(encoding='utf-8'))
            models = catalog['candidates'] if isinstance(catalog, dict) else catalog
            if not isinstance(models, list) or any(not isinstance(model, dict) for model in models):
                cli.error('Expected a list of catalog variant identities')
            commerce = {} if args.audit_live else None

            def consume(root):
                return import_inventory(queue, root, args.format, args.host, args.category, models, commerce=commerce)

            try:
                if args.file:
                    with args.file.open('rb') as stream:
                        report = consume(xml_root(stream.read(MAX_XML + 1)))
                else:
                    url = os.environ.get(args.url_env)
                    if not store_url(url, args.host):
                        cli.error('Expected a store-owned HTTPS feed URL in the named variable')
                    report = discover_inventory(queue, args.host, lambda inventory: consume(inventory.xml(url)))
            except InventoryError as error:
                cli.error(str(error))
            if commerce is not None and 'status' not in report:
                report['live_audit'] = audit_inventory_offers(queue, args.host, args.category, commerce, args.audit_live)
            print(json.dumps(report, ensure_ascii=False))
            if 'status' in report:
                raise SystemExit(1)
        elif args.command == 'verify':
            hosts = queue.hosts()
            if not 1 <= args.routes_per_store <= 100 or set(args.store) - set(hosts):
                cli.error('Choose valid stores and 1–100 routes per store')
            focus_gtins = ()
            if args.focus_gtins:
                if args.category != 'monitor':
                    cli.error('--focus-gtins requires --category monitor')
                try:
                    focus_gtins = json.loads(args.focus_gtins.read_text(encoding='utf-8'))
                except (OSError, UnicodeError, ValueError) as error:
                    cli.error(f'Cannot read focus GTINs: {error}')
                if (not isinstance(focus_gtins, list) or not 1 <= len(focus_gtins) <= 100
                        or any(not isinstance(gtin, str) or retailer_gtin13(gtin) != gtin for gtin in focus_gtins)
                        or len(set(focus_gtins)) != len(focus_gtins)):
                    cli.error('Expected a list of 1–100 distinct exact GTINs')
            with ThreadPoolExecutor(max_workers=8) as pool:
                jobs = {host: pool.submit(verify_store, args.db, host, args.routes_per_store, category=args.category,
                                          focus_gtins=focus_gtins)
                        for host in hosts if not args.store or host in args.store}
                print(json.dumps({host: job.result() for host, job in jobs.items()}, ensure_ascii=False))
        elif args.command == 'export':
            export_offers(queue, args.category, args.output)
        else:
            print(json.dumps(coverage_report(queue), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
