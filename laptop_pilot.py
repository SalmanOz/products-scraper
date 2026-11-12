"""Revalidate a candidate laptop cohort and generate a public pilot snapshot.

The input contains retailer URLs discovered from public categories/product
selectors and expected manufacturer codes/barcodes. It never contains prices.
All offer prices and seller identities are reread from the selected product pages.
"""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from io import BytesIO
from itertools import combinations
import json
from pathlib import Path
import re
from urllib.parse import urlparse

from PIL import Image, ImageOps, UnidentifiedImageError
from curl_cffi.requests import RequestsError
from requests import RequestException

from laptop_direct_source import DIRECT_STORES, direct_company_identity, selected_direct_laptop
from laptop_hepsiburada_source import (
    TEKNOSA_SELLER_PROFILES, hepsiburada_company_identity,
    hepsiburada_seller_profile_matches, selected_hepsiburada,
)
from laptop_retailer_sources import (
    marketplace_seller_identity, selected_mediamarkt, selected_n11, selected_teknosa,
    selected_trendyol, selected_vatan, WORDS,
)
from mediamarkt_source import MediaMarktSource, SourceBudgetExhausted, SourceCircuitOpen
from priority_retailer_transport import PriorityRetailerTransport, compatible_get
from retailer_barcode import retailer_gtin13
from tws_offer_pilot import independent_sellers
from tws_retailer_sources import BOT, STOP_REASONS, _vatan_url
from vatan_category_source import VatanCategorySource

MINIMUM = 25
PARSERS = {'www.n11.com': selected_n11, 'www.trendyol.com': selected_trendyol,
           'www.vatanbilgisayar.com': selected_vatan, 'www.mediamarkt.com.tr': selected_mediamarkt,
           'www.teknosa.com': selected_teknosa, 'www.hepsiburada.com': selected_hepsiburada,
           **dict.fromkeys(DIRECT_STORES, selected_direct_laptop)}
FIRST_PARTY = {'www.mediamarkt.com.tr', 'www.vatanbilgisayar.com'}
IMAGE_HOSTS = {'n11scdn.akamaized.net', 'n11scdn2-im.akamaized.net',
               'cdn.dsmcdn.com', 'assets.mmsrg.com', 'cdn.vatanbilgisayar.com', 'img-teknosa-sap.mncdn.com'}
CORE = ('mpn', 'brand', 'cpu', 'ram', 'ssd', 'discrete')
MAX_URLS = 200


def compatible(a, b):
    x, y = a['facts'], b['facts']
    return (a['gtin13'] == b['gtin13'] and all(x[k] == y[k] for k in CORE)
            and (x['os'].startswith(y['os']) or y['os'].startswith(x['os']))
            and (not x['screen'] or not y['screen'] or x['screen'] == y['screen']))


def _merchant_key(row):
    return WORDS(row['merchant']).replace(' ', '')


def valid_pair(a, b):
    return (compatible(a, b) and a['url'] != b['url'] and independent_sellers(a, b)
            and _merchant_key(a) != _merchant_key(b)
            and min(a['price'], b['price']) >= max(a['price'], b['price']) * .62)


def _cohort_score(rows):
    return len({row['platform'] for row in rows}), len(rows), -min(row['price'] for row in rows)


def select_offers(observations, *, required_platforms=frozenset()):
    """Prefer stores across platforms; every published seller must be independent."""
    best = []
    for pair in combinations(observations, 2):
        if not valid_pair(*pair):
            continue
        if (required_platforms <= {row['platform'] for row in pair}
                and (not best or _cohort_score(pair) > _cohort_score(best))):
            best = pair
        for third in observations:
            if all(valid_pair(o, third) for o in pair):
                triple = (*pair, third)
                if (required_platforms <= {row['platform'] for row in triple}
                        and (not best or _cohort_score(triple) > _cohort_score(best))):
                    best = triple
    return sorted(best, key=lambda row: (row['price'], row['url']))


def _explicit_os(offers):
    return max((o['facts']['os'] for o in offers), key=len)


def _agreed(offers, key):
    values = {row['facts'][key] for row in offers if row['facts'][key]}
    return next(iter(values)) if len(values) == 1 else None


