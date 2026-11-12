"""Verify retailer leads for existing phones using two independent sellers of one GTIN."""

import argparse
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from itertools import zip_longest
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup
import requests

from ayaz_phone_source import AYAZ_HOST, AYAZ_TERMS, ayaz_offer, ayaz_phone_url
from catalog_source_collector import catalog_document_reader

from competitor_catalog_gaps import public_phone_catalog, read_state, retailer_destination, write_json
from hepsiburada_catalog_discovery import public_model_search_cards
from hepsiburada_catalog_source import parse_catalog_buy_box
from laptop_pilot import _hepsiburada_public_get, verify_seller
from laptop_retailer_sources import marketplace_seller_identity
from mediamarkt_marketplace_audit import selected_marketplace_offer
from mediamarkt_source import _selected_offer as mediamarkt_first_party, product_url as mediamarkt_url
from n11_catalog_source import (discover_catalog_targets as n11_search_targets, parse_catalog_listing as n11_listing,
                                public_search_card_document)
from offer_ingestion_client import IngestionClientError, OfferIngestionClient
from ozgur_phone_source import OZGUR_HOST, OZGUR_TERMS, ozgur_offer, ozgur_phone_url
from pazarama_category_source import pazarama_product_url
from pazarama_offer_source import native_script, pazarama_selected
from priority_retailer_transport import PriorityRetailerTransport, product_host
from selected_sku_evidence import selected_sku_evidence
from teknosa_reviewed_source import parse_catalog_offer as parse_teknosa_offer
from teknosa_seller_cards import canonical_document_url as teknosa_document_url
from tr_price_scraper import TRPriceScraper
from trendyol_catalog_source import parse_catalog_listing as trendyol_listing, selected_product_state
from tws_offer_pilot import independent_sellers
from tws_retailer_sources import selected_vatan_tws
from update_prices import PriceUpdater
from retailer_barcode import retailer_gtin13


RETAILER_CATEGORIES = {'apple-iphone-16-plus': (
    'https://www.mediamarkt.com.tr/tr/category/iphone-16-plus-903529.html',
)}
IPHONE_16_PLUS_PROFILES = Path(__file__).with_name('offer_batches') / 'comparison-iphone-16-plus.json'
IPHONE_16_PLUS_SCOPE = {
    'apple-iphone-16-plus': '0195949723001',
    'apple-iphone-16-plus-8gb-256gb': '0195949723193',
}
MM_SELLERS = 'https://www.mediamarkt.com.tr/tr/marketplace/seller/'
NORMALIZE = TRPriceScraper._normalize_words


def mediamarkt_selected(html, url):
    for condition_card in (False, True):
        offer = selected_marketplace_offer(html, url, allow_refurbished_alternatives=True,
            allow_cents=True, allow_delivery_card=condition_card, require_gtin_variant=True)
        if offer:
            return {**offer, 'merchant': offer['seller_name'], 'merchant_id': offer['seller_id'],
                    'seller_url': MM_SELLERS + offer['seller_id']}

    def variant(product, _title, _brand):
        gtin = retailer_gtin13(product.get('gtin13'))
        return ({'gtin13': gtin, 'platform': 'MediaMarkt',
                 '_legal_name': 'media markt turkey ticaret limited sirketi'} if gtin else None)

    return mediamarkt_first_party(html, url, verify_product=variant)


def teknosa_selected(html, url, name, specs):
    offer, _ = parse_teknosa_offer(html, url, {'name': name, 'specs': specs})
    if not offer:
        return None
    if offer.get('selected_teknosa_first_party_verified') is True:
        return {**offer, '_legal_name': 'teknosa ic ve dis ticaret anonim sirketi'}
    if offer.get('seller_url'):
        return offer
    soup = BeautifulSoup(html, 'html.parser')
    links = soup.select('.pdp-seller-info > a[href]')
    if len(links) != 1 or not re.fullmatch(r'/magaza/[a-z0-9-]+', links[0]['href']):
        return None
    return {**offer, 'seller_url': urljoin(url, links[0]['href'])}


