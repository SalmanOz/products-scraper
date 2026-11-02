"""Read-only selected S24 FE 8/128 marketplace offer and exact-GTIN audit."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import gettempdir

from mediamarkt_marketplace_preflight import no_merchant_collision
from mediamarkt_marketplace_source import (
    S24FE_PROFILES, S24FE_RADIO_EVIDENCE, S24FE_URL,
    reviewed_marketplace_offer,
)
from offer_ingestion_client import OfferIngestionClient
from retailer_offer_refresh import RetailerOfferRefresher
from reviewed_offer_targets import with_reviewed_radio
from update_prices import PriceUpdater


OUT = Path(os.environ.get('MEDIAMARKT_S24FE_AUDIT_OUTPUT_DIR', gettempdir()))
PROFILE = S24FE_PROFILES[S24FE_URL]


def run_audit(reader=None, catalog_client=None):
    reader = reader or RetailerOfferRefresher()
    catalog_client = catalog_client or OfferIngestionClient.from_env()
    report = {'purpose': 'read_only_mediamarkt_s24fe_exact_gtin',
              'writes': False, 'status': 'failed', 'checked_at': None,
              'robots_status': None, 'radio_evidence': S24FE_RADIO_EVIDENCE,
              'rows': []}
    try:
        catalog = catalog_client.get_published_products()
        matches = [p for p in catalog if isinstance(p, dict) and p.get('id') == PROFILE['id']]
        if len(matches) != 1:
            raise ValueError('catalog_identity_missing')
        product = matches[0]
        specs = with_reviewed_radio(product, PriceUpdater.get_expected_specs(product.get('attributes')))
        expected = {key: PROFILE[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
        if (product.get('slug') != PROFILE['slug'] or product.get('name') != PROFILE['name']
                or any(specs.get(key) != value for key, value in expected.items())):
            raise ValueError('catalog_identity_or_variant_changed')
        if not no_merchant_collision(product, PROFILE, S24FE_URL):
            raise ValueError('merchant_name_cross_platform_collision_or_target_truncation')
        targets = [{'source_url': S24FE_URL}]
        candidates = reader.refresh_existing_offer_targets(PROFILE['name'], targets, specs)
        attempts = reader.target_report(PROFILE['name'], targets, specs)
        offer = candidates[0] if len(candidates) == 1 else None
        attempt = attempts[0] if len(attempts) == 1 else None
        report['rows'].append({'url': S24FE_URL,
                               'product': {'id': PROFILE['id'], 'slug': PROFILE['slug'],
                                           'name': PROFILE['name'], 'expected_specs': expected},
                               'catalog_reason': 'catalog_exact',
                               'merchant_collision': False,
                               'source_attempt': attempt, 'verified_offer': offer})
        if (not reviewed_marketplace_offer(offer, PROFILE['name'], specs, product)
                or not attempt or attempt.get('merchant') != PROFILE['seller_name']
                or attempt.get('reason') != 'observed_exact_new_marketplace_offer'
                or attempt.get('http_status') != 200):
            raise ValueError('no_exact_new_selected_offer')
        report['robots_status'] = reader.mediamarkt_source.report().get('robots_status')
        if report['robots_status'] != 'ready':
            raise ValueError('robots_unavailable')
        report['status'] = 'passed'
    except (RuntimeError, ValueError, TypeError, KeyError, AttributeError, OSError) as error:
        report['error'] = str(error) if isinstance(error, ValueError) else type(error).__name__
    report['checked_at'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    return report


def summary(report):
    lines = ['## MediaMarkt S24 FE 8/128 exact GTIN (salt okuma)',
             f"Durum: {report['status']}; doğrulanan "
             f"{sum(bool(r.get('verified_offer')) for r in report['rows'])}/1; "
             f"robots {report['robots_status']}."]
    for row in report['rows']:
        attempt = row.get('source_attempt') or {}
        offer = row.get('verified_offer') or {}
        lines.append(f"{PROFILE['slug']}: {attempt.get('reason')} HTTP "
                     f"{attempt.get('http_status')} · {S24FE_URL}")
        if offer:
            lines.append(f"{offer['seller_name']} via MediaMarkt: {offer['price']:,.2f} TRY, "
                         f"GTIN {offer.get('gtin13')}, {offer['availability']}, "
                         f"observed {offer['observed_at']}.")
    lines.append('5G eşleşmesi MediaMarkt ilanından değil, doğrulanmış exact GTIN + '
                 'Samsung model spesifikasyonundan türetilmiştir.')
    if report.get('error'):
        lines.append('Hata: ' + report['error'])
    lines.append('Katalog, teklif, skor, görsel veya IndexNow yazımı yok.')
    return '\n'.join(lines) + '\n'


def main():
    report = run_audit()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'mediamarkt-s24fe-gtin.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    text = summary(report)
    (OUT / 'mediamarkt-s24fe-gtin.md').write_text(text)
    print(text, end='')
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