def _display_name(offers, facts):
    code = facts['mpn']
    ranked = sorted(offers, key=lambda o: (o['platform'] == 'MediaMarkt', len(o['source_title'])))
    for row in ranked:
        raw = row['source_title']
        name = re.split(r'(?i)(?<![A-Z0-9])' + re.escape(code) + r'(?![A-Z0-9])|'
                        r'\b(?:Intel|AMD|Core[ -]?[3579]|Ryzen|R[3579][ -]*\d+[A-Z]*|i[3579][ -]*\d+[A-Z]*|'
                        r'M[1-9]\b|\d\.Nesil|\d{1,2}\s*GB|Ultra[ -]?[579]|Dizüstü)\b', raw, maxsplit=1)[0]
        name = re.sub(r'\s+', ' ', name).strip(' /-–,')
        head, _, tail = name.partition(' ')
        if len(tail) > 3 and WORDS(head) == WORDS(facts['brand']):
            tail = re.sub(r'^NB\s+', '', tail, flags=re.I)
            tail = re.sub(r'\bideapad\b', 'IdeaPad', tail, flags=re.I)
            tail = re.sub(r'\bvivobook\b', 'Vivobook', tail, flags=re.I)
            return f"{facts['brand']} {tail}"
    return f"{facts['brand']} {code}"


def _storage(size):
    return f'{size // 1024} TB SSD' if size >= 1024 else f'{size} GB SSD'


def _public_offers(offers):
    return [{**{key: offer[key] for key in ('platform', 'merchant', 'url', 'observed_at', 'price')},
             'basket': offer.get('price_kind') == 'basket'} for offer in offers]


def build_model(offers, photo_offer):
    first = offers[0]
    facts = first['facts']
    screen, graphics = _agreed(offers, 'screen'), facts['discrete'] or _agreed(offers, 'graphics')
    os_name = _explicit_os(offers)
    specs = [
        ('Model kodu', facts['mpn']), ('İşlemci', facts['cpu']),
        ('Bellek (RAM)', f"{facts['ram']} GB"), ('Depolama', _storage(facts['ssd'])),
        ('İşletim sistemi', os_name), ('Ekran boyutu', f"{screen.replace('.', ',')} inç" if screen else None),
        ('Ekran kartı', graphics), ('Çözünürlük', _agreed(offers, 'resolution')),
        ('Ekran yenileme hızı', _agreed(offers, 'refresh')), ('Bellek türü', _agreed(offers, 'memory_type')),
    ]
    return {
        'name': _display_name(offers, facts),
        'slug': re.sub(r'[^a-z0-9]+', '-', f"{facts['brand']}-{facts['mpn']}".lower()).strip('-'),
        **{key: facts[key] for key in CORE},
        'gtin': first['gtin13'], 'screen': screen, 'os': os_name, 'graphics': graphics,
        'photoSource': photo_offer['url'],
        'specSources': [{'name': o['merchant'] if o['merchant'] == o['platform'] else
                         f"{o['platform']} · {o['merchant']}", 'url': o['url']} for o in offers],
        'specs': [{'label': label, 'value': value} for label, value in specs if value],
        'offers': _public_offers(offers),
    }


def _image(url, *, allowed_hosts=IMAGE_HOSTS):
    route = urlparse(url)
    if route.scheme != 'https' or route.netloc not in allowed_hosts or route.query or route.fragment:
        return None
    try:
        with closing(compatible_get(url, timeout=(6, 18), stream=True, allow_redirects=False,
                                    headers={'Accept': 'image/avif,image/webp,image/png,image/jpeg'})) as response:
            if response.status_code != 200 or not response.headers.get('Content-Type', '').startswith('image/'):
                return None
            buffer = bytearray()
            for chunk in response.iter_content(32768):
                buffer.extend(chunk)
                if len(buffer) > 5_000_000:
                    return None
        with Image.open(BytesIO(buffer)) as source:
            if min(source.size) < 220 or source.width * source.height > 16_000_000:
                return None
            source.load()
            transformed = ImageOps.exif_transpose(source).convert('RGBA')
            rgb = Image.new('RGB', transformed.size, 'white')
            rgb.paste(transformed, mask=transformed.getchannel('A'))
            rgb.thumbnail((780, 650))
            if min(rgb.size) < 180 or ImageOps.grayscale(rgb).getextrema()[0] > 190:
                return None
            image = BytesIO()
            rgb.save(image, 'WEBP', quality=83, method=6)
            return image.getvalue()
    except (OSError, ValueError, UnidentifiedImageError, RequestsError):
        return None


