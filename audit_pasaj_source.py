"""Read-only exact-PDP pilot; no catalog update or price write."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import gettempdir

from pasaj_source import IPHONE17_256_URL, PasajSource
from retailer_offer_refresh import RetailerOfferRefresher


OUTPUT_DIR = Path(os.environ.get('PASAJ_AUDIT_OUTPUT_DIR', gettempdir()))
PROFILE = {'name': 'Apple iPhone 17', 'ram_gb': 8, 'storage_gb': 256,
           'network_generation': '5g'}


def run_audit(source=None):
    source = source or PasajSource()
    refresher = RetailerOfferRefresher(pasaj_source=source)
    specs = {key: PROFILE[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
    targets = [{'source_url': IPHONE17_256_URL}]
    offers = refresher.refresh_existing_offer_targets(PROFILE['name'], targets, specs)
    attempts = refresher.target_report(PROFILE['name'], targets, specs)
    return {'purpose': 'read_only_public_pasaj_pdp_pilot',
            'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'status': 'passed' if len(offers) == 1 else 'failed',
            'profile': PROFILE, 'source': source.report(),
            'source_attempts': attempts, 'verified_offers': offers}


def format_summary(report):
    lines = ['## Turkcell Pasaj public PDP pilot',
             f"Status: {report['status']}; robots {report['source']['robots_status']}; "
             f"PDP requests {report['source']['pdp_requests']}."]
    for attempt in report['source_attempts']:
        lines.append(f"- {attempt['merchant']}: {attempt['reason']} (HTTP {attempt['http_status']}).")
    for offer in report['verified_offers']:
        lines.append(f"- {offer['source_title']}: {offer['price']:,.2f} TRY, "
                     f"{offer['availability']}, {offer['observed_at']}.")
    lines.append('Read-only: one reviewed PDP; no secrets, catalog calls or price writes.')
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
