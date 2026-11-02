"""Read-only live proof of two selected Galaxy A37 5G 6/128 sellers."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import gettempdir

from mediamarkt_marketplace_preflight import no_merchant_collision
from mediamarkt_marketplace_source import A37_PROFILES, reviewed_marketplace_offer
from offer_ingestion_client import OfferIngestionClient
from retailer_offer_refresh import RetailerOfferRefresher
from reviewed_offer_targets import with_reviewed_radio
from update_prices import PriceUpdater


OUT = Path(os.environ.get('MEDIAMARKT_A37_AUDIT_OUTPUT_DIR', gettempdir()))
EXPECTED_SLUG = 'samsung-galaxy-a37-5g'


def run_audit(reader=None, catalog_client=None):
    reader = reader or RetailerOfferRefresher()
    catalog_client = catalog_client or OfferIngestionClient.from_env()
    report = {'purpose': 'read_only_mediamarkt_a37_two_selected_offers',
              'writes': False, 'status': 'failed', 'checked_at': None,
              'robots_status': None, 'rows': []}
    try:
        catalog = catalog_client.get_published_products()
        if not isinstance(catalog, list):
            raise ValueError('catalog_invalid')
        ids = {profile['id'] for profile in A37_PROFILES.values()}
        if ids != {278}:
            raise ValueError('profile_scope_changed')
        matches = [p for p in catalog if isinstance(p, dict) and p.get('id') == 278]
        if len(matches) != 1:
            raise ValueError('catalog_identity_missing')
        product = matches[0]
        specs = with_reviewed_radio(product, PriceUpdater.get_expected_specs(product.get('attributes')))
        urls = list(A37_PROFILES)
        for url, profile in A37_PROFILES.items():
            expected = {key: profile[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
            if (product.get('slug') != profile['slug'] or product.get('name') != profile['name']
                    or any(specs.get(key) != value for key, value in expected.items())):
                raise ValueError('catalog_identity_or_variant_changed')
            if not no_merchant_collision(product, profile, url):
                raise ValueError('merchant_name_cross_platform_collision')
        targets = [{'source_url': url} for url in urls]
        candidates = reader.refresh_existing_offer_targets(product['name'], targets, specs)
        attempts = {row['source_url']: row for row in reader.target_report(product['name'], targets, specs)}
        observed = {offer['url']: offer for offer in candidates}
        if len(observed) != len(candidates) or len(attempts) != len(urls):
            raise ValueError('selected_offer_duplicate_or_missing')
        for url, profile in A37_PROFILES.items():
            expected = {key: profile[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
            offer, attempt = observed.get(url), attempts[url]
            report['rows'].append({
                'url': url, 'product': {'id': profile['id'], 'slug': profile['slug'],
                                      'name': profile['name'], 'expected_specs': expected},
                'catalog_reason': 'catalog_exact', 'merchant_collision': False,
                'source_attempt': attempt, 'verified_offer': offer,
            })
            if (not reviewed_marketplace_offer(offer, profile['name'], specs, product)
                    or attempt.get('merchant') != profile['seller_name']
                    or attempt.get('reason') != 'observed_exact_new_marketplace_offer'
                    or attempt.get('http_status') != 200):
                raise ValueError('no_exact_new_selected_offer')
        report['robots_status'] = reader.mediamarkt_source.report().get('robots_status')
        if report['robots_status'] != 'ready' or set(observed) != set(A37_PROFILES):
            raise ValueError('robots_or_offer_count_failed')
        report['status'] = 'passed'
    except (RuntimeError, ValueError, TypeError, KeyError, AttributeError, OSError) as error:
        report['error'] = str(error) if isinstance(error, ValueError) else type(error).__name__
    report['checked_at'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    return report


def summary(report):
    lines = ['## MediaMarkt Galaxy A37 5G 6/128 · iki seçili satıcı (salt okuma)',
             f"Durum: {report['status']}; doğrulanan "
             f"{sum(bool(r.get('verified_offer')) for r in report['rows'])}/2; "
             f"robots {report['robots_status']}."]
    for row in report['rows']:
        attempt = row.get('source_attempt') or {}
        offer = row.get('verified_offer') or {}
        lines.append(f"- {attempt.get('reason')} HTTP {attempt.get('http_status')} · {row['url']}")
        if offer:
            lines.append(f"  {offer['seller_name']} via MediaMarkt: {offer['price']:,.2f} TRY, "
                         f"GTIN {offer.get('gtin13')}, {offer['availability']}, "
                         f"observed {offer['observed_at']}.")
    if report.get('error'):
        lines.append('Hata: ' + report['error'])
    lines.append('Katalog, teklif, skor, görsel veya IndexNow yazımı yok.')
    return '\n'.join(lines) + '\n'


def main():
    report = run_audit()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'mediamarkt-a37-two.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    text = summary(report)
    (OUT / 'mediamarkt-a37-two.md').write_text(text)
    print(text, end='')
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