def _target(url):
    p = urlparse(url)
    return p if (p.scheme == 'https' and p.netloc in PARSERS and len(url) <= 800
                 and not p.fragment
                 and not (p.netloc in FIRST_PARTY | DIRECT_STORES.keys() | {'www.teknosa.com'} and p.query)
                 and (p.netloc not in DIRECT_STORES or re.fullmatch(r'/[a-z0-9./-]{10,600}', p.path))) else None


def _hepsiburada_public_get(url, **kwargs):
    # The public storefront responds normally to the browser client's standard
    # headers. Reuse the same robots checks, host quota and denial circuit.
    if urlparse(url).netloc == 'www.hepsiburada.com':
        kwargs['headers'] = {'Accept': kwargs['headers']['Accept']}
    return compatible_get(url, **kwargs)


def verify_seller(offer, source, transport, identities):
    url = offer.get('seller_url')
    if not url:
        return bool(offer.get('_legal_name'))
    key = (url, offer['merchant_id'], offer['merchant'])
    if key not in identities:
        raw = None
        if offer['platform'] == 'MediaMarkt':
            if source._rules.can_fetch(BOT, url):
                body, reason, _ = source._direct(url, 5 * 1024 * 1024, 'text/html')
                if reason == 'ready':
                    raw = body
        elif offer['platform'] == 'Teknosa':
            raw, _, _ = transport.read_teknosa_seller(url)
        elif offer['platform'] == 'Hepsiburada':
            if (offer['merchant'] == 'Hepsiburada'
                    and offer['merchant_id'] == 'e750130c-d0ff-469f-8767-66a63af8ea0c'
                    and url == 'https://www.hepsiburada.com/magaza/hepsiburada'):
                raw, _, _ = transport._read_document(
                    'https://www.hepsiburada.com/iletisim', 'www.hepsiburada.com', category=True)
                if raw:
                    identities[key] = hepsiburada_company_identity(raw)
            else:
                identities[key] = _hepsiburada_marketplace_identity(offer, transport)
        elif urlparse(url).netloc in DIRECT_STORES:
            raw, _, _ = transport._read_document(url, urlparse(url).netloc, category=True)
            if raw:
                identities[key] = direct_company_identity(raw, url)
        if key not in identities:
            identities[key] = marketplace_seller_identity(raw, offer) if raw else None
    if not identities[key]:
        return False
    offer.update(identities[key])
    return bool(offer.get('_legal_name'))


def _hepsiburada_marketplace_identity(offer, transport):
    slug = urlparse(offer['seller_url']).path.rsplit('/', 1)[-1]
    profile = TEKNOSA_SELLER_PROFILES.get(slug)
    if not profile or offer['merchant_id'] != profile[0]:
        return None
    raw, _, _ = transport._read_document(
        f'https://www.hepsiburada.com/magaza/{slug}', 'www.hepsiburada.com', category=True)
    if not raw or not hepsiburada_seller_profile_matches(raw, offer, slug):
        return None
    company, _, _ = transport.read_teknosa_seller(f'https://www.teknosa.com/magaza/{slug}')
    identity = marketplace_seller_identity(company, {
        'platform': 'Teknosa', 'merchant': profile[1], 'merchant_id': profile[2],
    }) if company else None
    return identity if identity and identity['_legal_name'] == profile[3] else None


