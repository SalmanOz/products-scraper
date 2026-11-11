"""Generate a TWS catalog addition from live selected listings and sourced specs."""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from itertools import combinations
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from requests import RequestException
from curl_cffi.requests import RequestsError

from laptop_pilot import IMAGE_HOSTS, _hepsiburada_public_get, _image, _public_offers
from mediamarkt_source import MediaMarktSource, _selected_offer
from priority_retailer_transport import PriorityRetailerTransport, compatible_get, product_host
from retailer_barcode import retailer_gtin13
from tws_offer_pilot import NEW_MODEL_NAMES, _mediamarkt_tws_url, independent_sellers, model_name, selected_tws_offer
from tws_retailer_sources import (BOT, STOP_REASONS, _vatan_url, selected_hepsiburada_tws,
                                  selected_n11_tws, selected_photo, selected_teknosa_tws, selected_vatan_tws)
from tr_price_scraper import TRPriceScraper
from vatan_category_source import VatanCategorySource

MARKETPLACES = {'www.n11.com', 'www.trendyol.com', 'www.hepsiburada.com', 'www.teknosa.com'}
STORES = {'www.vatanbilgisayar.com', 'www.mediamarkt.com.tr'}
TWS_IMAGE_HOSTS = IMAGE_HOSTS | {'productimages.hepsiburada.net', 'reimg-teknosa-cloud-prod.mncdn.com'}


def exact_tws_sellers(offers):
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
    candidates = (rows for group in groups.values() for size in (2, 3)
                  for rows in combinations(group, size) if all(distinct(a, b) for a, b in combinations(rows, 2)))
    best = max(candidates, key=lambda rows: (len({row['platform'] for row in rows}), len(rows),
               -min(row['price'] for row in rows), tuple(row['url'] for row in rows)), default=())
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


def reread(host, rows):
    observations, failures = defaultdict(list), Counter()
    transport = None
    if host in MARKETPLACES:
        options = {'get': _hepsiburada_public_get, 'browser_fallback': False} if host == 'www.hepsiburada.com' else {}
        transport = PriorityRetailerTransport(maximum_per_host=min(260, len(rows)), **options)
    source = None
    if host in STORES:
        source = VatanCategorySource() if host.startswith('www.vatan') else MediaMarktSource(get=compatible_get)
        if isinstance(source, MediaMarktSource):
            source.configure_catalog_budget(len(rows))
        source._load_robots()
    for name, target in rows:
        url = target['url']
        try:
            if transport:
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
            if host == 'www.n11.com':
                current, reason = selected_n11_tws(html, final, name, model_name)
            elif host == 'www.trendyol.com':
                current, reason = selected_tws_offer(html, final, name)
            elif host == 'www.hepsiburada.com':
                current, reason = selected_hepsiburada_tws(html, final, name, model_name)
            elif host == 'www.teknosa.com':
                current, reason = selected_teknosa_tws(html, final, name, model_name)
            else:
                current = (selected_vatan_tws(html, final, name, model_name)
                           if host == 'www.vatanbilgisayar.com' else verify_media(html, final, name))
                reason = 'first_party_evidence_missing'
        except (RequestException, RequestsError):
            failures['transport_error'] += 1
            break
        except (TypeError, ValueError):
            failures['invalid_page'] += 1
            continue
        if (current and current['gtin13'] == target['gtin13']
                and current.get('merchant_id') == target.get('merchant_id')
                and current['merchant'] == target['merchant']
                and current['availability'] == 'in_stock' and current['condition'] == 'new'
                and current['currency'] == 'TRY' and 100 <= current['price'] <= 150_000):
            observations[name].append(current)
        else:
            failures[reason if not current else 'selected_seller_or_gtin_changed'] += 1
    return observations, failures


def stage(catalog, snapshot, specs, candidates, output):
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
                    or p.netloc == 'www.vatanbilgisayar.com' and _vatan_url(url)
                    or p.netloc == 'www.mediamarkt.com.tr' and _mediamarkt_tws_url(url)):
                sources[p.netloc].append((name, row))
                seen.add(url)
    verified, failures = defaultdict(list), Counter()
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {h: pool.submit(reread, h, rows) for h, rows in sources.items()}
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
    for name, rows in sorted(verified.items()):
        offers = exact_tws_sellers([row for row in rows if len(gtin_models[row['gtin13']]) == 1])
        if len(offers) < 2:
            failures['independent_pair_missing'] += 1
            continue
        spec = specs[name]
        slug = re.sub(r'[^a-z0-9]+', '-', name.casefold()).strip('-')
        if (len(spec['specs']) < 5 or len({row['label'] for row in spec['specs']}) != len(spec['specs'])
                or not spec['summary'] or not spec['highlights']
                or urlsplit(spec['specSource']).scheme != 'https'):
            raise ValueError(f'Incomplete manufacturer specification for {name}')
        for offer in offers:
            image = _image(offer['_image'], allowed_hosts=TWS_IMAGE_HOSTS) if offer.get('_image') else None
            if image:
                photos[slug] = image
                products.append({**spec, 'name': name, 'slug': slug, 'photoSource': offer['url'],
                                 'requiresIndependentPair': True})
                prices.append({'name': name, 'gtin': offer['gtin13'], 'offers': _public_offers(offers)})
                break
        else:
            failures['selected_image_missing'] += 1
    print(json.dumps({'qualified': [p['name'] for p in products], 'rejections': failures}, ensure_ascii=False))
    if not products:
        raise ValueError('No new TWS model has two independent sellers and a selected product photo')
    now = datetime.now(timezone.utc).isoformat()
    output.mkdir(parents=True, exist_ok=True)
    (output / 'earbuds').mkdir(exist_ok=True)
    for slug, image in photos.items():
        (output / 'earbuds' / f'{slug}.webp').write_bytes(image)
    for filename, payload in (
        ('twsProductCatalog.json', {'reviewedAt': now, 'models': [*catalog['models'], *products]}),
        ('twsOfferSnapshot.json', {'checked_at': now, 'models': [*snapshot['models'], *prices]}),
    ):
        (output / filename).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ('catalog', 'snapshot', 'specs', 'candidates', 'output'):
        parser.add_argument(argument, type=Path)
    args = parser.parse_args()
    stage(*(json.loads(path.read_text()) for path in (args.catalog, args.snapshot, args.specs, args.candidates)),
          args.output)
