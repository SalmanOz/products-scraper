"""Merge verified additions into the current private site catalog."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import shutil
import sys
import unicodedata
from urllib.parse import urlsplit

from PIL import Image

from laptop_price_stage import targets_for_catalog as laptop_targets
from monitor_refresh import targets_for_catalog as monitor_targets
from monitor_pilot import MANUFACTURER_SPECS, group_targets
from monitor_sources import code_key
from pc_parts_multistore import GAMING, offer_host
from retailer_catalog_feeds import FEEDS
from retailer_barcode import retailer_gtin13


def read(path):
    return json.loads(path.read_bytes())


def key(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', value.casefold()) if c.isalnum())


def identities(model):
    # MPNs are scoped to the maker so unrelated brands can reuse a code.
    result = [('slug', key(model['slug']))]
    if model.get('gtin'):
        result.append(('gtin', model['gtin']))
    if model.get('mpn'):
        result.append(('mpn', key(model['brand']), key(model['mpn'])))
    else:
        result.append(('name', key(model['name'])))
    return result


def fresh_offers(offers, timestamp, now, merchant='merchant', minimum=2):
    if len(offers) < minimum or len({o['platform'] for o in offers}) < minimum:
        return False
    if len({key(o[merchant]) for o in offers}) != len(offers) or len({o['url'] for o in offers}) != len(offers):
        return False
    return all(0 < o['price'] <= 1_000_000 and urlsplit(o['url']).scheme == 'https' and
               timedelta(0) <= now - datetime.fromisoformat(o[timestamp]) < timedelta(hours=1) for o in offers)


class Expansion:
    def __init__(self, frontend, reports, output):
        self.frontend, self.reports, self.output = frontend, reports, output
        self.now = datetime.now(timezone.utc)
        self.stamp = self.now.isoformat()
        self.files = []
        self.products = []
        self.duplicates = 0
        self.known = set()
        self.urls = set()
        for name, collection in [('monitorPilot', 'models'), ('laptopPilot', 'models'),
                                 ('pcParts', 'parts'), ('twsProductCatalog', 'models')]:
            catalog = self.catalog(name)
            self.urls.update(row['url'] for row in catalog.get('discoveryTargets', []))
            for model in catalog[collection]:
                self.known.update(identities(model))
                self.urls.update(o['url'] for o in model.get('offers', []))
                self.urls.update(model.get('offerTargets', []))
        snapshot = self.catalog('twsOfferSnapshot')
        self.urls.update(row['url'] for rows in snapshot.get('sourceTargets', {}).values() for row in rows)
        for model in snapshot['models']:
            if model.get('gtin'):
                self.known.add(('gtin', model['gtin']))
            self.urls.update(o['url'] for o in model['offers'])

    def catalog(self, name):
        return read(self.frontend / 'src/data' / f'{name}.json')

    def snapshot(self, name, data):
        path = Path('src/data') / f'{name}.json'
        self.files.append(path.as_posix())
        (self.output / path.name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')

    def accept(self, model, media, source, offers, urls):
        ids = identities(model)
        image_path = Path('public/media') / media / source.name
        if self.known.intersection(ids) or self.urls.intersection(urls) or (self.frontend / image_path).exists():
            self.duplicates += 1
            return False
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', model['slug']):
            raise ValueError('Invalid staged product slug')
        with Image.open(source) as image:
            if image.format != 'WEBP' or min(image.size) < 180 or source.stat().st_size <= 1000:
                raise ValueError('Invalid staged product image')
            image.verify()
        destination = self.frontend / image_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        self.known.update(ids)
        self.urls.update(urls)
        self.files.append(image_path.as_posix())
        self.products.append({'category': media, 'slug': model['slug'],
                              'offers': [{'url': o['url'], 'price': o['price']} for o in offers]})
        return True

    def accessories(self):
        root = self.reports / 'accessories'
        report = read(root / 'report.json')
        for kind, name, media in [('monitor', 'monitorPilot', 'monitors'), ('laptop', 'laptopPilot', 'laptops')]:
            catalog = self.catalog(name)
            targets = {r['url']: r for r in catalog.get('discoveryTargets', [])}
            added = []
            for bundle in report['categories'][kind]['staged']:
                model = bundle['model']
                offers = model['offers']
                feed_verified = (len(offers) == 1 and bundle.get('feedGtin') == model['gtin']
                                 and offer_host(offers[0]['url']) in FEEDS)
                if (not fresh_offers(offers, 'observed_at', self.now, minimum=1 if feed_verified else 2)
                        or retailer_gtin13(model['gtin']) != model['gtin']):
                    continue
                rows = bundle['targets']
                if kind == 'monitor' and any(r['code'] != code_key(model['mpn']) for r in rows):
                    profile = MANUFACTURER_SPECS.get(model['gtin'], {})
                    code = profile.get('code')
                    if (profile.get('model'), profile.get('brand'), {r['code'] for r in rows}) != (
                            model['mpn'], model['brand'], {code}):
                        continue
                    model['mpn'] = code.upper()
                    next(s for s in model['specs'] if s['label'] == 'Model kodu')['value'] = model['mpn']
                if any(r['gtin'] != model['gtin'] or r['url'] in targets for r in rows):
                    self.duplicates += 1
                    continue
                urls = {r['url'] for r in rows}
                if not {o['url'] for o in offers} <= urls:
                    raise ValueError('Staged accessory offers have no refresh targets')
                if self.accept(model, media, root / kind / (model['slug'] + '.webp'), offers, urls):
                    targets.update((r['url'], r) for r in rows)
                    added.append(model)
            if not added:
                continue
            expanded = {**catalog, 'checkedAt': self.stamp, 'models': [*catalog['models'], *added],
                        'discoveryTargets': list(targets.values())}
            if kind == 'monitor':
                routes = monitor_targets(expanded, read(Path('monitor_pilot_targets.json')))
                group_targets(routes, len(routes))
            else:
                laptop_targets(expanded)
            self.snapshot(name, expanded)

    def components(self):
        root = self.reports / 'components'
        name = 'pcParts'
        catalog = self.catalog(name)
        added = []
        for model in read(root / 'report.json')['staged']:
            feed_verified = model.pop('feedGtin', None) == model['gtin'] and offer_host(model['sourceUrl']) == GAMING
            offers = model['offers']
            if (retailer_gtin13(model['gtin']) == model['gtin'] and fresh_offers(offers, 'checkedAt', self.now, 'seller', minimum=1 if feed_verified else 2)
                    and self.accept(model, 'pc-parts', root / 'photos' / (model['gtin'] + '.webp'),
                                    offers, set(model['offerTargets']))):
                added.append(model)
        if added:
            self.snapshot(name, {**catalog, 'checkedAt': self.stamp, 'parts': [*catalog['parts'], *added]})

    def earbuds(self):
        root = self.reports / 'earbud-expansion'
        qualified = read(self.reports / 'earbud-expansion-report.json')['qualified']
        if not qualified:
            return
        catalog_name, offer_name = 'twsProductCatalog', 'twsOfferSnapshot'
        catalog, snapshot = self.catalog(catalog_name), self.catalog(offer_name)
        staged_catalog, staged_snapshot = (read(root / (name + '.json')) for name in (catalog_name, offer_name))
        products, prices = [], []
        targets = dict(snapshot.get('sourceTargets', {}))
        for model in staged_catalog['models']:
            if model['name'] not in qualified:
                continue
            if self.known.intersection(identities(model)):
                self.duplicates += 1
                continue
            variants = [r for r in staged_snapshot['models'] if r['name'] == model['name']]
            selected, selected_targets, urls, barcodes = [], {}, set(), set()
            for row in variants:
                gtin, offers = row['gtin'], row['offers']
                catalog_key = f"{model['name']}/{gtin}" if len(variants) > 1 else model['name']
                routes = staged_snapshot['sourceTargets'][catalog_key]
                route_urls = {r['url'] for r in routes}
                if (('gtin', gtin) in self.known or gtin in barcodes or self.urls.intersection(route_urls)
                        or urls.intersection(route_urls) or catalog_key in targets):
                    self.duplicates += 1
                    continue
                if (not gtin or retailer_gtin13(gtin) != gtin or not fresh_offers(offers, 'observed_at', self.now)
                        or not {o['url'] for o in offers} <= route_urls):
                    continue
                selected.append(row)
                selected_targets[catalog_key] = routes
                urls.update(route_urls)
                barcodes.add(gtin)
            if selected and self.accept(model, 'earbuds', root / 'earbuds' / (model['slug'] + '.webp'),
                                        [o for r in selected for o in r['offers']], urls):
                for gtin in barcodes:
                    self.known.add(('gtin', gtin))
                products.append(model)
                prices.extend(selected)
                if len(selected) == 1:
                    targets[model['name']] = next(iter(selected_targets.values()))
                else:
                    targets.update(selected_targets)
        if products:
            self.snapshot(catalog_name, {**catalog, 'reviewedAt': self.stamp, 'models': [*catalog['models'], *products]})
            self.snapshot(offer_name, {**snapshot, 'checked_at': self.stamp, 'models': [*snapshot['models'], *prices],
                                       'sourceTargets': targets})


def main():
    frontend, reports, output = map(Path, sys.argv[1:])
    output.mkdir(parents=True, exist_ok=True)
    expansion = Expansion(frontend, reports, output)
    expansion.accessories()
    expansion.components()
    expansion.earbuds()
    (output / 'manifest.json').write_text(json.dumps({'files': expansion.files, 'products': expansion.products,
                                                     'duplicates': expansion.duplicates}))


if __name__ == '__main__':
    main()
