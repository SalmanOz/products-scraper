"""Merge independently verified crawl state without replacing newer observations."""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from retail_html_catalog import database
from retail_catalog_publish import invalidate_changed_identities


def backup(source, destination):
    if Path(source).resolve() == Path(destination).resolve():
        raise ValueError('State output must differ from input')
    with closing(sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)


def stamp(value):
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def compatible_schema(left, right, names):
    for name in names:
        columns = lambda db: [(r[1], r[2], r[5]) for r in db.execute(f'PRAGMA table_info({name})')]
        if not columns(left) or columns(left) != columns(right):
            raise ValueError(f'Incompatible state schema: {name}')
    for db in (left, right):
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Crawl state integrity check failed')


def observation(data):
    return max(stamp(data.get('checkedAt')), stamp(data.get('offerCheckedAt')))


def preserve_rejections(db, path):
    gtins = {r[0] for r in db.execute('SELECT gtin FROM catalog_targets')}
    gtins.update(r[0] for r in db.execute("SELECT json_extract(data,'$.gtin') FROM queue WHERE data IS NOT NULL"))
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as source:
        if 'catalog_targets' in tables(source):
            gtins.update(r[0] for r in source.execute('SELECT gtin FROM catalog_targets'))
        gtins.update(r[0] for r in source.execute("SELECT json_extract(data,'$.gtin') FROM queue WHERE data IS NOT NULL"))
    rows, _ = invalidate_changed_identities([{'gtin': gtin, 'offers': []} for gtin in gtins if gtin], Path(path))
    for row in rows:
        for url, checked in row.get('offerRejections', {}).items():
            db.execute('INSERT INTO identity_rejections VALUES(?,?,?) ON CONFLICT(url,gtin) DO UPDATE SET checked_at=excluded.checked_at WHERE julianday(excluded.checked_at)>julianday(identity_rejections.checked_at)',
                       (url, row['gtin'], checked))
    db.commit()


def merge_queue(local, remote, output):
    if Path(output).resolve() in {Path(local).resolve(), Path(remote).resolve()}:
        raise ValueError('Merged state must not overwrite its inputs')
    backup(local, output)
    with closing(database(Path(output))) as dst, closing(sqlite3.connect(Path(remote).resolve().as_uri() + '?mode=ro', uri=True)) as src:
        remote_tables = tables(src)
        # Historical state had only queue/pages. Missing additive tables mean
        # no such evidence exists, not permission to manufacture observations.
        optional = ['refresh_attempts', 'identity_rejections', 'catalog_targets', 'catalog_target_issues']
        compatible_schema(dst, src, ['queue', 'pages', *(name for name in optional if name in remote_tables)])
        preserve_rejections(dst, output)
        # Persist remote negatives independently of whichever positive data wins.
        preserve_rejections(dst, remote)
        local_rows = {row[0]: row for row in dst.execute('SELECT url,host,kind,title,status,data,checked_at FROM queue')}
        added = updated = 0
        for row in src.execute('SELECT url,host,kind,title,status,data,checked_at FROM queue'):
            old = local_rows.get(row[0])
            new_data, old_data = json.loads(row[5] or '{}'), json.loads(old[5] or '{}') if old else {}
            if old and row[4] == old[4] == 'verified' and observation(new_data) == observation(old_data) and new_data != old_data:
                raise ValueError(f'Different observations share a timestamp: {row[0]}')
            accept = not old or row[4] == 'verified' and (
                old[4] != 'verified' or observation(new_data) > observation(old_data))
            if old and row[4] != 'verified' and old[4] != 'verified':
                accept = stamp(row[6]) > stamp(old[6])
            if accept:
                dst.execute('INSERT OR REPLACE INTO queue VALUES(?,?,?,?,?,?,?)', row)
                added += not bool(old)
                updated += bool(old)
        for table, keys, date in [('pages',['url'],'checked_at'), ('refresh_attempts',['url'],'attempted_at'), ('identity_rejections',['url','gtin'],'checked_at')]:
            if table not in remote_tables:
                continue
            columns = [row[1] for row in dst.execute(f'PRAGMA table_info({table})')]
            for row in src.execute(f'SELECT {",".join(columns)} FROM {table}'):
                mapped = dict(zip(columns,row))
                key_values = [mapped[key] for key in keys]
                old = dst.execute(f'SELECT {date} FROM {table} WHERE ' + ' AND '.join(f'{key}=?' for key in keys), key_values).fetchone()
                if not old or stamp(mapped[date]) > stamp(old[0]):
                    dst.execute(f'INSERT OR REPLACE INTO {table}({",".join(columns)}) VALUES({",".join("?" for _ in columns)})', row)
        # Catalog targets will be reseeded from the latest production catalog.
        dst.commit()
        dst.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        print(json.dumps({'queue_added_from_cloud':added,'newer_cloud_observations':updated,'queue_total':dst.execute('SELECT count(*) FROM queue').fetchone()[0],
                          'durable_identity_rejections':dst.execute('SELECT count(*) FROM identity_rejections').fetchone()[0]}))


