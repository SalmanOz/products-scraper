"""Check retailer links for phones missing from the catalog against Turkish manufacturer specs."""

import argparse
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

import requests

from comparison_phone_offers import indexed_phone_leads, phone_lead_index, run
from competitor_catalog_gaps import phone_catalog_family, phone_family, public_phone_catalog, read_state, write_json
from daily_discovery import LISTINGS, PublicReader, discover_candidates, may_duplicate, staged_attributes, utc_now
from manufacturer_specs import manufacturer_spec_links, missing_critical_facts, parse_manufacturer_specifications


def candidate_queue(state, catalog, candidates, limit):
    index = phone_lead_index(state, expanded=True, include_unlisted=True)
    families = {phone_catalog_family(row['slug']) for row in catalog}
    names = {phone_family(row['name']) for row in catalog if isinstance(row.get('name'), str)}
    last_checked = state.get('unlistedPhoneAudits', {})
    queue = []
    seen_pages = set()
    for candidate in candidates:
        slug, name, page = (candidate[key] for key in ('slug', 'name', 'evidenceUrl'))
        brand = phone_family(name).partition(' ')[0]
        if (slug in families or page in seen_pages
                or any(known.partition(' ')[0] == brand and may_duplicate(name, known) for known in names)):
            continue
        # The official catalog supplies names. Comparison titles and URLs
        # only prioritize reads of the manufacturer and retailer documents.
        family = phone_family(name).replace(' ', '-')
        hosts = {urlsplit(url).netloc for url in indexed_phone_leads(index, family)}
        if len(hosts) >= 2:
            queue.append({**candidate, 'leadPlatforms': len(hosts), 'leadFamily': family})
            seen_pages.add(page)
    queue.sort(key=lambda row: (last_checked.get(row['slug'], ''), -row['leadPlatforms'], row['slug']))
    return len(queue), queue[:limit]


def audit(state, catalog, reader, limit):
    if not catalog:
        raise ValueError('public_phone_catalog_unavailable')
    candidates, sources = [], {}
    for brand, url in LISTINGS.items():
        try:
            found = discover_candidates(brand, reader.read(url), url)
        except (ValueError, requests.RequestException):
            sources[brand] = 'unavailable'
            continue
        sources[brand] = len(found)
        candidates.extend(found)
    queued, queue = candidate_queue(state, catalog, candidates, limit)
    ready, rows = [], {}
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
                if not attributes.get('ram_gb') or not attributes.get('storage_gb'):
                    row['status'] = 'manufacturer_memory_variant_unresolved'
                    continue
                ready.append({'slug': family, 'name': candidate['name'], 'attributes': attributes})
                row['status'] = 'retailer_check_pending'
                break
        except (ValueError, requests.RequestException) as error:
            row['sourceError'] = type(error).__name__
        state.setdefault('unlistedPhoneAudits', {})[slug] = utc_now()

    if ready:
        offers = run(state, [row['slug'] for row in ready], products=ready, maximum=12,
                     public_audit=True, unlisted_audit=True)
        for offer in offers:
            row = rows[offer['slug']]
            row['retailerAudit'] = {key: value for key, value in offer.items() if key != 'slug'}
            row['status'] = 'verified_offer_pair' if offer['sameGtin'] else 'no_verified_offer_pair'
    return {'checkedAt': utc_now(), 'manufacturerSources': sources, 'queued': queued,
            'statuses': dict(Counter(row['status'] for row in rows.values())), 'rows': list(rows.values())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--limit', type=int, choices=range(1, 9), default=4)
    args = parser.parse_args()
    if not args.state.is_file():
        parser.error('Provide an existing competitor discovery state')
    state = read_state(args.state)
    report = audit(state, public_phone_catalog(), PublicReader(), args.limit)
    write_json(args.output, report)
    write_json(args.state, state)
    print(f"Unlisted phones: {report['queued']} queued, {report['statuses']}")


if __name__ == '__main__':
    main()
