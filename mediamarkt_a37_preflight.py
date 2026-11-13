"""Five-minute handoff for two exact A37 5G marketplace observations."""

from datetime import datetime, timezone
import json
from pathlib import Path

from mediamarkt_marketplace_source import A37_PROFILES, reviewed_marketplace_offer


MAX_AGE_SECONDS = 5 * 60


def _time(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('timezone missing')
    return stamp.astimezone(timezone.utc)


def validated_offers(report, now=None):
    try:
        now = now or datetime.now(timezone.utc)
        if (not isinstance(report, dict)
                or report.get('purpose') != 'read_only_mediamarkt_a37_two_selected_offers'
                or report.get('status') != 'passed' or report.get('writes') is not False):
            return None
        checked = _time(report['checked_at'])
        if now.tzinfo is None or not -60 <= (now - checked).total_seconds() <= MAX_AGE_SECONDS:
            return None
        rows = report.get('rows')
        if not isinstance(rows, list) or len(rows) != len(A37_PROFILES):
            return None
        selected = {}
        for row in rows:
            if not isinstance(row, dict):
                return None
            url = row.get('url')
            profile = A37_PROFILES.get(url)
            product = row.get('product')
            offer = row.get('verified_offer')
            attempt = row.get('source_attempt')
            if url in selected or profile is None or not isinstance(product, dict):
                return None
            specs = {key: profile[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
            if (row.get('catalog_reason') != 'catalog_exact'
                    or row.get('merchant_collision') is not False
                    or product.get('id') != profile['id']
                    or product.get('slug') != profile['slug']
                    or product.get('name') != profile['name']
                    or product.get('expected_specs') != specs
                    or not isinstance(attempt, dict)
                    or attempt.get('source_url') != url
                    or attempt.get('merchant') != profile['seller_name']
                    or attempt.get('reason') != 'observed_exact_new_marketplace_offer'
                    or attempt.get('http_status') != 200
                    or not reviewed_marketplace_offer(offer, profile['name'], specs)):
                return None
            observed = _time(offer['observed_at'])
            if (not -60 <= (now - observed).total_seconds() <= MAX_AGE_SECONDS
                    or not -60 <= (checked - observed).total_seconds() <= MAX_AGE_SECONDS):
                return None
            selected[url] = dict(offer)
        if (set(selected) != set(A37_PROFILES)
                or {row['seller_name'] for row in selected.values()} !=
                    {profile['seller_name'] for profile in A37_PROFILES.values()}):
            return None
        return selected
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None


def load_verified_offers(path, now=None):
    try:
        report = json.loads(Path(path).read_text())
    except (OSError, TypeError, ValueError):
        return None
    return validated_offers(report, now)
