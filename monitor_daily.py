"""Measure freshly reverified independent monitor offers on consecutive days."""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from catalog_queue import CatalogQueue
from monitor_pilot import candidates
from monitor_scale import read_json, write_json
from retailer_barcode import retailer_gtin13


ZONE = ZoneInfo('Europe/Istanbul')
MINIMUM_PAIRS = 80
COHORT_SIZE = 100
REQUIRED_DAYS = 3


def cohort(plan):
    gtins = sorted(model['gtin'] for model in plan['candidates'])
    if len(gtins) != COHORT_SIZE or len(set(gtins)) != COHORT_SIZE or any(retailer_gtin13(g) != g for g in gtins):
        raise ValueError('Daily audit requires the same 100 distinct exact GTINs')
    return frozenset(gtins)


def fingerprint(gtins):
    return sha256('\n'.join(sorted(gtins)).encode('ascii')).hexdigest()


def snapshot(queue, gtins):
    now = int(queue.clock())
    local = datetime.fromtimestamp(now, ZONE)
    since = int(local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    grouped = defaultdict(list)
    for offer in queue.fresh_offers('monitor', since=since, now=now):
        if offer['gtin13'] in gtins:
            grouped[offer['gtin13']].append(offer)
    priced = sorted(grouped)
    paired = sorted(gtin for gtin, offers in grouped.items() if len(next(candidates(offers), ())) == 2)
    platforms = Counter(offer['platform'] for offers in grouped.values() for offer in offers)
    return {'day': local.date().isoformat(), 'captured_at': local.isoformat(), 'cohort': fingerprint(gtins),
            'models': len(gtins), 'priced_gtins': priced, 'paired_gtins': paired,
            'priced': len(priced), 'paired': len(paired), 'target_pairs': MINIMUM_PAIRS,
            'offers_by_platform': dict(platforms)}


def daily_history(gtins, reports):
    reports = list(reports)
    by_day = {}
    for report in reports:
        day = date.fromisoformat(report['day'])
        timestamp = datetime.fromisoformat(report['captured_at'])
        priced, paired = set(report['priced_gtins']), set(report['paired_gtins'])
        if (timestamp.tzinfo is None or timestamp.astimezone(ZONE).date() != day or day in by_day
                or report['cohort'] != fingerprint(gtins) or report['models'] != len(gtins)
                or not paired <= priced <= gtins or len(priced) != report['priced'] or len(paired) != report['paired']
                or len(report['priced_gtins']) != len(priced) or len(report['paired_gtins']) != len(paired)):
            raise ValueError('Daily snapshots must describe the same cohort on distinct local dates')
        by_day[day] = paired
    ordered = sorted(by_day)
    streak = 0
    for day in reversed(ordered):
        if day != ordered[-1] - timedelta(days=streak) or len(by_day[day]) < MINIMUM_PAIRS:
            break
        streak += 1
    recent = ordered[-REQUIRED_DAYS:]
    overlap = len(set.intersection(*(by_day[day] for day in recent))) if recent else 0
    return {'status': 'daily_target_met' if streak >= REQUIRED_DAYS else 'daily_target_unproven',
            'required_consecutive_days': REQUIRED_DAYS, 'target_pairs_per_day': MINIMUM_PAIRS,
            'consecutive_days_at_target': streak, 'models_paired_in_all_recent_snapshots': overlap,
            'days': [{'day': day.isoformat(), 'paired': len(by_day[day])} for day in ordered]}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('plan', type=Path)
    commands = cli.add_subparsers(dest='command', required=True)
    capture = commands.add_parser('capture')
    capture.add_argument('db', type=Path)
    capture.add_argument('report', type=Path)
    history = commands.add_parser('history')
    history.add_argument('reports', nargs='+', type=Path)
    history.add_argument('--output', type=Path, required=True)
    args = cli.parse_args()
    gtins = cohort(read_json(args.plan))
    if args.command == 'capture':
        if not args.db.is_file():
            cli.error('Expected an existing monitor crawl database')
        with closing(CatalogQueue(args.db)) as queue:
            write_json(args.report, snapshot(queue, gtins))
    else:
        write_json(args.output, daily_history(gtins, (read_json(path) for path in args.reports)))


if __name__ == '__main__':
    main()
