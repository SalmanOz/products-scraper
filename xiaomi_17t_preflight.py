"""Opt-in, same-run handoff of two fresh reviewed MediaMarkt observations."""

import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from tr_price_scraper import TRPriceScraper


MAX_PREFLIGHT_AGE_SECONDS = 5 * 60
PROFILES = {
    'https://www.mediamarkt.com.tr/tr/product/_xiaomi-17t-pro-12512gb-akilli-telefon-mor-1254681.html':
        (270, 'xiaomi-17t-pro', 'Xiaomi 17T Pro', 12, 512, '5g', 'mor'),
    'https://www.mediamarkt.com.tr/tr/product/_xiaomi-17t-12256-gb-akilli-telefon-beyaz-1254679.html':
        (295, 'xiaomi-17t', 'Xiaomi 17T', 12, 256, '5g', 'beyaz'),
}


def _fold(value):
    text = unicodedata.normalize('NFKD', value).casefold().replace('ı', 'i')
    return re.sub(r'[^a-z0-9]+', ' ', ''.join(c for c in text if not unicodedata.combining(c))).split()


def _time(value):
    timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if timestamp.tzinfo is None:
        raise ValueError('timezone missing')
    return timestamp.astimezone(timezone.utc)


def validated_offers(report, now=None):
    """Return both pinned offers only when the entire preflight remains exact."""
    try:
        now = now or datetime.now(timezone.utc)
        if (not isinstance(report, dict)
                or report.get('purpose') != 'read_only_xiaomi_17t_first_party_runner_proof'
                or report.get('writes') is not False
                or report.get('verified_products') != 2):
            return None
        checked = _time(report['checked_at'])
        if now.tzinfo is None or not -60 <= (now - checked).total_seconds() <= MAX_PREFLIGHT_AGE_SECONDS:
            return None
        rows = report.get('products')
        if not isinstance(rows, list) or len(rows) != 2:
            return None
        matcher = TRPriceScraper()
        offers = {}
        for row in rows:
            if not isinstance(row, dict):
                return None
            url = row.get('source_url')
            profile = PROFILES.get(url)
            if profile is None or url in offers:
                return None
            id, slug, name, ram, storage, radio, color = profile
            specs = {'ram_gb': ram, 'storage_gb': storage, 'network_generation': radio}
            if (row.get('id') != id or row.get('slug') != slug
                    or row.get('expected_variant') != {'name': name, **specs, 'color': color}
                    or row.get('catalog_reason') != 'catalog_exact'):
                return None
            attempts = row.get('source_attempts')
            if (not isinstance(attempts, list) or len(attempts) != 1
                    or attempts[0].get('source_url') != url
                    or attempts[0].get('merchant') != 'MediaMarkt'
                    or attempts[0].get('reason') != 'observed_exact_offer'
                    or attempts[0].get('http_status') != 200):
                return None
            offer = row.get('verified_offer')
            if not isinstance(offer, dict):
                return None
            price = offer.get('price')
            title = offer.get('source_title')
            if (offer.get('merchant') != 'MediaMarkt'
                    or offer.get('url') != url
                    or offer.get('currency') != 'TRY'
                    or offer.get('availability') not in {'in_stock', 'limited'}
                    or not isinstance(price, (int, float)) or isinstance(price, bool)
                    or not math.isfinite(price) or not 0 < price <= 5_000_000
                    or price != int(price)
                    or not isinstance(title, str)
                    or color not in _fold(title) or color not in _fold(url)
                    or not matcher.is_strict_match(name, title, specs)
                    or not matcher.has_observed_offer_evidence(name, offer, specs)):
                return None
            observed = _time(offer['observed_at'])
            if (not -60 <= (now - observed).total_seconds() <= MAX_PREFLIGHT_AGE_SECONDS
                    or not -60 <= (checked - observed).total_seconds() <= MAX_PREFLIGHT_AGE_SECONDS):
                return None
            offers[url] = dict(offer)
        return offers if len(offers) == len(PROFILES) else None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def load_verified_offer(path, url, now=None):
    if url not in PROFILES:
        return None
    try:
        report = json.loads(Path(path).read_text())
    except (OSError, TypeError, ValueError):
        return None
    return (validated_offers(report, now) or {}).get(url)
