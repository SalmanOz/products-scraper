"""Read-only offer audit for Turkish TWS models.

The cohort contains product names only. Category cards discover product URLs.
Every price comes from a selected product page.
"""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi.requests.exceptions import RequestException as CompatibleRequestException
import requests

from mediamarkt_source import BOT, MAX_HTML_BYTES, MediaMarktSource, _selected_offer
from priority_retailer_transport import (
    PriorityRetailerTransport,
    TRENDYOL_TWS_CATEGORY,
    TRENDYOL_TWS_PAGE_LIMIT,
    compatible_get,
    product_host,
)
from retailer_barcode import retailer_gtin13
from tr_price_scraper import TRPriceScraper
from trendyol_catalog_source import _parse_selected_listing, _selected_group_root
from tws_retailer_sources import n11_offers, selected_hepsiburada_tws, selected_photo, vatan_offers
from vatan_category_source import VatanCategorySource

# Match variant words explicitly when retail titles share the same base name.
COHORT = (
    ('Xiaomi', 'Redmi Buds 6 Play', r'redmi buds 6 play'),
    ('Xiaomi', 'Redmi Buds 6 Active', r'redmi buds 6 active'),
    ('Xiaomi', 'Redmi Buds 6 Pro', r'redmi buds 6 pro'),
    ('Xiaomi', 'Redmi Buds 8', r'redmi buds 8(?! active| lite| pro| play| plus)'),
    ('Xiaomi', 'Redmi Buds 8 Active', r'redmi buds 8 active'),
    ('Xiaomi', 'Redmi Buds 8 Lite', r'redmi buds 8 lite'),
    ('Xiaomi', 'Redmi Buds 8 Pro', r'redmi buds 8 pro'),
    ('Huawei', 'FreeBuds SE 2', r'freebuds se 2'),
    ('Huawei', 'FreeBuds SE 3', r'freebuds se 3'),
    ('Huawei', 'FreeBuds SE 4 ANC', r'freebuds se 4 anc'),
    ('Huawei', 'FreeBuds 6i', r'freebuds 6i'),
    ('Huawei', 'FreeBuds 7i', r'freebuds 7i'),
    ('Apple', 'AirPods 4', r'airpods 4(?: nesil)? mxp63tu ?a'),
    ('Apple', 'AirPods 4 ANC', r'airpods 4 .*mxp93tu ?a'),
    ('Apple', 'AirPods Pro 3', r'airpods pro 3'),
    ('Samsung', 'Galaxy Buds Core', r'galaxy buds core'),
    ('Samsung', 'Galaxy Buds4 Pro', r'galaxy buds ?4 pro'),
    ('JBL', 'Wave Flex', r'wave flex(?! ?2| pro| plus)'),
    ('JBL', 'Wave Beam 2', r'wave beam ?2'),
    ('JBL', 'Wave Buds 2', r'wave buds ?2'),
    ('Anker', 'Soundcore Liberty 5', r'soundcore liberty 5(?! pro| plus| nc)'),
    ('Anker', 'Soundcore K20i', r'soundcore k20i'),
    ('Anker', 'Soundcore P40i', r'soundcore p40i'),
    ('QCY', 'HT08 MeloBuds Pro', r'ht08 melobuds pro|melobuds pro ht08'),
    ('QCY', 'MeloBuds N50', r'melobuds n50'),
)
EXPANSION_COHORT = (
    ('JBL', 'Tune Beam 2', r'tune beam ?2'),
    ('JBL', 'Tune Flex 2', r'tune flex ?2'),
    ('JBL', 'Tune Buds', r'tune buds(?! ?2| pro| plus)'),
    ('JBL', 'Wave Buds', r'wave buds(?! ?2| pro| plus)'),
    ('JBL', 'Wave Flex 2', r'wave flex ?2'),
    ('Anker', 'Soundcore P20i', r'soundcore p20i|p20i (?:tws|kulak|bluetooth)'),
    ('Anker', 'Soundcore Liberty 4 Pro', r'(?:soundcore )?liberty 4 pro'),
    ('Anker', 'Soundcore P41i', r'(?:soundcore )?p41i'),
    ('Anker', 'Soundcore A30i', r'(?:soundcore )?a30i'),
    ('Huawei', 'FreeClip', r'freeclip(?! ?2| pro| plus)'),
    ('QCY', 'T13 Pro', r't13 pro(?! ?2| plus)'),
    ('QCY', 'MeloBuds N20', r'melobuds n20'),
    ('Honor', 'Earbuds 4', r'earbuds ?4(?! ?i| pro| plus)'),
    ('realme', 'Buds T100', r'buds t100(?! pro| plus)'),
    ('Philips', 'TAT3509', r'tat ?3509(?:bk|wt|gy)?'),
)
NEW_MODELS = (
    ('Xiaomi', 'Redmi Buds 6', r'redmi buds 6(?! play| active| lite| pro| plus)'),
    ('Xiaomi', 'Buds 5', r'xiaomi buds 5(?! pro| lite| plus)'),
    ('Xiaomi', 'Buds 5 Pro', r'xiaomi buds 5 pro(?! wi fi)'),
    ('Huawei', 'FreeClip 2', r'freeclip ?2(?! pro| plus)'),
    ('Huawei', 'FreeBuds Pro 5', r'freebuds pro ?5'),
    ('Samsung', 'Galaxy Buds FE', r'galaxy buds fe'),
    ('Samsung', 'Galaxy Buds3 FE', r'galaxy buds ?3 fe'),
    ('Samsung', 'Galaxy Buds3', r'galaxy buds ?3(?! fe| pro)'),
    ('Samsung', 'Galaxy Buds3 Pro', r'galaxy buds ?3 pro'),
    ('Samsung', 'Galaxy Buds4', r'galaxy buds ?4(?! fe| pro)'),
    ('Sony', 'WF-C510', r'wf ?c510'),
    ('Sony', 'WF-C710N', r'wf ?c710n'),
    ('Sony', 'WF-1000XM5', r'wf ?1000xm5'),
    ('Sony', 'WF-1000XM6', r'wf ?1000xm6'),
    ('Sony', 'LinkBuds Clip', r'linkbuds clip|wf ?lc900b?(?: ce7)?'),
    ('Sony', 'LinkBuds Fit', r'linkbuds fit|wf ?ls910n'),
    ('JBL', 'Wave Beam', r'wave beam(?! ?2| pro| plus)'),
    ('JBL', 'Tune Beam', r'tune beam(?! ?2| pro| plus)'),
    ('JBL', 'Tune Buds 2', r'tune buds ?2'),
    ('JBL', 'Live Beam 3', r'live beam ?3'),
    ('JBL', 'Live Buds 3', r'live buds ?3'),
    ('JBL', 'Live Flex 3', r'live flex ?3'),
    ('JBL', 'Endurance Race 2', r'endurance race ?2'),
    ('JBL', 'Tour Pro 3', r'tour pro ?3'),
    ('Anker', 'Soundcore Liberty 4 NC', r'(?:soundcore )?liberty 4 nc'),
    ('Anker', 'Soundcore R50i NC', r'(?:soundcore )?r50i nc'),
    ('Anker', 'Soundcore C30i', r'(?:soundcore )?c30i'),
    ('Anker', 'Soundcore AeroFit 2', r'(?:soundcore )?aerofit ?2(?! pro)'),
    ('Anker', 'Soundcore Sport X20', r'(?:soundcore )?sport x20'),
    ('QCY', 'T13 ANC', r't13 anc(?! ?2| pro| plus)'),
    ('QCY', 'T13 ANC 2', r't13 anc ?2'),
    ('QCY', 'T17', r't17(?! ?s| pro| plus)'),
    ('QCY', 'AilyBuds Pro+', r'ailybuds pro plus'),
    ('realme', 'Buds Air 6 Pro', r'buds air 6 pro'),
)
NEW_MODEL_NAMES = tuple(f'{brand} {name}' for brand, name, _ in NEW_MODELS)
PAIR_REQUIRED_MODEL_NAMES = (
    'Huawei FreeBuds Pro 5', 'Huawei FreeClip 2', 'JBL Live Flex 3', 'JBL Tune Buds 2',
    'Samsung Galaxy Buds FE', 'Samsung Galaxy Buds3', 'Samsung Galaxy Buds4',
    'Anker Soundcore Liberty 4 NC', 'Anker Soundcore R50i NC', 'Samsung Galaxy Buds3 FE',
    'JBL Tour Pro 3', 'Sony WF-1000XM5',
    'JBL Live Beam 3', 'Anker Soundcore Sport X20', 'JBL Live Buds 3', 'JBL Endurance Race 2',
    'Sony WF-C510', 'Sony WF-C710N', 'Sony LinkBuds Clip', 'QCY T13 ANC',
)
PILOT_MODEL_NAMES = tuple(f'{brand} {name}' for brand, name, _ in COHORT)
MODEL_NAMES = (*PILOT_MODEL_NAMES, *(f'{brand} {name}' for brand, name, _ in EXPANSION_COHORT),
               *PAIR_REQUIRED_MODEL_NAMES)
