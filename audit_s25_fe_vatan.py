"""Read-only GitHub runner check of one exact Vatan S25 FE 8/256 PDP."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir

from retailer_offer_refresh import RetailerOfferRefresher


NAME = 'Samsung Galaxy S25 FE 256GB'
SPECS = {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'}
URL = 'https://www.vatanbilgisayar.com/samsung-galaxy-s25-fe-8-256-gb-akilli-telefon-buz-mavisi.html'
OUT = Path(os.environ.get('S25_FE_VATAN_AUDIT_OUTPUT_DIR', gettempdir()))


def run_audit(reader=None):
    reader = reader or RetailerOfferRefresher()
    targets = [{'source_url': URL}]
    offers = reader.refresh_existing_offer_targets(NAME, targets, SPECS)
    verified = [offer for offer in offers if offer.get('merchant') == 'Vatan Bilgisayar'
                and offer.get('url') == URL and offer.get('availability') in {'in_stock', 'limited'}]
    return {'purpose': 'read_only_exact_vatan_s25_fe_8_256_runner_proof',
            'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'catalog_variant': {'name': NAME, **SPECS}, 'url': URL,
            'verified': len(verified) == 1,
            'offer': {key: verified[0].get(key) for key in
                      ('merchant', 'price', 'currency', 'availability', 'source_title', 'url')}
                     if len(verified) == 1 else None,
            'source_attempts': reader.target_report(NAME, targets, SPECS),
            'vatan_source': reader.vatan_reviewed_source.report(),
            'writes': False}


def summary(report):
    attempt = report['source_attempts'][0] if report['source_attempts'] else {}
    offer = report['offer'] or {}
    return ('## Vatan S25 FE 8/256 exact PDP audit (read only)\n'
            f"Verified: {report['verified']}; {attempt.get('reason', 'not_attempted')} "
            f"HTTP {attempt.get('http_status')}; exact cash TRY {offer.get('price')}.\n"
            'No catalog, price, schema, R2, or IndexNow writes.\n')


def main():
    report = run_audit()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 's25-fe-vatan-audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    text = summary(report)
    (OUT / 's25-fe-vatan-audit.md').write_text(text)
    print(text, end='')
    return 0 if report['verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
