"""Read-only proof for two exact, currently unpriced MediaMarkt phone PDPs.

The fixed URLs are hints, not offers. Each run rereads robots and the selected
PDP. Price, seller, stock, GTIN and variant must agree in the selected page.
No ingestion client write method is called by this module.
"""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
from urllib.parse import urlparse

import requests

from mediamarkt_marketplace_audit import catalog_variant_matches, selected_marketplace_offer
from mediamarkt_marketplace_preflight import no_merchant_collision
from mediamarkt_source import BOT, MAX_HTML_BYTES, MediaMarktSource, product_url
from offer_ingestion_client import OfferIngestionClient
from update_prices import PriceUpdater


PROFILES = {
    'https://www.mediamarkt.com.tr/tr/product/_xiaomi-redmi-note-15-pro-5g-8-gb-256-gb-akilli-telefon-buz-mavisi-169130258.html': {
        'id': 238, 'slug': 'xiaomi-redmi-note-15-pro',
        'name': 'Xiaomi Redmi Note 15 Pro', 'ram_gb': 8, 'storage_gb': 256,
        'network_generation': '5g', 'sku': '169130258',
        'gtin13': '6932554491543',
        'source_title': 'XIAOMI Redmi Note 15 Pro 5G 8 GB 256 GB Akıllı Telefon Buz Mavisi',
        'seller_name': 'CepHane Teknoloji', 'seller_id': '11289',
    },
    'https://www.mediamarkt.com.tr/tr/product/_poco-x6-pro-8-gb-256-gb-akilli-telefon-siyah-142988315.html': {
        'id': 183, 'slug': 'poco-x6-pro', 'name': 'POCO X6 Pro',
        'ram_gb': 8, 'storage_gb': 256, 'network_generation': None,
        'sku': '142988315', 'gtin13': '6941812757994',
        'source_title': 'POCO X6 Pro 8 GB 256 GB Akıllı Telefon Siyah',
        'seller_name': 'Başaran Ticaret', 'seller_id': '12140',
    },
}
PURPOSE = 'read_only_mediamarkt_two_unpriced_selected_marketplace_offers'


def _catalog_rows(client):
    products = client.get_published_products()
    prices = client.get_public_price_priority()
    if not isinstance(products, list) or not isinstance(prices, dict):
        raise ValueError('catalog_unavailable')
    selected = {}
    for url, profile in PROFILES.items():
        rows = [row for row in products if isinstance(row, dict)
                and row.get('id') == profile['id']]
        if len(rows) != 1:
            raise ValueError('catalog_identity_missing')
        product = rows[0]
        if (product.get('slug') != profile['slug']
                or product.get('name') != profile['name']
                or not isinstance(product.get('attributes'), dict)
                or not isinstance(product.get('offer_targets'), list)):
            raise ValueError('catalog_identity_changed')
        specs = PriceUpdater.get_expected_specs(product['attributes'])
        if (specs.get('ram_gb') != profile['ram_gb']
                or specs.get('storage_gb') != profile['storage_gb']
                or specs.get('network_generation') not in
                    (None, profile['network_generation'])
                or prices.get(profile['id']) != (profile['slug'], False)):
            raise ValueError('catalog_variant_or_unpriced_status_changed')
        selected[url] = product
    return selected


def _collision_evidence(product, profile, url):
    """Explain the unchanged fail-closed seller/platform and target-cap gate."""
    targets = product['offer_targets']
    seller_targets = [row for row in targets
                      if isinstance(row, dict) and
                      str(row.get('merchant_name', '')).strip().casefold() ==
                      profile['seller_name'].casefold()]
    reasons = []
    if len(targets) >= 12:
        reasons.append('target_page_full')
    if any(not isinstance(row, dict) for row in targets):
        reasons.append('invalid_target_row')
    if any(row.get('source_url') != url or
           urlparse(str(row.get('source_url') or '')).hostname !=
           'www.mediamarkt.com.tr' for row in seller_targets):
        reasons.append('seller_name_on_other_url_or_platform')
    return {
        'blocked': not no_merchant_collision(product, profile, url),
        'reasons': reasons,
        'target_count': len(targets),
        'targets': [{key: row.get(key) for key in
                     ('merchant_name', 'source_url', 'checked_at')}
                    for row in targets if isinstance(row, dict)],
    }


def _offer_matches(offer, url, profile):
    """Fail closed on selected identity, seller, visible cash and stock drift."""
    if not isinstance(offer, dict):
        return False
    specs = {key: profile[key] for key in
             ('ram_gb', 'storage_gb', 'network_generation')}
    try:
        if isinstance(offer['price'], bool):
            return False
        price = float(offer['price'])
        observed = datetime.fromisoformat(offer['observed_at'].replace('Z', '+00:00'))
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return False
    return bool(
        offer.get('url') == url and product_url(url)
        and offer.get('platform') == 'MediaMarkt'
        and offer.get('merchant') == profile['seller_name']
        and offer.get('seller_name') == profile['seller_name']
        and offer.get('seller_id') == profile['seller_id']
        and offer.get('sku') == profile['sku']
        and offer.get('gtin13') == profile['gtin13']
        and offer.get('source_title') == profile['source_title']
        and offer.get('ram_gb') == profile['ram_gb']
        and offer.get('storage_gb') == profile['storage_gb']
        and offer.get('network_generation') == profile['network_generation']
        and offer.get('condition') == 'new'
        and offer.get('availability') == 'in_stock'
        and offer.get('currency') == 'TRY'
        and isinstance(offer.get('offer_id'), str)
        and re.fullmatch(r'\d{1,15}', offer['offer_id'])
        and math.isfinite(price) and 0 < price <= 5_000_000
        and observed.tzinfo is not None
        and -60 <= (datetime.now(timezone.utc) - observed).total_seconds() <= 300
        and catalog_variant_matches(offer, profile['name'], specs)
    )


