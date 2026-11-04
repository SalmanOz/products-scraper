"""Bounded continuation of observed public category URLs; never offer evidence.

Prices, source documents and seller/SKU observations are deliberately absent.
Every restored destination is read again under the ordinary source policy.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

VERSION = 'public-hb-category-frontier-v1'
STATE_PATH = Path('.discovery-state/priority-hb-category.json')
MAX_AGE_SECONDS = 6 * 60 * 60
MAX_PENDING = 512
MAX_VISITED = 2048
MAX_BYTES = 640 * 1024
FIELDS = {'version', 'catalog_fingerprint', 'cycle_started_at', 'updated_at', 'pending', 'visited'}


def catalog_fingerprint(products):
    from update_prices import PriceUpdater
    identities = []
    for product in products:
        specs = PriceUpdater.get_expected_specs(product.get('attributes'))
        identities.append([product['id'], product['slug'], product['name'],
                           *[specs.get(k) for k in ('ram_gb', 'storage_gb', 'network_generation')]])
    value = json.dumps(sorted(identities, key=lambda p: p[0]), ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(value.encode()).hexdigest()


def category_url(value):
    from hepsiburada_catalog_discovery import category_identity
    return (isinstance(value, str) and len(value) <= 200
            and not re.search(r'[\x00-\x20\\]', value) and category_identity(value) is not None)


def _urls(value, maximum):
    return (isinstance(value, list) and len(value) <= maximum
            and all(category_url(url) for url in value) and len(value) == len(set(value)))


def _time(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('An aware timestamp is required')
    return stamp


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate state key')
        result[key] = value
    return result


def load_frontier(path, products, *, now=None):
    now = now or datetime.now(timezone.utc)
    if path is None:
        return {'catalog_fingerprint': None, 'cycle_started_at': now.isoformat(),
                'pending': [], 'visited': []}, 'disabled'
    empty = {'catalog_fingerprint': catalog_fingerprint(products),
             'cycle_started_at': now.isoformat(), 'pending': [], 'visited': []}
    path = Path(path)
    try:
        if not path.exists():
            return empty, 'absent'
        if path.is_symlink():
            return empty, 'invalid'
        with path.open('rb') as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return empty, 'oversized'
        data = json.loads(raw, object_pairs_hook=_unique_object)
        if (not isinstance(data, dict) or set(data) != FIELDS or data['version'] != VERSION
                or not _urls(data['pending'], MAX_PENDING) or not _urls(data['visited'], MAX_VISITED)
                or set(data['pending']) & set(data['visited'])):
            return empty, 'invalid'
        started, updated = _time(data['cycle_started_at']), _time(data['updated_at'])
        if (updated < started or (now - started).total_seconds() < -60
                or (now - updated).total_seconds() < -60):
            return empty, 'invalid'
        if (now - started).total_seconds() > MAX_AGE_SECONDS:
            return empty, 'expired'
        if data['catalog_fingerprint'] != empty['catalog_fingerprint']:
            # New physical variants change which cards need a fresh selected
            # PDP read. Never reuse the old visited/exclusion set. However,
            # already-observed unread category URLs remain useful routing
            # hints: dropping them on every successful variant publication
            # repeatedly spends the budget on the same category prefix.
            return {**empty, 'cycle_started_at': data['cycle_started_at'],
                    'pending': data['pending']}, 'catalog_changed_pending_retained'
        if not data['pending']:
            return empty, 'drained'  # Start a fresh discovery cycle next time.
        return {k: data[k] for k in ('catalog_fingerprint', 'cycle_started_at', 'pending', 'visited')}, 'ready'
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        return empty, 'invalid'


def save_frontier(path, result, *, now=None):
    """Save a completed category pass's URL queue independently of later prices."""
    continuation = result.get('hepsiburada_category_continuation')
    discovery = result.get('hepsiburada_category_discovery')
    if not continuation or not discovery or continuation.get('status') == 'disabled':
        return {'status': 'not_enabled'}
    pending = discovery.get('pending_observed_urls', [])
    visited = discovery.get('visited_category_urls', [])
    if not _urls(pending, MAX_PENDING) or not _urls(visited, MAX_VISITED) or set(pending) & set(visited):
        return {'status': 'invalid_or_oversized_queue'}
    now = now or datetime.now(timezone.utc)
    data = {'version': VERSION, 'catalog_fingerprint': continuation['catalog_fingerprint'],
            'cycle_started_at': continuation['cycle_started_at'], 'updated_at': now.isoformat(),
            'pending': pending, 'visited': visited}
    raw = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode()
    if len(raw) > MAX_BYTES:
        return {'status': 'oversized'}
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_bytes(raw)
        temporary.replace(path)
    except OSError:
        return {'status': 'unavailable'}
    return {'status': 'saved', 'pending': len(pending), 'visited': len(visited),
            'prices_or_offer_evidence_cached': False}
