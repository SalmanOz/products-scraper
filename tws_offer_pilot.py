"""Read-only offer audit for a fixed cohort of 25 Turkish TWS models.

The cohort contains product names only. Category cards discover product URLs.
Every price comes from a selected product page.
"""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlparse

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
    ('Apple', 'AirPods 4', r'airpods 4(?: nesil)? mxp63tu a'),
    ('Apple', 'AirPods 4 ANC', r'airpods 4 .*mxp93tu a'),
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
MODEL_NAMES = tuple(f'{brand} {name}' for brand, name, _ in COHORT)
BAD_CONDITION = re.compile(r'\b(?:yenilenmis|ikinci el|teshir|outlet|muadil|replika|sadece kutu)\b')
MEDIAMARKT_CATEGORIES = (
    ('https://www.mediamarkt.com.tr/tr/category/kulak-ici-kulakliklar-795546.html', 'kulak ici kulaklik'),
    ('https://www.mediamarkt.com.tr/tr/category/anker-kulaklik-901010.html', 'anker kulaklik'),
)
SOURCE_STOP_REASONS = {'http_403', 'http_429', 'transport_error'}


def model_name(title, brand):
    words = TRPriceScraper._normalize_words(title)
    if BAD_CONDITION.search(words):
        return None
    matches = [f'{maker} {model}' for maker, model, pattern in COHORT
               if maker.casefold() == brand.casefold()
               and re.search(r'\b(?:' + pattern + r')\b', words)]
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


def selected_tws_offer(html, url, expected_model):
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
        tax_number = merchant.get('taxNumber')
        return {
            'model': expected_model,
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
    return offer, reason


def independent_sellers(first, second):
    return (first['merchant_id'] != second['merchant_id']
            and bool(first['_legal_name'] and second['_legal_name'])
            and first['_legal_name'] != second['_legal_name']
            and not (first['_tax_key'] and first['_tax_key'] == second['_tax_key']))


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
                    'gtin13': gtin, 'condition': 'new'}

        offer = _selected_offer(html, url, allowed_url=_mediamarkt_tws_url, verify_product=verify_product)
        if offer and offer['availability'] == 'in_stock':
            offers[name].append(offer)
        else:
            failures['mediamarkt_selected_offer_unproven'] += 1
    return offers


def audit(transport, mediamarkt=None):
    candidates, failures, results = defaultdict(list), Counter(), {}
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

    def read(url, name):
        html, reason, _ = transport.read_document(url)
        if html is None:
            failures[reason] += 1
            return None
        offer, reason = selected_tws_offer(html, transport.resolved_url(url), name)
        if offer is None:
            failures[reason] += 1
        return offer

    inspected = set()
    for name in MODEL_NAMES:
        offers = results[name] = []
        for url in candidates[name][:3]:
            inspected.add(url)
            primary = read(url, name)
            if primary is not None:
                offers.append(primary)
                break

    if mediamarkt is not None:
        for name, alternatives in _mediamarkt_offers(mediamarkt, failures).items():
            offers = results[name]
            if not offers:
                offers.append(alternatives[0])
                continue
            primary = offers[0]
            seller = primary['_legal_name'].replace(' ', '')
            exact = next((offer for offer in alternatives if offer['gtin13'] == primary['gtin13']), None)
            if exact and seller and 'mediamarkt' not in seller:
                offers.append(exact)

    # First cover the whole cohort. Then inspect other observed canonical
    # listings, whose own selected seller survives the public document read.
    for name, offers in results.items():
        if len(offers) != 1 or offers[0]['platform'] != 'Trendyol':
            continue
        primary = offers[0]
        for url in candidates[name][:6]:
            if url in inspected:
                continue
            inspected.add(url)
            extra = read(url, name)
            if extra is None:
                continue
            if extra['gtin13'] != primary['gtin13']:
                failures['alternate_listing_has_different_gtin'] += 1
                continue
            if extra['listing_id'] != primary['listing_id'] and independent_sellers(primary, extra):
                offers.append(extra)
                break
            failures['independent_seller_not_established'] += 1

    owners = defaultdict(set)
    for name, offers in results.items():
        for offer in offers:
            owners[offer['gtin13']].add(name)
    for name, offers in results.items():
        if any(len(owners[offer['gtin13']]) != 1 for offer in offers):
            failures['gtin_claimed_by_multiple_models'] += 1
            results[name] = []
    rows = [{
        'model': name, 'candidate_urls': len(candidates[name]),
        'offers': [{key: val for key, val in offer.items() if not key.startswith('_')} for offer in offers],
    } for name, offers in results.items()]
    priced = sum(bool(row['offers']) for row in rows)
    multi = sum(len(row['offers']) >= 2 for row in rows)
    return {
        'mode': 'readonly_tws_offer_pilot', 'writes': False, 'cohort_size': len(rows),
        'verified_models': priced, 'models_with_two_independent_sellers_on_same_sku': multi,
        'publication_gate_passed': priced >= 20 and multi >= 15,
        'rejections': dict(failures.most_common()), 'transport': transport.report(),
        'models': rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = audit(PriorityRetailerTransport(maximum_per_host=150), MediaMarktSource(get=compatible_get))
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'models'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
