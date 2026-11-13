"""Durable local state for exact retailer crawl targets and verified observations."""
from contextlib import contextmanager
from datetime import datetime
import json
from math import ceil
import os
from pathlib import Path
import sqlite3
from time import time
from urllib.parse import urlsplit
from uuid import uuid4


DAY = 24 * 60 * 60
LEASE_SECONDS = 180
DEFAULT_HTTP_BUDGET = 60
MIN_REQUEST_INTERVAL = 1
FRESH_SECONDS = DAY
RETRY_SECONDS = 15 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    host TEXT PRIMARY KEY,
    lease TEXT,
    lease_until INTEGER NOT NULL DEFAULT 0,
    next_request INTEGER NOT NULL DEFAULT 0,
    blocked_until INTEGER NOT NULL DEFAULT 0,
    blocked_reason TEXT
);
CREATE TABLE IF NOT EXISTS targets (
    url TEXT PRIMARY KEY,
    host TEXT NOT NULL REFERENCES stores(host),
    category TEXT NOT NULL,
    gtin TEXT NOT NULL,
    route TEXT NOT NULL,
    due INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    inflight TEXT,
    last_outcome TEXT,
    observed INTEGER,
    expires INTEGER,
    offer TEXT
);
CREATE INDEX IF NOT EXISTS targets_due ON targets(host, due, priority DESC, url);
CREATE INDEX IF NOT EXISTS targets_category_due ON targets(host, category, due, priority DESC, url);
CREATE INDEX IF NOT EXISTS targets_verified_due ON targets(host, category, due, priority DESC, url) WHERE expires IS NOT NULL;
CREATE INDEX IF NOT EXISTS targets_coverage ON targets(category, gtin, host, observed);
CREATE TABLE IF NOT EXISTS rotations (
    host TEXT PRIMARY KEY REFERENCES stores(host),
    category TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    host TEXT NOT NULL REFERENCES stores(host),
    requested INTEGER NOT NULL,
    PRIMARY KEY(host, requested)
);
CREATE TABLE IF NOT EXISTS request_counts (
    host TEXT PRIMARY KEY REFERENCES stores(host),
    count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS store_policy (
    host TEXT PRIMARY KEY REFERENCES stores(host),
    daily_budget INTEGER NOT NULL,
    min_interval INTEGER NOT NULL
);
"""


class LeaseLost(RuntimeError):
    pass


class SourceDeferred(RuntimeError):
    def __init__(self, reason, retry_at=0):
        super().__init__(reason)
        self.reason = reason
        self.retry_at = retry_at


class CatalogQueue:
    def __init__(self, path, *, clock=time):
        self.clock = clock
        path = Path(path)
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            if path.stat().st_mode & 0o077:
                raise ValueError('Queue files must be private to their owner')
        self.db = sqlite3.connect(path, timeout=20, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    @contextmanager
    def write(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def enqueue(self, routes, *, conflicts=None):
        """Insert price-free routes once. A URL cannot silently change identity."""
        now = int(self.clock())
        inserted = 0
        with self.write():
            hosts = set()
            for host, category, route, priority in routes:
                if host not in hosts:
                    self.db.execute('INSERT OR IGNORE INTO stores(host) VALUES (?)', (host,))
                    hosts.add(host)
                payload = json.dumps(route, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                url = route['url']
                cursor = self.db.execute(
                    """INSERT INTO targets(url, host, category, gtin, route, due, priority)
                       VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(url) DO NOTHING""",
                    (url, host, category, route['gtin'], payload, now, priority))
                if cursor.rowcount:
                    inserted += 1
                else:
                    previous = self.db.execute('SELECT category, route FROM targets WHERE url = ?', (url,)).fetchone()
                    if tuple(previous) != (category, payload):
                        if conflicts is not None:
                            conflicts['conflicting_target_identity'] += 1
                            continue
                        raise ValueError(f'Conflicting identity for an existing {host} destination')
                    self.db.execute('UPDATE targets SET priority = max(priority, ?) WHERE url = ?', (priority, url))
        return inserted

    def hosts(self):
        return [row[0] for row in self.db.execute('SELECT host FROM stores ORDER BY host')]

    def configure(self, host, daily_budget, min_interval):
        if (type(daily_budget) is not int or type(min_interval) is not int
                or not MIN_REQUEST_INTERVAL <= min_interval <= 30 or not 1 <= daily_budget <= DAY // min_interval):
            raise ValueError('The store budget must fit its minimum request interval')
        with self.write():
            if host not in self.hosts():
                raise ValueError('Store has no crawl state')
            self.db.execute("""INSERT INTO store_policy VALUES (?, ?, ?)
                              ON CONFLICT(host) DO UPDATE SET daily_budget = excluded.daily_budget,
                                  min_interval = excluded.min_interval""", (host, daily_budget, min_interval))

    def acquire(self, host, *, discovery=False, category=None):
        now = int(self.clock())
        lease = uuid4().hex
        with self.write():
            if discovery:
                self.db.execute('INSERT OR IGNORE INTO stores(host) VALUES (?)', (host,))
            cursor = self.db.execute(
                """UPDATE stores SET lease = ?, lease_until = ? WHERE host = ?
                   AND lease_until <= ? AND blocked_until <= ?
                   AND (? OR EXISTS(SELECT 1 FROM targets WHERE host = ? AND due <= ?
                                    AND (? IS NULL OR category = ?)))""",
                (lease, now + LEASE_SECONDS, host, now, now, discovery, host, now, category, category))
        return lease if cursor.rowcount else None

    def _owned(self, host, lease, now):
        store = self.db.execute(
            'SELECT * FROM stores WHERE host = ? AND lease = ? AND lease_until > ?',
            (host, lease, now)).fetchone()
        if store is None:
            raise LeaseLost(f'Expired crawl lease for {host}')
        return store

    def _renew(self, host, now):
        self.db.execute('UPDATE stores SET lease_until = ? WHERE host = ?', (now + LEASE_SECONDS, host))

    def next_target(self, host, lease, *, category=None, focus_gtins=()):
        now = int(self.clock())
        with self.write():
            store = self._owned(host, lease, now)
            if store['blocked_until'] > now:
                return None
            category = category or self._next_category(host, now)
            if category is None:
                return None
            target = self.db.execute(
                """SELECT url, category, route FROM targets WHERE host = ? AND category = ? AND due <= ?
                   AND expires IS NOT NULL AND expires > ? ORDER BY due, priority DESC, url LIMIT 1""",
                (host, category, now, now)).fetchone()
            if target is None and category == 'monitor' and focus_gtins:
                placeholders = ','.join('?' for _ in focus_gtins)
                target = self.db.execute(
                    f"""SELECT url, category, route FROM targets WHERE host = ? AND category = ? AND due <= ?
                        AND gtin IN ({placeholders})
                        ORDER BY last_outcome IS NOT NULL, priority DESC, due, url LIMIT 1""",
                    (host, category, now, *focus_gtins)).fetchone()
            if target is None:
                target = self.db.execute(
                    """SELECT url, category, route FROM targets WHERE host = ? AND category = ? AND due <= ?
                       ORDER BY due, priority DESC, url LIMIT 1""", (host, category, now)).fetchone()
            if target is None:
                return None
            self.db.execute('UPDATE targets SET due = ?, inflight = ? WHERE url = ?',
                            (now + LEASE_SECONDS, lease, target['url']))
            self.db.execute("""INSERT INTO rotations VALUES (?, ?)
                              ON CONFLICT(host) DO UPDATE SET category = excluded.category""",
                            (host, target['category']))
            self._renew(host, now)
        return target['category'], json.loads(target['route'])

    def _next_category(self, host, now):
        last = self.db.execute('SELECT category FROM rotations WHERE host = ?', (host,)).fetchone()
        selected = self.db.execute(
            """SELECT category FROM targets WHERE host = ? AND category > ? AND due <= ?
               ORDER BY category LIMIT 1""", (host, last[0] if last else '', now)).fetchone()
        if selected is None and last:
            selected = self.db.execute('SELECT category FROM targets WHERE host = ? AND due <= ? ORDER BY category LIMIT 1',
                                       (host, now)).fetchone()
        return selected[0] if selected else None

    def before_request(self, host, lease):
        """Reserve a real outbound GET including redirects and seller pages."""
        clock = self.clock()
        now = int(clock)
        with self.write():
            store = self._owned(host, lease, now)
            if store['blocked_until'] > now:
                return store['blocked_reason'], 0
            if store['next_request'] > now:
                return 'wait', store['next_request']
            policy = self.db.execute('SELECT daily_budget, min_interval FROM store_policy WHERE host = ?', (host,)).fetchone()
            budget, interval = tuple(policy) if policy else (DEFAULT_HTTP_BUDGET, MIN_REQUEST_INTERVAL)
            saved = self.db.execute('SELECT count FROM request_counts WHERE host = ?', (host,)).fetchone()
            removed = self.db.execute('DELETE FROM requests WHERE host = ? AND requested <= ?', (host, now - DAY)).rowcount
            count = saved['count'] - removed if saved else self.db.execute(
                'SELECT count(*) FROM requests WHERE host = ?', (host,)).fetchone()[0]
            if count >= budget:
                # A reduced quota may need more than the oldest request to expire.
                first = self.db.execute('SELECT requested FROM requests WHERE host = ? ORDER BY requested LIMIT 1 OFFSET ?',
                                        (host, count - budget)).fetchone()[0]
                if removed:
                    self.db.execute('UPDATE request_counts SET count = ? WHERE host = ?', (count, host))
                return 'http_budget_exhausted', first + DAY
            self.db.execute('INSERT INTO requests VALUES (?, ?)', (host, now))
            self.db.execute("""INSERT INTO request_counts VALUES (?, ?)
                              ON CONFLICT(host) DO UPDATE SET count = excluded.count""", (host, count + 1))
            self.db.execute('UPDATE stores SET next_request = ? WHERE host = ?',
                            (ceil(clock + interval), host))
            self._renew(host, now)
        return 'ready', 0

    def block(self, host, lease, reason, seconds):
        now = int(self.clock())
        with self.write():
            self._owned(host, lease, now)
            self.db.execute('UPDATE stores SET blocked_until = ?, blocked_reason = ? WHERE host = ?',
                            (now + seconds, reason, host))

    def complete(self, host, lease, route, outcome, offer=None, *, retry_at=None, keep=False):
        """Commit one result. A restarted worker cannot overwrite a new lease."""
        now = int(self.clock())
        payload, observed, expires = None, None, None
        if offer is not None:
            dt = datetime.fromisoformat(offer['observed_at'])
            if dt.tzinfo is None:
                raise ValueError('Offer timestamps must be timezone-aware')
            observed = int(dt.timestamp())
            if (keep or outcome != 'verified' or offer.get('gtin13') != route['gtin'] or not offer.get('_legal_name')
                    or not offer.get('merchant') or urlsplit(offer.get('url', '')).netloc != host
                    or offer.get('currency') != 'TRY' or offer.get('condition') != 'new'
                    or offer.get('availability') != 'in_stock'
                    or type(offer.get('price')) not in (int, float) or not 0 < offer['price'] <= 1_000_000
                    or not now - FRESH_SECONDS < observed <= now):
                raise ValueError('Observation did not pass the queue evidence contract')
            payload = json.dumps(offer, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            expires = observed + FRESH_SECONDS
            if offer.get('price_kind') == 'basket' and offer.get('basket_promotion'):
                deadline = datetime.fromisoformat(offer['basket_promotion']['valid_until'])
                if deadline.tzinfo is None or deadline.timestamp() <= now:
                    raise ValueError('Basket promotion has expired')
                expires = min(expires, int(deadline.timestamp()))
        elif outcome == 'verified':
            raise ValueError('A verified outcome requires an observation')
        # Refresh before expiry. A transport failure cannot extend old evidence.
        if retry_at is not None:
            due = retry_at
        elif expires:
            due = max(now + RETRY_SECONDS, expires - 60 * 60)
        elif outcome in {'barcode_changed', 'http_404', 'http_410'}:
            due = now + 7 * DAY
        else:
            due = now + 6 * 60 * 60
        with self.write():
            self._owned(host, lease, now)
            cursor = self.db.execute(
                """UPDATE targets SET due = ?, inflight = NULL, last_outcome = ?,
                   observed = CASE WHEN ? THEN observed ELSE ? END,
                   expires = CASE WHEN ? THEN expires ELSE ? END,
                   offer = CASE WHEN ? THEN offer ELSE ? END
                   WHERE host = ? AND url = ? AND gtin = ? AND inflight = ?""",
                (due, outcome, keep, observed, keep, expires, keep, payload, host, route['url'], route['gtin'], lease))
            if cursor.rowcount != 1:
                raise LeaseLost('Result does not belong to the active target')
            self._renew(host, now)

    def release(self, host, lease):
        self.db.execute('UPDATE stores SET lease = NULL, lease_until = 0 WHERE host = ? AND lease = ?', (host, lease))

    def fresh_offers(self, category, *, since=0, now=None, gtin=None):
        now = int(self.clock()) if now is None else now
        scope = ' AND gtin = ?' if gtin is not None else ''
        params = (category, now, since, now, gtin) if gtin is not None else (category, now, since, now)
        for row in self.db.execute(
                'SELECT offer FROM targets WHERE category = ? AND expires > ? AND observed BETWEEN ? AND ?'
                + scope + ' ORDER BY gtin', params):
            yield json.loads(row['offer'])

    def report(self):
        now = int(self.clock())
        totals = [dict(row) for row in self.db.execute(
            """SELECT category, count(DISTINCT gtin) AS variants, count(*) AS routes,
               sum(due <= ?) AS due, sum(last_outcome IS NULL) AS unread,
               count(DISTINCT CASE WHEN expires > ? AND observed <= ? THEN gtin END) AS fresh_variants
               FROM targets GROUP BY category ORDER BY category""", (now, now, now))]
        stores = [dict(row) for row in self.db.execute(
            """SELECT s.host, count(t.url) AS routes, coalesce(sum(t.due <= ?), 0) AS due,
               s.blocked_until, s.blocked_reason, coalesce(p.daily_budget, ?) AS http_budget_24h,
               coalesce(p.min_interval, ?) AS minimum_request_interval_seconds,
               (SELECT count(*) FROM requests r WHERE r.host = s.host AND r.requested > ?) AS http_last_24h
               FROM stores s LEFT JOIN targets t ON t.host = s.host LEFT JOIN store_policy p ON p.host = s.host
               GROUP BY s.host ORDER BY s.host""",
            (now, DEFAULT_HTTP_BUDGET, MIN_REQUEST_INTERVAL, now - DAY))]
        for store in stores:
            store['minimum_refresh_days'] = round(store['routes'] / store['http_budget_24h'], 2)
        outcomes = [dict(row) for row in self.db.execute(
            """SELECT host, last_outcome AS outcome, count(*) AS routes FROM targets
               WHERE last_outcome IS NOT NULL GROUP BY host, last_outcome ORDER BY host, last_outcome""")]
        return {'categories': totals, 'stores': stores, 'latest_outcomes': outcomes}
