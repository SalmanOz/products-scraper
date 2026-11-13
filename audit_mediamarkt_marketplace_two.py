"""Read-only live proof of two exact new MediaMarkt marketplace offers."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import gettempdir

from mediamarkt_marketplace_source import PROFILES, reviewed_marketplace_offer
from mediamarkt_marketplace_preflight import no_merchant_collision
from offer_ingestion_client import OfferIngestionClient
from retailer_offer_refresh import RetailerOfferRefresher
from reviewed_offer_targets import with_reviewed_radio
from update_prices import PriceUpdater


OUT = Path(os.environ.get('MEDIAMARKT_MARKETPLACE_AUDIT_OUTPUT_DIR', gettempdir()))


def run_audit(reader=None, catalog_client=None):
    reader = reader or RetailerOfferRefresher()
    catalog_client = catalog_client or OfferIngestionClient.from_env()
    report = {'purpose': 'read_only_mediamarkt_marketplace_two_selected_offers',
              'writes': False, 'status': 'failed', 'checked_at': None,
              'rows': []}
    try:
        catalog = catalog_client.get_published_products()
        if not isinstance(catalog, list):
            raise ValueError('catalog_invalid')
        for url, profile in PROFILES.items():
            matches = [p for p in catalog if isinstance(p, dict) and p.get('id') == profile['id']]
            if len(matches) != 1:
                raise ValueError('catalog_identity_missing')
            product = matches[0]
            specs = with_reviewed_radio(product, PriceUpdater.get_expected_specs(product.get('attributes')))
            expected = {key: profile[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
            if (product.get('slug') != profile['slug'] or product.get('name') != profile['name']
                    or any(specs.get(key) != value for key, value in expected.items())):
                raise ValueError('catalog_identity_or_variant_changed')
            collision = not no_merchant_collision(product, profile, url)
            if collision:
                raise ValueError('merchant_name_cross_platform_collision_or_target_truncation')
            targets = [{'source_url': url}]
            candidates = reader.refresh_existing_offer_targets(profile['name'], targets, specs)
            attempts = reader.target_report(profile['name'], targets, specs)
            offer = candidates[0] if len(candidates) == 1 else None
            attempt = attempts[0] if len(attempts) == 1 else None
            row = {'url': url, 'product': {'id': profile['id'], 'slug': profile['slug'],
                   'name': profile['name'], 'expected_specs': expected},
                   'catalog_reason': 'catalog_exact', 'merchant_collision': False,
                   'source_attempt': attempt, 'verified_offer': offer}
            report['rows'].append(row)
            if (not reviewed_marketplace_offer(offer, profile['name'], specs, product)
                    or not attempt or attempt.get('merchant') != profile['seller_name']
                    or attempt.get('reason') != 'observed_exact_new_marketplace_offer'
                    or attempt.get('http_status') != 200):
                raise ValueError('no_exact_new_selected_offer')
        if len(report['rows']) != len(PROFILES):
            raise ValueError('offer_count_failed')
        report['status'] = 'passed'
    except (RuntimeError, ValueError, TypeError, KeyError, AttributeError, OSError) as error:
        report['error'] = str(error) if isinstance(error, ValueError) else type(error).__name__
    report['checked_at'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    return report


def summary(report):
    lines = ['## MediaMarkt pazar yeri: iki seçili yeni satıcı teklifi (salt okuma)',
             f"Durum: {report['status']}; doğrulanan {sum(bool(r.get('verified_offer')) for r in report['rows'])}/2; "
             "."]
    for row in report['rows']:
        attempt = row.get('source_attempt') or {}
        offer = row.get('verified_offer') or {}
        lines.append(f"- {row['product']['slug']}: {attempt.get('reason')} HTTP "
                     f"{attempt.get('http_status')} · {row['url']}")
        if offer:
            lines.append(f"  {offer['seller_name']} via MediaMarkt: {offer['price']:,.2f} TRY, "
                         f"{offer['availability']}, observed {offer['observed_at']}.")
    if report.get('error'):
        lines.append('Hata: ' + report['error'])
    lines.append('Katalog, teklif, skor, görsel veya IndexNow yazımı yok.')
    return '\n'.join(lines) + '\n'


def main():
    report = run_audit()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'mediamarkt-marketplace-two.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    text = summary(report)
    (OUT / 'mediamarkt-marketplace-two.md').write_text(text)
    print(text, end='')
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