def _read_selected(source, url):
    """One direct GET per PDP and one exact-URL retry only if direct GET is 403."""
    try:
        source._load_robots()
        if (source.report().get('robots_status') != 'ready' or source._rules is None
                or not source._rules.can_fetch(BOT, url)):
            return None, {'source_url': url, 'reason': 'robots_unavailable_or_disallowed',
                          'http_status': None}
        body, reason, status = source._direct(url, MAX_HTML_BYTES, 'text/html')
        if reason == 'http_403':
            body, final_url, reason = source._browser_get(url)
            if reason != 'ready' or final_url != url:
                return None, {'source_url': url, 'reason': 'browser_or_url_changed',
                              'http_status': status}
        elif reason != 'ready':
            return None, {'source_url': url, 'reason': reason,
                          'http_status': status}
        evidence = selected_marketplace_offer(body, url)
        if evidence is None:
            return None, {'source_url': url, 'reason': 'no_exact_new_marketplace_offer',
                          'http_status': 200}
        offer = {**evidence, 'merchant': evidence['seller_name'],
                 'observed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}
        return offer, {'source_url': url,
                       'reason': 'observed_exact_new_marketplace_offer',
                       'http_status': 200}
    except (requests.RequestException, OSError, TypeError, ValueError, AttributeError):
        return None, {'source_url': url, 'reason': 'source_unavailable',
                      'http_status': None}


def audit(*, client=None, source=None):
    client = client or OfferIngestionClient.from_env()
    source = source or MediaMarktSource()
    report = {'purpose': PURPOSE, 'writes': False, 'status': 'failed',
              'checked_at': None, 'robots_status': None, 'source': None,
              'rows': [], 'catalog_collisions': {}}
    try:
        catalog = _catalog_rows(client)
        for url, profile in PROFILES.items():
            collision = _collision_evidence(catalog[url], profile, url)
            report['catalog_collisions'][profile['slug']] = collision
            offer, attempt = _read_selected(source, url)
            verified = offer if _offer_matches(offer, url, profile) else None
            report['rows'].append({
                'url': url,
                'product': {'id': profile['id'], 'slug': profile['slug'],
                            'name': profile['name'], 'expected_specs': {
                                key: profile[key] for key in
                                ('ram_gb', 'storage_gb', 'network_generation')}},
                'catalog_reason': 'catalog_exact_unpriced',
                'merchant_collision': collision['blocked'],
                'source_attempt': attempt, 'verified_offer': verified,
            })
        report['robots_status'] = source.report().get('robots_status')
        if len(report['rows']) != 2:
            raise ValueError('robots_or_scope_changed')
        errors = []
        if report['robots_status'] != 'ready':
            errors.append('robots_unavailable')
        if any(row['merchant_collision'] for row in report['rows']):
            errors.append('merchant_name_cross_platform_collision')
        if any(row['verified_offer'] is None for row in report['rows']):
            errors.append('selected_offer_or_variant_not_verified')
        if errors:
            raise ValueError(','.join(errors))
        report['status'] = 'passed'
    except (RuntimeError, ValueError, TypeError, KeyError, AttributeError, OSError) as error:
        report['error'] = str(error) if isinstance(error, ValueError) else type(error).__name__
    report['checked_at'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    report['source'] = source.report()
    return report


def summary(report):
    lines = [f"İki fiyatsız telefon MediaMarkt salt okuma denetimi: {report['status']}; "
             f"robots {report['robots_status']}; doğrulanan "
             f"{sum(bool(row.get('verified_offer')) for row in report['rows'])}/2."]
    for row in report['rows']:
        offer = row.get('verified_offer') or {}
        attempt = row.get('source_attempt') or {}
        lines.append(f"- {row['product']['slug']}: {attempt.get('reason')} "
                     f"HTTP {attempt.get('http_status')}; "
                     f"{offer.get('seller_name') or '?'} "
                     f"{offer.get('price') or '?'} TRY, "
                     f"SKU {offer.get('sku') or '?'}, GTIN {offer.get('gtin13') or '?'}, "
                     f"{offer.get('availability') or '?'}.")
        collision = report['catalog_collisions'].get(row['product']['slug']) or {}
        if collision.get('blocked'):
            lines.append(f"  Katalog hedefleri {collision.get('target_count')}/12; "
                         f"çakışma {', '.join(collision.get('reasons', [])) or 'unknown'}.")
    if report.get('error'):
        lines.append('Hata: ' + report['error'])
    lines.append('Katalog, fiyat, skor veya arama dizini yazımı yok.')
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--audit-file', type=Path, required=True)
    args = parser.parse_args(argv)
    report = audit()
    args.audit_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    args.audit_file.with_suffix('.md').write_text(summary(report))
    print(summary(report), end='')
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
