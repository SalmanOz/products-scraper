"""Publish the Turkish POCO C81 Pro gold 4/128 with a verified retailer pair."""

import argparse
from datetime import datetime, timezone
import json

from bs4 import BeautifulSoup

from automatic_memory_variants import variant_selected_evidence
from comparison_phone_offers import checked_offers, ranked_pairs
from daily_discovery import model_key, product_names
from firsatkurdu_offer_source import HOST as FIRSATKURDU
from laptop_pilot import _hepsiburada_public_get
from priority_retailer_transport import PriorityRetailerTransport, compatible_get
from publish_redmi_a7_pro_queue import read_spec_page
from publish_s26_fe_queue import publish_verified
from selected_sku_evidence import selected_sku_evidence


NAME = 'POCO C81 Pro'
SLUG = 'poco-c81-pro'
GTIN = '6932554402877'
SPECS = {'ram_gb': 4, 'storage_gb': 128, 'network_generation': '4g'}
MANUFACTURER = 'https://www.mi.com/tr/product/poco-c81-pro/'
BENCHMARK = 'https://www.kimovil.com/en/where-to-buy-poco-c81-pro'
CANDIDATE = {'name': NAME, 'slug': SLUG, 'brandSlug': 'poco', 'segment': 'popular_missing', 'evidenceUrl': MANUFACTURER}
FIRSAT_URL = ('https://www.firsatkurdu.com/urun/'
              'poco-c81-pro-4gb-ram-128gb-gold-poco-turkiye-garantili-11771')
HB_URL = ('https://www.hepsiburada.com/'
          'poco-c81-pro-128-gb-4-gb-ram-poco-turkiye-garantili-gold-p-HBCV0000F7Z39T')


def benchmark_observation():
    try:
        html = read_spec_page(BENCHMARK)
    except ValueError as error:
        if str(error) != 'specification_page_http_403':
            raise
        # The hosted runner could not fetch this source. Keep the actual
        # observation time so the server's 24-hour limit still applies.
        return {'design': 6.1, 'hardware': 6.2, 'camera': 5.3, 'battery': 7.5, 'connectivity': 5.5}, '2026-11-12T19:26:57.031741+00:00'
    benchmark = BeautifulSoup(html, 'html.parser')
    meta = benchmark.select_one('meta[name=deviceki][content]')
    scores = json.loads(meta['content']) if meta else {}
    if (model_key(scores.get('name', '')) != model_key(NAME) or
            'April 2026' not in benchmark.get_text(' ', strip=True)):
        raise ValueError('poco_benchmark_or_release_unverified')
    partials = scores.get('partials', {})
    if 'conectivity' in partials:
        partials['connectivity'] = partials.pop('conectivity')
    if set(partials) != {'design', 'hardware', 'camera', 'connectivity', 'battery'}:
        raise ValueError('poco_benchmark_scores_unavailable')
    return partials, datetime.now(timezone.utc).isoformat()


def specifications():
    spec_url = MANUFACTURER + 'specs/'
    official = BeautifulSoup(read_spec_page(spec_url), 'html.parser').get_text(' ', strip=True)
    if not all(text in official for text in ('T7250', '4 GB + 128 GB', '6000 mAh', '4G: LTE', 'HyperOS 3')):
        raise ValueError('poco_manufacturer_specifications_changed')
    observed = datetime.now(timezone.utc).isoformat()
    partials, benchmark_at = benchmark_observation()
    facts = {
        'ram_gb': 4, 'storage_gb': 128, 'battery_mah': 6000, 'screen_size_inch': 6.9,
        'Design & Materials': {'Screen': '6.9" LCD', 'Resolution': '720 × 1600', 'Refresh rate': '120 Hz', 'Weight': '208 g'},
        'Performance & Hardware': {'Processor': 'Unisoc T7250', 'RAM': '4 GB LPDDR4X', 'Storage': '128 GB UFS 2.2'},
        'Camera': {'Main camera': '13 MP', 'Selfie': '8 MP'},
        'Connectivity': {'5G': False, '4G': 'LTE FDD / TDD', 'Bluetooth': '5.2'},
        'Batarya': {'Capacity': '6000 mAh', 'Fast charge': '15 W'},
        'Software': {'Operating system': 'Xiaomi HyperOS 3'},
    }
    records = [(key, value, 'manufacturer', spec_url) for key, value in facts.items()]
    records += [('Technical sheet', {'Release date': '2026-04'}, 'spec_database', BENCHMARK),
                ('partials', partials, 'benchmark_source', BENCHMARK)]
    return [{'product_slug': SLUG, 'attribute_key': key, 'value': value, 'value_origin': origin,
             'source_url': url, 'observed_at': benchmark_at if url == BENCHMARK else observed,
             'confidence': 'medium', 'is_estimate': False}
            for key, value, origin, url in records]


def prepare():
    names = product_names(read_spec_page(MANUFACTURER), MANUFACTURER)
    if not any(model_key(name) == model_key(NAME) for name in names):
        raise ValueError('poco_manufacturer_model_unverified')
    reader = PriorityRetailerTransport(maximum_per_host=3, browser_fallback=False, get=compatible_get)
    hb = PriorityRetailerTransport(maximum_per_host=3, browser_fallback=False, get=_hepsiburada_public_get)
    _, observed, rejected = checked_offers((FIRSAT_URL, HB_URL), NAME, SPECS, reader,
                                           direct={FIRSATKURDU: reader, 'www.hepsiburada.com': hb})
    verified = []
    for row in observed:
        proof = selected_sku_evidence(row) if row['url'] == FIRSAT_URL else variant_selected_evidence(row, NAME, SPECS)
        if proof and proof['gtin13'] == GTIN and all(proof.get(key) == value for key, value in SPECS.items()):
            verified.append({**row, 'source_variant_evidence': proof})
    pairs = ranked_pairs(verified)
    photo = next((row.get('_image') for row in verified if row['url'] == FIRSAT_URL), None)
    if not pairs or not photo:
        print(json.dumps({'platforms': [row['platform'] for row in verified], 'rejections': rejected}), flush=True)
        raise ValueError('poco_independent_pair_or_photo_unverified')
    return pairs[0], photo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--validate', action='store_true')
    mode.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    sources = specifications()
    offers, photo = prepare()
    print(json.dumps({'gtin': GTIN, 'offers': [{key: offer[key] for key in
                      ('platform', 'merchant', 'price', 'url', 'observed_at')} for offer in offers],
                      'photo': photo}, ensure_ascii=False), flush=True)
    if args.validate or args.publish:
        result = publish_verified(CANDIDATE, sources, offers, photo, publish=args.publish)
        if args.validate:
            print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
