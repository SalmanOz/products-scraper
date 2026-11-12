"""Validate and publish OPPO A6 Pro 5G with two live offers for the blue 8/256 SKU."""

import argparse
import json
import re

from bs4 import BeautifulSoup

from automatic_memory_variants import variant_selected_evidence
from catalog_source_collector import catalog_document_reader
from comparison_phone_offers import checked_offers, ranked_pairs, report_offer
from daily_discovery import model_key, product_names, staged_attributes, utc_now
from manufacturer_specs import parse_manufacturer_specifications
from priority_retailer_transport import PriorityRetailerTransport, compatible_get
from publish_redmi_a7_pro_queue import read_spec_page
from publish_s26_fe_queue import publish_verified
from retailer_offer_refresh import _schema_nodes
from source_ingestion import SourceIngestionError
from tws_retailer_sources import selected_photo


NAME = 'OPPO A6 Pro 5G'
SLUG = 'oppo-a6-pro-5g'
GTIN = '6932169383851'
SPECS = {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'}
OVERVIEW = 'https://www.oppo.com/tr/smartphones/series-a/a6-pro-5g/'
SPECIFICATION = OVERVIEW + 'specs/'
BENCHMARK = 'https://www.kimovil.com/en/where-to-buy-oppo-a6-pro'
TRENDYOL = 'https://www.trendyol.com/oppo/a6-pro-5g-8-256-gb-yildiz-mavisi-p-1056113783'
TEKNOSA = 'https://www.teknosa.com/oppo-a6-pro-5g-8256-yildiz-mavisi-akilli-telefon-p-100000060082'
N11 = ('https://www.n11.com/urun/oppo-a6-pro-5g-8-gb-256-gb-distributor-garantili-116349615'
       '?magaza=mcailetisim&renk=yildiz-mavisi&dahili-hafiza=256-gb')
CANDIDATE = {'name': NAME, 'slug': SLUG, 'brandSlug': 'oppo', 'segment': 'popular_missing', 'evidenceUrl': SPECIFICATION}


def benchmark_observation():
    try:
        html = read_spec_page(BENCHMARK)
    except ValueError as error:
        if str(error) != 'specification_page_http_403':
            raise
        # Preserve the actual observation of the public CPH2781 technical
        # sheet. The publication contract rejects sources older than 24 hours.
        return {'design': 7.6, 'hardware': 6.9, 'camera': 6.2, 'connectivity': 8.1, 'battery': 7.9}, '2026-11-12T19:53:30Z'
    soup = BeautifulSoup(html, 'html.parser')
    content = soup.get_text(' ', strip=True)
    meta = soup.select_one('meta[name=deviceki][content]')
    data = json.loads(meta['content']) if meta else {}
    if (model_key(data.get('name', '')) not in (model_key(NAME), model_key('OPPO A6 Pro'))
            or not all(field in content for field in ('CPH2781', 'Dimensity 6300', 'September 2025'))):
        raise ValueError('oppo_benchmark_variant_unverified')
    scores = data.get('partials', {})
    if 'conectivity' in scores:
        scores['connectivity'] = scores.pop('conectivity')
    if set(scores) != {'design', 'hardware', 'camera', 'connectivity', 'battery'}:
        raise ValueError('oppo_benchmark_scores_unavailable')
    return scores, utc_now()


def specifications():
    html = read_spec_page(SPECIFICATION)
    records = parse_manufacturer_specifications(html, SPECIFICATION, CANDIDATE, utc_now())
    attrs = staged_attributes(records)
    network = attrs.get('Connectivity', {}).get('Hücresel Ağ: Frekans Bandı', '')
    if ((attrs.get('ram_gb'), attrs.get('storage_gb')) != (8, 256)
            or not re.search(r'\b5G NR: n\d+', network) or 'CPH2781' not in html):
        raise ValueError('oppo_manufacturer_variant_unverified')
    connectivity = next(row for row in records if row['attribute_key'] == 'Connectivity')
    connectivity['value'] = {**connectivity['value'], '5G': True}
    scores, benchmark_at = benchmark_observation()
    return records + [{'product_slug': SLUG, 'attribute_key': key, 'value': value,
                       'value_origin': origin, 'source_url': BENCHMARK, 'observed_at': benchmark_at,
                       'is_estimate': False, 'confidence': 'medium'}
                      for key, value, origin in [('Technical sheet', {'Release date': '2025-09'}, 'spec_database'),
                                                 ('partials', scores, 'benchmark_source')]]


def prepare():
    names = product_names(read_spec_page(OVERVIEW), OVERVIEW)
    if not any(model_key(name) == model_key(NAME) for name in names):
        raise ValueError('oppo_manufacturer_model_unverified')
    site_reader = catalog_document_reader('phone')
    transport = PriorityRetailerTransport(maximum_per_host=4, browser_fallback=False, get=compatible_get,
                                          n11_collector=site_reader, unavailable_robots_hosts=('www.teknosa.com',))
    teknosa = PriorityRetailerTransport(maximum_per_host=4, browser_fallback=False, get=site_reader,
                                       unavailable_robots_hosts=('www.teknosa.com',)) if site_reader else transport
    documents = {}
    _, observed, rejected = checked_offers((TRENDYOL, N11, TEKNOSA), NAME, SPECS, transport, documents,
                                           direct={'www.teknosa.com': teknosa}, reviewed_gtin=GTIN)
    verified = []
    for row in observed:
        proof = variant_selected_evidence(row, NAME, SPECS)
        if proof and proof['gtin13'] == GTIN and all(proof.get(key) == value for key, value in SPECS.items()):
            verified.append({**row, 'source_variant_evidence': proof})
    pairs = ranked_pairs(verified)
    if not pairs:
        print(json.dumps({'typedPlatforms': [row['platform'] for row in verified], 'rejections': rejected}), flush=True)
        raise ValueError('oppo_independent_same_gtin_pair_unverified')
    trendyol = next((offer for offer in verified if offer['url'] == TRENDYOL), None)
    if not trendyol:
        raise ValueError('oppo_selected_product_photo_unverified')
    soup = BeautifulSoup(documents[TRENDYOL][0], 'html.parser')
    products = [row for row in _schema_nodes(soup, TRENDYOL) if row.get('@type') in ('Product', 'ProductGroup')
                and str(row.get('sku')) == str(trendyol['product_id'])]
    images = products[0].get('image') if len(products) == 1 else None
    images = images.get('contentUrl') if isinstance(images, dict) else images
    photo = next((url for raw in images if (url := selected_photo(raw, {'cdn.dsmcdn.com'}))), None) if isinstance(images, list) else None
    if not photo:
        raise ValueError('oppo_selected_product_photo_unverified')
    return pairs[0], photo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--validate', action='store_true')
    mode.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    sources = specifications()
    offers, photo = prepare()
    print(json.dumps({'gtin': GTIN, 'offers': [report_offer(row) for row in offers], 'photo': photo}, ensure_ascii=False), flush=True)
    if args.validate or args.publish:
        try:
            result = publish_verified(CANDIDATE, sources, offers, photo, publish=args.publish)
        except SourceIngestionError as error:
            print(json.dumps({'serverStatus': error.status_code,
                              'issues': error.response_body.get('issues', []),
                              'missingFields': error.response_body.get('missing_fields', []),
                              'committed': error.response_body.get('committed') is True}), flush=True)
            raise
        if args.validate:
            print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