def read_retailer(host, rows, *, document_reader=None):
    failures, output = Counter(), []
    identities = {}
    parser = PARSERS[host]
    hb = host == 'www.hepsiburada.com'
    transport = PriorityRetailerTransport(maximum_per_host=min(MAX_URLS, max(1, len(rows))),
        get=document_reader or (_hepsiburada_public_get if hb else None),
        browser_fallback=not (document_reader or hb or host in DIRECT_STORES))
    source = None
    if host in FIRST_PARTY:
        source = (VatanCategorySource() if host.startswith('www.vatan') else
                  MediaMarktSource(get=compatible_get))
        if host.startswith('www.mediamarkt'):
            source.configure_catalog_budget(len(rows) * 2)
        source._load_robots()

    for row in rows:
        url = row['url']
        try:
            if host in FIRST_PARTY:
                if (source._rules is None or not source._rules.can_fetch(BOT, url)
                        or host.startswith('www.vatan') and not _vatan_url(url)
                        or host.startswith('www.mediamarkt') and not re.fullmatch(
                            r'/tr/product/_[a-z0-9-]+-\d{6,12}\.html', urlparse(url).path)):
                    failures['source_policy'] += 1
                    continue
                if host.startswith('www.vatan'):
                    source._sleep(max(.5, source._delay))
                    raw, reason, _ = source._read(url, 5 * 1024 * 1024, 'text/html')
                else:
                    raw, reason, _ = source._direct(url, 5 * 1024 * 1024, 'text/html')
                html = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else raw
                final = url if reason == 'ready' else None
            elif host in DIRECT_STORES:
                html, reason, _ = transport._read_document(url, host, fresh=True)
                final = url if html else None
            else:
                html, reason, _ = transport.read_fresh_document(url)
                final = transport.resolved_url(url)
            if not html or not final:
                failures[reason] += 1
                if reason in STOP_REASONS:
                    break
                continue
            if urlparse(url).query != urlparse(final).query:
                failures['selected_seller_route_changed'] += 1
                continue
            offer, reason = parser(html, final)
            if (offer and offer['facts']['mpn'] == row['mpn'] and offer['gtin13'] == row['gtin']
                    and not verify_seller(offer, source, transport, identities)):
                offer, reason = None, 'selected_seller_legal_identity_unproven'
        except (SourceBudgetExhausted, SourceCircuitOpen):
            failures['source_budget_or_circuit'] += 1
            break
        except (RequestsError, RequestException):
            skipped_reason = 'transport_error'
            failures[skipped_reason] += 1
            break
        except (OSError, UnicodeError, ValueError):
            failures['read_or_parse_error'] += 1
            continue
        if (offer and offer.get('_legal_name') and offer['facts']['mpn'] == row['mpn']
                and offer['gtin13'] == row['gtin'] and offer['currency'] == 'TRY'
                and offer['condition'] == 'new' and offer['availability'] == 'in_stock'
                and 3_000 <= offer['price'] <= 1_000_000):
            output.append(offer)
        else:
            failures[reason if not offer else 'selected_identity_mismatch'] += 1
    return output, failures


def collect_offers(targets, minimum_targets, *, n11_document_reader=None):
    by_host, grouped, skipped = defaultdict(list), defaultdict(list), Counter()
    if not isinstance(targets, list) or not minimum_targets <= len(targets) <= MAX_URLS:
        raise ValueError(f'Expected {minimum_targets}–{MAX_URLS} read-only candidate URLs')
    seen = set()
    for row in targets:
        url, mpn, gtin = row['url'], row['mpn'], row['gtin']
        route = _target(url)
        if (not route or url in seen or not re.fullmatch(r'[A-Z0-9.\-/+#]{5,36}', mpn)
                or not gtin or retailer_gtin13(gtin) != gtin):
            raise ValueError('Invalid or duplicate selected product target')
        seen.add(url)
        by_host[route.netloc].append(row)
    with ThreadPoolExecutor(max_workers=len(by_host)) as pool:
        jobs = {host: pool.submit(read_retailer, host, rows,
                                  document_reader=n11_document_reader if host == 'www.n11.com' else None)
                for host, rows in by_host.items()}
        for host, job in jobs.items():
            offers, reasons = job.result()
            for o in offers:
                grouped[o['facts']['mpn'], o['gtin13']].append(o)
            skipped.update({f'{host}:{reason}': count for reason, count in reasons.items()})
    return grouped, skipped