def trendyol_selected(html, url, name, specs, *, reviewed_gtin=None):
    offer, reason = trendyol_listing(html, url, name, specs, reviewed_gtin=reviewed_gtin)
    if not offer:
        return None, reason
    if not offer.get('gtin13') or offer.get('price_kind') == 'basket':
        return None, 'selected_gtin_or_cash_unverified'
    state, _ = selected_product_state(BeautifulSoup(html, 'html.parser'))
    seller = (state or {}).get('merchantListing', {}).get('merchant', {})
    legal = seller.get('officialName')
    tax = seller.get('taxNumber')
    if not isinstance(legal, str) or not NORMALIZE(legal) or str(seller.get('id')) != str(offer.get('merchant_id')):
        return None, 'selected_legal_seller_identity_missing'
    return {**offer, '_legal_name': NORMALIZE(legal),
            '_tax_key': tax if isinstance(tax, str) and re.fullmatch(r'\d{10,11}', tax) else ''}, reason


def seller_identity(html, offer):
    if offer['platform'] == 'Teknosa':
        identity = marketplace_seller_identity(html, offer)
        return (identity['_legal_name'], identity['_tax_key']) if identity else None
    soup = BeautifulSoup(html, 'html.parser')
    headers = soup.select('h1')
    if len(headers) != 1 or NORMALIZE(headers[0].get_text(' ', strip=True)) != NORMALIZE(offer['merchant']):
        return None
    if offer['platform'] == 'MediaMarkt':
        text = ' '.join(soup.get_text(' ', strip=True).split())
        match = re.search(r'Firma adı:\s*(.{3,160}?)\s+Şehir:.*?MERSİS No:\s*([0-9]{15,16})\b'
                          r'.*?Vergi No:\s*(\d{10,11})\b', text)
        return (NORMALIZE(match[1]), match[3]) if match else None
    state = native_script(soup)
    matches = re.findall(r'sellerMersisNumber:"(\d{16})"[^{}]{0,1400}?'
                         r'sellerTradeName:("(?:[^"\\]|\\.)+")', state)
    if len(matches) != 1:
        return None
    mersis, name = matches[0]
    return NORMALIZE(json.loads(name)), mersis[1:11]


