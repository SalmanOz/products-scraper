"""Read-only runner probe of stock-card discovery and selected cash PDPs."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import gettempdir

from tr_price_scraper import TRPriceScraper
from turk_telekom_source import TurkTelekomSource


PROFILES = (
    {'slug': 'samsung-galaxy-a57-5g', 'name': 'Samsung Galaxy A57 5G',
     'ram_gb': 8, 'storage_gb': 128, 'network_generation': '5g'},
    {'slug': 'samsung-galaxy-s26', 'name': 'Samsung Galaxy S26',
     'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g'},
    {'slug': 'xiaomi-17t', 'name': 'Xiaomi 17T',
     'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g'},
)
OUTPUT_DIR = Path(os.environ.get('TT_AUDIT_OUTPUT_DIR', gettempdir()))


def run_audit(source=None):
    source = source if source is not None else TurkTelekomSource()
    matcher = TRPriceScraper()
    products = []
    for profile in PROFILES:
        specs = {key: profile[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
        row = {'slug': profile['slug'], 'expected_specs': specs,
               'candidate_urls': [], 'source_attempts': [], 'verified_offers': []}
        products.append(row)
        try:
            targets = source.discover(profile['name'], specs)
            row['candidate_urls'] = [target['source_url'] for target in targets]
            for target in targets:
                offer, reason, http_status = source.read_offer(target['source_url'])
                row['source_attempts'].append({'url': target['source_url'],
                                               'reason': reason, 'http_status': http_status})
                if (offer and offer['availability'] == 'in_stock'
                        and matcher.has_observed_offer_evidence(profile['name'], offer, specs)):
                    row['verified_offers'].append({
                        key: offer[key] for key in ('merchant', 'price', 'currency',
                                                     'availability', 'source_title',
                                                     'url', 'observed_at')})
        except Exception as error:
            row['error'] = type(error).__name__
        row['status'] = ('verified' if row['verified_offers'] else
                         'no_stock_card' if not row['candidate_urls'] else 'no_exact_offer')
    verified = sum(row['status'] == 'verified' for row in products)
    return {'purpose': 'read_only_runner_access_probe',
            'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'status': 'passed' if verified == len(PROFILES) else 'partial' if verified else 'failed',
            'verified_products': verified, 'required_products': len(PROFILES),
            'source': source.report(), 'products': products}


def format_summary(report):
    lines = ['## Türk Telekom public PDP access audit',
             f"Status: {report['status']} — {report['verified_products']}/{report['required_products']} exact profiles.",
             f"Source: {report['source']['status']}; robots {report['source']['robots_status']}; "
             f"stock cards {report['source']['stock_card_urls']}."]
    for row in report['products']:
        offers = ', '.join(f"{offer['price']:,.2f} TRY ({offer['availability']})"
                           for offer in row['verified_offers'])
        reasons = ', '.join(attempt['reason'] for attempt in row['source_attempts'])
        lines.append(f"- {row['slug']}: {row['status']}; {offers or reasons or row.get('error') or 'no stock card'}")
    lines.append('Read-only: no secrets, catalog calls or price writes; category cards supply URLs only.')
    return '\n'.join(lines) + '\n'


def main():
    report = run_audit()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / 'turk-telekom-source-audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    (OUTPUT_DIR / 'turk-telekom-source-audit.md').write_text(format_summary(report))
    print(format_summary(report), end='')
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
