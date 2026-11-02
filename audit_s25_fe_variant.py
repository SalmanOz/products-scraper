"""Read-only GitHub runner proof for two exact first-party S25 FE 8/256 PDPs."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir

from retailer_offer_refresh import RetailerOfferRefresher


NAME = 'Samsung Galaxy S25 FE 8GB 256GB'
SPECS = {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'}
URLS = (
    'https://www.mediamarkt.com.tr/tr/product/_samsung-galaxy-s25fe-8256gb-lacivert-1249060.html',
    'https://www.mediamarkt.com.tr/tr/product/_samsung-galaxy-s25fe-8256gb-buz-mavisi-1249062.html',
)
OUT = Path(os.environ.get('S25_FE_VARIANT_AUDIT_OUTPUT_DIR', gettempdir()))


def run_audit(reader=None):
    reader = reader or RetailerOfferRefresher()
    rows = []
    for url in URLS:
        target = [{'source_url': url}]
        offers = reader.refresh_existing_offer_targets(NAME, target, SPECS)
        verified = [offer for offer in offers if offer.get('merchant') == 'MediaMarkt'
                    and offer.get('url') == url
                    and offer.get('availability') in {'in_stock', 'limited'}]
        row = {'url': url, 'verified_purchasable': len(verified),
               'offer': {key: verified[0].get(key) for key in
                         ('merchant', 'price', 'currency', 'availability', 'source_title', 'url')}
                        if len(verified) == 1 else None,
               'source_attempts': reader.target_report(NAME, target, SPECS)}
        rows.append(row)
    return {'purpose': 'read_only_samsung_s25_fe_8_256_variant_runner_proof',
            'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'catalog_variant': {'name': NAME, **SPECS},
            'verified_urls': sum(row['verified_purchasable'] == 1 for row in rows),
            'products': rows, 'mediamarkt_source': reader.mediamarkt_source.report(),
            'writes': False}


def summary(report):
    source = report['mediamarkt_source']
    lines = ['## Samsung Galaxy S25 FE 8/256 first-party PDP pilot (read only)',
             f"Verified {report['verified_urls']}/{len(URLS)} exact colors; "
             f"robots {source['robots_status']}; browser GET "
             f"{source['browser_html_responses']}/{source['browser_requests']}."]
    for row in report['products']:
        attempt = row['source_attempts'][0] if row['source_attempts'] else {}
        offer = row['offer'] or {}
        lines.append(f"- {row['url']}: {attempt.get('reason', 'not_attempted')} "
                     f"HTTP {attempt.get('http_status')}; exact cash TRY {offer.get('price')}.")
    lines.append('No catalog, price, schema, R2, or IndexNow writes.')
    return '\n'.join(lines) + '\n'


def main():
    report = run_audit()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 's25-fe-variant-reader.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    text = summary(report)
    (OUT / 's25-fe-variant-reader.md').write_text(text)
    print(text, end='')
    return 0 if report['verified_urls'] == len(URLS) else 1


if __name__ == '__main__':
    raise SystemExit(main())
