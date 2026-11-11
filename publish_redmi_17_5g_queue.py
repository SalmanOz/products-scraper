"""Verify the selected Redmi 17 5G 4/256 before daily discovery publishes it."""

import argparse
from datetime import datetime, timezone
import json

from daily_discovery import MAX_BODY, model_key, product_names
from laptop_discovery import Reader
from priority_retailer_transport import compatible_get
from publish_redmi_note_17_pro_queue import selected_mediamarkt_phone_offer
from publish_s26_fe_queue import publish_verified
from tr_price_scraper import TRPriceScraper
from tws_offer_pilot import independent_sellers
from tws_retailer_sources import selected_vatan_tws
from vatan_reviewed_source import VatanReviewedSource

NAME = 'Xiaomi Redmi 17 5G'
SLUG = 'xiaomi-redmi-17-5g'
GTIN = '6939093012628'
SPECS = {'ram_gb': 4, 'storage_gb': 256, 'network_generation': '5g'}
MANUFACTURER = 'https://www.mi.com/tr/product/redmi-17-5g/'
SPEC_DATABASE = 'https://www.kimovil.com/en/where-to-buy-xiaomi-redmi-17-5ginternational-4gb-256gb'
MEDIAMARKT = ('https://www.mediamarkt.com.tr/tr/product/'
              '_xiaomi-redmi-17-5g-4256gb-akilli-telefon-siyah-1257671.html')
VATAN = 'https://www.vatanbilgisayar.com/xiaomi-redmi-17-5g-akilli-telefon.html'
CANDIDATE = {'name': NAME, 'slug': SLUG, 'brandSlug': 'xiaomi', 'segment': 'new_release',
             'evidenceUrl': MANUFACTURER}


def specifications():
    observed = datetime.now(timezone.utc).isoformat()
    physical = {
        'ram_gb': 4, 'storage_gb': 256, 'battery_mah': 7500, 'screen_size_inch': 6.9,
        'Design & Materials': {'Screen': '6.9" LCD', 'Resolution': '720 × 1600',
                               'Refresh rate': '120 Hz', 'Weight': '232 g'},
        'Performance & Hardware': {'Processor': 'Qualcomm Snapdragon 4 Gen 5', 'RAM': '4 GB LPDDR4X',
                                   'Storage': '256 GB UFS 2.2'},
        'Camera': {'Main camera': '50 MP', 'Selfie': '8 MP'},
        'Connectivity': {'5G': True, 'Bluetooth': '5.1', 'NFC': True},
        'Batarya': {'Capacity': '7500 mAh', 'Fast charge': '45 W'},
        'Software': {'Operating system': 'Xiaomi HyperOS 3'},
    }
    records = [(key, value, 'manufacturer', MANUFACTURER + 'specs/') for key, value in physical.items()]
    records += [
        ('Technical sheet', {'Release date': '2026-08'}, 'spec_database', SPEC_DATABASE),
        ('partials', {'design': 6.3, 'hardware': 6.6, 'camera': 5.8, 'connectivity': 7.4, 'battery': 8.4},
         'benchmark_source', SPEC_DATABASE),
    ]
    return [{'product_slug': SLUG, 'attribute_key': key, 'value': value, 'value_origin': origin,
             'source_url': url, 'observed_at': observed, 'confidence': 'medium', 'is_estimate': False}
            for key, value, origin, url in records]


def prepare():
    official = compatible_get(MANUFACTURER, timeout=15, allow_redirects=False)
    if (official.status_code != 200 or len(official.content) > MAX_BODY
            or 'text/html' not in official.headers.get('Content-Type', '')
            or not any(model_key(name) == model_key(NAME) for name in product_names(official.text, MANUFACTURER))):
        raise ValueError('manufacturer_model_unverified')

    reader = Reader('www.vatanbilgisayar.com', 2)
    html, final = reader.read(VATAN)
    if reader.last_reason == 'http_403':
        html, _ = VatanReviewedSource(additional_reviewed_urls=(VATAN,)).browser_get(VATAN)
        final = VATAN if html else None
    if not html or final != VATAN:
        raise ValueError('vatan_selected_page_unavailable')

    def match(title, brand):
        words = TRPriceScraper._normalize_words(title)
        expected = 'xiaomi redmi 17 5g 4 256gb siyah akilli telefon mzb0pcptr'
        return NAME if brand.casefold() == 'xiaomi' and words == expected else None

    vatan = selected_vatan_tws(html, final, NAME, match)
    if (not vatan or vatan['gtin13'] != GTIN or vatan['sku'] != '159733'
            or vatan['retailer_model_code'] != 'MZB0PCPTR'):
        raise ValueError('vatan_selected_black_variant_unverified')
    vatan['source_variant_evidence'] = {
        'version': 'selected-retailer-sku-v1', 'platform': vatan['platform'], 'gtin13': GTIN,
        'product_id': vatan['sku'], 'sku': vatan['sku'], 'merchant_id': 'vatan-bilgisayar',
        'merchant_name': vatan['merchant'], **SPECS,
    }

    media = selected_mediamarkt_phone_offer(MEDIAMARKT, '1257671', GTIN, SPECS,
                                  'xiaomi redmi 17 5g 4 256gb akilli telefon siyah')
    if not independent_sellers(vatan, media):
        raise ValueError('two_independent_in_stock_offers_required')
    return [vatan, media], media['photo']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--validate', action='store_true')
    mode.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    rows, photo = prepare()
    print(json.dumps({'gtin': GTIN, 'photo': photo,
                      'offers': [{k: row[k] for k in ('merchant', 'price', 'url', 'observed_at')} for row in rows]},
                     ensure_ascii=False), flush=True)
    if args.publish or args.validate:
        print(json.dumps(publish_verified(CANDIDATE, specifications(), rows, photo, publish=args.publish)), flush=True)


if __name__ == '__main__':
    main()
