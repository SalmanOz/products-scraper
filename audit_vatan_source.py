"""Read-only GitHub runner probe of three exact Vatan phone offers.

The sitemap supplies URL hints only. Every reported price and stock state must
come from the normal exact-PDP reader during this run. No catalog API, secret,
ingestion endpoint, or saved price is used.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir

from retailer_offer_refresh import RetailerOfferRefresher
from vatan_sitemap_source import VatanSitemapSource


PROFILES = (
    {'slug': 'apple-iphone-17', 'name': 'Apple iPhone 17',
     'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'},
    {'slug': 'xiaomi-17t', 'name': 'Xiaomi 17T',
     'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g'},
    {'slug': 'samsung-galaxy-s26', 'name': 'Samsung Galaxy S26',
     'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g'},
)
OUTPUT_DIR = Path(os.environ.get('VATAN_AUDIT_OUTPUT_DIR', gettempdir()))
REPORT_PATH = OUTPUT_DIR / 'vatan-source-audit.json'
SUMMARY_PATH = OUTPUT_DIR / 'vatan-source-audit.md'
PURCHASABLE = {'in_stock', 'limited'}


def run_audit(source=None, refresher=None):
    source = source if source is not None else VatanSitemapSource()
    refresher = refresher if refresher is not None else RetailerOfferRefresher()
    products = []
    for profile in PROFILES:
        specs = {key: profile[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
        row = {'slug': profile['slug'], 'name': profile['name'], 'expected_specs': specs,
               'candidate_urls': [], 'source_attempts': [], 'verified_offers': []}
        products.append(row)
        targets = []
        try:
            targets = source.discover(profile['name'], specs)
            row['candidate_urls'] = [target['source_url'] for target in targets]
            if targets:
                offers = refresher.refresh_existing_offer_targets(profile['name'], targets, specs)
                row['verified_offers'] = [
                    {key: offer[key] for key in ('merchant', 'price', 'currency',
                                                  'availability', 'source_title', 'url', 'observed_at')}
                    for offer in offers
                    if offer.get('merchant') == 'Vatan Bilgisayar'
                    and offer.get('availability') in PURCHASABLE
                ]
        except Exception as error:
            # Keep the user-facing artifact even when a source fails. Exception
            # types are sufficient diagnostics; response bodies never belong here.
            row['error'] = type(error).__name__
        finally:
            if targets:
                row['source_attempts'] = refresher.target_report(profile['name'], targets, specs)
        row['status'] = ('verified' if row['verified_offers'] else
                         'no_sitemap_candidate' if not row['candidate_urls'] else 'no_exact_offer')
    verified = sum(row['status'] == 'verified' for row in products)
    return {'purpose': 'read_only_runner_access_probe',
            'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'status':
            'passed' if verified == len(PROFILES) else 'partial' if verified else 'failed',
            'verified_products': verified, 'required_products': len(PROFILES),
            'sitemap': source.report(), 'products': products}


def format_summary(report):
    lines = ['## Vatan public PDP source audit',
             f"Status: {report['status']} — {report['verified_products']}/{report['required_products']} exact catalog profiles verified.",
             f"Sitemap: {report['sitemap']['status']}; eligible URLs: {report['sitemap']['eligible_urls']}."]
    for row in report['products']:
        prices = ', '.join(f"{offer['price']:,.2f} TRY ({offer['availability']})"
                           for offer in row['verified_offers'])
        reasons = ', '.join(sorted({attempt['reason'] for attempt in row['source_attempts']}))
        detail = prices or reasons or row.get('error') or 'no sitemap candidate'
        lines.append(f"- {row['slug']}: {row['status']}; {detail}")
    lines.append('Read-only: no catalog or price writes; sitemap prices are never used.')
    return '\n'.join(lines) + '\n'


def write_outputs(report, report_path=REPORT_PATH, summary_path=SUMMARY_PATH):
    report_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    summary_path.write_text(format_summary(report))


def main():
    report = run_audit()
    write_outputs(report, REPORT_PATH, SUMMARY_PATH)
    print(format_summary(report), end='')
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
