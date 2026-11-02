"""Read-only runner proof through the exact production MediaMarkt offer reader."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir

from mediamarkt_source import MediaMarktSource
from retailer_offer_refresh import RetailerOfferRefresher
from reviewed_offer_targets import MEDIAMARKT_BATCH_PATH


OUT = Path(os.environ.get('MEDIAMARKT_READER_AUDIT_OUTPUT_DIR', gettempdir()))


def run_audit(reader=None):
    reader = reader or RetailerOfferRefresher()
    rows = []
    for profile in json.loads(MEDIAMARKT_BATCH_PATH.read_text())['products']:
        targets = [{'source_url': url} for url in profile['targets']]
        specs = {key: profile[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
        offers = reader.refresh_existing_offer_targets(profile['name'], targets, specs)
        verified = [offer for offer in offers if offer.get('availability') in {'in_stock', 'limited'}]
        rows.append({
            'slug': profile['slug'], 'verified_purchasable': len(verified),
            'observed_offer': {key: verified[0].get(key) for key in
                               ('merchant', 'price', 'currency', 'availability', 'url')}
                              if verified else None,
            'source_attempts': reader.target_report(profile['name'], targets, specs),
        })
    return {'purpose': 'read_only_mediamarkt_production_reader_pilot',
            'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'total_products': len(rows),
            'verified_products': sum(bool(row['verified_purchasable']) for row in rows),
            'products': rows,
            'mediamarkt_source': reader.mediamarkt_source.report()}


def summary(report):
    source = report['mediamarkt_source']
    lines = ['## MediaMarkt selected first-party PDP reader (read only)',
             f"Verified {report['verified_products']}/{report['total_products']} reviewed variants; "
             f"robots {source['robots_status']}; browser GET {source['browser_html_responses']}/{source['browser_requests']}."]
    for row in report['products']:
        attempt = row['source_attempts'][0] if row['source_attempts'] else {}
        offer = row['observed_offer'] or {}
        lines.append(f"- {row['slug']}: {attempt.get('reason', 'not_attempted')} "
                     f"HTTP {attempt.get('http_status')}; verified cash {offer.get('price')}.")
    lines.append('No catalog, price, schema, R2, or IndexNow writes.')
    return '\n'.join(lines) + '\n'


def main():
    report = run_audit()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'mediamarkt-reader.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    text = summary(report)
    (OUT / 'mediamarkt-reader.md').write_text(text)
    print(text, end='')
    by_slug = {row['slug']: row for row in report['products']}
    return 0 if (by_slug['apple-iphone-17']['verified_purchasable'] >= 1
                 and by_slug['samsung-galaxy-a27-5g']['verified_purchasable'] == 0
                 and by_slug['samsung-galaxy-s23']['verified_purchasable'] == 0) else 1


if __name__ == '__main__':
    raise SystemExit(main())
