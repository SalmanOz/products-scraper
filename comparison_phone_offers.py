"""Verify retailer leads for existing phones using two independent sellers of one GTIN."""

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from competitor_catalog_gaps import read_state, retailer_destination
from mediamarkt_marketplace_audit import selected_marketplace_offer
from mediamarkt_source import product_url as mediamarkt_url
from offer_ingestion_client import OfferIngestionClient
from pazarama_category_source import pazarama_product_url
from priority_retailer_transport import PriorityRetailerTransport
from retailer_offer_refresh import _product_offer, _schema_nodes
from tr_price_scraper import TRPriceScraper
from tws_offer_pilot import independent_sellers
from update_prices import PriceUpdater
from retailer_barcode import retailer_gtin13


SUPPORTED = ('www.mediamarkt.com.tr', 'www.pazarama.com')
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


def seller_identity(html, offer):
    soup = BeautifulSoup(html, 'html.parser')
    headers = soup.select('h1')
    if len(headers) != 1 or NORMALIZE(headers[0].get_text(' ', strip=True)) != NORMALIZE(offer['merchant']):
        return None
    if offer['platform'] == 'MediaMarkt':
        text = ' '.join(soup.get_text(' ', strip=True).split())
        match = re.search(r'Firma adı:\s*(.{3,160}?)\s+Şehir:.*?MERSİS No:\s*([0-9]{15,16})\b', text)
        return (NORMALIZE(match[1]), match[2].zfill(16)) if match else None
    state = native_script(soup)
    matches = re.findall(r'sellerMersisNumber:"(\d{16})"[^{}]{0,1400}?'
                         r'sellerTradeName:("(?:[^"\\]|\\.)+")', state)
    if len(matches) != 1:
        return None
    mersis, name = matches[0]
    return NORMALIZE(json.loads(name)), mersis


def checked_offers(urls, name, specs, transport, documents=None):
    observed, rejects = [], []
    documents = {} if documents is None else documents
    matcher = TRPriceScraper()
    for url in urls:
        host = urlsplit(url).netloc
        reader = mediamarkt_selected if mediamarkt_url(url) else pazarama_selected if pazarama_product_url(url) else None
        if not reader:
            continue
        if url not in documents:
            documents[url] = transport._read_document(url, host)
        html, reason, _ = documents[url]
        offer = reader(html, url) if html else None
        if not offer or not matcher.has_observed_offer_evidence(name, offer, specs):
            rejects.append({'url': url, 'reason': 'selected_offer_unverified' if html else reason})
            continue
        seller_url = offer['seller_url']
        if seller_url not in documents:
            documents[seller_url] = transport._read_document(seller_url, host)
        seller_document, _, _ = documents[seller_url]
        identity = seller_identity(seller_document, offer) if seller_document else None
        if not identity or not identity[0]:
            rejects.append({'url': url, 'reason': 'seller_identity_unverified'})
            continue
        observed.append({**offer, '_legal_name': identity[0], '_tax_key': identity[1],
                         'observed_at': datetime.now(timezone.utc).isoformat()})
    groups = defaultdict(list)
    for offer in sorted(observed, key=lambda row: (row['price'], row['url'])):
        groups[offer['gtin13']].append(offer)
    pairs = [([a, b]) for group in groups.values() for i, a in enumerate(group) for b in group[i + 1:]
             if a['platform'] != b['platform'] and independent_sellers(a, b) and a['price'] >= b['price'] * .60]
    return min(pairs, key=lambda pair: (pair[0]['price'], pair[1]['price']), default=[]), observed, rejects


def candidate_urls(state, slug):
    variants = []
    family = re.sub(r'-[1-9]\d?gb-[1-9]\d{1,3}gb$', '', slug)
    for row in state['products'].values():
        if row.get('category') != 'phone' or family != row.get('catalogFamily') and slug != row.get('catalogSlug'):
            continue
        variants.extend(row.get('retailerLeads', ()))
    return list(dict.fromkeys(url for raw in variants if (url := retailer_destination(raw))
                              and urlsplit(url).netloc in SUPPORTED))


def run(state, slugs, client, *, publish=False, maximum=12):
    products = {product['slug']: product for product in client.get_published_products()}
    results = []
    transport = PriorityRetailerTransport(maximum_per_host=maximum, browser_fallback=False)
    documents = {}
    for slug in slugs:
        product = products[slug]
        specs = PriceUpdater.get_expected_specs(product.get('attributes'))
        urls = candidate_urls(state, slug)
        pair, observations, rejects = checked_offers(urls, product['name'], specs, transport, documents)
        result = {'slug': slug, 'sameGtin': pair[0]['gtin13'] if pair else None,
                  'offers': [{key: row[key] for key in ('url', 'platform', 'merchant', 'price', 'gtin13')}
                             for row in observations], 'rejections': rejects}
        if publish and pair:
            proof = [client.get_raw_seller_collision_proof(product['id'], offer['merchant'], offer['url'])
                     for offer in pair]
            if not all(row.get('complete') is True and row.get('blocked') is False for row in proof):
                result['publicationReason'] = 'existing_seller_collision_or_incomplete_proof'
                results.append(result)
                continue
            if any((datetime.now(timezone.utc) - datetime.fromisoformat(offer['observed_at'])).total_seconds() > 180
                   for offer in pair):
                raise ValueError('Verified retailer pair expired before publication')
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
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--slug', action='append', required=True)
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    if not args.state.is_file() or not 1 <= len(args.slug) <= 4 or len(set(args.slug)) != len(args.slug):
        parser.error('Provide a discovery state and one to four distinct catalog slugs')
    print(json.dumps(run(read_state(args.state), args.slug, OfferIngestionClient.from_env(),
                         publish=args.publish), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
