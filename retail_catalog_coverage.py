"""Report observed catalog coverage and refresh failures without changing product evidence."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit


def current(value, now, hours=72):
    try:
        observed = datetime.fromisoformat(value)
        return timedelta() <= now - observed <= timedelta(hours=hours)
    except (ValueError, TypeError):
        return False


def coverage(products, db, now=None):
    now = now or datetime.now(timezone.utc)
    categories = {}
    missing_prices = []
    conflicts = []
    for product in products:
        kind = product['kind']
        counts = categories.setdefault(kind, Counter())
        counts['products'] += 1
        counts['technical_fields'] += len(product.get('specs', {}))
        counts['with_technical_details'] += bool(product.get('specs'))
        counts['with_field_sources'] += bool(product.get('specSources'))
        offers = [o for o in product.get('offers', []) if current(o.get('checkedAt'), now, 26 if o.get('storeOnly') else 72)]
        counts['with_current_price'] += bool(offers)
        counts['current_offers'] += len(offers)
        if not offers:
            missing_prices.append({'gtin': product['gtin'], 'kind': kind, 'name': product['name']})
        if product.get('specConflicts'):
            counts['with_specification_conflicts'] += 1
            conflicts.append({'gtin': product['gtin'], 'fields': sorted(product['specConflicts'])})
    queue = [dict(zip(('host', 'kind', 'status', 'count'), row)) for row in db.execute(
        'SELECT host,kind,status,count(*) FROM queue GROUP BY host,kind,status ORDER BY host,kind,status')]
    attempts = []
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='refresh_attempts'").fetchone():
        attempts = [dict(zip(('host', 'kind', 'outcome', 'count', 'lastAttempt'), row)) for row in db.execute(
            'SELECT host,kind,outcome,count(*),max(attempted_at) FROM refresh_attempts '
            'GROUP BY host,kind,outcome ORDER BY host,kind,outcome')]
    unrouted = []
    target_issues = Counter()
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalog_target_issues'").fetchone():
        for url, kind, gtin, reason in db.execute('SELECT url,kind,gtin,reason FROM catalog_target_issues ORDER BY kind,url,gtin'):
            host = urlsplit(url).netloc
            target_issues[host, kind, reason] += 1
            unrouted.append({'url': url, 'host': host, 'kind': kind, 'gtin': gtin, 'reason': reason})
    issues = [{'host': host, 'kind': kind, 'reason': reason, 'count': count}
              for (host, kind, reason), count in sorted(target_issues.items())]
    return {'targetCoverageIssues': issues, 'unroutedPublishedTargets': unrouted, 'checkedAt': now.isoformat(), 'latestRefreshAttempts': attempts, 'categories': categories, 'missingCurrentPrices': missing_prices,
            'specificationConflicts': conflicts, 'queue': queue}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with closing(sqlite3.connect(f'{args.db.resolve().as_uri()}?mode=ro', uri=True)) as db:
        result = coverage(json.loads(args.catalog.read_text()), db)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(args.output)
    print(json.dumps({'categories': result['categories'], 'unroutedPublishedTargets': len(result['unroutedPublishedTargets']),
                      'missingCurrentPrices': len(result['missingCurrentPrices']),
                      'specificationConflicts': len(result['specificationConflicts'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