BAD_CONDITION = re.compile(
    r'\b(?:yenilenmis|ikinci el|teshir|outlet|muadil|replika|sadece kutu|yedek|kilif|tek kulaklik)\b')
MEDIAMARKT_CATEGORIES = (
    ('https://www.mediamarkt.com.tr/tr/category/kulak-ici-kulakliklar-795546.html', 'kulak ici kulaklik'),
    ('https://www.mediamarkt.com.tr/tr/category/anker-kulaklik-901010.html', 'anker kulaklik'),
)
TWS_PRICE_RATIO_BY_MODEL = {'JBL Endurance Race 2': .60}
SOURCE_STOP_REASONS = {'http_403', 'http_429', 'transport_error'}


def model_name(title, brand):
    words = TRPriceScraper._normalize_words(title)
    if BAD_CONDITION.search(words):
        return None
    maker_name = TRPriceScraper._normalize_words(brand)
    if maker_name == 'soundcore':
        maker_name = 'anker'
    matches = [f'{maker} {model}' for maker, model, pattern in (*COHORT, *EXPANSION_COHORT, *NEW_MODELS)
               if maker.casefold() == maker_name
               and re.search(r'\b(?:' + pattern + r')\b', words)]
    if matches == ['JBL Tune Flex 2'] and re.search(r'\bghost\b', words):
        return None
    return matches[0] if len(matches) == 1 else None


