"""Five-minute handoff for one reviewed, exact-GTIN S24 FE offer."""

from datetime import datetime, timezone
import json
from pathlib import Path

from mediamarkt_marketplace_source import (
    S24FE_PROFILES, S24FE_RADIO_EVIDENCE, S24FE_URL,
    reviewed_marketplace_offer,
)


MAX_AGE_SECONDS = 5 * 60
PROFILE = S24FE_PROFILES[S24FE_URL]


def _time(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('timezone missing')
    return stamp.astimezone(timezone.utc)


def validated_offer(report, now=None):
    try:
        now = now or datetime.now(timezone.utc)
        if (not isinstance(report, dict)
                or report.get('purpose') != 'read_only_mediamarkt_s24fe_exact_gtin'
                or report.get('status') != 'passed' or report.get('writes') is not False
                or report.get('robots_status') != 'ready'
                or report.get('radio_evidence') != S24FE_RADIO_EVIDENCE):
            return None
        checked = _time(report['checked_at'])
        if now.tzinfo is None or not -60 <= (now - checked).total_seconds() <= MAX_AGE_SECONDS:
            return None
        rows = report.get('rows')
        if not isinstance(rows, list) or len(rows) != 1:
            return None
        row = rows[0]
        product = row.get('product')
        attempt = row.get('source_attempt')
        offer = row.get('verified_offer')
        specs = {key: PROFILE[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
        if (row.get('url') != S24FE_URL or row.get('catalog_reason') != 'catalog_exact'
                or row.get('merchant_collision') is not False
                or not isinstance(product, dict)
                or product.get('id') != PROFILE['id']
                or product.get('slug') != PROFILE['slug']
                or product.get('name') != PROFILE['name']
                or product.get('expected_specs') != specs
                or not isinstance(attempt, dict)
                or attempt.get('source_url') != S24FE_URL
                or attempt.get('merchant') != PROFILE['seller_name']
                or attempt.get('reason') != 'observed_exact_new_marketplace_offer'
                or attempt.get('http_status') != 200
                or not reviewed_marketplace_offer(offer, PROFILE['name'], specs)):
            return None
        observed = _time(offer['observed_at'])
        if (not -60 <= (now - observed).total_seconds() <= MAX_AGE_SECONDS
                or not -60 <= (checked - observed).total_seconds() <= MAX_AGE_SECONDS):
            return None
        return {S24FE_URL: dict(offer)}
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None


def load_verified_offer(path, now=None):
    try:
        report = json.loads(Path(path).read_text())
    except (OSError, TypeError, ValueError):
        return None
    return validated_offer(report, now)
