"""Verify and publish the selected Redmi Note 17 Pro 5G 8/256 Turkish SKU."""

import argparse
from datetime import datetime, timezone
import json

from bs4 import BeautifulSoup

from automatic_memory_variants import variant_selected_evidence
from daily_discovery import MAX_BODY, model_key, product_names
from mediamarkt_source import _selected_offer
from priority_retailer_transport import PriorityRetailerTransport, compatible_get
from publish_s26_fe_queue import publish_verified, read_mediamarkt_exact
from tr_price_scraper import TRPriceScraper
from trendyol_catalog_source import parse_catalog_listing, selected_product_state
from tws_offer_pilot import independent_sellers
from tws_retailer_sources import selected_photo

NAME = 'Xiaomi Redmi Note 17 Pro 5G'
SLUG = 'xiaomi-redmi-note-17-pro-5g'
GTIN = '6939093009734'
SPECS = {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'}
MANUFACTURER = 'https://www.mi.com/tr/product/redmi-note-17-pro-5g/'
SPEC_DATABASE = 'https://www.kimovil.com/en/where-to-buy-xiaomi-redmi-note-17-pro'
MEDIAMARKT = ('https://www.mediamarkt.com.tr/tr/product/'
              '_xiaomi-note-17-pro-5g-8256g-akilli-telefon-gok-mavisi-1256448.html')
TRENDYOL = ('https://www.trendyol.com/xiaomi/'
            'redmi-note-17-pro-5g-8gb-256gb-mavi-xiaomi-turkiye-garantili-p-1194808538')
CANDIDATE = {'name': NAME, 'slug': SLUG, 'brandSlug': 'xiaomi', 'segment': 'new_release',
             'evidenceUrl': MANUFACTURER}


def specifications():
    observed = datetime.now(timezone.utc).isoformat()
    physical = {
        'ram_gb': 8, 'storage_gb': 256, 'battery_mah': 8340, 'screen_size_inch': 6.83,
        'Design & Materials': {'Screen': '6.83" AMOLED', 'Resolution': '1280 × 2772', 'Refresh rate': '120 Hz'},
        'Performance & Hardware': {'Processor': 'Qualcomm Snapdragon 6s Gen 4', 'RAM': '8 GB LPDDR4X',
                                   'Storage': '256 GB UFS 3.1'},
        'Camera': {'Main camera': '50 MP OIS', 'Wide angle': '8 MP', 'Selfie': '16 MP'},
        'Connectivity': {'5G': True},
        'Batarya': {'Capacity': '8340 mAh', 'Fast charge': '67 W'},
        'Software': {'Operating system': 'Xiaomi HyperOS 3'},
    }
    sources = [(key, value, 'manufacturer', MANUFACTURER + 'specs/') for key, value in physical.items()]
    sources.extend([
        ('Technical sheet', {'Release date': '2026-09'}, 'spec_database', SPEC_DATABASE),
        ('partials', {'design': 9.0, 'hardware': 7.4, 'camera': 6.6, 'connectivity': 7.8, 'battery': 8.6},
         'benchmark_source', SPEC_DATABASE),
    ])
    return [{'product_slug': SLUG, 'attribute_key': key, 'value': value, 'value_origin': origin,
             'source_url': url, 'observed_at': observed, 'confidence': 'medium', 'is_estimate': False}
            for key, value, origin, url in sources]


def selected_mediamarkt_phone_offer(url, sku, gtin, specs, expected_title, *, expected_brand="xiaomi"):
    def verify(product, title, brand):
        if (str(product.get('sku')) != sku or product.get('gtin13') != gtin or brand.casefold() != expected_brand
                or TRPriceScraper._normalize_words(title) != expected_title):
            return None
        images = product.get('image') or []
        photo = next((photo for url in images if (photo := selected_photo(url, {'assets.mmsrg.com'}))), None)
        if photo is None:
            return None
        merchant = 'MediaMarkt'
        proof = {'version': 'selected-retailer-sku-v1', 'platform': merchant, 'merchant_id': 'mediamarkt',
                 'merchant_name': merchant, 'product_id': sku, 'sku': sku, 'gtin13': gtin, **specs}
        return {'gtin13': gtin, 'platform': merchant, '_legal_name': 'media markt turkey ticaret limited sirketi',
                'source_variant_evidence': proof, 'photo': photo}

    first = _selected_offer(read_mediamarkt_exact(url), url, verify_product=verify)
    if not first or first['availability'] != 'in_stock' or first['currency'] != 'TRY':
        raise ValueError('mediamarkt_exact_in_stock_offer_unverified')

    return first


def prepare():
    official = compatible_get(MANUFACTURER, timeout=15, allow_redirects=False)
    if (official.status_code != 200 or len(official.content) > MAX_BODY
            or 'text/html' not in official.headers.get('Content-Type', '')
            or not any(model_key(name) == model_key(NAME) for name in product_names(official.text, MANUFACTURER))):
        raise ValueError('manufacturer_model_unverified')

    first = selected_mediamarkt_phone_offer(MEDIAMARKT, '1256448', GTIN, SPECS,
                                  'xiaomi note 17 pro 5g 8 256g akilli telefon gok mavisi')

    transport = PriorityRetailerTransport(maximum_per_host=4, browser_fallback=False, get=compatible_get)
    html, reason, _ = transport.read_document(TRENDYOL)
    if not html or transport.resolved_url(TRENDYOL) != TRENDYOL:
        raise ValueError(f'trendyol_{reason}')
    second, reason = parse_catalog_listing(html, TRENDYOL, NAME, SPECS)
    product, _ = selected_product_state(BeautifulSoup(html, 'html.parser'))
    seller = (product or {}).get('merchantListing', {}).get('merchant', {})
    if (not second or second['merchant'] != 'Trendyol' or second['gtin13'] != GTIN
            or second['availability'] != 'in_stock' or second['currency'] != 'TRY'
            or str(second['merchant_id']) != str(seller.get('id'))):
        raise ValueError(f'trendyol_exact_offer_unverified_{reason}')
    second['_legal_name'] = TRPriceScraper._normalize_words(seller.get('officialName'))
    second['source_variant_evidence'] = variant_selected_evidence(second, NAME, SPECS)
    if not second['source_variant_evidence'] or not independent_sellers(first, second):
        raise ValueError('two_independent_in_stock_offers_required')
    return [first, second], first['photo']


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
