"""Stage verified price refreshes for the published laptop configurations."""

import argparse
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

from catalog_source_collector import catalog_document_reader
from competitor_catalog_gaps import write_json
from laptop_pilot import _public_offers, collect_offers, compatible, select_offers


def targets_for_catalog(catalog):
    models = {(row['mpn'], row['gtin']): row for row in catalog['models']}
    if len(models) != len(catalog['models']) or len({gtin for _, gtin in models}) != len(models):
        raise ValueError('Duplicate laptop model or barcode')
    targets = {}

    def add(url, mpn, gtin):
        if (mpn, gtin) not in models:
            raise ValueError('Target does not identify a published laptop configuration')
        target = {'url': url, 'mpn': mpn, 'gtin': gtin}
        if targets.setdefault(url, target) != target:
            raise ValueError('Laptop URL is assigned to multiple configurations')

    for (mpn, gtin), model in models.items():
        for offer in model['offers']:
            add(offer['url'], mpn, gtin)
    for target in catalog.get('discoveryTargets', []):
        add(target['url'], target['mpn'], target['gtin'])
    return list(targets.values())


def stage(catalog, observations):
    models, refreshed, counts = [], [], Counter()
    for model in catalog['models']:
        key = model['mpn'], model['gtin']
        expected = {'facts': model, 'gtin13': key[1]}
        matching = [offer for offer in observations.get(key, ()) if compatible(offer, expected)]
        offers = select_offers(matching)
        platforms = {offer['platform'] for offer in offers}
        if len(platforms) >= 2:
            offers = _public_offers(offers)
            models.append({**model, 'offers': offers})
            refreshed.append({'slug': model['slug'], 'gtin': key[1], 'offers': len(offers),
                              'platforms': sorted(platforms)})
        else:
            models.append(model)
            counts['second_platform_missing' if matching else 'no_fresh_offer'] += 1
    result = ({**catalog, 'checkedAt': datetime.now(timezone.utc).isoformat(), 'models': models}
              if refreshed else catalog)
    return result, {'staged': refreshed, 'remaining': dict(counts), 'publishedModels': len(models)}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('catalog', type=Path)
    cli.add_argument('output', type=Path)
    args = cli.parse_args()
    if args.catalog.resolve() == (args.output / 'staged-laptop-catalog.json').resolve():
        cli.error('Stage into a separate output directory')
    payload = args.catalog.read_bytes()
    catalog = json.loads(payload)
    targets = targets_for_catalog(catalog)
    observations, failures = collect_offers(targets, 1, n11_document_reader=catalog_document_reader('accessory-discovery'))
    staged, report = stage(catalog, observations)
    if staged is not catalog:
        displayed = {offer['url'] for model in staged['models'] for offer in model['offers']}
        retained = {target['url'] for target in catalog.get('discoveryTargets', [])}
        staged['discoveryTargets'] = [target for target in targets
                                      if target['url'] not in displayed or target['url'] in retained]
    report.update(baseCatalogSha256=sha256(payload).hexdigest(), configuredTargets=len(targets),
                  verifiedOffers=sum(map(len, observations.values())), rejections=dict(failures))
    write_json(args.output / 'staged-laptop-report.json', report)
    write_json(args.output / 'staged-laptop-catalog.json', staged)
    print(json.dumps({'laptopsStaged': len(report['staged']), 'laptopsInCatalog': report['publishedModels'],
                      'laptopTargets': len(targets), 'laptopOffersVerified': report['verifiedOffers'],
                      'remaining': report['remaining']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
