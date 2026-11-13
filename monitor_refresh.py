"""Refresh prices for the published monitor GTINs without changing the catalog."""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from threading import Lock, local

from laptop_pilot import _public_offers
from monitor_pilot import CORE, CROSS_PLATFORM_MINIMUM, MANUFACTURER_SPECS, PILOT_SIZE, candidates, collect_observations
from monitor_sources import code_key
from offer_ingestion_client import OfferIngestionClient
from retailer_barcode import retailer_gtin13

def matches_model(offer, model):
    facts = offer['facts']
    if offer['gtin13'] != model['gtin'] or facts['brand'] != model['brand']:
        return False
    official = MANUFACTURER_SPECS.get(model['gtin'], {})
    overrides = official.get('facts', {}) if (
        offer['code'] == official.get('code') and official.get('brand', model['brand']) == model['brand']) else {}
    for key in CORE:
        observed = facts[key]
        if observed is None or overrides.get(key) == model[key] or observed == model[key]:
            continue
        if key != 'size' or abs(observed - model[key]) > .25:
            return False
    return True


def refresh(catalog, groups, now):
    by_gtin = defaultdict(list)
    for (_, gtin), offers in groups.items():
        by_gtin[gtin].extend(offers)
    updated, fresh, paired = [], 0, 0
    for model in catalog['models']:
        observations = [offer for offer in by_gtin[model['gtin']] if matches_model(offer, model)]
        cohort = next(candidates(observations), ())
        offers = model['offers']
        if cohort:
            fresh += 1
            paired += len(cohort) == 2
            offers = _public_offers(sorted(cohort, key=lambda offer: (offer['price'], offer['url'])))
            if len(offers) == 1:
                # Retain a recently verified pair only if this is one of the
                # same seller's same listing URLs. Keep the other observation's
                # timestamp, so it expires even if this source remains healthy.
                key = lambda row: (row['platform'], row['merchant'], row['url'])
                observed = offers[0]
                if any(key(old) == key(observed) for old in model['offers']):
                    prior = [old for old in model['offers'] if key(old) != key(observed)
                             and now - datetime.fromisoformat(old['observed_at']) <= timedelta(hours=72)]
                    offers = sorted([observed, *prior], key=lambda offer: (offer['price'], offer['url']))
        updated.append({**model, 'offers': offers})
    return {**catalog, 'checkedAt': now.isoformat(), 'models': updated}, fresh, paired


def targets_for_catalog(catalog, legacy):
    by_gtin = {model['gtin']: model for model in catalog['models']}
    targets = {}
    for row in legacy:
        if row['gtin'] in by_gtin:
            if row['url'] in targets and targets[row['url']]['gtin'] != row['gtin']:
                raise ValueError('Monitor source URL is assigned to more than one barcode')
            targets[row['url']] = row
    for row in catalog.get('discoveryTargets', []):
        model = by_gtin.get(row['gtin'])
        if model is None or row['code'] != code_key(model['mpn']):
            raise ValueError('Discovered target does not identify a published monitor')
        if row['url'] in targets and targets[row['url']]['gtin'] != row['gtin']:
            raise ValueError('Monitor source URL is assigned to more than one barcode')
        targets.setdefault(row['url'], row)
    return list(targets.values())


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('targets', type=Path)
    cli.add_argument('catalog', type=Path)
    cli.add_argument('output', type=Path)
    cli.add_argument('--collection', choices=('site', 'mixed'), default='site')
    cli.add_argument('--report', type=Path)
    args = cli.parse_args()
    catalog = json.loads(args.catalog.read_text())
    gtins = {model['gtin'] for model in catalog['models']}
    if (len(gtins) < PILOT_SIZE or len(gtins) != len(catalog['models'])
            or any(retailer_gtin13(gtin) != gtin for gtin in gtins)):
        raise ValueError('Expected unique, valid GTINs for the published monitor catalog')
    targets = targets_for_catalog(catalog, json.loads(args.targets.read_text()))
    token = os.getenv('MONITOR_READER_OIDC')
    sessions = local()
    collector_slot = Lock()

    def read_document(url, **options):
        if not hasattr(sessions, 'client'):
            sessions.client = OfferIngestionClient('https://teknoskor.com', token, timeout=(5, 25), max_attempts=2)
        # Keep the production gateway to one large upstream document at a
        # time. Direct retailer readers still run independently.
        with collector_slot:
            return sessions.client.monitor_document_get(url, **options)

    started = datetime.now(timezone.utc)
    direct_hosts = {'www.mediamarkt.com.tr'} if args.collection == 'mixed' else ()
    groups, rejected = collect_observations(targets, read_document if token else None, direct_hosts=direct_hosts)
    snapshot, fresh, paired = refresh(catalog, groups, datetime.now(timezone.utc))
    discovery_gtins = {row['gtin'] for row in catalog.get('discoveryTargets', [])}
    report = {'fresh_models': fresh, 'fresh_pairs': paired, 'rejected': rejected,
              'discovery_models': [{'gtin': row['gtin'], 'mpn': row['mpn'],
                                    'fresh_offers': sum(datetime.fromisoformat(offer['observed_at']) >= started
                                                        for offer in row['offers'])}
                                   for row in snapshot['models'] if row['gtin'] in discovery_gtins]}
    payload = json.dumps(report, ensure_ascii=False)
    print(payload, flush=True)
    if args.report:
        args.report.write_text(payload + '\n', encoding='utf-8')
    if paired < CROSS_PLATFORM_MINIMUM:
        raise SystemExit(f'Refresh stopped: only {paired} models had two newly verified independent platform offers')
    args.output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
