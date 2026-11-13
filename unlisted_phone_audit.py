"""Check retailer links for phones missing from the catalog against Turkish manufacturer specs."""

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from urllib.parse import urlsplit

import requests

from comparison_phone_offers import indexed_phone_leads, phone_lead_index, ranked_pairs, run
from competitor_catalog_gaps import phone_catalog_family, phone_family, public_phone_catalog, read_state, write_json
from daily_discovery import LISTINGS, PublicReader, discover_candidates, may_duplicate, staged_attributes, utc_now
from manufacturer_specs import manufacturer_spec_links, missing_critical_facts, parse_manufacturer_specifications


MANUFACTURER_LISTINGS = {**LISTINGS, 'general-mobile': 'https://www.generalmobile.com/tr/urunler/mobilephone'}


def candidate_queue(state, catalog, candidates, limit):
    index = phone_lead_index(state, expanded=True, include_unlisted=True)
    families = {phone_catalog_family(row['slug']) for row in catalog}
    names = {phone_family(row['name']) for row in catalog if isinstance(row.get('name'), str)}
    last_checked = state.get('unlistedPhoneAudits', {})
    queue = []
    seen_pages, seen_families = set(), set()
    for candidate in candidates:
        slug, name, page = (candidate[key] for key in ('slug', 'name', 'evidenceUrl'))
        family = phone_family(name).replace(' ', '-')
        brand = family.partition('-')[0]
        if (slug in families or page in seen_pages or family in seen_families
                or any(known.partition(' ')[0] == brand and may_duplicate(name, known) for known in names)):
            continue
        # The official catalog supplies names. Comparison titles and URLs
        # only prioritize reads of the manufacturer and retailer documents.
        hosts = {urlsplit(url).netloc for url in indexed_phone_leads(index, family)}
        if len(hosts) >= 2:
            queue.append({**candidate, 'leadPlatforms': len(hosts), 'leadFamily': family})
            seen_pages.add(page)
            seen_families.add(family)
    queue.sort(key=lambda row: (last_checked.get(row['slug'], ''), -row['leadPlatforms'], row['slug']))
    return len(queue), queue[:limit]


def audit(state, catalog, reader, limit, *, verified_candidates=None):
    if not catalog:
        raise ValueError('public_phone_catalog_unavailable')
    candidates, sources = [], {}
    for brand, url in MANUFACTURER_LISTINGS.items():
        try:
            found = discover_candidates(brand, reader.read(url), url)
        except (ValueError, requests.RequestException):
            sources[brand] = 'unavailable'
            continue
        sources[brand] = len(found)
        candidates.extend(found)
    queued, queue = candidate_queue(state, catalog, candidates, limit)
    ready, rows, variants = [], {}, {}
    for candidate in queue:
        slug, page, family = (candidate[key] for key in ('slug', 'evidenceUrl', 'leadFamily'))
        row = rows[family] = {**candidate, 'status': 'manufacturer_spec_unavailable'}
        try:
            overview = reader.read(page)
            for url in manufacturer_spec_links(overview, candidate):
                html = overview if url == page else reader.read(url)
                records = parse_manufacturer_specifications(html, url, candidate, utc_now())
                attributes = staged_attributes(records)
                row.update(specificationUrl=url, missingPublicationFields=missing_critical_facts(records))
                options = attributes.get('Performance & Hardware', {}).get('Memory options', [])
                if attributes.get('ram_gb') and attributes.get('storage_gb'):
                    options = [{key: attributes[key] for key in ('ram_gb', 'storage_gb')}]
                if not options or not all(isinstance(option, dict) for option in options):
                    row['status'] = 'manufacturer_memory_variant_unresolved'
                    continue
                for option in options[:4]:
                    variant = f"{family}-{option['ram_gb']}gb-{option['storage_gb']}gb"
                    ready.append({'slug': variant, 'name': candidate['name'], 'attributes': {**attributes, **option}})
                    variants[variant] = (row, option)
                row['status'] = 'retailer_check_pending'
                break
        except (ValueError, requests.RequestException) as error:
            row['sourceError'] = type(error).__name__
        state.setdefault('unlistedPhoneAudits', {})[slug] = utc_now()

    if ready:
        observations = {} if verified_candidates is not None else None
        offers = run(state, [row['slug'] for row in ready], products=ready, maximum=12,
                     public_audit=True, unlisted_audit=True, verified_observations=observations)
        for offer in offers:
            row, option = variants[offer['slug']]
            row.setdefault('variants', []).append({**option, **{key: value for key, value in offer.items() if key != 'slug'}})
        for row in rows.values():
            if row.get('variants'):
                row['status'] = 'verified_offer_pair' if any(result['sameGtin'] for result in row['variants']) else 'no_verified_offer_pair'
        if verified_candidates is not None:
            for candidate in queue:
                family = candidate['leadFamily']
                if rows[family]['status'] == 'verified_offer_pair':
                    verified_candidates[candidate['slug']] = (
                        {k: candidate[k] for k in ('slug', 'name', 'brandSlug', 'evidenceUrl', 'segment')},
                        [row for slug, (info, _) in variants.items() if info is rows[family]
                         for row in observations.get(slug, ())],
                    )
    return {'checkedAt': utc_now(), 'manufacturerSources': sources, 'queued': queued,
            'statuses': dict(Counter(row['status'] for row in rows.values())), 'rows': list(rows.values())}