def _category_links(html, page):
    soup = BeautifulSoup(html, 'html.parser')
    headings = soup.select('h1.title')
    if (len(headings) != 1 or TRPriceScraper._normalize_words(headings[0].get_text(' ', strip=True))
            != 'kulak ici bluetooth kulaklik'):
        return [], None
    cards = []
    for card in soup.select('a.product-card[href]'):
        title = card.select_one('h2.title')
        if title is None:
            continue
        try:
            observed = urljoin(TRENDYOL_TWS_CATEGORY, card['href'])
            url = urlparse(observed)._replace(query='').geturl()
        except ValueError:
            continue
        if product_host(url) != 'www.trendyol.com':
            continue
        brand = urlparse(url).path.split('/')[1]
        name = model_name(title.get_text(' ', strip=True), brand)
        if name is not None:
            cards.append((name, url))
    next_url = f'{TRENDYOL_TWS_CATEGORY}?pi={page + 1}'
    has_next = bool(soup.find('a', href=(next_url, urlparse(next_url).path + f'?pi={page + 1}')))
    return cards, next_url if has_next and page < TRENDYOL_TWS_PAGE_LIMIT else None


def selected_tws_offer(html, url, expected_model, *, seller_hints=None):
    discovered = []
    def variant(product, title, brand):
        category = product.get('category')
        if (not isinstance(category, dict) or category.get('id') != 1058
                or TRPriceScraper._normalize_words(category.get('name')) != 'kulak ici tws bluetooth kulaklik'
                or not isinstance(brand, dict) or not isinstance(brand.get('name'), str)
                or model_name(title, brand['name']) != expected_model):
            return None, 'selected_tws_model_or_category_mismatch'
        listing = product.get('merchantListing')
        merchant = listing.get('merchant') if isinstance(listing, dict) else None
        if not isinstance(merchant, dict):
            return None, 'selected_merchant_missing'
        if seller_hints is not None:
            rows = listing.get('otherMerchants')
            if isinstance(rows, list):
                discovered.extend(rows)
        tax_number = merchant.get('taxNumber')
        return {
            'model': expected_model,
            '_image': next((photo for url in product.get('images') or []
                            if (photo := selected_photo(url, {'cdn.dsmcdn.com'}))), None),
            'retailer_model_code': str(product.get('productCode') or ''),
            '_legal_name': TRPriceScraper._normalize_words(merchant.get('officialName')),
            '_tax_key': tax_number if isinstance(tax_number, str) and re.fullmatch(r'\d{10,11}', tax_number) else '',
        }, None

    offer, reason = _parse_selected_listing(
        html, url, variant,
        lambda schema, identity, url, physical, brand: _selected_group_root(schema, identity, url, brand),
    )
    if offer and not offer['gtin13']:
        return None, 'selected_tws_gtin_required'
    if offer and seller_hints is not None:
        for row in discovered:
            if not isinstance(row, dict):
                continue
            seller_id = row.get('id')
            raw = row.get('url')
            variants = row.get('variants')
            if (type(seller_id) is not int or seller_id <= 0 or seller_id == offer['merchant_id']
                    or not isinstance(raw, str) or not raw.startswith('/') or len(raw) > 1000
                    or not isinstance(variants, list) or not any(
                        isinstance(v, dict) and v.get('inStock') is True and v.get('sellable') is True
                        and v.get('itemNumber') == offer['item_number']
                        and retailer_gtin13(v.get('barcode')) == offer['gtin13'] for v in variants)):
                continue
            try:
                target = urljoin(url, raw)
                route = urlparse(target)
                params = parse_qs(route.query)
            except ValueError:
                continue
            if params.pop('sav', ['true']) != ['true']:
                continue
            target = route._replace(query='&'.join(
                f'{key}={values[0]}' for key, values in params.items() if len(values) == 1)).geturl()
            if (product_host(target) == 'www.trendyol.com' and route.path == urlparse(url).path
                    and params.get('merchantId') == [str(seller_id)]
                    and set(params) <= {'merchantId', 'boutiqueId'}
                    and all(len(values) == 1 for values in params.values())):
                seller_hints.append((target, seller_id))
    return offer, reason


