"""Verify retailer leads for existing phones using two independent sellers of one GTIN."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from catalog_source_collector import catalog_document_reader

from competitor_catalog_gaps import public_phone_catalog, read_state, retailer_destination, write_json
from hepsiburada_catalog_source import parse_catalog_buy_box
from laptop_pilot import _hepsiburada_public_get, verify_seller
from laptop_retailer_sources import marketplace_seller_identity
from mediamarkt_marketplace_audit import selected_marketplace_offer
from mediamarkt_source import product_url as mediamarkt_url
from n11_catalog_source import parse_catalog_listing as n11_listing
from offer_ingestion_client import IngestionClientError, OfferIngestionClient
from pazarama_category_source import pazarama_product_url
from priority_retailer_transport import PriorityRetailerTransport, product_host
from retailer_offer_refresh import _product_offer, _schema_nodes
from teknosa_marketplace_source import parse_selected_marketplace
from tr_price_scraper import TRPriceScraper
from trendyol_catalog_source import parse_catalog_listing as trendyol_listing, selected_product_state
from tws_offer_pilot import independent_sellers
from tws_retailer_sources import selected_vatan_tws
from update_prices import PriceUpdater
from retailer_barcode import retailer_gtin13


RETAILER_CATEGORIES = {'apple-iphone-16-plus': (
    'https://www.mediamarkt.com.tr/tr/category/iphone-16-plus-903529.html',
)}
MM_SELLERS = 'https://www.mediamarkt.com.tr/tr/marketplace/seller/'
NORMALIZE = TRPriceScraper._normalize_words


def native_script(soup):
    scripts = [node.get_text() for node in soup.select('script')
               if node.get_text().startswith('window.__NUXT__=(function(')]
    return scripts[0] if len(scripts) == 1 else ''


def pazarama_selected(html, url):
    if not pazarama_product_url(url):
        return None
    soup = BeautifulSoup(html, 'html.parser')
    cash = _product_offer(soup, url)
    products = [p for p in _schema_nodes(soup, url) if p.get('@type') == 'Product' and p.get('url') == url]
    if not cash or len(products) != 1 or cash['availability'] != 'in_stock':
        return None
    product = products[0]
    seller = product['offers'].get('seller')
    listing_id = str(product.get('sku', ''))
    if not isinstance(seller, str) or not re.fullmatch(r'[0-9a-f-]{36}', listing_id):
        return None
    document = native_script(soup)
    selected = document.split('UI_PRODUCT_DETAIL:{', 1)
    if len(selected) != 2:
        return None
    detail = selected[1][:30000]
    identities = re.match(r'name:\w+,displayName:\w+,sellerName:("(?:[^"\\]|\\.)+"),'
                          r'sellerSeoName:"([a-z0-9-]+)",[^{}]{0,1000}?'
                          r'\bcode:"(\d{12,13})",stockCount:', detail)
    if not identities or NORMALIZE(json.loads(identities[1])) != NORMALIZE(seller):
        return None
    gtin = retailer_gtin13(identities[3])
    if not gtin or not url.endswith('-p-' + identities[3]):
        return None
    if re.search(r'\bproductSellerId:"' + re.escape(listing_id) + r'"', detail) is None:
        return None
    seller_path = '/magaza/' + identities[2]
    anchors = soup.select('.product-detail a[href]')
    if not any(a['href'] == seller_path and
               NORMALIZE(a.get_text(' ', strip=True)).startswith('satici ' + NORMALIZE(seller))
               for a in anchors):
        return None
    return {**cash, 'merchant': seller, 'platform': 'Pazarama', 'gtin13': gtin,
            'merchant_id': identities[2], 'seller_url': 'https://www.pazarama.com' + seller_path}


def mediamarkt_selected(html, url):
    for condition_card in (False, True):
        offer = selected_marketplace_offer(html, url, allow_refurbished_alternatives=True,
            allow_cents=True, allow_delivery_card=condition_card, require_gtin_variant=True)
        if offer:
            return {**offer, 'merchant': offer['seller_name'], 'merchant_id': offer['seller_id'],
                    'seller_url': MM_SELLERS + offer['seller_id']}
    return None


def teknosa_selected(html, url, name, specs):
    offer, _ = parse_selected_marketplace(html, url, {'name': name, 'specs': specs})
    if not offer:
        return None
    soup = BeautifulSoup(html, 'html.parser')
    links = soup.select('.pdp-seller-info > a[href]')
    if len(links) != 1 or not re.fullmatch(r'/magaza/[a-z0-9-]+', links[0]['href']):
        return None
    return {**offer, 'seller_url': urljoin(url, links[0]['href'])}


def trendyol_selected(html, url, name, specs):
    offer, reason = trendyol_listing(html, url, name, specs)
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


def checked_offers(urls, name, specs, transport, documents=None, *, direct=None, seller_cache=None):
    observed, rejects = [], []
    documents = {} if documents is None else documents
    direct = direct or {}
    seller_cache = {} if seller_cache is None else seller_cache
    matcher = TRPriceScraper()
    for url in urls:
        host = urlsplit(url).netloc
        reader = direct.get(host, transport)
        if url not in documents:
            documents[url] = reader._read_document(url, host)
        html, reason, _ = documents[url]
        offer = None
        rejected = 'selected_offer_unverified'
        if html:
            if mediamarkt_url(url):
                offer = mediamarkt_selected(html, url)
            elif pazarama_product_url(url):
                offer = pazarama_selected(html, url)
            elif host == 'www.teknosa.com':
                offer = teknosa_selected(html, url, name, specs)
            elif host == 'www.trendyol.com':
                resolved = transport.resolved_url(url)
                offer, rejected = trendyol_selected(html, resolved, name, specs)
            elif host == 'www.n11.com':
                resolved = transport.resolved_url(url)
                offer, rejected = n11_listing(html, resolved, name, specs, include_legal_identity=True)
            elif host == 'www.hepsiburada.com' and host in direct:
                offer, rejected = parse_catalog_buy_box(html, url, name, specs)
                if offer and not verify_seller(offer, None, reader, seller_cache):
                    offer, rejected = None, 'seller_identity_unverified'
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
    if route.netloc in ('www.n11.com', 'www.trendyol.com'):
        return product_host(url) == route.netloc
    if expanded and route.netloc == 'www.hepsiburada.com':
        return product_host(url) == route.netloc and 'yenilenmis' not in route.path
    if route.query:
        return False
    if route.netloc == 'www.mediamarkt.com.tr':
        return mediamarkt_url(url) and 'akilli-telefon' in route.path
    if not any(token in route.path for token in ('iphone', 'akilli-telefon', 'cep-telefonu')):
        return False
    if expanded and route.netloc == 'www.vatanbilgisayar.com':
        return vatan_phone_url(url)
    if route.netloc == 'www.teknosa.com':
        return product_host(url) == route.netloc
    return pazarama_product_url(url) and bool(re.fullmatch(r'/[a-z0-9-]+-p-\d{12,13}', route.path))


def candidate_urls(state, slug, *, expanded=False):
    variants = []
    family = product_family(slug)
    for row in state['products'].values():
        if row.get('category') != 'phone' or family != row.get('catalogFamily') and slug != row.get('catalogSlug'):
            continue
        variants.extend(row.get('retailerLeads', ()))
    return list(dict.fromkeys(url for raw in variants if (url := retailer_destination(raw))
                              and supported_phone_url(url, expanded=expanded)))


def audit_queue(state, products, maximum=8):
    audited = state.get('phoneOfferAudits', {})
    candidates = []
    by_family = defaultdict(set)
    for row in state['products'].values():
        if row.get('category') != 'phone' or not row.get('catalogFamily'):
            continue
        for raw in row.get('retailerLeads', ()):
            if url := retailer_destination(raw):
                if supported_phone_url(url, expanded=True):
                    by_family[row['catalogFamily']].add(urlsplit(url).netloc)
    for product in products:
        slug = product['slug']
        if not product.get('name') or not isinstance(product.get('attributes'), dict):
            continue
        family = product_family(slug)
        if len(by_family[family]) >= 2:
            candidates.append(product)
    return sorted(candidates, key=lambda row: (
        audited.get(row['slug'], ''), row.get('fresh_offer_count') not in (0, '0'), row['slug']))[:maximum]


def publishable_pair(observations, client, product):
    decisions, diagnostics = {}, []
    for pair in ranked_pairs(observations):
        for offer in pair:
            key = offer['url'], offer['merchant']
            if key not in decisions:
                try:
                    proof = client.get_raw_seller_collision_proof(product['id'], offer['merchant'], offer['url'])
                    other = proof.get('proposed_seller_other_url_rows')
                    historical = proof.get('proposed_seller_historical_other_url_rows')
                    expired_conflicts = (proof.get('reasons') == ['proposed_seller_on_other_url_or_platform']
                                         and type(historical) is int and historical > 0 and other == historical)
                    decisions[key] = proof.get('complete') is True and (proof.get('blocked') is False or expired_conflicts)
                    diagnostics.append({'platform': offer['platform'], 'reasons': proof.get('reasons'),
                                        'otherUrls': other, 'expiredOtherUrls': historical})
                except IngestionClientError as error:
                    match = re.match(r'HTTP ([45]\d{2})\b', str(error))
                    diagnostics.append({'platform': offer['platform'], 'reasons': [
                        'collision_proof_http_' + match[1] if match else 'collision_proof_contract_unavailable']})
                    decisions[key] = False
        if all(decisions[offer['url'], offer['merchant']] for offer in pair):
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
                    or not (supported_phone_url(url) or vatan_phone_url(url)) for url in urls) or
                len(set(urls)) != len(urls) or
                len({urlsplit(url).netloc for url in urls}) < 2):
            raise ValueError('Invalid reviewed capacity or original retailer URLs')
        profiles[slug] = row
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
    transport = PriorityRetailerTransport(maximum_per_host=maximum, browser_fallback=False,
                                          get=catalog_document_reader('phone'),
                                          n11_collector=client.n11_document_get if client else None,
                                          unavailable_robots_hosts=('www.teknosa.com', 'www.pazarama.com'))
    direct = {}
    if public_audit:
        direct = {
            'www.hepsiburada.com': PriorityRetailerTransport(maximum_per_host=maximum, browser_fallback=False,
                                                            get=_hepsiburada_public_get),
            'www.vatanbilgisayar.com': PriorityRetailerTransport(maximum_per_host=maximum, browser_fallback=False),
        }
    documents = {}
    seller_cache = {}
    for slug in slugs:
        product = products[slug]
        specs = PriceUpdater.get_expected_specs(product.get('attributes'))
        profile = profiles[slug] if profiles else None
        urls = list(dict.fromkeys(profile['urls'] if profile else
            category_leads(slug, product['name'], specs, transport, documents)
            + candidate_urls(state, slug, expanded=public_audit)))
        # Spend each store's budget on the capacity being audited first.
        capacity = re.compile(r'-' + str(specs.get('storage_gb')) + '-?gb(?:-|_)')
        urls.sort(key=lambda url: not bool(capacity.search(urlsplit(url).path)))
        if public_audit:
            per_host = Counter()
            limited_urls = []
            for url in urls:
                host = urlsplit(url).netloc
                if per_host[host] < 4:
                    limited_urls.append(url)
                    per_host[host] += 1
            urls = limited_urls
        pair, observations, rejects = checked_offers(urls, product['name'], specs, transport, documents,
                                                    direct=direct, seller_cache=seller_cache)
        if profile:
            observations = [row for row in observations if row['gtin13'] == profile['gtin']]
            pair = next(iter(ranked_pairs(observations)), [])
        result = {'slug': slug, 'sameGtin': pair[0]['gtin13'] if pair else None,
                  'selectedPair': [report_offer(row) for row in pair],
                  'offers': [report_offer(row) for row in observations], 'rejections': rejects}
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
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    if args.reviewed_pairs is None and (not args.state or not args.state.is_file()):
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
        write_json(args.state, state)
        write_json(args.audit_queue, {'checkedAt': now, 'catalogAvailable': bool(public), 'rows': rows})
        print(json.dumps({'phonesAudited': len(rows), 'verifiedPairs': sum(bool(row['sameGtin']) for row in rows)}))
        return
    profiles = load_reviewed_profiles(args.reviewed_pairs) if args.reviewed_pairs else None
    slugs = list(profiles) if profiles else args.slug
    if not 1 <= len(slugs) <= 4 or len(set(slugs)) != len(slugs):
        parser.error('Provide one to four distinct catalog slugs')
    rows = run(state, slugs, OfferIngestionClient.from_env(), publish=args.publish, profiles=profiles)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    if args.publish and any(row.get('readback') != 'verified' for row in rows):
        raise SystemExit('One or more requested capacities have no verified publication')


if __name__ == '__main__':
    main()
