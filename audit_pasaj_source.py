"""Read-only reviewed Pasaj PDP audit; no catalog update or price write."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import gettempdir

from pasaj_source import A26_6_128_URL, IPHONE16_128_URL, IPHONE17_256_URL, PasajSource
from retailer_offer_refresh import RetailerOfferRefresher


OUTPUT_DIR = Path(os.environ.get('PASAJ_AUDIT_OUTPUT_DIR', gettempdir()))
PROFILES = (
    {'name': 'Apple iPhone 17', 'ram_gb': 8, 'storage_gb': 256,
     'network_generation': '5g', 'url': IPHONE17_256_URL},
    {'name': 'Apple iPhone 16', 'ram_gb': 8, 'storage_gb': 128,
     'network_generation': '5g', 'url': IPHONE16_128_URL},
    {'name': 'Samsung Galaxy A26 5G', 'ram_gb': 6, 'storage_gb': 128,
     'network_generation': '5g', 'url': A26_6_128_URL},
)


def run_audit(source=None):
    source = source or PasajSource()
    refresher = RetailerOfferRefresher(pasaj_source=source)
    products = []
    for profile in PROFILES:
        specs = {key: profile[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
        targets = [{'source_url': profile['url']}]
        offers = refresher.refresh_existing_offer_targets(profile['name'], targets, specs)
        attempts = refresher.target_report(profile['name'], targets, specs)
        products.append({'name': profile['name'], 'expected_specs': specs,
                         'source_attempts': attempts, 'verified_offers': offers,
                         'status': 'verified' if len(offers) == 1 else 'no_exact_offer'})
    verified = sum(row['status'] == 'verified' for row in products)
    return {'purpose': 'read_only_public_pasaj_pdp_pilot',
            'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'status': 'passed' if verified == len(PROFILES) else 'partial' if verified else 'failed',
            'verified_products': verified, 'required_products': len(PROFILES),
            'source': source.report(), 'products': products}


def format_summary(report):
    lines = ['## Turkcell Pasaj public PDP pilot',
             f"Status: {report['status']} ({report['verified_products']}/{report['required_products']}); "
             f"PDP requests {report['source']['pdp_requests']}."]
    for row in report['products']:
        for attempt in row['source_attempts']:
            lines.append(f"- {row['name']}: {attempt['reason']} (HTTP {attempt['http_status']}).")
        for offer in row['verified_offers']:
            lines.append(f"  {offer['price']:,.2f} TRY, {offer['availability']}, {offer['observed_at']}.")
    lines.append('Read-only: three reviewed PDPs; no secrets, catalog calls or price writes.')
    return '\n'.join(lines) + '\n'


def main():
    report = run_audit()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / 'pasaj-source-audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    (OUTPUT_DIR / 'pasaj-source-audit.md').write_text(format_summary(report))
    print(format_summary(report), end='')
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