def checked_offers(urls, name, specs, transport, documents=None, *, direct=None, seller_cache=None, reviewed_gtin=None):
    observed, rejects = [], []
    documents = {} if documents is None else documents
    direct = direct or {}
    seller_cache = {} if seller_cache is None else seller_cache
    matcher = TRPriceScraper()
    for url in urls:
        host = urlsplit(url).netloc
        reader = direct.get(host, transport)
        document_url = teknosa_document_url(url) if host == 'www.teknosa.com' else url
        if not document_url:
            continue
        if document_url not in documents:
            documents[document_url] = reader._read_document(document_url, host)
        html, reason, _ = documents[document_url]
        offer = None
        rejected = 'selected_offer_unverified'
        if html:
            if mediamarkt_url(url):
                offer = mediamarkt_selected(html, url)
            elif pazarama_product_url(url):
                offer = pazarama_selected(html, url, phone=True)
            elif host == 'www.teknosa.com':
                offer = teknosa_selected(html, url, name, specs)
            elif host == 'www.trendyol.com':
                resolved = transport.resolved_url(url)
                offer, rejected = trendyol_selected(html, resolved, name, specs, reviewed_gtin=reviewed_gtin)
            elif host == 'www.n11.com':
                resolved = transport.resolved_url(url)
                offer, rejected = n11_listing(html, resolved, name, specs, include_legal_identity=True)
            elif host == 'www.hepsiburada.com' and host in direct:
                offer, rejected = parse_catalog_buy_box(html, url, name, specs)
                if offer and not verify_seller(offer, None, reader, seller_cache):
                    offer, rejected = None, 'seller_identity_unverified'
            elif ozgur_phone_url(url) and host in direct:
                if OZGUR_TERMS not in documents:
                    documents[OZGUR_TERMS] = reader._read_document(OZGUR_TERMS, host, category=True)
                terms, legal_reason, _ = documents[OZGUR_TERMS]
                if terms:
                    try:
                        offer, _ = ozgur_offer(html, terms, url=url, name=name, specs=specs)
                    except (ValueError, TypeError, KeyError, AttributeError) as error:
                        rejected = str(error) if str(error).startswith('ozgur_') else 'ozgur_invalid_source_data'
                else:
                    rejected = 'ozgur_seller_identity_' + legal_reason
            elif ayaz_phone_url(url) and host in direct:
                if AYAZ_TERMS not in documents:
                    documents[AYAZ_TERMS] = reader._read_document(AYAZ_TERMS, host, category=True)
                terms, _, _ = documents[AYAZ_TERMS]
                if terms:
                    offer = ayaz_offer(html, terms, url=url, name=name, specs=specs)
                if offer is None:
                    rejected = 'ayaz_selected_offer_or_seller_unverified'
            elif vatan_phone_url(url):
                offer = selected_vatan_tws(html, url, name,
                    lambda title, _brand: name if matcher.is_strict_match(name, title, specs) else None)
        if (not offer or not retailer_gtin13(offer.get('gtin13')) or offer.get('price_kind') == 'basket' or
                offer.get('availability') != 'in_stock' or offer.get('currency') != 'TRY' or
                not matcher.has_observed_offer_evidence(name, offer, specs)):
            rejects.append({'url': url, 'reason': rejected if html and offer is None else
                            'catalog_variant_or_cash_mismatch' if html else reason})
            continue
        identity = (offer['_legal_name'], offer.get('_tax_key', '')) if offer.get('_legal_name') else None
        if not identity and (seller_url := offer.get('seller_url')):
            if seller_url not in documents:
                documents[seller_url] = transport._read_document(seller_url, host)
            seller_document, _, _ = documents[seller_url]
            identity = seller_identity(seller_document, offer) if seller_document else None
        if not identity or not identity[0]:
            rejects.append({'url': url, 'reason': 'seller_identity_unverified'})
            continue
        observed.append({**offer, '_legal_name': identity[0], '_tax_key': identity[1],
                         'observed_at': datetime.now(timezone.utc).isoformat()})
    return next(iter(ranked_pairs(observed)), []), observed, rejects


def ranked_pairs(observed):
    groups = defaultdict(list)
    for offer in sorted(observed, key=lambda row: (row['price'], row['url'])):
        groups[offer['gtin13']].append(offer)
    pairs = [[first, second] for group in groups.values()
             for i, first in enumerate(group) for second in group[i + 1:]
             if (first['platform'] != second['platform'] and independent_sellers(first, second)
                 and first['price'] >= second['price'] * .60)]
    return sorted(pairs, key=lambda pair: (pair[0]['price'], pair[1]['price']))


def product_family(slug):
    return re.sub(r'-[1-9]\d?gb-[1-9]\d{1,3}gb$', '', slug)


def capacity_priority(url, storage_gb):
    """Rank public URL hints without treating them as variant evidence."""
    storage_gb = TRPriceScraper._spec_int(storage_gb)
    if not storage_gb:
        return 1
    route = urlsplit(url)
    selector = parse_qs(route.query).get('dahili-hafiza', [])
    text = selector[0] if len(selector) == 1 else route.path
    if route.netloc in ('www.mediamarkt.com.tr', 'www.teknosa.com'):
        text = TRPriceScraper.split_joined_retailer_url_capacity(text)
    values = {
        int(amount) * (1024 if unit == 'tb' else 1)
        for amount, unit in re.findall(r'(?<!\d)([1-9]\d{0,3})[-_ ]?(gb|tb)(?=$|[-_.\d])', text.casefold())
    }
    plausible = {value for value in values if value >= min(storage_gb, 64)}
    return 0 if storage_gb in plausible else 2 if plausible else 1