def independent_sellers(first, second):
    def identity(offer):
        company = offer.get('_legal_name', '')
        compact = company.replace(' ', '')
        for retailer in ('mediamarkt', 'vatanbilgisayar'):
            if retailer in compact:
                return retailer
        return re.sub(r' (?:sanayi ve ticaret |ticaret )?(?:anonim|limited) sirketi$', '', company)

    first_identity, second_identity = identity(first), identity(second)
    return (bool(first_identity and second_identity) and first_identity != second_identity
            and not (first.get('_tax_key') and first['_tax_key'] == second.get('_tax_key'))
            and (first['platform'] != second['platform']
                 or first.get('merchant_id') != second.get('merchant_id')))


def _mediamarkt_tws_url(url):
    return bool(re.fullmatch(r'https://www\.mediamarkt\.com\.tr/tr/product/_[a-z0-9-]+-\d{6,12}\.html', url))


def _mediamarkt_offers(source, failures):
    def read(url):
        if not source._rules.can_fetch(BOT, url):
            return None, 'robots_disallowed'
        try:
            body, reason, _ = source._direct(url, MAX_HTML_BYTES, 'text/html')
            return body if reason == 'ready' else None, reason
        except (requests.RequestException, CompatibleRequestException):
            return None, 'transport_error'

    try:
        source._load_robots()
    except CompatibleRequestException:
        failures['mediamarkt_robots_unavailable'] += 1
        return {}
    if source._rules is None:
        failures[f'mediamarkt_{source._robots_status}'] += 1
        return {}
    candidates = {}
    for category, heading in MEDIAMARKT_CATEGORIES:
        html, reason = read(category)
        if html is None:
            failures[f'mediamarkt_{reason}'] += 1
            if reason in SOURCE_STOP_REASONS:
                return {}
            continue
        soup = BeautifulSoup(html, 'html.parser')
        headings = soup.select('h1')
        if (len(headings) != 1 or TRPriceScraper._normalize_words(headings[0].get_text(' ', strip=True))
                != heading):
            failures['mediamarkt_category_identity_mismatch'] += 1
            continue
        for card in soup.select('a[data-test="mms-router-link"][href^="/tr/product/"]'):
            title = card.get_text(' ', strip=True)
            if not title:
                continue
            name = model_name(title, title.split()[0])
            url = urljoin(category, card['href'])
            if name and _mediamarkt_tws_url(url):
                candidates[url] = name
    offers = defaultdict(list)
    for url, name in list(candidates.items())[:24]:
        html, reason = read(url)
        if html is None:
            failures[f'mediamarkt_{reason}'] += 1
            if reason in SOURCE_STOP_REASONS:
                break
            continue

        def verify_product(product, title, brand):
            gtin = retailer_gtin13(product.get('gtin13'))
            if not gtin or model_name(title, brand) != name:
                return None
            return {'platform': 'MediaMarkt', 'model': name, 'sku': str(product['sku']),
                    'gtin13': gtin, 'condition': 'new', '_legal_name': 'media markt turkey ticaret limited sirketi'}

        offer = _selected_offer(html, url, allowed_url=_mediamarkt_tws_url, verify_product=verify_product)
        if offer and offer['availability'] == 'in_stock':
            offers[name].append(offer)
        else:
            failures['mediamarkt_selected_offer_unproven'] += 1
    return offers


