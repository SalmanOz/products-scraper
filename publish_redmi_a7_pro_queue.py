"""Verify and publish the Turkish Redmi A7 Pro 4/128 GB black SKU."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json

from bs4 import BeautifulSoup

from automatic_memory_variants import variant_selected_evidence
from daily_discovery import MAX_BODY, model_key, product_names
from laptop_discovery import Reader
from priority_retailer_transport import PriorityRetailerTransport, compatible_get
from publish_s26_fe_queue import publish_verified
from retailer_offer_refresh import _schema_nodes
from tr_price_scraper import TRPriceScraper
from trendyol_catalog_source import parse_catalog_listing, selected_product_state
from tws_offer_pilot import independent_sellers
from tws_retailer_sources import selected_photo, selected_vatan_tws
from vatan_reviewed_source import VatanReviewedSource

NAME = 'Xiaomi Redmi A7 Pro'
SLUG = 'xiaomi-redmi-a7-pro'
GTIN = '6932554497132'
SPECS = {'ram_gb': 4, 'storage_gb': 128, 'network_generation': '4g'}
MANUFACTURER = 'https://www.mi.com/tr/product/redmi-a7-pro/'
BENCHMARK = 'https://www.kimovil.com/en/where-to-buy-xiaomi-redmi-a7-pro-international-4gb-128gb'
VATAN = 'https://www.vatanbilgisayar.com/xiaomi-redmi-a7-pro-4-128gb-akilli-telefon.html'
TRENDYOL = 'https://www.trendyol.com/xiaomi/redmi-a7-pro-4gb-128gb-siyah-xiaomi-turkiye-garantili-p-1175413438'
CANDIDATE = {'name': NAME, 'slug': SLUG, 'brandSlug': 'xiaomi', 'segment': 'new_release',
             'evidenceUrl': MANUFACTURER}


def read_spec_page(url):
    with closing(compatible_get(url, timeout=15, allow_redirects=False, stream=True)) as response:
        if response.status_code != 200 or 'text/html' not in response.headers.get('content-type', ''):
            raise ValueError(f'specification_page_http_{response.status_code}')
        body = bytearray()
        for chunk in response.iter_content(65536):
            if len(body) + len(chunk) > MAX_BODY:
                raise ValueError('specification_page_too_large')
            body.extend(chunk)
    return body.decode('utf-8', errors='replace')


def specifications():
    official = BeautifulSoup(read_spec_page(MANUFACTURER + 'specs/'), 'html.parser').get_text(' ', strip=True)
    if not all(word in official
               for word in ('T7250', '4+128 GB', '6000 mAh', '4G: LTE')):
        raise ValueError('manufacturer_specifications_changed')
    benchmark = BeautifulSoup(read_spec_page(BENCHMARK), 'html.parser')
    meta = benchmark.select_one('meta[name=deviceki][content]')
    scores = json.loads(meta['content']) if meta is not None else {}
    if model_key(scores.get('name', '')) != model_key(NAME) or 'T7250' not in benchmark.get_text(' ', strip=True):
        raise ValueError('benchmark_variant_changed')
    partials = scores['partials']
    partials['connectivity'] = partials.pop('conectivity')
    if set(partials) != {'design', 'hardware', 'camera', 'connectivity', 'battery'}:
        raise ValueError('benchmark_scores_unavailable')
    facts = {
        'ram_gb': 4, 'storage_gb': 128, 'battery_mah': 6000, 'screen_size_inch': 6.9,
        'Design & Materials': {'Screen': '6.9" LCD', 'Resolution': '720 × 1600', 'Refresh rate': '120 Hz', 'Weight': '208 g'},
        'Performance & Hardware': {'Processor': 'Unisoc T7250', 'RAM': '4 GB LPDDR4X', 'Storage': '128 GB UFS 2.2'},
        'Camera': {'Main camera': '13 MP', 'Selfie': '8 MP'},
        'Connectivity': {'5G': False, '4G': 'LTE FDD / TDD', 'Bluetooth': '5.2'},
        'Batarya': {'Capacity': '6000 mAh', 'Fast charge': '15 W'},
        'Software': {'Operating system': 'Xiaomi HyperOS 3'},
    }
    records = [(key, value, 'manufacturer', MANUFACTURER + 'specs/') for key, value in facts.items()]
    records.append(('partials', partials, 'benchmark_source', BENCHMARK))
    now = datetime.now(timezone.utc).isoformat()
    return [{'product_slug': SLUG, 'attribute_key': key, 'value': value, 'value_origin': origin,
             'source_url': url, 'observed_at': now, 'confidence': 'medium', 'is_estimate': False}
            for key, value, origin, url in records]


def prepare():
    official = read_spec_page(MANUFACTURER)
    if not any(model_key(name) == model_key(NAME) for name in product_names(official, MANUFACTURER)):
        raise ValueError('manufacturer_model_unverified')

    reader = Reader('www.vatanbilgisayar.com', 2)
    html, final = reader.read(VATAN)
    if reader.last_reason == 'http_403':
        html, _ = VatanReviewedSource(additional_reviewed_urls=(VATAN,)).browser_get(VATAN)
        final = VATAN if html else None

    def match(title, brand):
        observed = TRPriceScraper._normalize_words(title)
        expected = 'xiaomi redmi a7 pro 4 128gb akilli telefon siyah redmi a7 pro siyah'
        return NAME if brand.casefold() == 'xiaomi' and observed == expected else None

    vatan = selected_vatan_tws(html, final, NAME, match) if html and final == VATAN else None
    if not vatan or vatan['gtin13'] != GTIN or vatan['sku'] != '159478':
        raise ValueError('vatan_selected_variant_unverified')
    vatan['source_variant_evidence'] = {
        'version': 'selected-retailer-sku-v1', 'platform': vatan['platform'], 'gtin13': GTIN,
        'product_id': vatan['sku'], 'sku': vatan['sku'], 'merchant_id': 'vatan-bilgisayar',
        'merchant_name': vatan['merchant'], **SPECS,
    }

    transport = PriorityRetailerTransport(maximum_per_host=3, browser_fallback=False, get=compatible_get)
    html, reason, _ = transport.read_document(TRENDYOL)
    if not html or transport.resolved_url(TRENDYOL) != TRENDYOL:
        raise ValueError('trendyol_' + reason)
    offer, reason = parse_catalog_listing(html, TRENDYOL, NAME, SPECS)
    soup = BeautifulSoup(html, 'html.parser')
    product, _ = selected_product_state(soup)
    seller = (product or {}).get('merchantListing', {}).get('merchant', {})
    if (not offer or offer['merchant'] != 'Trendyol' or offer['gtin13'] != GTIN
            or offer['merchant_id'] != seller.get('id') or offer['availability'] != 'in_stock'):
        raise ValueError('trendyol_selected_variant_unverified_' + reason)
    offer['_legal_name'] = TRPriceScraper._normalize_words(seller.get('officialName'))
    offer['source_variant_evidence'] = variant_selected_evidence(offer, NAME, SPECS)
    if not offer['source_variant_evidence'] or not independent_sellers(vatan, offer):
        raise ValueError('two_independent_in_stock_offers_required')
    products = [p for p in _schema_nodes(soup, TRENDYOL) if p.get('@type') in ('Product', 'ProductGroup')
                and str(p.get('sku')) == str(offer['product_id'])]
    image = products[0].get('image') if len(products) == 1 else None
    candidates = image.get('contentUrl') if isinstance(image, dict) else image
    photo = next((url for raw in candidates if (url := selected_photo(raw, {'cdn.dsmcdn.com'}))), None) if isinstance(candidates, list) else None
    if not photo:
        raise ValueError('selected_photo_unavailable')
    return [vatan, offer], photo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--validate', action='store_true')
    mode.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    rows, photo = prepare()
    sources = specifications()
    print(json.dumps({'gtin': GTIN, 'photo': photo,
                      'offers': [{k: row[k] for k in ('merchant', 'price', 'url', 'observed_at')} for row in rows]},
                     ensure_ascii=False), flush=True)
    if args.publish or args.validate:
        print(json.dumps(publish_verified(CANDIDATE, sources, rows, photo, publish=args.publish)), flush=True)


if __name__ == '__main__':
    main()
