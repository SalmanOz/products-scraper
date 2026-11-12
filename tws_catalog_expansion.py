"""Generate a TWS catalog addition from live selected listings and sourced specs."""
import argparse
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from itertools import combinations
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from requests import RequestException
from curl_cffi.requests import RequestsError

from catalog_source_collector import catalog_document_reader
from hepsiburada_catalog_discovery import discover_product_sellers as hepsiburada_seller_targets
from laptop_pilot import IMAGE_HOSTS, _hepsiburada_public_get, _image, _public_offers, verify_seller
from mediamarkt_source import MediaMarktSource, _selected_offer
from n11_catalog_source import discover_product_sellers
from pazarama_category_source import HOST as PAZARAMA, pazarama_product_url
from pazarama_offer_source import pazarama_seller_identity
from priority_retailer_transport import PriorityRetailerTransport, compatible_get, product_host
from retailer_barcode import retailer_gtin13
from tws_offer_pilot import NEW_MODEL_NAMES, _mediamarkt_tws_url, independent_sellers, model_name, selected_tws_offer
from tws_retailer_sources import (BOT, STOP_REASONS, _vatan_url, selected_hepsiburada_tws,
                                  selected_n11_tws, selected_pazarama_tws, selected_photo, selected_teknosa_tws,
                                  selected_vatan_store_tws, selected_vatan_tws)
from tr_price_scraper import TRPriceScraper
from vatan_category_source import VatanCategorySource

MARKETPLACES = {'www.n11.com', 'www.trendyol.com', 'www.hepsiburada.com', 'www.teknosa.com'}
STORES = {'www.vatanbilgisayar.com', 'www.mediamarkt.com.tr'}
TWS_IMAGE_HOSTS = IMAGE_HOSTS | {'productimages.hepsiburada.net', 'reimg-teknosa-cloud-prod.mncdn.com', 'img.pzrmcdn.com'}
ALTERNATE_SELLER_BUDGET = 8
COLOR_NAMES = {
    'beyaz': 'Beyaz', 'bej': 'Bej', 'gri': 'Gri', 'gumus': 'Gümüş', 'kirmizi': 'Kırmızı',
    'lacivert': 'Lacivert', 'mavi': 'Mavi', 'mor': 'Mor', 'pembe': 'Pembe', 'sari': 'Sarı',
    'siyah': 'Siyah', 'turuncu': 'Turuncu', 'yesil': 'Yeşil',
}


def common_color(offers):
    colors = set()
    for offer in offers:
        title_words = TRPriceScraper._normalize_words(offer.get('source_title') or '').split()
        title_colors = set(title_words).intersection(COLOR_NAMES)
        selected = offer.get('selected_n11_color_slug', '').split('-')[-1]
        if selected in COLOR_NAMES:
            title_colors.add(selected)
        if len(title_colors) != 1:
            return None
        colors.update(title_colors)
    return COLOR_NAMES[next(iter(colors))] if len(colors) == 1 else None


def public_tws_offers(offers):
    public = _public_offers(offers)
    for row, offer in zip(public, offers, strict=True):
        if 'storePickup' in offer:
            row['storePickup'] = offer['storePickup']
    return public


def tws_source_target(offer, previous=None):
    target = {key: offer[key] for key in ('url', 'merchant')}
    pickup = offer.get('storePickup')
    sku = pickup.get('sku') if isinstance(pickup, dict) else (previous or {}).get('storePickupSku')
    if isinstance(sku, str) and re.fullmatch(r'[1-9][0-9]{4,6}', sku) and _vatan_url(offer['url']):
        target['storePickupSku'] = sku
    return target


def exact_tws_sellers(offers, *, maximum=3):
    """Prefer different storefronts, counting each legal seller only once."""
    def distinct(a, b):
        return (a['gtin13'] == b['gtin13'] and a['url'] != b['url']
                and independent_sellers(a, b)
                and TRPriceScraper._normalize_words(a['merchant']).replace(' ', '')
                != TRPriceScraper._normalize_words(b['merchant']).replace(' ', '')
                and min(a['price'], b['price']) >= .60 * max(a['price'], b['price']))

    groups = defaultdict(list)
    for offer in offers:
        groups[offer['gtin13']].append(offer)
    candidates = (rows for group in groups.values() for size in range(2, maximum + 1)
                  for rows in combinations(group, size) if all(distinct(a, b) for a, b in combinations(rows, 2)))
    best = max(candidates, key=lambda rows: (len(rows), len({row['platform'] for row in rows}),
               tuple(-price for price in sorted(row['price'] for row in rows)),
               tuple(row['url'] for row in rows)), default=())
    return sorted(best, key=lambda offer: (offer['price'], offer['url']))


