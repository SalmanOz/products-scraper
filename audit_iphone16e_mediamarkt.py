"""Read-only runner proof for the exact iPhone 16e MediaMarkt PDP."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir

from retailer_offer_refresh import RetailerOfferRefresher
from reviewed_offer_targets import MEDIAMARKT_BATCH_PATH


PROFILE = next(row for row in json.loads(MEDIAMARKT_BATCH_PATH.read_text())['products']
               if row['id'] == 250 and row['slug'] == 'apple-iphone-16e')
OUT = Path(os.environ.get('IPHONE16E_MEDIAMARKT_AUDIT_OUTPUT_DIR', gettempdir()))


def run_audit(reader=None):
    reader = reader or RetailerOfferRefresher()
    targets = [{'source_url': url} for url in PROFILE['targets']]
    specs = {key: PROFILE[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
    offers = reader.refresh_existing_offer_targets(PROFILE['name'], targets, specs)
    exact = [offer for offer in offers
             if offer.get('merchant') == 'MediaMarkt'
             and offer.get('url') in PROFILE['targets']
             and offer.get('currency') == 'TRY'
             and offer.get('availability') in {'in_stock', 'limited'}
             and isinstance(offer.get('price'), (int, float))
             and not isinstance(offer.get('price'), bool)
             and 0 < offer['price'] <= 5_000_000]
    return {
        'purpose': 'read_only_iphone16e_exact_first_party_cash_offer',
        'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'catalog_variant': {key: PROFILE[key] for key in
                            ('id', 'slug', 'name', 'ram_gb', 'storage_gb', 'network_generation')},
        'verified': len(exact) == 1,
        'offer': {key: exact[0].get(key) for key in
                  ('merchant', 'price', 'currency', 'availability', 'source_title', 'url')}
                 if len(exact) == 1 else None,
        'source_attempts': reader.target_report(PROFILE['name'], targets, specs),
        'mediamarkt_source': reader.mediamarkt_source.report(),
        'writes': False,
    }


def summary(report):
    attempt = report['source_attempts'][0] if report['source_attempts'] else {}
    offer = report['offer'] or {}
    return ('## iPhone 16e 8/128 MediaMarkt PDP audit (read only)\n'
            f"Verified: {report['verified']}; {attempt.get('reason', 'not_attempted')} "
            f"HTTP {attempt.get('http_status')}; selected cash TRY {offer.get('price')}.\n"
            'No catalog, price, schema, R2, or IndexNow writes.\n')


def main():
    report = run_audit()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'iphone16e-mediamarkt-audit.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    text = summary(report)
    (OUT / 'iphone16e-mediamarkt-audit.md').write_text(text)
    print(text, end='')
    return 0 if report['verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
