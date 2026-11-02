"""Read-only GitHub runner proof through the strict Apple Store offer reader."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir

from retailer_offer_refresh import RetailerOfferRefresher
from reviewed_offer_targets import daily_offer_targets


BATCH_PATH = Path(__file__).parent / 'offer_batches/apple-store-exact-2026-11-02.json'
OUT = Path(os.environ.get('APPLE_STORE_READER_AUDIT_OUTPUT_DIR', gettempdir()))


def run_audit(reader=None):
    reader = reader or RetailerOfferRefresher()
    rows = []
    for profile in json.loads(BATCH_PATH.read_text())['products']:
        url = profile['targets'][0]
        specs = {key: profile[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
        product = {key: profile[key] for key in ('id', 'slug', 'name')}
        seeded = [target for target in daily_offer_targets(product, specs)
                  if target['merchant_name'] == 'Apple Store']
        offers = reader.refresh_existing_offer_targets(profile['name'], seeded, specs)
        offer = offers[0] if len(offers) == 1 else None
        verified = bool(offer and offer.get('availability') == 'in_stock'
                        and offer.get('url') == url)
        attempts = reader.target_report(profile['name'], seeded, specs)
        attempt = attempts[0] if len(attempts) == 1 else {}
        rows.append({'slug': profile['slug'],
                     'reason': attempt.get('reason', 'not_attempted'),
                     'http_status': attempt.get('http_status'),
                     'verified': verified,
                     'offer': {key: offer.get(key) for key in
                               ('merchant', 'price', 'currency', 'availability', 'url')}
                              if verified else None})
    return {'purpose': 'read_only_apple_store_integrated_production_reader_pilot',
            'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'verified_products': sum(row['verified'] for row in rows),
            'total_products': len(rows), 'products': rows,
            'apple_store_source': reader.apple_store_source.report()}


def summary(report):
    source = report['apple_store_source']
    lines = ['## Apple Store selected first-party PDP reader (read only)',
             f"Verified {report['verified_products']}/{report['total_products']} exact variants; "
             f"robots {source['robots_status']}; browser GET "
             f"{source['browser_html_responses']}/{source['browser_requests']}."]
    for row in report['products']:
        offer = row['offer'] or {}
        lines.append(f"- {row['slug']}: {row['reason']} HTTP {row['http_status']}; "
                     f"verified cash {offer.get('price')} TRY.")
    lines.append('No catalog, price, schema, R2, or IndexNow writes.')
    return '\n'.join(lines) + '\n'


def main():
    report = run_audit()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'apple-store-reader.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    text = summary(report)
    (OUT / 'apple-store-reader.md').write_text(text)
    print(text, end='')
    return 0 if report['verified_products'] == report['total_products'] == 4 else 1


if __name__ == '__main__':
    raise SystemExit(main())
