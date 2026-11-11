"""Verify the selected Galaxy A08 4/128 before daily discovery publishes it."""

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

NAME = 'Samsung Galaxy A08'
SLUG = 'samsung-galaxy-a08'
GTIN = '8806099447335'
SPECS = {'ram_gb': 4, 'storage_gb': 128, 'network_generation': '4g'}
MANUFACTURER = 'https://www.samsung.com/tr/smartphones/galaxy-a/galaxy-a08-dark-blue-128gb-sm-a085fdbgtur/'
SPEC_DATABASE = 'https://www.kimovil.com/en/where-to-buy-samsung-galaxy-a08'
MEDIAMARKT = ('https://www.mediamarkt.com.tr/tr/product/'
              '_samsung-galaxy-a08-lte-4128gb-akilli-telefon-lacivert-1257276.html')
VATAN = 'https://www.vatanbilgisayar.com/samsung-galaxy-a08-4-128-gb-akilli-telefon-lacivert.html'
CANDIDATE = {'name': NAME, 'slug': SLUG, 'brandSlug': 'samsung', 'segment': 'new_release',
             'evidenceUrl': MANUFACTURER}


def specifications():
    observed = datetime.now(timezone.utc).isoformat()
    physical = {
        'ram_gb': 4, 'storage_gb': 128, 'battery_mah': 6000, 'screen_size_inch': 6.7,
        'Design & Materials': {'Screen': '6.7" PLS LCD', 'Resolution': '720 × 1600'},
        'Performance & Hardware': {'Processor': 'Octa-core, up to 2.2 GHz', 'RAM': '4 GB', 'Storage': '128 GB'},
        'Camera': {'Main camera': '50 MP', 'Depth': '2 MP', 'Selfie': '8 MP'},
        'Connectivity': {'5G': False, '4G': 'LTE FDD / TDD'},
        'Batarya': {'Capacity': '6000 mAh'}, 'Software': {'Operating system': 'Android'},
    }
    records = [(key, value, 'manufacturer', MANUFACTURER) for key, value in physical.items()]
    records += [
        ('Technical sheet', {'Release date': '2026-09'}, 'spec_database', SPEC_DATABASE),
        ('partials', {'design': 5.6, 'hardware': 6.0, 'camera': 6.2, 'connectivity': 5.4, 'battery': 7.7},
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
        expected = 'samsung galaxy a08 4 128 gb akilli telefon lacivert sm a085fdbgtur'
        return NAME if brand.casefold() == 'samsung' and words == expected else None

    vatan = selected_vatan_tws(html, final, NAME, match)
    if (not vatan or vatan['gtin13'] != GTIN or vatan['sku'] != '160479'
            or vatan['retailer_model_code'] != 'SM-A085FDBGTUR'):
        raise ValueError('vatan_selected_blue_variant_unverified')
    vatan['source_variant_evidence'] = {
        'version': 'selected-retailer-sku-v1', 'platform': vatan['platform'], 'gtin13': GTIN,
        'product_id': vatan['sku'], 'sku': vatan['sku'], 'merchant_id': 'vatan-bilgisayar',
        'merchant_name': vatan['merchant'], **SPECS,
    }

    media = selected_mediamarkt_phone_offer(MEDIAMARKT, '1257276', GTIN, SPECS,
                                  'samsung galaxy a08 lte 4 128gb akilli telefon lacivert', expected_brand='samsung')
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
