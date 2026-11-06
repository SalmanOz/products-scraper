"""Export a fresh read-only TWS audit to the isolated public catalog."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from priority_retailer_transport import PriorityRetailerTransport
from retailer_barcode import retailer_gtin13
from tws_offer_pilot import MODEL_NAMES, independent_sellers, model_name, selected_tws_offer
from tws_retailer_sources import selected_n11_tws

PLATFORMS = {
    'Trendyol': 'www.trendyol.com',
    'n11': 'www.n11.com',
    'MediaMarkt': 'www.mediamarkt.com.tr',
    'Vatan Bilgisayar': 'www.vatanbilgisayar.com',
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
    else:
        current, reason = selected_n11_tws(html, resolved, name, model_name)
        selector = 'magaza'
    if current is None:
        return None, reason
    if (parse_qs(urlparse(resolved).query).get(selector) != parse_qs(urlparse(url).query).get(selector)
            or any(current.get(key) != offer.get(key) for key in ('platform', 'merchant_id', 'merchant', 'gtin13'))):
        return None, 'snapshot_selected_seller_or_gtin_changed'
    return current, None


def public_snapshot(report):
    if (report.get('mode') != 'readonly_tws_offer_pilot' or report.get('writes') is not False
            or report.get('publication_gate_passed') is not True
            or [row.get('model') for row in report['models']] != list(MODEL_NAMES)):
        raise ValueError('A successful audit of the full 25-model cohort is required')

    now = datetime.now(timezone.utc)
    failures = Counter()
    verified = {name: [] for name in MODEL_NAMES}
    candidates = {platform: [] for platform in PLATFORMS}
    seen_gtins = set()
    for row in report['models']:
        name, offers = row['model'], row['offers']
        gtins = {offer['gtin13'] for offer in offers}
        if len(offers) > 2 or len(gtins) > 1 or seen_gtins.intersection(gtins):
            raise ValueError(f'Ambiguous selected GTIN for {name}')
        seen_gtins.update(gtins)
        for offer in offers:
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
            candidates[platform].append((name, offer))

    def reread(rows):
        transport = PriorityRetailerTransport(maximum_per_host=55)
        return [(name, *selected_marketplace_offer(transport, name, offer)) for name, offer in rows]

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(reread, candidates[platform]) for platform in ('Trendyol', 'n11')]
        for job in jobs:
            for name, offer, reason in job.result():
                if offer is None:
                    failures[reason] += 1
                else:
                    verified[name].append(offer)

    for platform in ('MediaMarkt', 'Vatan Bilgisayar'):
        for name, offer in candidates[platform]:
            # Direct storefront links have already selected the same first-party
            # merchant and SKU in this run. The audit report omits tax identifiers.
            verified[name].append({**offer, '_legal_name': platform.lower()})

    rows = []
    for name, offers in verified.items():
        offers.sort(key=lambda offer: offer['price'])
        if len(offers) == 2 and not independent_sellers(*offers):
            failures['snapshot_seller_identity_collision'] += 1
            offers = offers[:1]
        rows.append({
            'name': name,
            'gtin': offers[0]['gtin13'] if offers else None,
            'offers': [{
                'platform': offer['platform'], 'merchant': offer['merchant'], 'url': offer['url'],
                'price': offer['price'], 'basket': offer.get('price_kind') == 'basket',
                'observed_at': offer['observed_at'],
            } for offer in offers],
        })
    pair_count = sum(len(row['offers']) == 2 for row in rows)
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
    args = parser.parse_args()
    data, counts = public_snapshot(json.loads(args.audit.read_text(encoding='utf-8')))
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(counts, ensure_ascii=False))


if __name__ == '__main__':
    main()
