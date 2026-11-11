"""Export a fresh read-only TWS audit to the isolated public catalog."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from laptop_pilot import _hepsiburada_public_get
from priority_retailer_transport import PriorityRetailerTransport
from retailer_barcode import retailer_gtin13
from tws_offer_pilot import (
    MODEL_NAMES, PAIR_REQUIRED_MODEL_NAMES, PILOT_MODEL_NAMES, TWS_PRICE_RATIO_BY_MODEL,
    model_name, select_sku_offers, selected_tws_offer,
)
from tws_retailer_sources import selected_hepsiburada_tws, selected_n11_tws

PLATFORMS = {
    'Trendyol': 'www.trendyol.com',
    'n11': 'www.n11.com',
    'MediaMarkt': 'www.mediamarkt.com.tr',
    'Vatan Bilgisayar': 'www.vatanbilgisayar.com',
    'Hepsiburada': 'www.hepsiburada.com',
}


def selected_marketplace_offer(transport, name, offer):
    platform = offer['platform']
    url = offer['url']
    if platform == 'n11' and not parse_qs(urlparse(url).query).get('magaza'):
        html, reason, _ = transport.read_fresh_document(url)
        if html is None:
            return None, reason
        current, reason = selected_n11_tws(html, transport.resolved_url(url), name, model_name)
        if current is None:
            return None, reason
        if any(current.get(key) != offer.get(key) for key in ('merchant_id', 'merchant', 'gtin13')):
            return None, 'snapshot_selected_seller_or_gtin_changed'
        url = current['url']

    # A Trendyol canonical PDP proves its current winner directly. Appending a
    # seller parameter to these pages redirects back to the same canonical PDP.
    html, reason, _ = transport.read_fresh_document(url)
    if html is None:
        return None, reason
    resolved = transport.resolved_url(url)
    if platform == 'Trendyol':
        current, reason = selected_tws_offer(html, resolved, name)
        selector = 'merchantId'
    elif platform == 'Hepsiburada':
        if resolved != url:
            return None, 'snapshot_hepsiburada_sku_changed'
        current, reason = selected_hepsiburada_tws(html, resolved, name, model_name)
        selector = 'magaza'
    else:
        current, reason = selected_n11_tws(html, resolved, name, model_name)
        selector = 'magaza'
    if current is None:
        return None, reason
    if (parse_qs(urlparse(resolved).query).get(selector) != parse_qs(urlparse(url).query).get(selector)
            or any(current.get(key) != offer.get(key) for key in ('platform', 'merchant_id', 'merchant', 'gtin13'))):
        return None, 'snapshot_selected_seller_or_gtin_changed'
    return current, None


def public_snapshot(report, catalog_names=MODEL_NAMES):
    published = set(catalog_names)
    if not set(PILOT_MODEL_NAMES) <= published <= set(MODEL_NAMES):
        raise ValueError('The catalog must contain the original models and only verified cohort candidates')
    if (report.get('mode') != 'readonly_tws_offer_pilot' or report.get('writes') is not False
            or report.get('publication_gate_passed') is not True
            or [row.get('model') for row in report['models']] != list(MODEL_NAMES)):
        raise ValueError('A successful audit of the full configured cohort is required')

    now = datetime.now(timezone.utc)
    failures = Counter()
    verified = {name: [] for name in MODEL_NAMES if name in published}
    primary = {platform: [] for platform in PLATFORMS}
    backup = {platform: [] for platform in PLATFORMS}
    owners = {}
    for row in report['models']:
        name, offers = row['model'], row['offers']
        if name not in published:
            continue
        gtins = {offer['gtin13'] for offer in offers}
        if len(offers) > 3 or len(gtins) > 1:
            raise ValueError(f'Ambiguous selected GTIN for {name}')
        seen = set()
        alternatives = row.get('alternatives', [])[:8]
        for offer, queue in [*((o, primary) for o in offers), *((o, backup) for o in alternatives)]:
            platform = offer['platform']
            route = urlparse(offer['url'])
            observed = datetime.fromisoformat(offer['observed_at'])
            if (retailer_gtin13(offer['gtin13']) != offer['gtin13'] or platform not in PLATFORMS
                    or route.scheme != 'https' or route.netloc != PLATFORMS[platform]
                    or route.fragment or offer.get('model') not in (None, name)
                    or offer.get('currency') != 'TRY' or offer.get('condition') != 'new'
                    or offer.get('availability') != 'in_stock' or not 0 < offer['price'] <= 1_000_000
                    or not now - timedelta(hours=3) <= observed <= now):
                raise ValueError(f'Invalid or stale selected offer for {name}')
            if platform in ('MediaMarkt', 'Vatan Bilgisayar'):
                source_title = f"{offer.get('source_title', '')} {offer.get('retailer_model_code', '')}"
                if model_name(source_title, name.split()[0]) != name:
                    failures['snapshot_storefront_model_mismatch'] += 1
                    continue
            if owners.setdefault(offer['gtin13'], name) != name:
                raise ValueError(f'GTIN also observed for a different model: {name}')
            key = (platform, offer['url'], offer['gtin13'])
            if key not in seen:
                seen.add(key)
                queue[platform].append((name, offer))

    platforms = ('Trendyol', 'n11', 'Hepsiburada')
    transports = {platform: PriorityRetailerTransport(maximum_per_host=135) for platform in platforms}
    transports['Hepsiburada'] = PriorityRetailerTransport(maximum_per_host=12, get=_hepsiburada_public_get,
                                                         browser_fallback=False)

    def reread(platform, rows):
        transport = transports[platform]
        return [(name, *selected_marketplace_offer(transport, name, offer)) for name, offer in rows]

    def verify(queue, needed):
        with ThreadPoolExecutor(max_workers=len(platforms)) as pool:
            jobs = [pool.submit(reread, platform, [(name, offer) for name, offer in queue[platform]
                                                   if name in needed]) for platform in platforms]
            for job in jobs:
                for name, offer, reason in job.result():
                    if offer is None:
                        failures[reason] += 1
                    else:
                        verified[name].append(offer)
        for platform in ('MediaMarkt', 'Vatan Bilgisayar'):
            for name, offer in queue[platform]:
                if name in needed:
                    # The live audit already proved the first-party seller on this URL.
                    verified[name].append({**offer, '_legal_name': platform.lower()})

    verify(primary, published)
    verify(backup, {name for name, offers in verified.items() if len(select_sku_offers(offers, min_price_ratio=TWS_PRICE_RATIO_BY_MODEL.get(name, 0))) < 2})

    rows = []
    for name, observations in verified.items():
        offers = select_sku_offers(observations, min_price_ratio=TWS_PRICE_RATIO_BY_MODEL.get(name, 0))
        if name in PAIR_REQUIRED_MODEL_NAMES and len(offers) < 2:
            failures['snapshot_independent_pair_missing'] += 1
            continue
        if not offers and name not in PILOT_MODEL_NAMES:
            continue
        rows.append({
            'name': name,
            'gtin': offers[0]['gtin13'] if offers else None,
            'offers': [{
                'platform': offer['platform'], 'merchant': offer['merchant'], 'url': offer['url'],
                'price': offer['price'], 'basket': offer.get('price_kind') == 'basket',
                'observed_at': offer['observed_at'],
            } for offer in offers],
        })
    pair_count = sum(len(row['offers']) >= 2 for row in rows)
    priced_count = sum(bool(row['offers']) for row in rows)
    if pair_count < 15 or priced_count < 20:
        raise ValueError(f'Publication gate failed on readback: {pair_count} pairs, {priced_count} priced; {dict(failures)}')
    return {
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'models': rows,
    }, {'paired': pair_count, 'priced': priced_count, 'rejections': dict(failures)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('audit', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--catalog', type=Path, required=True,
                        help='Only publish models that have a product detail record')
    args = parser.parse_args()
    catalog = json.loads(args.catalog.read_text(encoding='utf-8'))
    data, counts = public_snapshot(json.loads(args.audit.read_text(encoding='utf-8')),
                                  [row['name'] for row in catalog['models']])
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(counts, ensure_ascii=False))


if __name__ == '__main__':
    main()