def category_leads(slug, name, specs, transport, documents):
    matcher = TRPriceScraper()
    leads = set()
    for category in RETAILER_CATEGORIES.get(product_family(slug), ()):
        host = urlsplit(category).netloc
        if category not in documents:
            documents[category] = transport._read_document(category, host, category=True)
        html, _, _ = documents[category]
        if not html:
            continue
        soup = BeautifulSoup(html, 'html.parser')
        for anchor in soup.select('a[href*="/tr/product/"]'):
            url = retailer_destination(urljoin(category, anchor['href']))
            if url and mediamarkt_url(url) and matcher.is_strict_match(name, anchor.get_text(' ', strip=True), specs):
                leads.add(url)
    return sorted(leads)


def vatan_phone_url(url):
    route = urlsplit(url)
    return (route.netloc == 'www.vatanbilgisayar.com' and not route.query
            and any(token in route.path for token in ('iphone', 'akilli-telefon', 'cep-telefonu'))
            and bool(re.fullmatch(r'/[a-z0-9-]+\.html', route.path)))


def supported_phone_url(url, *, expanded=False):
    route = urlsplit(url)
    if route.netloc == OZGUR_HOST:
        return expanded and ozgur_phone_url(url)
    if route.netloc == AYAZ_HOST:
        return expanded and ayaz_phone_url(url)
    if route.netloc in ('www.n11.com', 'www.trendyol.com'):
        return product_host(url) == route.netloc
    if expanded and route.netloc == 'www.hepsiburada.com':
        return product_host(url) == route.netloc and 'yenilenmis' not in route.path
    if route.netloc == 'www.teknosa.com':
        return product_host(url) == route.netloc and any(
            token in route.path for token in ('iphone', 'akilli-telefon', 'cep-telefonu'))
    if route.query:
        return False
    if route.netloc == 'www.mediamarkt.com.tr':
        return mediamarkt_url(url) and 'akilli-telefon' in route.path
    if not any(token in route.path for token in ('iphone', 'akilli-telefon', 'cep-telefonu')):
        return False
    if expanded and route.netloc == 'www.vatanbilgisayar.com':
        return vatan_phone_url(url)
    return pazarama_product_url(url) and bool(re.fullmatch(r'/[a-z0-9-]+-p-\d{12,13}', route.path))


def phone_lead_index(state, *, expanded=False):
    by_family, by_slug = defaultdict(dict), defaultdict(dict)
    for row in state['products'].values():
        family, slug = row.get('catalogFamily'), row.get('catalogSlug')
        if row.get('category') != 'phone' or not (family or slug):
            continue
        urls = {url: None for raw in row.get('retailerLeads', ())
                if (url := retailer_destination(raw)) and supported_phone_url(url, expanded=expanded)}
        if family:
            by_family[family].update(urls)
        if slug:
            by_slug[slug].update(urls)
    return by_family, by_slug


def indexed_phone_leads(index, slug):
    by_family, by_slug = index
    return list(dict.fromkeys([*by_slug.get(slug, ()), *by_family.get(product_family(slug), ())]))


def candidate_urls(state, slug, *, expanded=False):
    return indexed_phone_leads(phone_lead_index(state, expanded=expanded), slug)


