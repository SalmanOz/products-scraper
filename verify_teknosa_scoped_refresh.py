"""Fail a scoped workflow if other merchants hid a Teknosa source failure."""

import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from teknosa_reviewed_source import URL


def validate_scoped_refresh(audit, report):
    if not isinstance(audit, dict) or not isinstance(report, dict):
        return False, 'invalid audit or writer report'
    if (audit.get('verified') is not True
            or audit.get('reason') != 'observed_exact_first_party_cash_offer'
            or audit.get('http_status') != 200
            or audit.get('catalog_id') != 248
            or audit.get('slug') != 'apple-iphone-17e'
            or audit.get('url') != URL):
        return False, 'exact preflight missing'
    observed = audit.get('offer')
    if not isinstance(observed, dict) or observed.get('merchant') != 'Teknosa':
        return False, 'preflight offer missing'
    products = report.get('products')
    if (report.get('status') != 'completed'
            or report.get('mode') != 'apply'
            or report.get('known_pdp_only') is not True
            or not isinstance(products, list) or len(products) != 1
            or not isinstance(products[0], dict)
            or products[0].get('slug') != 'apple-iphone-17e'):
        return False, 'scoped writer did not complete for exact SKU'
    product = products[0]
    attempts = [row for row in product.get('source_attempts', [])
                if isinstance(row, dict)
                if row.get('source_url') == URL]
    if (len(attempts) != 1
            or attempts[0].get('reason') != 'observed_exact_first_party_cash_offer_from_preflight'
            or attempts[0].get('http_status') != 200):
        return False, 'Teknosa verified handoff was not consumed'
    prepared = product.get('prepared_offers')
    if not isinstance(prepared, list):
        return False, 'prepared offer records missing'
    teknosa = [row for row in prepared
               if isinstance(row, dict) and row.get('seller') == 'Teknosa']
    if len(teknosa) != 1:
        return False, 'Teknosa offer absent from prepared records'
    row = teknosa[0]
    try:
        price_matches = Decimal(str(row.get('price'))) == Decimal(str(observed.get('price')))
    except (TypeError, ValueError, InvalidOperation):
        price_matches = False
    if (not price_matches or row.get('product_id') != 248
            or row.get('product_slug') != 'apple-iphone-17e'
            or row.get('source_url') != URL
            or row.get('availability') != 'in_stock'
            or row.get('currency') != 'TRY'
            or row.get('source_title') != observed.get('source_title')
            or row.get('observed_at') != observed.get('observed_at')
            or row.get('checked_at') != observed.get('observed_at')):
        return False, 'prepared Teknosa offer differs from preflight'
    ingestion = report.get('ingestion') or {}
    if (ingestion.get('committed') is not True
            or ingestion.get('variant_rejected') != 0
            or ingestion.get('stale_ignored') != 0
            or ingestion.get('accepted', -1) < len(prepared)):
        return False, 'not all prepared offers were accepted'
    return True, 'exact Teknosa offer committed'


def main():
    if len(sys.argv) != 3:
        raise SystemExit('usage: verify_teknosa_scoped_refresh.py AUDIT REPORT')
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
