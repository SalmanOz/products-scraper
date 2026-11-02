"""Read-only runner proof of two selected MediaMarkt Xiaomi cash offers."""

import json
import math
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir

import requests

from retailer_offer_refresh import RetailerOfferRefresher
from update_prices import PriceUpdater


BATCH_PATH = Path(__file__).parent / 'offer_batches/xiaomi-17t-first-party-exact-2026-11-02.json'
OUT = Path(os.environ.get('XIAOMI_17T_AUDIT_OUTPUT_DIR', gettempdir()))
CATALOG_BASE = 'https://teknoskor.com/api/products/'
MAX_CATALOG_BYTES = 128 * 1024
REVIEWED = (
    (270, 'xiaomi-17t-pro', 'Xiaomi 17T Pro', 12, 512, '5g',
     'https://www.mediamarkt.com.tr/tr/product/_xiaomi-17t-pro-12512gb-akilli-telefon-mor-1254681.html', 'mor'),
    (295, 'xiaomi-17t', 'Xiaomi 17T', 12, 256, '5g',
     'https://www.mediamarkt.com.tr/tr/product/_xiaomi-17t-12256-gb-akilli-telefon-beyaz-1254679.html', 'beyaz'),
)


def _fold(value):
    text = unicodedata.normalize('NFKD', value).casefold().replace('ı', 'i')
    return re.sub(r'[^a-z0-9]+', ' ', ''.join(c for c in text if not unicodedata.combining(c))).strip()


def _profiles():
    rows = json.loads(BATCH_PATH.read_text())['products']
    actual = tuple((p.get('id'), p.get('slug'), p.get('name'), p.get('ram_gb'),
                    p.get('storage_gb'), p.get('network_generation'),
                    *p.get('targets', [])) for p in rows)
    expected = tuple(row[:-1] for row in REVIEWED)
    if actual != expected:
        raise ValueError('reviewed_xiaomi_profiles_changed')
    return rows


def _catalog_verified(profile, get):
    response = get(CATALOG_BASE + str(profile['id']), timeout=(5, 12),
                   stream=True, allow_redirects=False,
                   headers={'Accept': 'application/json',
                            'User-Agent': 'TeknoskorPriceBot/1.0 (+https://teknoskor.com)'})
    try:
        if response.status_code != 200:
            return False, 'catalog_http_' + str(response.status_code)
        length = response.headers.get('Content-Length', '')
        if length.isdigit() and int(length) > MAX_CATALOG_BYTES:
            return False, 'catalog_oversized'
        data = bytearray()
        for chunk in response.iter_content(chunk_size=16 * 1024):
            data.extend(chunk)
            if len(data) > MAX_CATALOG_BYTES:
                return False, 'catalog_oversized'
        product = json.loads(data)
        specs = PriceUpdater.get_expected_specs(product.get('attributes'))
        required = ('ram_gb', 'storage_gb', 'network_generation')
        identity = (product.get('id') == profile['id']
                    and product.get('slug') == profile['slug']
                    and product.get('name') == profile['name']
                    and all(specs.get(key) == profile[key] for key in required))
        return identity, 'catalog_exact' if identity else 'catalog_identity_or_variant_changed'
    finally:
        response.close()


def _verified_offer(offers, profile, color):
    if len(offers) != 1:
        return None
    offer = offers[0]
    price = offer.get('price')
    title = offer.get('source_title')
    url = profile['targets'][0]
    if (offer.get('merchant') != 'MediaMarkt' or offer.get('url') != url
            or offer.get('currency') != 'TRY'
            or offer.get('availability') not in {'in_stock', 'limited'}
            or not isinstance(price, (int, float)) or isinstance(price, bool)
            or not math.isfinite(price) or not 0 < price <= 5_000_000
            or not isinstance(title, str) or color not in _fold(title).split()
            or color not in _fold(url).split()):
        return None
    return {key: offer.get(key) for key in
            ('merchant', 'price', 'currency', 'availability', 'source_title', 'url')}


def run_audit(reader=None, catalog_get=None):
    reader = reader or RetailerOfferRefresher()
    catalog_get = catalog_get or requests.get
    rows = []
    for profile, reviewed in zip(_profiles(), REVIEWED):
        color = reviewed[-1]
        try:
            catalog_ok, catalog_reason = _catalog_verified(profile, catalog_get)
        except (requests.RequestException, ValueError, TypeError, UnicodeError, KeyError):
            catalog_ok, catalog_reason = False, 'catalog_unavailable'
        target = [{'source_url': profile['targets'][0]}]
        specs = {key: profile[key] for key in
                 ('ram_gb', 'storage_gb', 'network_generation')}
        offers = reader.refresh_existing_offer_targets(profile['name'], target, specs) if catalog_ok else []
        offer = _verified_offer(offers, profile, color) if catalog_ok else None
        rows.append({'id': profile['id'], 'slug': profile['slug'],
                     'expected_variant': {'name': profile['name'], **specs, 'color': color},
                     'source_url': profile['targets'][0],
                     'catalog_reason': catalog_reason,
                     'source_attempts': reader.target_report(profile['name'], target, specs) if catalog_ok else [],
                     'verified_offer': offer})
    return {'purpose': 'read_only_xiaomi_17t_first_party_runner_proof',
            'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'verified_products': sum(row['verified_offer'] is not None for row in rows),
            'products': rows, 'mediamarkt_source': reader.mediamarkt_source.report(),
            'writes': False}


def summary(report):
    source = report['mediamarkt_source']
    lines = ['## Xiaomi 17T and 17T Pro exact MediaMarkt PDP audit (read only)',
             f"Verified {report['verified_products']}/{len(REVIEWED)}; robots "
             f"{source.get('robots_status')}; browser GET "
             f"{source.get('browser_html_responses')}/{source.get('browser_requests')}."]
    for row in report['products']:
        attempt = row['source_attempts'][0] if row['source_attempts'] else {}
        offer = row['verified_offer'] or {}
        lines.append(f"- {row['slug']}: {row['catalog_reason']}; "
                     f"{attempt.get('reason', 'not_attempted')} HTTP {attempt.get('http_status')}; "
                     f"selected cash TRY {offer.get('price', 'unverified')} · {row['source_url']}")
    lines.append('No catalog, offer, score, image, or IndexNow writes.')
    return '\n'.join(lines) + '\n'


def main():
    report = run_audit()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'xiaomi-17t-first-party.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    text = summary(report)
    (OUT / 'xiaomi-17t-first-party.md').write_text(text)
    print(text, end='')
    return 0 if report['verified_products'] == len(REVIEWED) else 1


if __name__ == '__main__':
    raise SystemExit(main())