def verify_media(html, url, name):
    def variant(product, title, brand):
        gtin = retailer_gtin13(product.get('gtin13'))
        if not gtin or model_name(title, brand) != name:
            return None
        photo = next((v for u in product.get('image') or [] if (v := selected_photo(u, {'assets.mmsrg.com'}))), None)
        return {'platform': 'MediaMarkt', 'model': name, 'gtin13': gtin, 'condition': 'new',
                '_legal_name': 'media markt turkey ticaret limited sirketi', '_image': photo}
    return _selected_offer(html, url, allowed_url=_mediamarkt_tws_url, verify_product=variant)


def reread(host, rows, statuses=None, document_reader=None, *, discover=False):
    observations, failures = defaultdict(list), Counter()
    seller_budget = ALTERNATE_SELLER_BUDGET if discover and host in {
        'www.n11.com', 'www.trendyol.com', 'www.hepsiburada.com',
    } else 0
    pending = deque(rows)
    queued = {target['url'] for _, target in pending}
    expanded = set()
    identities = {}
    transport = None
    if host in MARKETPLACES or host == PAZARAMA:
        options = {}
        if document_reader is not None:
            options.update(get=document_reader, browser_fallback=False)
        elif host == 'www.hepsiburada.com':
            options.update(get=_hepsiburada_public_get, browser_fallback=False)
        elif discover:
            options['browser_fallback'] = False
        if host == 'www.teknosa.com':
            options.update(browser_fallback=False, unavailable_robots_hosts=(host,))
        transport = PriorityRetailerTransport(maximum_per_host=min(260, len(rows) + seller_budget), **options)
    source = None
    if host in STORES:
        source = (VatanCategorySource(fetch=document_reader) if host.startswith('www.vatan')
                  else MediaMarktSource(get=document_reader or compatible_get))
        if isinstance(source, MediaMarktSource):
            source.configure_catalog_budget(len(rows))
        source._load_robots()

    def vatan_inventory(url):
        if not source._rules.can_fetch(BOT, url):
            return None, 'robots_disallowed', None
        source._sleep(max(.5, source._delay))
        return source._read(url, 64 * 1024, 'application/json')

    while pending:
        name, target = pending.popleft()
        url = target['url']
        seller_hints = [] if seller_budget and 'gtin13' not in target else None
        if statuses is not None:
            statuses[url] = 'unavailable'
        try:
            if host == PAZARAMA:
                if not pazarama_product_url(url):
                    failures['source_policy'] += 1
                    continue
                html, reason, _ = transport._read_document(url, host, fresh=True)
                final = url
            elif transport:
                html, reason, _ = transport.read_fresh_document(url)
                if reason == 'source_cooldown' and transport.wait_for_cooldown(url):
                    html, reason, _ = transport.read_fresh_document(url)
                final = transport.resolved_url(url)
            elif source._rules and source._rules.can_fetch(BOT, url):
                if isinstance(source, VatanCategorySource):
                    source._sleep(max(.5, source._delay))
                    html, reason, _ = source._read(url, 5 * 1024 * 1024, 'text/html')
                else:
                    html, reason, _ = source._direct(url, 5 * 1024 * 1024, 'text/html')
                final = url
                if isinstance(html, bytes):
                    html = html.decode('utf-8', errors='replace')
            else:
                failures['source_policy'] += 1
                continue
            if not html or urlsplit(url).query != urlsplit(final).query:
                failures[reason] += 1
                if reason in STOP_REASONS:
                    break
                continue
            if statuses is not None:
                statuses[url] = 'unverified'
            if host == 'www.n11.com':
                current, reason = selected_n11_tws(html, final, name, model_name)
            elif host == 'www.trendyol.com':
                current, reason = selected_tws_offer(html, final, name, seller_hints=seller_hints)
            elif host == 'www.hepsiburada.com':
                current, reason = selected_hepsiburada_tws(html, final, name, model_name)
            elif host == 'www.teknosa.com':
                current, reason = selected_teknosa_tws(html, final, name, model_name)
            elif host == PAZARAMA:
                current = selected_pazarama_tws(html, final, name, model_name)
                reason = 'selected_pazarama_offer_unverified'
                if current:
                    seller = current['seller_url']
                    key = (seller, current['merchant'])
                    if key not in identities:
                        profile, outcome, _ = transport._read_document(seller, host, category=True)
                        identity = pazarama_seller_identity(profile, current) if profile else None
                        identities[key] = (identity, outcome)
                    identity, outcome = identities[key]
                    if identity:
                        current.update(_legal_name=identity[0], _tax_key=identity[1])
                    else:
                        current, reason = None, 'seller_identity_unverified'
                        if statuses is not None and outcome != 'document_read':
                            statuses[url] = 'unavailable'
            elif host == 'www.vatanbilgisayar.com':
                current = selected_vatan_tws(html, final, name, model_name)
                reason = 'first_party_evidence_missing'
                if current is None and (discover or 'storePickupSku' in target):
                    current, reason = selected_vatan_store_tws(
                        html, final, name, model_name, vatan_inventory, target.get('storePickupSku'))
                    if statuses is not None and reason.startswith('store_inventory_unavailable_'):
                        statuses[url] = 'unavailable'
            else:
                current = verify_media(html, final, name)
                reason = 'first_party_evidence_missing'
        except (RequestException, RequestsError):
            failures['transport_error'] += 1
            break
        except (TypeError, ValueError):
            failures['invalid_page'] += 1
            continue
        matches_selection = bool(current
            and all(current.get(key) == target[key] for key in ('gtin13', 'merchant_id', 'merchant') if key in target)
            and (retailer_gtin13(current.get('gtin13')) and
                 (current.get('_legal_name') or host == 'www.hepsiburada.com' and current.get('seller_url')) if discover
                 else 'gtin13' in target and 'merchant' in target))
        if (matches_selection and current['availability'] == 'in_stock' and current['condition'] == 'new'
                and current['currency'] == 'TRY' and 100 <= current['price'] <= 150_000):
            if (host == 'www.hepsiburada.com' and current.get('seller_url')
                    and not verify_seller(current, source, transport, identities)):
                failures['seller_identity_unverified'] += 1
                continue
            if statuses is not None:
                statuses[url] = 'verified'
            observations[name].append(current)
            sku = (name, current['gtin13'])
            if seller_hints is not None and sku not in expanded:
                if host == 'www.n11.com':
                    alternatives = (
                        (hint['source_url'], {'merchant': hint['seller_name']})
                        for hint in discover_product_sellers(html, final, maximum=3)
                        if hint['discovery_scope'] == 'public_other_seller_selector'
                    )
                elif host == 'www.hepsiburada.com':
                    alternatives = (
                        (hint['source_url'], {'merchant': hint['discovery_seller'],
                                              'merchant_id': hint['discovery_merchant_id']})
                        for hint in hepsiburada_seller_targets(html, final)
                    )
                else:
                    alternatives = ((destination, {'merchant_id': seller_id})
                                    for destination, seller_id in seller_hints)
                for destination, identity in alternatives:
                    if destination in queued:
                        continue
                    pending.append((name, {'url': destination, 'gtin13': current['gtin13'], **identity}))
                    queued.add(destination)
                    expanded.add(sku)
                    seller_budget -= 1
                    break
        else:
            failures[reason if not current else 'selected_seller_or_gtin_changed'] += 1
    return observations, failures


