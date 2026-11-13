"""Validate a fresh two-retailer Fold8 Ultra audit before a scoped write."""

from datetime import datetime, timezone
import json
import math
from pathlib import Path

from tr_price_scraper import TRPriceScraper


MAX_PREFLIGHT_AGE_SECONDS = 5 * 60
PROFILE = (293, 'samsung-galaxy-z-fold8-ultra',
           'Samsung Galaxy Z Fold8 Ultra', 12, 256, '5g')
URLS = {
    'https://emagaza.turktelekom.com.tr/tr/products/samsung/samsung-galaxy-z-fold8-ultra?v=2908':
        'Türk Telekom eMağaza',
    'https://www.turkcell.com.tr/pasaj/cep-telefonu/android-telefonlar/samsung-galaxy-z-fold8-ultra-12gb-256gb':
        'Pasaj',
}
OFFER_KEYS = frozenset({'merchant', 'price', 'currency', 'availability',
                        'source_title', 'url', 'observed_at'})


def _time(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('timezone missing')
    return stamp.astimezone(timezone.utc)


def validated_offers(report, now=None):
    """Return both exact observations, or None for any missing/stale evidence."""
    try:
        now = now or datetime.now(timezone.utc)
        if (not isinstance(report, dict)
                or report.get('purpose') != 'read_only_fold8_ultra_two_first_party_runner_proof'
                or report.get('status') != 'passed'
                or report.get('writes') is not False
                or report.get('catalog_reason') != 'catalog_exact'
                or report.get('category_reason') != 'exact_stock_card'):
            return None
        id, slug, name, ram, storage, radio = PROFILE
        specs = {'ram_gb': ram, 'storage_gb': storage, 'network_generation': radio}
        if report.get('product') != {'id': id, 'slug': slug, 'name': name,
                                     'expected_specs': specs}:
            return None
        checked = _time(report['checked_at'])
        if now.tzinfo is None or not -60 <= (now - checked).total_seconds() <= MAX_PREFLIGHT_AGE_SECONDS:
            return None
        sources = report.get('sources')
        if (not isinstance(sources, dict)
                or sources.get('turk_telekom', {}).get('status') != 'ready'):
            return None
        attempts = report.get('source_attempts')
        if not isinstance(attempts, list) or len(attempts) != 2:
            return None
        checked_urls = set()
        for row in attempts:
            url = row.get('source_url')
            if (url not in URLS or url in checked_urls
                    or row.get('merchant') != URLS[url]
                    or row.get('reason') != 'observed_exact_cash_offer'
                    or row.get('http_status') != 200):
                return None
            checked_urls.add(url)
        if checked_urls != set(URLS):
            return None
        rows = report.get('verified_offers')
        if not isinstance(rows, list) or len(rows) != 2:
            return None
        matcher = TRPriceScraper()
        offers = {}
        for offer in rows:
            if not isinstance(offer, dict) or set(offer) != OFFER_KEYS:
                return None
            url = offer['url']
            price = offer['price']
            if (url not in URLS or url in offers
                    or offer['merchant'] != URLS[url]
                    or offer['currency'] != 'TRY'
                    or offer['availability'] != 'in_stock'
                    or not isinstance(price, (int, float)) or isinstance(price, bool)
                    or not math.isfinite(price) or not 0 < price <= 5_000_000
                    or not isinstance(offer['source_title'], str)
                    or not matcher.is_strict_match(name, offer['source_title'], specs)
                    or not matcher.has_observed_offer_evidence(name, offer, specs)):
                return None
            observed = _time(offer['observed_at'])
            if (not -60 <= (now - observed).total_seconds() <= MAX_PREFLIGHT_AGE_SECONDS
                    or not -60 <= (checked - observed).total_seconds() <= MAX_PREFLIGHT_AGE_SECONDS):
                return None
            offers[url] = dict(offer)
        return offers if set(offers) == set(URLS) else None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def load_verified_offers(path, now=None):
    try:
        report = json.loads(Path(path).read_text())
    except (OSError, TypeError, ValueError):
        return None
    return validated_offers(report, now)