def published_offer_urls(slug):
    if not re.fullmatch(r'[a-z0-9-]{1,160}', slug):
        return []
    url = 'https://teknoskor.com/product/' + slug
    try:
        with requests.get(url, timeout=(4, 10), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                return []
            body = bytearray()
            for chunk in response.iter_content(65536):
                if len(body) + len(chunk) > 3 * 1024 * 1024:
                    return []
                body.extend(chunk)
    except requests.RequestException:
        return []
    soup = BeautifulSoup(body.decode('utf-8', errors='replace'), 'html.parser')
    return list(dict.fromkeys(
        target for link in soup.select('[data-current-offer] a[href]')
        if (target := retailer_destination(link['href'])) and supported_phone_url(target, expanded=True)
    ))


def saved_phone_leads(state, slug):
    return [url for raw in state.get('phoneNativeLeads', {}).get(slug, ())
            if (url := retailer_destination(raw)) and supported_phone_url(url, expanded=True)]


def round_robin_phone_families(rows, maximum):
    families = defaultdict(deque)
    for row in rows:
        families[product_family(row['slug'])].append(row)
    queue = deque(families.values())
    selected = []
    while queue and len(selected) < maximum:
        siblings = queue.popleft()
        selected.append(siblings.popleft())
        if siblings:
            queue.append(siblings)
    return selected


def audit_queue(state, products, maximum=8):
    audited = state.get('phoneOfferAudits', {})
    candidates, unrouted = [], []
    index = phone_lead_index(state, expanded=True)
    for product in products:
        slug = product['slug']
        if not product.get('name') or not isinstance(product.get('attributes'), dict):
            continue
        urls = [*indexed_phone_leads(index, slug), *saved_phone_leads(state, slug)]
        storage = PriceUpdater.get_expected_specs(product['attributes']).get('storage_gb')
        compatible_hosts = {urlsplit(url).netloc for url in urls if capacity_priority(url, storage) < 2}
        if compatible_hosts:
            candidates.append((product, len(compatible_hosts)))
        elif TRPriceScraper._spec_int(storage) and product.get('fresh_offer_count') in (0, '0'):
            unrouted.append(product)
    ranked = sorted(candidates, key=lambda entry: (
        audited.get(entry[0]['slug'], ''), entry[0].get('fresh_offer_count') not in (0, '0'),
        -min(entry[1], 2), entry[0]['slug']))
    reserved = min(len(unrouted), maximum // 4)
    selected = round_robin_phone_families((row for row, _ in ranked), maximum - reserved)
    unrouted.sort(key=lambda row: (audited.get(row['slug'], ''), row['slug']))
    return selected + round_robin_phone_families(unrouted, maximum - len(selected))


def n11_phone_leads(name, specs, reader, excluded_urls, cache=None):
    storage = TRPriceScraper._spec_int(specs.get('storage_gb'))
    model = TRPriceScraper._catalog_model_name(name, specs)
    if not storage or not model:
        return [], {'reason': 'phone_search_identity_incomplete', 'destinations': 0}
    model = ' '.join(re.sub(r'\b(?:4g|5g|lte)\b', '', model, flags=re.I).split())
    capacity = f'{storage // 1024} TB' if storage >= 1024 and storage % 1024 == 0 else f'{storage} GB'
    query = f'{model} {capacity}'
    key = ('n11', query)
    if cache is not None and key in cache:
        html, reason, status = cache[key], 'same_run_search_hints', 200
    else:
        html, reason, status = reader.read_n11_phone_search(query)
        if cache is not None and html and status == 200:
            cache[key] = public_search_card_document(html)
    urls = [row['source_url'] for row in n11_search_targets(html, name, specs, excluded_urls=excluded_urls)]
    return urls, {'reason': reason, 'httpStatus': status, 'destinations': len(urls)}


def hepsiburada_phone_leads(name, specs, reader, excluded_urls, cache=None):
    storage = TRPriceScraper._spec_int(specs.get('storage_gb'))
    model = TRPriceScraper._catalog_model_name(name, specs)
    if not storage or not model:
        return [], {'reason': 'phone_search_identity_incomplete', 'destinations': 0}
    radio = specs.get('network_generation')
    if radio and TRPriceScraper._dual_radio_family(model) and not TRPriceScraper._radio_generation(model):
        model = f'{model} {radio.upper()}'
    key = ('Hepsiburada', model)
    if cache is not None and key in cache:
        cards, reason, status, parser_status = cache[key], 'same_run_search_hints', 200, 'parsed'
    else:
        html, reason, status = reader.read_hepsiburada_search(model)
        cards, parser_status = public_model_search_cards(html, model) if html else ([], None)
        if cache is not None and parser_status == 'parsed' and status == 200:
            cache[key] = cards
    matcher = TRPriceScraper()
    urls = [card['source_url'] for card in cards
            if card['source_url'] not in excluded_urls and capacity_priority(card['source_url'], storage) < 2
            and matcher.is_strict_match(name, card['discovery_title'], specs)]
    urls.sort(key=lambda url: capacity_priority(url, storage))
    return urls[:2], {'reason': reason, 'httpStatus': status, 'parser': parser_status, 'destinations': min(2, len(urls))}


def publishable_pair(observations, client, product):
    decisions, diagnostics = {}, []
    for pair in ranked_pairs(observations):
        for offer in pair:
            key = offer['url'], offer['merchant'], offer['gtin13']
            if key not in decisions:
                try:
                    selected = selected_sku_evidence(offer)
                    gtin = selected.get('gtin13') if selected else None
                    proof = client.get_raw_seller_collision_proof(
                        product['id'], offer['merchant'], offer['url'], **({'gtin13': gtin} if gtin else {}))
                    other = proof.get('proposed_seller_other_url_rows')
                    historical = proof.get('proposed_seller_historical_other_url_rows')
                    scope = proof.get('verified_variant_scope') if gtin else None
                    other_listing_reason = proof.get('reasons') == ['proposed_seller_on_other_url_or_platform']
                    if scope is not None:
                        compatible = (scope['conflicting_exact_url_rows'] == 0 and scope['unresolved_other_url_rows'] == 0
                                      and (proof.get('blocked') is False or other_listing_reason))
                    else:
                        expired = other_listing_reason and type(historical) is int and historical > 0 and other == historical
                        compatible = proof.get('blocked') is False or expired
                    decisions[key] = proof.get('complete') is True and compatible
                    diagnostics.append({'platform': offer['platform'], 'reasons': proof.get('reasons'),
                                        'otherUrls': other, 'expiredOtherUrls': historical,
                                        **({'variantScope': scope} if scope else {})})
                except IngestionClientError as error:
                    match = re.match(r'HTTP ([45]\d{2})\b', str(error))
                    diagnostics.append({'platform': offer['platform'], 'reasons': [
                        'collision_proof_http_' + match[1] if match else 'collision_proof_contract_unavailable']})
                    decisions[key] = False
        if all(decisions[offer['url'], offer['merchant'], offer['gtin13']] for offer in pair):
            return pair, diagnostics
    return [], diagnostics


def report_offer(row):
    return {key: row[key] for key in ('url', 'platform', 'merchant', 'price', 'gtin13')}


def load_reviewed_profiles(path):
    if path.stat().st_size > 12_000:
        raise ValueError('Reviewed capacity file exceeds the input limit')
    rows = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(rows, list) or not 1 <= len(rows) <= 4:
        raise ValueError('Provide one to four reviewed capacities')
    profiles = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'slug', 'gtin', 'urls'}:
            raise ValueError('Review profiles must contain only a slug, GTIN and retailer URLs')
        slug, gtin, urls = row['slug'], row['gtin'], row['urls']
        if (not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9-]{1,160}', slug) or slug in profiles or
                not isinstance(gtin, str) or retailer_gtin13(gtin) != gtin or
                not isinstance(urls, list) or not 2 <= len(urls) <= 6 or
                any(not url or retailer_destination(url) != url
                    or not supported_phone_url(url, expanded=True) for url in urls) or
                len(set(urls)) != len(urls) or
                len({urlsplit(url).netloc for url in urls}) < 2):
            raise ValueError('Invalid reviewed capacity or original retailer URLs')
        profiles[slug] = row
    return profiles


