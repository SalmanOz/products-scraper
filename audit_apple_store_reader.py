"""Read-only GitHub runner proof through the strict Apple Store offer reader."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir

from apple_store_source import AppleStoreSource
from tr_price_scraper import TRPriceScraper


BATCH_PATH = Path(__file__).parent / 'offer_batches/apple-store-exact-2026-11-02.json'
OUT = Path(os.environ.get('APPLE_STORE_READER_AUDIT_OUTPUT_DIR', gettempdir()))


def run_audit(source=None):
    source = source or AppleStoreSource()
    matcher = TRPriceScraper()
    rows = []
    for profile in json.loads(BATCH_PATH.read_text())['products']:
        url = profile['targets'][0]
        offer, reason, status = source.read_offer(url)
        specs = {key: profile[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
        verified = bool(offer and matcher.has_observed_offer_evidence(
            profile['name'], offer, specs))
        rows.append({'slug': profile['slug'], 'reason': reason, 'http_status': status,
                     'verified': verified,
                     'offer': {key: offer.get(key) for key in
                               ('merchant', 'price', 'currency', 'availability', 'url')}
                              if verified else None})
    return {'purpose': 'read_only_apple_store_strict_production_reader_pilot',
            'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'verified_products': sum(row['verified'] for row in rows),
            'total_products': len(rows), 'products': rows,
            'apple_store_source': source.report()}


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
