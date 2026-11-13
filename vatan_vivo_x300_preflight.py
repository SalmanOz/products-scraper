"""Only a fresh exact Vivo Vatan PDP audit can authorize its offer write."""

import json
import math
from datetime import datetime, timezone
from pathlib import Path

from audit_vatan_vivo_exact_readonly import PROFILE, SELECTED_H1, SPECS, URL
from tr_price_scraper import TRPriceScraper


MAX_PREFLIGHT_AGE_SECONDS = 5 * 60


def _time(value):
    if not isinstance(value, str):
        raise ValueError('timestamp missing')
    timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if timestamp.tzinfo is None:
        raise ValueError('timezone missing')
    return timestamp.astimezone(timezone.utc)


def validated_offer(report, now=None):
    try:
        now = now or datetime.now(timezone.utc)
        if (now.tzinfo is None or not isinstance(report, dict)
                or report.get('purpose') != 'read_only_vatan_vivo_exact_selected_pdp_proof'
                or report.get('writes') is not False
                or report.get('status') != 'passed'
                or report.get('catalog_status') != 'ready'
                or report.get('catalog_confirmed') is not True
                or report.get('catalog_variant') != {
                    'id': PROFILE['id'], 'slug': PROFILE['slug'],
                    'name': PROFILE['name'], 'specs': SPECS}
                or report.get('url') != URL
                or report.get('offer_confirmed') is not True):
            return None
        checked = _time(report['checked_at'])
        if not -60 <= (now - checked).total_seconds() <= MAX_PREFLIGHT_AGE_SECONDS:
            return None
        attempts = report.get('source_attempts')
        if (not isinstance(attempts, list) or len(attempts) != 1
                or attempts[0].get('source_url') != URL
                or attempts[0].get('merchant') != 'Vatan Bilgisayar'
                or attempts[0].get('reason') != 'observed_exact_offer'
                or attempts[0].get('http_status') != 200):
            return None
        offer = report.get('offer')
        if not isinstance(offer, dict):
            return None
        price = offer.get('price')
        if (offer.get('merchant') != 'Vatan Bilgisayar'
                or offer.get('url') != URL
                or offer.get('currency') != 'TRY'
                or offer.get('availability') != 'in_stock'
                or offer.get('selected_vatan_variant_verified') is not True
                or offer.get('source_title') != SELECTED_H1
                or not isinstance(price, (int, float)) or isinstance(price, bool)
                or not math.isfinite(price) or not 0 < price <= 5_000_000
                or not isinstance(offer.get('source_title'), str)
                or not TRPriceScraper().has_observed_offer_evidence(
                    PROFILE['name'], offer, SPECS)):
            return None
        observed = _time(offer['observed_at'])
        if (not -60 <= (now - observed).total_seconds() <= MAX_PREFLIGHT_AGE_SECONDS
                or not -60 <= (checked - observed).total_seconds() <= MAX_PREFLIGHT_AGE_SECONDS):
            return None
        return dict(offer)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def load_verified_offer(path, now=None):
    try:
        report = json.loads(Path(path).read_text())
    except (OSError, TypeError, ValueError):
        return None
    return validated_offer(report, now)