def _trendyol_offers(transport, failures, models=MODEL_NAMES):
    candidates, results = defaultdict(list), defaultdict(list)
    url = TRENDYOL_TWS_CATEGORY
    for page in range(1, TRENDYOL_TWS_PAGE_LIMIT + 1):
        html, reason, _ = transport.read_trendyol_tws_category(url)
        if html is None:
            failures[reason] += 1
            break
        cards, url = _category_links(html, page)
        for name, destination in cards:
            if destination not in candidates[name]:
                candidates[name].append(destination)
        if url is None:
            break

    def read(url, name, *, seller_hints=None):
        html, reason, _ = transport.read_document(url)
        if html is None:
            failures[reason] += 1
            return None
        offer, reason = selected_tws_offer(html, transport.resolved_url(url), name, seller_hints=seller_hints)
        if offer is None:
            failures[reason] += 1
        return offer

    alternatives = defaultdict(list)
    for index in range(6):
        for name in models:
            if len(candidates[name]) <= index:
                continue
            url = candidates[name][index]
            hints = []
            offer = read(url, name, seller_hints=hints)
            if offer:
                results[name].append(offer)
                if not alternatives[name]:
                    alternatives[name].extend((offer['gtin13'], destination, seller_id)
                                              for destination, seller_id in hints[:2])
    for name, hints in alternatives.items():
        for gtin, destination, seller_id in hints:
            other = read(destination, name)
            if not other:
                continue
            if (other['gtin13'], other['merchant_id']) != (gtin, seller_id):
                failures['trendyol_alternative_seller_or_gtin_mismatch'] += 1
                continue
            results[name].append(other)
    return results, candidates


def select_sku_offers(observations, *, min_price_ratio=0):
    by_gtin = defaultdict(list)
    for offer in sorted(observations, key=lambda o: (o['price'], o['url'])):
        by_gtin[offer['gtin13']].append(offer)
    for size in (3, 2):
        matches = (offers for group in by_gtin.values() for offers in combinations(group, size)
                   if all(independent_sellers(a, b) and min(a['price'], b['price']) >= min_price_ratio * max(a['price'], b['price'])
                          for a, b in combinations(offers, 2)))
        best = min(matches, key=lambda offers: (sum(o['price'] for o in offers),
                   tuple(o['url'] for o in offers)), default=None)
        if best:
            return list(best)
    return [min(observations, key=lambda o: (o['price'], o['url']))] if observations else []


HEPSIBURADA_TWS_TARGETS = (
    ('QCY T13 ANC',
     'https://www.hepsiburada.com/qcy-t13-anc-4-mikrofon-bluetooth-5-3-kablosuz-kulaklik-'
     'aktif-gurultu-engelleme-siyah-p-HBCV000047WC98',
     '6957141407882'),
    ('JBL Endurance Race 2',
     'https://www.hepsiburada.com/jbl-endurance-race2-tws-kulaklik-ie-siyah-p-HBCV000095WJQ4',
     '1200130014291'),
    ('JBL Endurance Race 2',
     'https://www.hepsiburada.com/jbl-endurance-race2-tws-kulaklik-ie-siyah-p-HBCV000095WJQ4?magaza=BURAK+ONL%C4%B0NE',
     '1200130014291'),
    ('Anker Soundcore Sport X20',
     'https://www.hepsiburada.com/anker-soundcore-sport-x20-kablosuz-egzersiz-kulakligi-'
     'gurultu-engelleme-siyah-a3968-p-HBCV0000735D89',
     '0194644187019'),
)


