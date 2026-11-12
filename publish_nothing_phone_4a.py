"""Validate or publish the first Nothing phone using two current selected-SKU offers."""

import argparse
from datetime import datetime, timezone
import json

from comparison_phone_offers import checked_offers, ranked_pairs
from daily_discovery import PublicReader, model_key, product_names
from priority_retailer_transport import PriorityRetailerTransport, compatible_get
from publish_s26_fe_queue import publish_verified
from selected_sku_evidence import selected_sku_evidence
from tr_price_scraper import TRPriceScraper
from tws_offer_pilot import independent_sellers


NAME = 'Nothing Phone (4a)'
SLUG = 'nothing-phone-4a'
GTIN = '6974434228078'
SPECS = {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'}
MANUFACTURER = 'https://tr.nothing.tech/products/phone-4a'
SPEC_DATABASE = 'https://www.kimovil.com/en/where-to-buy-nothing-phone-4a'
CANDIDATE = {'name': NAME, 'slug': SLUG, 'brandSlug': 'nothing', 'segment': 'popular_missing',
             'evidenceUrl': MANUFACTURER}
EVOFONE = 'https://evofone.com/nothing-phone-4a-beyaz-8gb-256gb'
OFFER_URLS = (EVOFONE,
              'https://www.pazarama.com/nothing-phone-4a-akilli-telefon-beyaz-8gb-256gb-5g-usb-type-c-6974434228078-p-6974434228078',
              'https://www.trendyol.com/nothing/phone-4a-8-256-gb-nothing-turkiye-garantili-p-1123138356')


def sourced_specifications():
    observed = datetime.now(timezone.utc).isoformat()
    facts = {
        'ram_gb': 8, 'storage_gb': 256, 'battery_mah': 5080, 'screen_size_inch': 6.78,
        'Technical sheet': {'Release date': '2026-03'},
        'Design & Materials': {'Screen': '6.78" AMOLED', 'Resolution': '1224 × 2720',
                               'Refresh rate': '120 Hz', 'Weight': '204 g', 'Resistance': 'IP64'},
        'Performance & Hardware': {'Processor': 'Qualcomm Snapdragon 7s Gen 4', 'RAM': '8 GB', 'Storage': '256 GB'},
        'Camera': {'Main camera': '50 MP', 'Wide angle': '8 MP', 'Telephoto': '50 MP'},
        'Connectivity': {'5G': True, 'Bluetooth': '5.4', 'NFC': True, 'USB Type C': True},
        'Batarya': {'Capacity': '5080 mAh', 'Fast charge': '50 W'},
        'Software': {'Operating system': 'Android 16'},
        'partials': {'design': 7.2, 'hardware': 7.4, 'camera': 7.6, 'connectivity': 8.0, 'battery': 7.0},
    }
    return [{'product_slug': SLUG, 'attribute_key': key,
             'value_origin': 'benchmark_source' if key == 'partials' else 'spec_database',
             'source_url': SPEC_DATABASE, 'observed_at': observed, 'is_estimate': False,
             'confidence': 'medium', 'value': value} for key, value in facts.items()]


def evofone_with_radio_witness(offer, other):
    proof = other['source_variant_evidence']
    ram, storage, ambiguous = TRPriceScraper._unambiguous_listing_capacities(offer['source_title'])
    radio = TRPriceScraper._radio_generation(offer['source_title'])
    if (ambiguous or ram != proof['ram_gb'] or storage != proof['storage_gb']
            or radio not in (None, proof['network_generation']) or offer['gtin13'] != proof['gtin13']
            or not independent_sellers(offer, other)):
        return None
    observed = datetime.fromisoformat(other['observed_at']).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    return {**offer, 'source_variant_evidence': {
        'version': 'selected-retailer-sku-v3', 'platform': 'Evofone', 'merchant_name': 'Evofone',
        'merchant_id': offer['merchant_id'], 'product_id': offer['product_id'], 'sku': offer['sku'],
        'gtin13': offer['gtin13'], 'ram_gb': ram, 'storage_gb': storage,
        'network_generation': proof['network_generation'], 'independent_radio': {
            'source_url': other['url'], 'source_title': other['source_title'],
            'observed_at': observed, 'sku_evidence': proof,
        },
    }}


def prepare():
    names = product_names(PublicReader().read(MANUFACTURER), MANUFACTURER)
    if not any(model_key(name) in {model_key(NAME), 'phone4a'} for name in names):
        raise ValueError('nothing_manufacturer_model_unverified')
    reader = PriorityRetailerTransport(maximum_per_host=10, browser_fallback=False, get=compatible_get,
                                       unavailable_robots_hosts=('www.pazarama.com',))
    _, observed, rejected = checked_offers(OFFER_URLS, NAME, SPECS, reader, direct={'evofone.com': reader})
    offers = []
    for offer in observed:
        proof = selected_sku_evidence(offer)
        if proof and proof['gtin13'] == GTIN and all(proof.get(key) == value for key, value in SPECS.items()):
            offers.append({**offer, 'source_variant_evidence': proof})
    direct = next((offer for offer in observed if offer['url'] == EVOFONE and offer['gtin13'] == GTIN), None)
    photo = direct.get('_image') if direct else None
    pairs = ranked_pairs(offers)
    if direct and not pairs:
        for other in sorted(offers, key=lambda row: row['price']):
            fallback = evofone_with_radio_witness(direct, other)
            if fallback:
                pairs = ranked_pairs([fallback, other])
                if pairs:
                    break
    if not pairs or not photo:
        print(json.dumps({'qualified_platforms': [offer['platform'] for offer in offers], 'rejections': rejected}), flush=True)
        raise ValueError('nothing_independent_same_gtin_pair_or_photo_unverified')
    return pairs[0], photo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--validate', action='store_true')
    mode.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    offers, photo = prepare()
    print(json.dumps({'gtin': GTIN, 'offers': [{key: offer[key] for key in
                      ('platform', 'merchant', 'price', 'url', 'observed_at')} for offer in offers],
                      'photo': photo}, ensure_ascii=False), flush=True)
    if args.validate or args.publish:
        result = publish_verified(CANDIDATE, sourced_specifications(), offers, photo, publish=args.publish)
        if args.validate:
            print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