def improve_offers(targets, output, existing):
    """Refresh exact catalog variants when verified cross-platform coverage grows."""
    grouped, skipped = collect_offers(targets, 2)
    models, improved = [], {}
    for original in existing['models']:
        model = dict(original)
        current = {o['platform'] for o in model['offers']}
        expected = {'facts': model, 'gtin13': model['gtin']}
        options = [o for o in grouped[model['mpn'], model['gtin']] if compatible(o, expected)]
        selected = select_offers(options, required_platforms=current)
        platforms = {o['platform'] for o in selected}
        if current < platforms:
            model['offers'] = _public_offers(selected)
            improved[model['mpn']] = sorted(platforms - current)
        models.append(model)
    print(json.dumps({'improved': improved, 'rejected': dict(skipped)}, ensure_ascii=False), flush=True)
    if not improved:
        raise ValueError('No existing model gained a verified platform')
    snapshot = {**existing, 'checkedAt': datetime.now(timezone.utc).isoformat(), 'models': models}
    output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def publish(targets, output, images, *, pinned=None, existing=None):
    target_count = len(pinned) if pinned is not None else MINIMUM
    grouped, skipped = collect_offers(targets, 2 if existing is not None else 2 * target_count)
    ready = []
    for identity, options in grouped.items():
        if pinned and identity not in pinned:
            continue
        offers = select_offers(options)
        if offers:
            ready.append((identity, offers, options))
    print(json.dumps({'pairs': len(ready), 'rejected': dict(skipped)}, ensure_ascii=False), flush=True)
    models = [dict(model) for model in existing or []]
    files = {}
    mpns = {model['mpn'] for model in models}
    gtins = {model['gtin'] for model in models}
    brand_counts = Counter(model['brand'] for model in models)
    if existing is not None:
        fresh = {identity: offers for identity, offers, _ in ready}
        for model in models:
            offers = fresh.get((model['mpn'], model['gtin']))
            if offers and all(offer['facts']['cpu'] == model['cpu']
                              and offer['facts']['ram'] == model['ram']
                              and offer['facts']['ssd'] == model['ssd']
                              and offer['facts']['discrete'] == model['discrete']
                              and (offer['facts']['os'].startswith(model['os']) or model['os'].startswith(offer['facts']['os']))
                              for offer in offers):
                model['offers'] = _public_offers(offers)
    while ready:
        # Prefer cross-platform offers, then diversify the pilot across brands.
        ready.sort(key=lambda item: (-_cohort_score(item[1])[0],
                                     brand_counts[item[1][0]['facts']['brand']],
                                     -len(item[1]), item[0]))
        (mpn, gtin), offers, options = ready.pop(0)
        if mpn in mpns or gtin in gtins:
            continue
        photos = [*offers, *(o for o in options if all(compatible(o, item) for item in offers))]
        used = set()
        for photo in photos:
            url = photo.get('_image')
            if not url or url in used:
                continue
            used.add(url)
            image = _image(url)
            if image:
                model = build_model(offers, photo)
                models.append(model)
                files[model['slug']] = image
                mpns.add(mpn)
                gtins.add(gtin)
                brand_counts[model['brand']] += 1
                break
        if existing is None and len(models) == target_count:
            break
    if existing is not None and len(models) <= len(existing):
        raise ValueError('No new model passed the offer and photo checks')
    if existing is None and (len(models) != target_count or pinned and {(m['mpn'], m['gtin']) for m in models} != pinned):
        raise ValueError(f'Publication gate: {len(models)}/{target_count} complete products with photos and two sellers')
    images.mkdir(parents=True, exist_ok=True)
    for slug, image in files.items():
        (images / f'{slug}.webp').write_bytes(image)
    snapshot = {'checkedAt': datetime.now(timezone.utc).isoformat(),
                'models': sorted(models, key=lambda row: row['slug'])}
    output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'models': len(models), 'offers': sum(len(m['offers']) for m in models)}, ensure_ascii=False))


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('targets', type=Path)
    cli.add_argument('output', type=Path)
    cli.add_argument('images', type=Path)
    mode = cli.add_mutually_exclusive_group()
    mode.add_argument('--keep-models', action='store_true', help='Keep the already published pilot cohort')
    mode.add_argument('--expand-from', type=Path, help='Preserve published models and add newly verified configurations')
    mode.add_argument('--improve-offers-from', type=Path, help='Keep existing variants and add verified platform coverage')
    args = cli.parse_args()
    targets = json.loads(args.targets.read_text())
    if args.improve_offers_from:
        improve_offers(targets, args.output, json.loads(args.improve_offers_from.read_text()))
        return
    pinned = {(m['mpn'], m['gtin']) for m in json.loads(args.output.read_text())['models']} if args.keep_models else None
    if pinned is not None and not MINIMUM <= len(pinned) <= MAX_URLS // 2:
        raise ValueError('Unexpected number of existing model codes/GTINs')
    existing = json.loads(args.expand_from.read_text())['models'] if args.expand_from else None
    publish(targets, args.output, args.images, pinned=pinned, existing=existing)


if __name__ == '__main__':
    main()