def merge_images(local, remote, output, started):
    if Path(output).resolve() in {Path(local).resolve(), Path(remote).resolve()}:
        raise ValueError('Merged images must not overwrite their inputs')
    backup(local, output)
    with closing(sqlite3.connect(output)) as dst, closing(sqlite3.connect(Path(remote).resolve().as_uri() + '?mode=ro', uri=True)) as src:
        compatible_schema(dst, src, ['images'])
        remote_urls = {row[0] for row in src.execute('SELECT url FROM images')}
        # Absence can be a rejected image. Neither side's older cache may
        # resurrect a positive removed by the other side during this run.
        for url, checked in dst.execute('SELECT url,checked_at FROM images').fetchall():
            if url not in remote_urls and stamp(checked) < stamp(started):
                dst.execute('DELETE FROM images WHERE url=?', (url,))
        count = 0
        for row in src.execute('SELECT url,verified,checked_at FROM images'):
            old = dst.execute('SELECT checked_at FROM images WHERE url=?',(row[0],)).fetchone()
            # An absent older URL may have been explicitly rejected by this run.
            if (old and stamp(row[2]) > stamp(old[0])) or (not old and stamp(row[2]) >= stamp(started)):
                dst.execute('INSERT OR REPLACE INTO images VALUES(?,?,?)',row)
                count += 1
        dst.commit()
        dst.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        print(json.dumps({'newer_cloud_image_checks':count,'images_total':dst.execute('SELECT count(*) FROM images').fetchone()[0]}))


def refresh_summary(path, since):
    if datetime.fromisoformat(since).tzinfo is None:
        raise ValueError('Refresh window must specify its timezone')
    counts = defaultdict(Counter)
    priced = 0
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        for host, outcome, raw in db.execute(
                'SELECT a.host,a.outcome,q.data FROM refresh_attempts a LEFT JOIN queue q USING(url) '
                'WHERE julianday(a.attempted_at)>=julianday(?) AND julianday(a.attempted_at)<=julianday(\'now\')', (since,)):
            counts[host][outcome] += 1
            offer = ((json.loads(raw or '{}') or {}).get('offer') or {})
            if outcome == 'verified' and offer.get('price') and stamp(offer.get('checkedAt')) >= stamp(since):
                priced += 1
    verified = sum(rows.get('verified', 0) for rows in counts.values())
    return {'windowStart': since, 'attempts': sum(sum(rows.values()) for rows in counts.values()),
            'verifiedObservations': verified, 'pricedObservations': priced,
            'byHost': dict(counts), 'health': 'confirmed' if verified else 'no_confirmed_refresh'}


if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--summary-queue'); p.add_argument('--since')
    for name in ['local-queue','remote-queue','output-queue','local-images','remote-images','output-images','started']:
        p.add_argument('--'+name)
    a=p.parse_args()
    if a.summary_queue:
        if not a.since: p.error('--summary-queue requires --since')
        print(json.dumps(refresh_summary(a.summary_queue, a.since), ensure_ascii=False))
        raise SystemExit(0)
    if not all(getattr(a, name) for name in ('local_queue','remote_queue','output_queue','local_images','remote_images','output_images','started')):
        p.error('All local/remote/output queue/image paths and --started are required')
    merge_queue(a.local_queue,a.remote_queue,a.output_queue)
    merge_images(a.local_images,a.remote_images,a.output_images,a.started)
