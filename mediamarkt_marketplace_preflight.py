"""Five-minute same-run handoff for two exact new marketplace seller offers."""

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import urlparse

from mediamarkt_marketplace_source import PROFILES, reviewed_marketplace_offer


MAX_AGE_SECONDS = 5 * 60


def _time(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('timezone missing')
    return stamp.astimezone(timezone.utc)


def no_merchant_collision(product, profile, url, expected_host='www.mediamarkt.com.tr'):
    """A seller name may identify only one platform for this catalog product."""
    if expected_host not in {'www.mediamarkt.com.tr', 'www.hepsiburada.com', 'www.trendyol.com', 'www.teknosa.com', 'www.n11.com'} or urlparse(url).hostname != expected_host:
        return False
    targets = product.get('offer_targets')
    truncated = product.get('offer_targets_truncated')
    total = product.get('offer_targets_total')
    snapshot = product.get('offer_targets_snapshot')
    if (not isinstance(targets, list) or len(targets) > 60 or
            truncated is True or
            (truncated is not None and not isinstance(truncated, bool)) or
            (len(targets) == 12 and truncated is not False)):
        return False  # A full page is safe only when the API proves completeness.
    if (len(targets) > 12 and (truncated is not False or
            type(total) is not int or total != len(targets) or
            not isinstance(snapshot, str) or
            not re.fullmatch(r'[0-9a-f]{64}', snapshot))):
        return False  # Multi-page evidence must prove a complete stable snapshot.
    for row in targets:
        if not isinstance(row, dict):
            return False
        seller = row.get('seller_name') if 'seller_name' in row else row.get('merchant_name')
        if not isinstance(seller, str) or not seller.strip():
            return False
        if seller.strip().casefold() != profile['seller_name'].casefold():
            continue
        source = row.get('source_url')
        if (not isinstance(source, str) or source != url or
                urlparse(source).hostname != expected_host):
            return False
    return True


def validated_offers(report, now=None):
    try:
        now = now or datetime.now(timezone.utc)
        if (not isinstance(report, dict)
                or report.get('purpose') != 'read_only_mediamarkt_marketplace_two_selected_offers'
                or report.get('status') != 'passed' or report.get('writes') is not False):
            return None
        checked = _time(report['checked_at'])
        if now.tzinfo is None or not -60 <= (now - checked).total_seconds() <= MAX_AGE_SECONDS:
            return None
        rows = report.get('rows')
        if not isinstance(rows, list) or len(rows) != len(PROFILES):
            return None
        selected = {}
        for row in rows:
            if not isinstance(row, dict):
                return None
            url = row.get('url')
            profile = PROFILES.get(url)
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
        return selected if set(selected) == set(PROFILES) else None
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None


def load_verified_offers(path, now=None):
    try:
        report = json.loads(Path(path).read_text())
    except (OSError, TypeError, ValueError):
        return None
    return validated_offers(report, now)
