"""Require both reviewed MediaMarkt offers in a fully accepted scoped write."""

import json
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from xiaomi_17t_preflight import PROFILES, validated_offers


def validate_scoped_refresh(audit, report):
    try:
        checked = datetime.fromisoformat(audit['checked_at'].replace('Z', '+00:00'))
        observed = validated_offers(audit, now=checked)
    except (KeyError, TypeError, ValueError, AttributeError):
        observed = None
    if not observed or set(observed) != set(PROFILES):
        return False, 'exact two-SKU preflight missing'
    if not isinstance(report, dict):
        return False, 'invalid writer report'
    products = report.get('products')
    if (report.get('status') != 'completed'
            or report.get('mode') != 'apply'
            or report.get('known_pdp_only') is not True
            or not isinstance(products, list) or len(products) != 2
            or {p.get('slug') for p in products if isinstance(p, dict)}
            != {profile[1] for profile in PROFILES.values()}):
        return False, 'scoped writer did not complete for exact two SKUs'
    total_prepared = 0
    for url, profile in PROFILES.items():
        id, slug, *_ = profile
        product = next(p for p in products if p['slug'] == slug)
        attempts = [row for row in product.get('source_attempts', [])
                    if isinstance(row, dict) and row.get('source_url') == url]
        if (len(attempts) != 1
                or attempts[0].get('reason') != 'observed_exact_offer_from_preflight'
                or attempts[0].get('http_status') != 200):
            return False, 'MediaMarkt preflight handoff was not consumed for ' + slug
        prepared = product.get('prepared_offers')
        if not isinstance(prepared, list) or len(prepared) != 1:
            return False, 'prepared offer records missing for ' + slug
        total_prepared += len(prepared)
        media = [row for row in prepared
                 if isinstance(row, dict) and row.get('seller') == 'MediaMarkt']
        if len(media) != 1:
            return False, 'MediaMarkt offer absent from prepared records for ' + slug
        row = media[0]
        offer = observed[url]
        try:
            price_matches = Decimal(str(row.get('price'))) == Decimal(str(offer['price']))
        except (TypeError, ValueError, InvalidOperation):
            price_matches = False
        if (not price_matches or row.get('product_id') != id
                or row.get('product_slug') != slug
                or row.get('source_url') != url
                or row.get('availability') != offer['availability']
                or row.get('currency') != 'TRY'
                or row.get('source_title') != offer['source_title']
                or row.get('observed_at') != offer['observed_at']
                or row.get('checked_at') != offer['observed_at']):
            return False, 'prepared MediaMarkt offer differs from preflight for ' + slug
    ingestion = report.get('ingestion') or {}
    if (total_prepared != 2 or ingestion.get('committed') is not True
            or ingestion.get('variant_rejected') != 0
            or ingestion.get('stale_ignored') != 0
            or ingestion.get('accepted') != 2):
        return False, 'not all prepared offers were accepted'
    return True, 'both exact MediaMarkt offers committed'


def main():
    if len(sys.argv) != 3:
        raise SystemExit('usage: verify_xiaomi_17t_scoped_refresh.py AUDIT REPORT')
    try:
        audit = json.loads(Path(sys.argv[1]).read_text())
        report = json.loads(Path(sys.argv[2]).read_text())
        valid, reason = validate_scoped_refresh(audit, report)
    except (OSError, ValueError, TypeError, AttributeError) as error:
        valid, reason = False, f'invalid audit or writer report: {type(error).__name__}'
    print(reason)
    return 0 if valid else 1


if __name__ == '__main__':
    raise SystemExit(main())
