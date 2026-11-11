"""Recheck the selected Turkish Redmi Note 17 Pro Max 5G 8/256 before publishing."""

import argparse
from datetime import datetime, timezone
import json
from urllib.robotparser import RobotFileParser

from daily_discovery import MAX_BODY, PublicReader, model_key, product_names
from priority_retailer_transport import compatible_get
from publish_galaxy_a07s_queue import OZGUR_BASE, OZGUR_TERMS, ozgur_offer
from publish_redmi_note_17_pro_queue import selected_mediamarkt_phone_offer
from publish_s26_fe_queue import publish_verified
from source_ingestion import SourceIngestionClient
from tws_offer_pilot import independent_sellers

NAME = 'Xiaomi Redmi Note 17 Pro Max 5G'
SLUG = 'xiaomi-redmi-note-17-pro-max-5g'
GTIN = '6939093005040'
SPECS = {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'}
MANUFACTURER = 'https://www.mi.com/tr/product/redmi-note-17-pro-max-5g/'
SPEC_DATABASE = 'https://www.kimovil.com/en/where-to-buy-xiaomi-redmi-note-17-pro-max'
MEDIAMARKT = ('https://www.mediamarkt.com.tr/tr/product/'
              '_xiaomi-note-17-pro-max-5g-8256g-akilli-telefon-siyah-1256421.html')
OZGUR = OZGUR_BASE + '/urun/xiaomi-redmi-note-17-pro-max-5g-siyah-256-gb'
CANDIDATE = {'name': NAME, 'slug': SLUG, 'brandSlug': 'xiaomi', 'segment': 'new_release',
             'evidenceUrl': MANUFACTURER}


def specifications():
    observed = datetime.now(timezone.utc).isoformat()
    physical = {
        'ram_gb': 8, 'storage_gb': 256, 'battery_mah': 10000, 'screen_size_inch': 6.83,
        'Design & Materials': {'Screen': '6.83" AMOLED', 'Resolution': '1280 × 2772',
                               'Refresh rate': '120 Hz', 'Weight': '229.5 g', 'Resistance': 'IP66 / IP68'},
        'Performance & Hardware': {'Processor': 'Qualcomm Snapdragon 6 Gen 5', 'RAM': '8 GB LPDDR5X',
                                   'Storage': '256 GB UFS 4.1'},
        'Camera': {'Main camera': '50 MP OIS', 'Wide angle': '8 MP', 'Selfie': '32 MP'},
        'Connectivity': {'5G': True, 'Bluetooth': '6.0', 'Wi-Fi': 'Wi-Fi 6'},
        'Batarya': {'Capacity': '10000 mAh', 'Fast charge': '100 W'},
        'Software': {'Operating system': 'Xiaomi HyperOS 3'},
    }
    sources = [(key, value, 'manufacturer', MANUFACTURER + 'specs/') for key, value in physical.items()]
    sources.extend([
        ('Technical sheet', {'Release date': '2026-08'}, 'spec_database', SPEC_DATABASE),
        ('partials', {'design': 8.4, 'hardware': 7.4, 'camera': 6.7, 'connectivity': 7.8, 'battery': 8.8},
         'benchmark_source', SPEC_DATABASE),
    ])
    return [{'product_slug': SLUG, 'attribute_key': key, 'value': value, 'value_origin': origin,
             'source_url': url, 'observed_at': observed, 'confidence': 'medium', 'is_estimate': False}
            for key, value, origin, url in sources]


def prepare():
    reader = PublicReader()
    official = compatible_get(MANUFACTURER, timeout=15, allow_redirects=False)
    if (official.status_code != 200 or len(official.content) > MAX_BODY
            or 'text/html' not in official.headers.get('Content-Type', '')
            or not any(model_key(name) == model_key(NAME) for name in product_names(official.text, MANUFACTURER))):
        raise ValueError('manufacturer_model_unverified')
    robots = RobotFileParser()
    robots.parse(reader.read(OZGUR_BASE + '/robots.txt').splitlines())
    if any(not robots.can_fetch('TeknoskorPriceBot', url) for url in (OZGUR, OZGUR_TERMS)):
        raise ValueError('ozgur_robots_disallowed')
    direct, _ = ozgur_offer(reader.read(OZGUR), reader.read(OZGUR_TERMS), url=OZGUR, name=NAME,
                               specs=SPECS, mpn='REDMI-NOTE-17-PRO-MAX-5G-SIYAH-256GB',
                               gtin=GTIN, brand='Xiaomi')
    primary = selected_mediamarkt_phone_offer(MEDIAMARKT, '1256421', GTIN, SPECS,
                                    'xiaomi note 17 pro max 5g 8 256g akilli telefon siyah')
    if not independent_sellers(primary, direct):
        raise ValueError('two_independent_in_stock_offers_required')
    return [primary, direct], primary['photo']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--publish', action='store_true')
    mode.add_argument('--validate', action='store_true')
    args = parser.parse_args()
    if args.publish and any(row.slug == SLUG for row in SourceIngestionClient.from_env().fetch_catalog()):
        print(json.dumps({'slug': SLUG, 'outcome': 'existing'}), flush=True)
        return
    rows, photo = prepare()
    print(json.dumps({'gtin': GTIN, 'offers': [{k: row[k] for k in ('merchant', 'price', 'url', 'observed_at')}
                                            for row in rows], 'photo': photo}, ensure_ascii=False), flush=True)
    if args.publish or args.validate:
        result = publish_verified(CANDIDATE, specifications(), rows, photo, publish=args.publish)
        if args.validate:
            print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
