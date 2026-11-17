"""Offline merge contracts for independently verified local/cloud crawl state."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from retail_html_catalog import database
from retail_catalog_publish import invalidate_changed_identities
import retail_crawl_state as state


EARLY = '2026-11-17T08:00:00+00:00'
STARTED = '2026-11-17T09:00:00+00:00'
RECENT = '2026-11-17T10:00:00+00:00'
LATEST = '2026-11-17T11:00:00+00:00'
NOW = '2026-11-17T12:00:00+00:00'
GTIN = '0194644186333'
URL = 'https://www.trendyol.com/example/exact-product-p-123456789'


def observed(url=URL, checked=RECENT, price=1000, **extra):
    return {'gtin': GTIN, 'kind': 'earbuds', 'name': 'Example Headphones', 'source': url,
            'checkedAt': checked, 'specs': {'Bluetooth': '5.3'},
            'offer': {'url': url, 'price': price, 'checkedAt': checked, 'platform': 'Trendyol',
                      'merchant': 'Exact Seller'}, **extra}


def put(db, url, row, status='verified', checked=RECENT):
    db.execute('INSERT OR REPLACE INTO queue VALUES(?,?,?,?,?,?,?)',
               (url, 'www.trendyol.com', 'earbuds', 'Example Headphones', status,
                json.dumps(row) if row is not None else None, checked))
    db.execute('INSERT OR REPLACE INTO catalog_targets VALUES(?,?,?)', (url, GTIN, 'earbuds'))


def queue_rows(path):
    with closing(sqlite3.connect(path)) as db:
        return {url: (status, json.loads(data) if data else None) for url, status, data in
                db.execute('SELECT url,status,data FROM queue')}


class LocalStateMergeTest(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.local, self.remote, self.output = [self.root / (name + '.sqlite') for name in ('local', 'remote', 'output')]
        for path in (self.local, self.remote):
            database(path).close()

    def test_newest_verified_observation_wins_and_both_independent_queues_survive(self):
        second, local_only, cloud_only = [URL + suffix for suffix in ('2', '3', '4')]
        with closing(database(self.local)) as db, db:
            put(db, URL, observed(checked=LATEST, price=1200))
            put(db, second, observed(second, EARLY, 2000))
            put(db, local_only, observed(local_only))
        with closing(database(self.remote)) as db, db:
            put(db, URL, observed(checked=RECENT, price=1000))
            put(db, second, observed(second, LATEST, 2200))
            put(db, cloud_only, observed(cloud_only))
        before_local, before_remote = queue_rows(self.local), queue_rows(self.remote)
        state.merge_queue(self.local, self.remote, self.output)
        merged = queue_rows(self.output)
        self.assertEqual(set(merged), {URL, second, local_only, cloud_only})
        self.assertEqual(merged[URL][1]['offer']['price'], 1200)
        self.assertEqual(merged[second][1]['offer']['price'], 2200)
        self.assertEqual(queue_rows(self.local), before_local)
        self.assertEqual(queue_rows(self.remote), before_remote)

    def test_newer_negative_observation_survives_older_positive_from_either_side(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                positive, negative = (self.local, self.remote) if reverse else (self.remote, self.local)
                for path in (positive, negative):
                    with closing(database(path)) as db, db:
                        db.execute('DELETE FROM queue')
                        db.execute('DELETE FROM identity_rejections')
                        db.execute('DELETE FROM refresh_attempts')
                with closing(database(positive)) as db, db:
                    put(db, URL, observed(checked=RECENT))
                with closing(database(negative)) as db, db:
                    put(db, URL, observed(checked=EARLY, offer=None, offerCheckedAt=LATEST))
                    db.execute('INSERT INTO refresh_attempts VALUES(?,?,?,?,?,?)',
                               (URL, 'www.trendyol.com', 'earbuds', 'verified', 200, LATEST))
                state.merge_queue(self.local, self.remote, self.output)
                self.assertIsNone(queue_rows(self.output)[URL][1]['offer'])
                rejected, count = invalidate_changed_identities(
                    [{'gtin': GTIN, 'offers': [observed()['offer']]}], self.output)
                self.assertEqual(count, 1)
                self.assertEqual(rejected[0]['offers'], [])
                self.assertEqual(rejected[0]['offerRejections'][URL], LATEST)

    def test_newer_unavailable_attempt_does_not_erase_verified_facts_or_invent_other_attempts(self):
        untouched = URL + '2'
        with closing(database(self.local)) as db, db:
            put(db, URL, observed())
            put(db, untouched, None, 'pending', None)
            db.execute('INSERT INTO refresh_attempts VALUES(?,?,?,?,?,?)',
                       (URL, 'www.trendyol.com', 'earbuds', 'verified', 200, RECENT))
        with closing(database(self.remote)) as db, db:
            put(db, URL, None, 'unverified', LATEST)
            db.execute('INSERT INTO refresh_attempts VALUES(?,?,?,?,?,?)',
                       (URL, 'www.trendyol.com', 'earbuds', 'http_403', 403, LATEST))
        state.merge_queue(self.local, self.remote, self.output)
        self.assertEqual(queue_rows(self.output)[URL][1], observed())
        self.assertEqual(queue_rows(self.output)[untouched], ('pending', None))
        with closing(sqlite3.connect(self.output)) as db:
            self.assertEqual(db.execute('SELECT url,outcome,attempted_at FROM refresh_attempts').fetchall(),
                             [(URL, 'http_403', LATEST)])

    def test_remote_only_identity_rejection_survives_a_newer_different_local_identity(self):
        other_gtin = '0195951489018'
        with closing(database(self.local)) as db, db:
            put(db, URL, observed(checked=LATEST))
        with closing(database(self.remote)) as db, db:
            put(db, URL, observed(checked=EARLY, gtin=other_gtin, offer=None, offerCheckedAt=RECENT))
            db.execute('UPDATE catalog_targets SET gtin=? WHERE url=?', (other_gtin, URL))
            db.execute('INSERT INTO refresh_attempts VALUES(?,?,?,?,?,?)',
                       (URL, 'www.trendyol.com', 'earbuds', 'verified', 200, RECENT))
        state.merge_queue(self.local, self.remote, self.output)
        self.assertEqual(queue_rows(self.output)[URL][1]['gtin'], GTIN)
        rejected, count = invalidate_changed_identities(
            [{'gtin': other_gtin, 'offers': [observed()['offer']]}], self.output)
        self.assertEqual(count, 1)
        self.assertEqual(rejected[0]['offerRejections'][URL], RECENT)

    def test_schema_drift_and_input_overwrites_fail_before_original_state_changes(self):
        with closing(database(self.local)) as db, db:
            put(db, URL, observed())
        before = queue_rows(self.local)
        for output in (self.local, self.remote):
            with self.assertRaises(ValueError):
                state.merge_queue(self.local, self.remote, output)
        with closing(sqlite3.connect(self.remote)) as db, db:
            db.execute('ALTER TABLE queue ADD COLUMN future_schema_field TEXT')
        with self.assertRaises(ValueError):
            state.merge_queue(self.local, self.remote, self.output)
        self.assertEqual(queue_rows(self.local), before)

    def test_legacy_two_table_queues_migrate_without_synthesizing_attempts_or_dates(self):
        for path, url in ((self.local, URL), (self.remote, URL + '2')):
            with closing(database(path)) as db, db:
                put(db, url, observed(url, EARLY))
                for table in ('refresh_attempts', 'catalog_targets', 'identity_rejections', 'catalog_target_issues'):
                    db.execute('DROP TABLE ' + table)
        state.merge_queue(self.local, self.remote, self.output)
        merged = queue_rows(self.output)
        self.assertEqual(set(merged), {URL, URL + '2'})
        self.assertTrue(all(data['checkedAt'] == EARLY for _, data in merged.values()))
        with closing(sqlite3.connect(self.output)) as db:
            for table in ('refresh_attempts', 'catalog_targets', 'identity_rejections', 'catalog_target_issues'):
                self.assertEqual(db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
        with closing(sqlite3.connect(self.remote)) as db:
            self.assertEqual({r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")},
                             {'pages', 'queue'}, 'Read-only legacy input must remain untouched')

    def test_equal_time_exact_duplicates_merge_but_conflicting_prices_fail_closed(self):
        for path in (self.local, self.remote):
            with closing(database(path)) as db, db:
                put(db, URL, observed())
        state.merge_queue(self.local, self.remote, self.output)
        self.assertEqual(queue_rows(self.output)[URL][1], observed())
        with closing(database(self.remote)) as db, db:
            put(db, URL, observed(price=999))
        with self.assertRaises((ValueError, RuntimeError)):
            state.merge_queue(self.local, self.remote, self.output)

    def test_image_merge_cannot_revive_old_deleted_images_but_accepts_new_cloud_checks(self):
        local, remote, output = [self.root / (name + '-images.sqlite') for name in ('local', 'remote', 'output')]
        for path in (local, remote):
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE images(url TEXT PRIMARY KEY,verified INT NOT NULL,checked_at TEXT)')
        with closing(sqlite3.connect(local)) as db, db:
            db.executemany('INSERT INTO images VALUES(?,?,?)',
                           [('kept-local', 1, LATEST), ('newer-cloud', 1, EARLY), ('deleted-by-cloud', 1, EARLY)])
        with closing(sqlite3.connect(remote)) as db, db:
            db.executemany('INSERT INTO images VALUES(?,?,?)',
                           [('kept-local', 1, RECENT), ('newer-cloud', 1, RECENT),
                            ('deleted-before-run', 1, EARLY), ('checked-during-run', 1, RECENT)])
        state.merge_images(local, remote, output, STARTED)
        with closing(sqlite3.connect(output)) as db:
            self.assertEqual(dict(db.execute('SELECT url,checked_at FROM images')),
                             {'kept-local': LATEST, 'newer-cloud': RECENT, 'checked-during-run': RECENT})

    def test_empty_refresh_summary_does_not_claim_success_and_requires_a_timezone(self):
        since = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        report = state.refresh_summary(self.local, since)
        self.assertEqual(report['attempts'], 0)
        self.assertEqual(report['verifiedObservations'], 0)
        self.assertEqual(report['pricedObservations'], 0)
        self.assertEqual(report['health'], 'no_confirmed_refresh')
        with self.assertRaises(ValueError):
            state.refresh_summary(self.local, '2026-11-17T10:00:00')

    def test_partial_summary_counts_actual_window_attempts_separately_from_prices(self):
        now = datetime.now(timezone.utc)
        since = (now - timedelta(minutes=10)).isoformat()
        cases = [(-9, 'verified', True), (-8, 'http_403', False), (-7, 'identity_changed', False),
                 (-6, 'verified', False), (-15, 'verified', True), (60, 'verified', True)]
        with closing(database(self.local)) as db, db:
            for index, (minutes, outcome, priced) in enumerate(cases):
                url = URL + str(index)
                when = (now + timedelta(minutes=minutes)).isoformat()
                row = observed(url, when)
                if not priced:
                    row['offer'] = None
                put(db, url, row)
                db.execute('INSERT INTO refresh_attempts VALUES(?,?,?,?,?,?)',
                           (url, 'www.trendyol.com', 'earbuds', outcome, 403 if outcome == 'http_403' else 200, when))
            put(db, URL + 'unattempted', None, 'pending', None)
        report = state.refresh_summary(self.local, since)
        self.assertEqual(report['attempts'], 4)
        self.assertEqual(report['verifiedObservations'], 2)
        self.assertEqual(report['pricedObservations'], 1)
        self.assertEqual(report['byHost']['www.trendyol.com'],
                         {'verified': 2, 'http_403': 1, 'identity_changed': 1})
        self.assertEqual(report['health'], 'confirmed')


if __name__ == '__main__':
    unittest.main()
