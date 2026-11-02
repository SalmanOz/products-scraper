"""Read-only runner audit for three Samsung SKUs after idefix quarantine.

The selected MediaMarkt reader rejects marketplace sellers. The A26 Pasaj
URL is an exact reviewed daily seed; the MediaMarkt candidates are audit-only.
Only a fresh selected first-party cash offer can be reported as verified.
"""

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir

from pasaj_source import A26_6_128_URL
from retailer_offer_refresh import ALLOWED_HOSTS, RetailerOfferRefresher
from urllib.parse import urlparse


S24_MEDIAMARKT_URL = ('https://www.mediamarkt.com.tr/tr/product/'
                      '_samsung-galaxy-s24-5g-8-gb-128-gb-akilli-telefon-siyah-166438182.html')
PROFILES = (
    {
        'id': 278, 'slug': 'samsung-galaxy-a37-5g', 'name': 'Samsung Galaxy A37 5G',
        'specs': {'ram_gb': 6, 'storage_gb': 128, 'network_generation': '5g'},
        'targets': (
            'https://www.mediamarkt.com.tr/tr/product/_samsung-galaxy-a37-5g-6-gb-128-gb-akilli-telefon-beyaz-181053717.html',
            'https://www.mediamarkt.com.tr/tr/product/_samsung-galaxy-a37-5g-6-gb-128-gb-akilli-telefon-antrasit-181053707.html',
            'https://www.mediamarkt.com.tr/tr/product/_samsung-galaxy-a37-5g-6-gb-128-gb-akilli-telefon-yesil-181551122.html',
        ),
    },
    {
        'id': 265, 'slug': 'samsung-galaxy-a26-5g', 'name': 'Samsung Galaxy A26 5G',
        'specs': {'ram_gb': 6, 'storage_gb': 128, 'network_generation': '5g'},
        'targets': (
            A26_6_128_URL,
            'https://www.mediamarkt.com.tr/tr/product/_samsung-galaxy-a26-5g-6-gb-128-gb-akilli-telefon-siyah-168123312.html',
            'https://www.mediamarkt.com.tr/tr/product/_samsung-galaxy-a26-6-gb-128-gb-akilli-telefon-beyaz-166774813.html',
            'https://www.mediamarkt.com.tr/tr/product/_samsung-galaxy-a26-5g-6-gb-128-gb-akilli-telefon-pembe-173576101.html',
        ),
    },
    {
        'id': 29, 'slug': 'samsung-galaxy-s24', 'name': 'Samsung Galaxy S24',
        'specs': {'ram_gb': 8, 'storage_gb': 128, 'network_generation': '5g'},
        'targets': (S24_MEDIAMARKT_URL,),
    },
)
OUT = Path(os.environ.get('THREE_SAMSUNG_AUDIT_OUTPUT_DIR', gettempdir()))


def _verified(offer, urls):
    price = offer.get('price')
    return (offer.get('url') in urls
            and offer.get('merchant') == ALLOWED_HOSTS[urlparse(offer['url']).hostname]
            and offer.get('currency') == 'TRY'
            and offer.get('availability') in {'in_stock', 'limited'}
            and isinstance(price, (int, float)) and not isinstance(price, bool)
            and math.isfinite(price) and 0 < price <= 5_000_000)


def run_audit(reader=None):
    reader = reader or RetailerOfferRefresher()
    products = []
    for profile in PROFILES:
        targets = [{'source_url': url} for url in profile['targets']]
        offers = reader.refresh_existing_offer_targets(
            profile['name'], targets, profile['specs'])
        verified = [offer for offer in offers if _verified(offer, profile['targets'])]
        products.append({
            'id': profile['id'], 'slug': profile['slug'],
            'expected_variant': {'name': profile['name'], **profile['specs']},
            'verified_cash_offers': [
                {key: offer.get(key) for key in
                 ('merchant', 'price', 'currency', 'availability', 'source_title', 'url')}
                for offer in verified
            ],
            'source_attempts': reader.target_report(profile['name'], targets, profile['specs']),
        })
    return {
        'purpose': 'read_only_three_samsung_first_party_cash_recovery_pilot',
        'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'verified_products': sum(bool(row['verified_cash_offers']) for row in products),
        'products': products,
        'source_reports': {'MediaMarkt': reader.mediamarkt_source.report(),
                           'Pasaj': reader.pasaj_source.report()},
        'writes': False,
    }


def summary(report):
    lines = ['## A37 6/128, A26 6/128, S24 8/128 first-party PDP audit (read only)',
             f"Verified {report['verified_products']}/3 exact variants."]
    for row in report['products']:
        offers = row['verified_cash_offers']
        lines.append(f"- {row['slug']}: " +
                     (', '.join(f"{offer['merchant']} {offer['price']} TRY" for offer in offers)
                      if offers else 'no verified in-stock first-party cash offer'))
        for attempt in row['source_attempts']:
            lines.append(f"  - {attempt.get('merchant')}: {attempt.get('reason')} "
                         f"HTTP {attempt.get('http_status')} · {attempt.get('source_url')}")
    lines.append('MediaMarkt candidates are audit-only; A26 Pasaj is a reviewed daily URL seed. No catalog, price or score writes.')
    return '\n'.join(lines) + '\n'


def main():
    report = run_audit()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'three-samsung-recovery.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    text = summary(report)
    (OUT / 'three-samsung-recovery.md').write_text(text)
    print(text, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