def candidates_from_audit(audit):
    """Carry retailer destinations and selected identities into a fresh read."""
    candidates = {}
    for row in audit['verified']:
        paired_gtins = {item['gtin13'] for item in row['verifiedVariants'] if len(item['offers']) >= 2}
        if paired_gtins:
            candidates[row['name']] = [
                {key: offer[key] for key in ('url', 'gtin13', 'merchant')}
                for offer in row['observations'] if offer['gtin13'] in paired_gtins
            ]
    return candidates


def stage(catalog, snapshot, specs, candidates, output, *, allow_empty=False):
    existing = {row['name'] for row in catalog['models']}
    owned = {row['gtin'] for row in snapshot['models'] if row['gtin']}
    sources, seen = defaultdict(list), set()
    for name, rows in candidates.items():
        if name not in NEW_MODEL_NAMES or name in existing or name not in specs:
            continue
        for row in rows:
            url = row['url']
            p = urlsplit(url)
            if (p.scheme != 'https' or url in seen or p.fragment
                    or not retailer_gtin13(row.get('gtin13')) or row['gtin13'] in owned):
                continue
            if (p.netloc in MARKETPLACES and product_host(url) == p.netloc
                    or p.netloc == PAZARAMA and pazarama_product_url(url)
                    or p.netloc == 'www.vatanbilgisayar.com' and _vatan_url(url)
                    or p.netloc == 'www.mediamarkt.com.tr' and _mediamarkt_tws_url(url)):
                sources[p.netloc].append((name, row))
                seen.add(url)
    verified, failures = defaultdict(list), Counter()
    n11_reader = catalog_document_reader('earbud-discovery') if 'www.n11.com' in sources else None
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {h: pool.submit(reread, h, rows,
                              document_reader=n11_reader if h == 'www.n11.com' else None)
                for h, rows in sources.items()}
        for host, job in jobs.items():
            observations, skipped = job.result()
            failures.update({f'{host}:{reason}': total for reason, total in skipped.items()})
            for name, offers in observations.items():
                verified[name].extend(offers)
    gtin_models = defaultdict(set)
    for name, rows in verified.items():
        for row in rows:
            gtin_models[row['gtin13']].add(name)
    products, prices, photos = [], [], {}
    targets = dict(snapshot.get('sourceTargets', {}))
    for name, rows in sorted(verified.items()):
        groups = defaultdict(list)
        for row in rows:
            if len(gtin_models[row['gtin13']]) == 1:
                groups[row['gtin13']].append(row)
        variants = [offers for group in groups.values() if (offers := exact_tws_sellers(group))]
        variants.sort(key=lambda offers: (offers[0]['price'], offers[0]['gtin13']))
        if not variants:
            failures['independent_pair_missing'] += 1
            continue
        spec = specs[name]
        slug = re.sub(r'[^a-z0-9]+', '-', name.casefold()).strip('-')
        if (len(spec['specs']) < 5 or len({row['label'] for row in spec['specs']}) != len(spec['specs'])
                or not spec['summary'] or not spec['highlights']
                or urlsplit(spec['specSource']).scheme != 'https'):
            raise ValueError(f'Incomplete manufacturer specification for {name}')
        for offer in (offer for offers in variants for offer in offers):
            image = _image(offer['_image'], allowed_hosts=TWS_IMAGE_HOSTS) if offer.get('_image') else None
            if image:
                photos[slug] = image
                products.append({**spec, 'name': name, 'slug': slug, 'photoSource': offer['url'],
                                 'requiresIndependentPair': True})
                break
        else:
            failures['selected_image_missing'] += 1
            continue
        for offers in variants:
            gtin = offers[0]['gtin13']
            row = {'name': name, 'gtin': gtin, 'offers': public_tws_offers(offers)}
            if color := common_color(offers):
                row['variant'] = color
            prices.append(row)
            target_key = f'{name}/{gtin}' if len(variants) > 1 else name
            targets[target_key] = [tws_source_target(current) for current in offers]
    report = {'qualified': [p['name'] for p in products], 'qualifiedVariants': len(prices),
              'rejections': dict(failures)}
    if not products:
        if not allow_empty:
            raise ValueError('No new TWS model has two independent sellers and a selected product photo')
        return report
    now = datetime.now(timezone.utc).isoformat()
    output.mkdir(parents=True, exist_ok=True)
    (output / 'earbuds').mkdir(exist_ok=True)
    for slug, image in photos.items():
        (output / 'earbuds' / f'{slug}.webp').write_bytes(image)
    for filename, payload in (
        ('twsProductCatalog.json', {'reviewedAt': now, 'models': [*catalog['models'], *products]}),
        ('twsOfferSnapshot.json', {**snapshot, 'checked_at': now, 'models': [*snapshot['models'], *prices],
                                  'sourceTargets': targets}),
    ):
        (output / filename).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ('catalog', 'snapshot', 'specs', 'candidates', 'output'):
        parser.add_argument(argument, type=Path)
    parser.add_argument('--from-audit', action='store_true', help='Read same-GTIN candidates from a retailer audit')
    args = parser.parse_args()
    catalog, snapshot, specs, candidates = (json.loads(path.read_text(encoding='utf-8'))
                                             for path in (args.catalog, args.snapshot, args.specs, args.candidates))
    if args.from_audit:
        candidates = candidates_from_audit(candidates)
    print(json.dumps(stage(catalog, snapshot, specs, candidates, args.output, allow_empty=args.from_audit),
                     ensure_ascii=False))