def discovery_offers(observations, name, attributes):
    from automatic_memory_variants import variant_selected_evidence
    from selected_sku_evidence import selected_sku_evidence
    from update_prices import PriceUpdater
    from tr_price_scraper import TRPriceScraper

    specs = PriceUpdater.get_expected_specs(attributes)
    keys = ('ram_gb', 'storage_gb', 'network_generation')
    if any(not specs.get(key) for key in keys):
        return []
    now = datetime.now(timezone.utc)
    for pair in ranked_pairs(observations):
        offers = []
        for offer in pair:
            proof = selected_sku_evidence(offer) or variant_selected_evidence(offer, name, specs)
            if not proof and offer['merchant'] == offer['platform'] and offer['platform'] in (
                    'Vatan Bilgisayar', 'MediaMarkt'):
                title = offer['source_title']
                ram, storage, ambiguous = TRPriceScraper._unambiguous_listing_capacities(title)
                radio = TRPriceScraper._radio_generation(title)
                sku = offer.get('sku')
                if sku and not ambiguous and (ram, storage, radio) == tuple(specs[k] for k in keys):
                    proof = {'version': 'selected-retailer-sku-v1', 'platform': offer['platform'],
                             'merchant_id': {'Vatan Bilgisayar': 'vatan-bilgisayar', 'MediaMarkt': 'mediamarkt'}[offer['platform']],
                             'merchant_name': offer['merchant'], 'product_id': sku, 'sku': sku,
                             'gtin13': offer['gtin13'], **{k: specs[k] for k in keys}}
            observed = offer['observed_at']
            if (not proof or any(proof.get(key) != specs[key] for key in keys) or
                    not timedelta(0) <= now - datetime.fromisoformat(observed) < timedelta(hours=1)):
                break
            offers.append({'merchant_name': offer['merchant'], 'price': offer['price'], 'currency': 'TRY',
                           'availability': 'in_stock', 'source_url': offer['url'], 'source_title': offer['source_title'],
                           'observed_at': observed, 'checked_at': observed, 'source_variant_evidence': proof})
        if len(offers) == 2:
            return offers
    return []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--limit', type=int, choices=range(1, 9), default=4)
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    if not args.state.is_file():
        parser.error('Provide an existing competitor discovery state')
    state = read_state(args.state)
    reader = PublicReader()
    candidates = {} if args.publish else None
    report = audit(state, public_phone_catalog(), reader, args.limit, verified_candidates=candidates)
    write_json(args.output, report)
    write_json(args.state, state)
    if candidates:
        from daily_discovery import DiscoveryImageStore, PublicSpecificationScraper, run_discovery
        from source_ingestion import SourceIngestionClient

        client = SourceIngestionClient.from_env()
        specs = PublicSpecificationScraper(reader, ingestion_client=client, require_images=False,
                                           image_store=DiscoveryImageStore(required=True))
        publication = args.output.with_name('phone-publication.json')
        run_discovery(client=client, reader=reader, specs=specs, price_scraper=None, refresher=None, publish=True,
                      competitor_candidates=candidates, checkpoint=lambda report: write_json(publication, report))
        result = json.loads(publication.read_bytes())
        for row in result['candidates']:
            if row['status'] not in ('created', 'committed_needs_attention'):
                continue
            response = requests.get('https://teknoskor.com/product/' + row['slug'],
                                    headers={'User-Agent': 'Mozilla/5.0', 'Cache-Control': 'no-cache'},
                                    timeout=20, allow_redirects=False)
            if response.status_code != 200:
                raise RuntimeError('A published phone did not appear on production')
    print(f"Unlisted phones: {report['queued']} queued, {report['statuses']}")


if __name__ == '__main__':
    main()