def _hepsiburada_offers(transport, failures):
    results = defaultdict(list)
    for name, url, gtin in HEPSIBURADA_TWS_TARGETS:
        html, reason, _ = transport.read_document(url)
        if html and transport.resolved_url(url) == url:
            offer, reason = selected_hepsiburada_tws(html, url, name, model_name)
            if offer and offer['gtin13'] == gtin:
                results[name].append(offer)
                continue
        failures['hepsiburada_' + reason] += 1
    return results


def audit(transport, mediamarkt=None, *, n11=None, vatan=None, hepsiburada=None):
    failures, n11_failures, vatan_failures, mm_failures = Counter(), Counter(), Counter(), Counter()
    hb_failures = Counter()
    with ThreadPoolExecutor(max_workers=5) as pool:
        trendyol = pool.submit(_trendyol_offers, transport, failures)
        mm = pool.submit(_mediamarkt_offers, mediamarkt, mm_failures) if mediamarkt is not None else None
        n11_task = pool.submit(n11_offers, n11, MODEL_NAMES, model_name, n11_failures) if n11 is not None else None
        vatan_task = pool.submit(vatan_offers, vatan, MODEL_NAMES, model_name, vatan_failures) if vatan is not None else None
        hb = pool.submit(_hepsiburada_offers, hepsiburada, hb_failures) if hepsiburada is not None else None
        observations, candidates = trendyol.result()
        all_sources = [observations, *(task.result() for task in (mm, n11_task, vatan_task, hb) if task is not None)]
    failures.update(mm_failures)
    failures.update(n11_failures)
    failures.update(vatan_failures)
    failures.update(hb_failures)
    observed = {name: [offer for source in all_sources for offer in source.get(name, [])]
                for name in MODEL_NAMES}
    owners = defaultdict(set)
    for name, offers in observed.items():
        for offer in offers:
            owners[offer['gtin13']].add(name)
    collisions = {gtin for gtin, names in owners.items() if len(names) != 1}
    if collisions:
        failures['gtin_claimed_by_multiple_models'] += len(collisions)
    eligible = {name: [o for o in offers if o['gtin13'] not in collisions] for name, offers in observed.items()}
    results = {name: select_sku_offers(offers, min_price_ratio=TWS_PRICE_RATIO_BY_MODEL.get(name, 0))
               for name, offers in eligible.items()}

    def public_evidence(offer):
        return {key: value for key, value in offer.items() if not key.startswith('_')}

    rows = [{
        'model': name, 'candidate_urls': len(candidates[name]),
        'offers': [public_evidence(offer) for offer in offers],
        'alternatives': [public_evidence(o) for o in sorted(eligible[name], key=lambda o: (o['price'], o['url']))
                         if o not in offers][:8],
    } for name, offers in results.items()]
    priced = sum(bool(row['offers']) for row in rows)
    multi = sum(len(row['offers']) >= 2 for row in rows)
    return {
        'mode': 'readonly_tws_offer_pilot', 'writes': False, 'cohort_size': len(rows),
        'verified_models': priced, 'models_with_two_independent_sellers_on_same_sku': multi,
        'publication_gate_passed': priced >= 20 and multi >= 15,
        'rejections': dict(failures.most_common()),
        'transport': {**transport.report(), **(n11.report() if n11 is not None else {})},
        'verified_observations_by_platform': dict(Counter(offer['platform'] for source in all_sources
            for offers in source.values() for offer in offers)),
        'models': rows,
    }


def main():
    from laptop_pilot import _hepsiburada_public_get
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = audit(PriorityRetailerTransport(maximum_per_host=250), MediaMarktSource(get=compatible_get),
                   n11=PriorityRetailerTransport(maximum_per_host=260), vatan=VatanCategorySource(),
                   hepsiburada=PriorityRetailerTransport(maximum_per_host=12, get=_hepsiburada_public_get,
                                                         browser_fallback=False))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'models'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