def load_iphone_16_plus_profiles(path=IPHONE_16_PLUS_PROFILES):
    profiles = load_reviewed_profiles(path)
    if {slug: row['gtin'] for slug, row in profiles.items()} != IPHONE_16_PLUS_SCOPE:
        raise ValueError('The daily iPhone 16 Plus refresh requires exactly the approved slugs and GTINs')
    return profiles


def run(state, slugs, client=None, *, publish=False, maximum=24, products=None, public_audit=False, profiles=None):
    if publish and public_audit:
        raise ValueError('Public queue verification is read-only')
    if publish and client is None:
        raise ValueError('Publication requires the authenticated catalog')
    if products is None:
        products = client.get_published_products()
    products = {product['slug']: product for product in products}
    results = []
    phone_reader = catalog_document_reader('phone')
    transport = PriorityRetailerTransport(maximum_per_host=maximum, browser_fallback=False,
                                          get=phone_reader,
                                          n11_collector=client.n11_document_get if client else None,
                                          unavailable_robots_hosts=('www.teknosa.com', 'www.pazarama.com'))
    direct = {}
    if public_audit or profiles:
        direct['www.hepsiburada.com'] = PriorityRetailerTransport(maximum_per_host=maximum,
            browser_fallback=False, get=_hepsiburada_public_get)
        direct[OZGUR_HOST] = PriorityRetailerTransport(maximum_per_host=maximum, browser_fallback=False)
        direct[AYAZ_HOST] = PriorityRetailerTransport(maximum_per_host=min(maximum, 8),
            browser_fallback=False, get=phone_reader)
    if public_audit:
        direct['www.vatanbilgisayar.com'] = PriorityRetailerTransport(maximum_per_host=maximum,
            browser_fallback=False)
        # Category search stays public. Destination pages use the same
        # retailer verifier and request budget as every other product lead.
        n11_search = PriorityRetailerTransport(maximum_per_host=min(maximum, 8), browser_fallback=False)
    documents = {}
    seller_cache = {}
    search_cache = {}
    lead_index = phone_lead_index(state, expanded=public_audit) if not profiles else None
    for slug in slugs:
        product = products[slug]
        specs = PriceUpdater.get_expected_specs(product.get('attributes'))
        profile = profiles[slug] if profiles else None
        if profile:
            urls = profile['urls'][:]
        else:
            discovered = category_leads(slug, product['name'], specs, transport, documents)
            discovered += indexed_phone_leads(lead_index, slug)
            if public_audit:
                discovered += saved_phone_leads(state, slug)
            known = published_offer_urls(slug) if public_audit else ()
            urls = list(dict.fromkeys(url for pair in zip_longest(discovered, known) for url in pair if url))
        # Keep compatible or unspecified URL hints for the public queue.
        # Reviewed publication still checks every explicitly supplied URL.
        storage = specs.get('storage_gb')
        urls.sort(key=lambda url: capacity_priority(url, storage))
        if public_audit:
            per_host = Counter()
            limited_urls = []
            for url in urls:
                host = urlsplit(url).netloc
                if per_host[host] < 4 and capacity_priority(url, storage) < 2:
                    limited_urls.append(url)
                    per_host[host] += 1
            urls = limited_urls
        pair, observations, rejects = checked_offers(
            urls, product['name'], specs, transport, documents, direct=direct, seller_cache=seller_cache,
            reviewed_gtin=profile['gtin'] if profile else None)
        searches, native_leads = {}, []
        if public_audit:
            sources = (('n11Search', n11_phone_leads, n11_search),
                       ('hepsiburadaSearch', hepsiburada_phone_leads, direct['www.hepsiburada.com']))
            for label, discover, reader in sources:
                if pair:
                    break
                extra_urls, searches[label] = discover(product['name'], specs, reader, {*urls, *native_leads}, search_cache)
                native_leads.extend(extra_urls)
                _, extra_offers, extra_rejects = checked_offers(
                    extra_urls, product['name'], specs, transport, documents, direct=direct, seller_cache=seller_cache)
                observations.extend(extra_offers)
                rejects.extend(extra_rejects)
                pair = next(iter(ranked_pairs(observations)), [])
        if profile:
            observations = [row for row in observations if row['gtin13'] == profile['gtin']]
            pair = next(iter(ranked_pairs(observations)), [])
        result = {'slug': slug, 'sameGtin': pair[0]['gtin13'] if pair else None,
                  'selectedPair': [report_offer(row) for row in pair],
                  'offers': [report_offer(row) for row in observations], 'rejections': rejects, **searches}
        if native_leads:
            result['nativeLeads'] = native_leads
        if (publish or profile) and pair:
            pair, result['sellerChecks'] = publishable_pair(observations, client, product)
            if not pair:
                result['publicationReason'] = 'existing_seller_collision_or_incomplete_proof'
                results.append(result)
                continue
            if any((datetime.now(timezone.utc) - datetime.fromisoformat(offer['observed_at'])).total_seconds() > 180
                   for offer in pair):
                raise ValueError('Verified retailer pair expired before publication')
            result['sameGtin'] = pair[0]['gtin13']
            result['selectedPair'] = [report_offer(row) for row in pair]
            if not publish:
                result['publicationReady'] = True
                results.append(result)
                continue
            records = []
            for offer in pair:
                record = PriceUpdater(ingestion_client=client).prepare_product_offers(
                    product, [offer], offer['observed_at'], offer['observed_at'])[0]
                records.append(record)
            outcome = client.ingest_offers(records, require_all_accepted=True)
            if not outcome.get('committed') or outcome.get('accepted') != len(records):
                raise RuntimeError('Verified pair was not committed atomically')
            result['published'] = outcome['accepted']
            identity = client.get_offer_identity_readiness()
            coverage = next((row for row in identity.get('productCoverage', ()) if row.get('slug') == slug), {})
            matched = {(row.get('source_url'), row.get('merchant_name'), float(row.get('price') or 0))
                       for row in coverage.get('matchedOffers', ())}
            if not all((offer['url'], offer['merchant'], offer['price']) in matched for offer in pair):
                raise RuntimeError('Public offer matching did not confirm both retailers')
            result['readback'] = 'verified'
        results.append(result)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--slug', action='append')
    mode.add_argument('--audit-queue', type=Path, metavar='REPORT')
    mode.add_argument('--reviewed-pairs', type=Path, metavar='PROFILES')
    mode.add_argument('--iphone-16-plus', action='store_true')
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    if not (args.iphone_16_plus or args.reviewed_pairs) and (not args.state or not args.state.is_file()):
        parser.error('Provide a saved discovery state')
    state = read_state(args.state)
    if args.audit_queue is not None:
        if args.publish:
            parser.error('The scheduled public audit is read-only')
        public = public_phone_catalog()
        queue = audit_queue(state, public)
        rows = run(state, [row['slug'] for row in queue], maximum=40, products=queue, public_audit=True)
        now = datetime.now(timezone.utc).isoformat()
        state.setdefault('phoneOfferAudits', {}).update({row['slug']: now for row in rows})
        for row in rows:
            if row.get('nativeLeads'):
                slug = row['slug']
                state.setdefault('phoneNativeLeads', {})[slug] = list(dict.fromkeys(
                    row['nativeLeads'] + saved_phone_leads(state, slug)))[:8]
        write_json(args.state, state)
        write_json(args.audit_queue, {'checkedAt': now, 'catalogAvailable': bool(public), 'rows': rows})
        print(json.dumps({'phonesAudited': len(rows), 'verifiedPairs': sum(bool(row['sameGtin']) for row in rows)}))
        return
    profiles = None
    if args.iphone_16_plus:
        profiles = load_iphone_16_plus_profiles()
    elif args.reviewed_pairs:
        profiles = load_reviewed_profiles(args.reviewed_pairs)
    slugs = list(profiles) if profiles else args.slug
    if not 1 <= len(slugs) <= 4 or len(set(slugs)) != len(slugs):
        parser.error('Provide one to four distinct catalog slugs')
    rows = run(state, slugs, OfferIngestionClient.from_env(), publish=args.publish, profiles=profiles)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    if args.publish and any(row.get('readback') != 'verified' for row in rows):
        raise SystemExit('One or more requested capacities have no verified publication')


if __name__ == '__main__':
    main()
