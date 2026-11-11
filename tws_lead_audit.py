"""Read original retailer listings discovered on comparison sites for known earbuds."""

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import urlsplit

from competitor_catalog_gaps import read_state, retailer_destination, write_json
from priority_retailer_transport import product_host
from tr_price_scraper import TRPriceScraper
from tws_catalog_expansion import exact_tws_sellers, reread
from tws_offer_pilot import COHORT, EXPANSION_COHORT, NEW_MODELS, _mediamarkt_tws_url, model_name
from tws_retailer_sources import _vatan_url


def identify(title):
    words = TRPriceScraper._normalize_words(title).split()
    if not words:
        return None
    brand = {'redmi': 'xiaomi', 'airpods': 'apple', 'soundcore': 'anker'}.get(words[0], words[0])
    if brand not in {row[0].casefold() for row in (*COHORT, *EXPANSION_COHORT, *NEW_MODELS)}:
        return None
    return model_name(title, brand)


def collect_leads(state, models=(), maximum=16):
    candidates = defaultdict(set)
    for row in state['products'].values():
        if row.get('category') != 'tws' or not row.get('retailerLeads'):
            continue
        name = identify(row['title'])
        if name is None or models and name not in models:
            continue
        for raw in row['retailerLeads']:
            url = retailer_destination(raw)
            if url and (product_host(url) or _vatan_url(url) or _mediamarkt_tws_url(url)):
                candidates[url].add(name)
    groups = defaultdict(list)
    previous = state.get('earbudLeadAudits', {})
    for url, names in sorted(candidates.items(), key=lambda item: (previous.get(item[0], ''), item[0])):
        if len(names) == 1:
            groups[urlsplit(url).netloc].append((next(iter(names)), {'url': url}))
    return {host: rows[:maximum] for host, rows in groups.items()}


def audit(state, models=()):
    groups = collect_leads(state, models)
    observations, failures = defaultdict(list), {}
    statuses = {host: {} for host in groups}
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {host: pool.submit(reread, host, rows, statuses[host], discover=True)
                for host, rows in groups.items()}
        for host, job in jobs.items():
            rows, rejects = job.result()
            failures[host] = dict(rejects)
            for model, offers in rows.items():
                observations[model].extend(offers)
    verified = []
    for name, offers in sorted(observations.items()):
        pair = exact_tws_sellers(offers)
        fields = ('url', 'platform', 'merchant', 'gtin13', 'price')
        verified.append({'name': name, 'observations': [{key: offer[key] for key in fields} for offer in offers],
                         'verifiedPair': [{key: offer[key] for key in fields} for offer in pair]})
    now = datetime.now(timezone.utc).isoformat()
    state.setdefault('earbudLeadAudits', {}).update({url: now for rows in statuses.values() for url in rows})
    return {'sourceRequests': {host: len(rows) for host, rows in statuses.items()},
            'rejections': failures, 'verified': verified}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', action='append', default=[])
    args = parser.parse_args()
    state = read_state(args.state)
    result = audit(state, args.model)
    write_json(args.state, state)
    write_json(args.output, result)
    print(json.dumps({'earbudLeadRequests': sum(result['sourceRequests'].values()),
                      'earbudModelsVerified': len(result['verified']),
                      'earbudPairsVerified': sum(bool(row['verifiedPair']) for row in result['verified'])}))


if __name__ == '__main__':
    main()
