"""Publish the queued Redmi Note 17 5G only with two exact in-stock offers."""

import argparse
from datetime import datetime, timezone
import json

from bs4 import BeautifulSoup

from automatic_memory_variants import variant_selected_evidence
from daily_discovery import MAX_BODY, model_key, product_names
from laptop_discovery import Reader
from priority_retailer_transport import PriorityRetailerTransport, compatible_get
from publish_s26_fe_queue import publish_verified
from tr_price_scraper import TRPriceScraper
from trendyol_catalog_source import parse_catalog_listing, selected_product_state
from tws_offer_pilot import independent_sellers
from tws_retailer_sources import selected_photo, selected_vatan_tws
from vatan_reviewed_source import VatanReviewedSource

NAME = 'Xiaomi Redmi Note 17 5G'
SLUG = 'xiaomi-redmi-note-17-5g'
GTIN = '6939093025178'
MANUFACTURER = 'https://www.mi.com/tr/product/redmi-note-17-5g/'
SPEC_DATABASE = 'https://www.kimovil.com/en/where-to-buy-xiaomi-redmi-note-17-6gb-256gb'
VATAN = 'https://www.vatanbilgisayar.com/xiaomi-redmi-note-17-5g-6-256gb-mavi-akilli-telefon.html'
TRENDYOL = ('https://www.trendyol.com/xiaomi/'
            'redmi-note-17-5g-6gb-256gb-mavi-xiaomi-turkiye-garantili-p-1194808547')
SPECS = {'ram_gb': 6, 'storage_gb': 256, 'network_generation': '5g'}
CANDIDATE = {'name': NAME, 'slug': SLUG, 'brandSlug': 'xiaomi', 'segment': 'new_release',
             'evidenceUrl': MANUFACTURER}


def specifications():
    observed = datetime.now(timezone.utc).isoformat()
    physical = {
        'ram_gb': 6, 'storage_gb': 256, 'battery_mah': 7700, 'screen_size_inch': 6.99,
        'Design & Materials': {'Screen': '6.99" FHD+ AMOLED', 'Resolution': '1080 × 2396',
                               'Refresh rate': '120 Hz', 'Weight': '225 g'},
        'Performance & Hardware': {'Processor': 'Qualcomm Snapdragon 4 Gen 4', 'RAM': '6 GB LPDDR4X',
                                   'Storage': '256 GB UFS 2.2'},
        'Camera': {'Main camera': '50 MP', 'Selfie': '16 MP'},
        'Connectivity': {'5G': True, 'Bluetooth': '5.1'},
        'Batarya': {'Capacity': '7700 mAh', 'Fast charge': '45 W'},
        'Software': {'Operating system': 'Xiaomi HyperOS 3'},
    }
    rows = [(key, value, 'manufacturer', MANUFACTURER + 'specs/') for key, value in physical.items()]
    rows.extend([
        ('Technical sheet', {'Release date': '2026-09'}, 'spec_database', SPEC_DATABASE),
        ('partials', {'design': 7.3, 'hardware': 6.6, 'camera': 5.7, 'connectivity': 7.7, 'battery': 8.5},
         'benchmark_source', SPEC_DATABASE),
    ])
    return [{'product_slug': SLUG, 'attribute_key': key, 'value': value, 'source_url': url,
             'value_origin': origin, 'observed_at': observed, 'is_estimate': False, 'confidence': 'medium'}
            for key, value, origin, url in rows]


def prepare():
    response = compatible_get(MANUFACTURER, timeout=15, allow_redirects=False)
    if (response.status_code != 200 or len(response.content) > MAX_BODY
            or 'text/html' not in response.headers.get('Content-Type', '')
            or not any(model_key(name) == model_key(NAME) for name in product_names(response.text, MANUFACTURER))):
        raise ValueError('manufacturer_model_unverified')

    matcher = TRPriceScraper()

    def match(title, brand):
        if brand.casefold() == 'xiaomi' and matcher.is_strict_match(NAME, title, SPECS):
            return NAME
        return None

    vatan_reader = Reader('www.vatanbilgisayar.com', 2)
    html, final = vatan_reader.read(VATAN)
    reason = vatan_reader.last_reason
    if reason == 'http_403':
        reviewed = VatanReviewedSource(additional_reviewed_urls=(VATAN,))
        html, reason = reviewed.browser_get(VATAN)
        final = VATAN if html else None
    if not html or final != VATAN:
        raise ValueError(f'vatan_{reason}')
    vatan = selected_vatan_tws(html, final, NAME, match)
    if (not vatan or vatan['gtin13'] != GTIN or vatan['sku'] != '159739'
            or vatan['retailer_model_code'] != 'MZB0Q4MTR'):
        raise ValueError('vatan_selected_variant_unverified')
    gallery = BeautifulSoup(html, 'html.parser').select('.product-detail a[href*="/Upload/PRODUCT/"]')
    photos = list(dict.fromkeys(url for anchor in gallery
                  if (url := selected_photo(anchor['href'], {'cdn.vatanbilgisayar.com'}))
                  and f"/{vatan['sku']}-" in url))
    if len(photos) < 2:
        raise ValueError('vatan_selected_photo_unverified')
    photo = photos[1]  # The frontal image is a narrow thumbnail; the second shows the selected color and body.

    transport = PriorityRetailerTransport(maximum_per_host=4, browser_fallback=False, get=compatible_get)
    html, reason, _ = transport.read_document(TRENDYOL)
    if not html or transport.resolved_url(TRENDYOL) != TRENDYOL:
        raise ValueError(f'trendyol_{reason}')
    trendyol, reason = parse_catalog_listing(html, TRENDYOL, NAME, SPECS)
    product, _ = selected_product_state(BeautifulSoup(html, 'html.parser'))
    seller = (product or {}).get('merchantListing', {}).get('merchant', {})
    company = matcher._normalize_words(seller.get('officialName'))
    if (not trendyol or trendyol['merchant'] != 'Trendyol' or trendyol['gtin13'] != GTIN
            or str(trendyol['merchant_id']) != str(seller.get('id'))):
        raise ValueError(f'trendyol_selected_variant_unverified_{reason}')
    trendyol.update(platform='Trendyol', _legal_name=company)
    offers = [vatan, trendyol]
    if not independent_sellers(*offers) or any(row['availability'] != 'in_stock' or row['currency'] != 'TRY' for row in offers):
        raise ValueError('two_independent_in_stock_offers_required')
    trendyol['source_variant_evidence'] = variant_selected_evidence(trendyol, NAME, SPECS)
    if not trendyol['source_variant_evidence']:
        raise ValueError('trendyol_selected_sku_evidence_missing')
    vatan['source_variant_evidence'] = {
        'version': 'selected-retailer-sku-v1', 'platform': vatan['platform'], 'gtin13': vatan['gtin13'],
        'product_id': vatan['sku'], 'sku': vatan['sku'], 'merchant_id': 'vatan-bilgisayar',
        'merchant_name': vatan['merchant'], **SPECS,
    }
    return offers, photo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    rows, photo = prepare()
    print(json.dumps({'gtin': GTIN, 'offers': [{key: row[key] for key in ('merchant', 'price', 'url', 'observed_at')}
                     for row in rows], 'photo': photo}, ensure_ascii=False), flush=True)
    if args.publish:
        publish_verified(CANDIDATE, specifications(), rows, photo)


if __name__ == '__main__':
    main()
