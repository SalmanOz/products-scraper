"""Read-only CI pilot of six reviewed Vatan SKU URLs through the daily reader."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir

from retailer_offer_refresh import RetailerOfferRefresher
from reviewed_offer_targets import daily_offer_targets
from vatan_reviewed_source import reviewed_profiles


OUTPUT_DIR = Path(os.environ.get('VATAN_REVIEWED_AUDIT_OUTPUT_DIR', gettempdir()))
REPORT_PATH = OUTPUT_DIR / 'vatan-reviewed-source.json'
SUMMARY_PATH = OUTPUT_DIR / 'vatan-reviewed-source.md'
PURCHASABLE = {'in_stock', 'limited'}


def run_audit(reader=None):
    reader = reader or RetailerOfferRefresher()
    rows = []
    for profile in reviewed_profiles():
        product = {key: profile[key] for key in ('id', 'slug', 'name')}
        specs = {key: profile[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
        targets = [target for target in daily_offer_targets(product, specs)
                   if target['merchant_name'] == 'Vatan Bilgisayar']
        offers = reader.refresh_existing_offer_targets(profile['name'], targets, specs)
        verified = [offer for offer in offers if offer.get('availability') in PURCHASABLE]
        rows.append({
            'slug': profile['slug'], 'targets': targets,
            'verified_purchasable': len(verified),
            'observed_offer': {key: verified[0].get(key) for key in
                               ('merchant', 'price', 'currency', 'availability', 'url')}
                              if verified else None,
            'source_attempts': reader.target_report(profile['name'], targets, specs),
        })
    return {
        'purpose': 'read_only_reviewed_vatan_production_reader_pilot',
        'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'verified_products': sum(bool(row['verified_purchasable']) for row in rows),
        'total_products': len(rows), 'products': rows,
        'vatan_reviewed_pdp': reader.vatan_reviewed_source.report(),
    }


def format_summary(report):
    count = report['verified_products']
    total = report['total_products']
    source = report['vatan_reviewed_pdp']
    lines = ['## Vatan reviewed exact-SKU PDP audit (read only)',
             f"Verified {count}/{total}; robots {source['robots_status']}; "
             f"browser GET {source['browser_requests']}/{source['reviewed_urls']} reviewed URLs."]
    for row in report['products']:
        attempt = row['source_attempts'][0] if row['source_attempts'] else {}
        lines.append(f"- {row['slug']}: {row['verified_purchasable']} purchasable; "
                     f"{attempt.get('reason', 'not_attempted')} HTTP {attempt.get('http_status')}.")
    lines.append('No catalog, price, schema, R2, or IndexNow writes.')
    return '\n'.join(lines) + '\n'


def main():
    report = run_audit()
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    SUMMARY_PATH.write_text(format_summary(report))
    print(format_summary(report), end='')
    return 0 if report['verified_products'] == report['total_products'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
