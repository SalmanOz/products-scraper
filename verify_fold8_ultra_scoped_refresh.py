"""Require both exact Fold8 first-party offers in a fully accepted write."""

from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys

from fold8_ultra_preflight import PROFILE, URLS, validated_offers


def validate_scoped_refresh(audit, report):
    observed = validated_offers(audit)
    if not observed or set(observed) != set(URLS):
        return False, 'exact two-retailer preflight missing or stale'
    if not isinstance(report, dict):
        return False, 'invalid writer report'
    products = report.get('products')
    if (report.get('status') != 'completed' or report.get('mode') != 'apply'
            or report.get('known_pdp_only') is not True
            or not isinstance(products, list) or len(products) != 1
            or not isinstance(products[0], dict)
            or products[0].get('slug') != PROFILE[1]):
        return False, 'writer scope differs from the exact Fold8 SKU'
    product = products[0]
    targets = product.get('targets')
    if (not isinstance(targets, list) or len(targets) != 2
            or {row.get('source_url') for row in targets if isinstance(row, dict)} != set(URLS)):
        return False, 'writer targets differ from the two reviewed URLs'
    attempts = product.get('source_attempts')
    if (not isinstance(attempts, list) or len(attempts) != 2
            or {row.get('source_url') for row in attempts if isinstance(row, dict)} != set(URLS)
            or any(row.get('reason') != 'observed_exact_cash_offer_from_preflight'
                   or row.get('http_status') != 200
                   or row.get('merchant') != URLS[row['source_url']]
                   for row in attempts)):
        return False, 'same-run two-offer handoff was not consumed'
    prepared = product.get('prepared_offers')
    if not isinstance(prepared, list) or len(prepared) != 2:
        return False, 'writer did not prepare exactly two offers'
    checked_urls = set()
    for row in prepared:
        if not isinstance(row, dict):
            return False, 'invalid prepared offer'
        url = row.get('source_url')
        if url not in URLS or url in checked_urls:
            return False, 'prepared URL is missing, duplicated or unreviewed'
        checked_urls.add(url)
        offer = observed[url]
        try:
            same_price = Decimal(str(row.get('price'))) == Decimal(str(offer['price']))
        except (TypeError, ValueError, InvalidOperation):
            same_price = False
        if (not same_price or row.get('product_id') != PROFILE[0]
                or row.get('product_slug') != PROFILE[1]
                or row.get('seller') != URLS[url]
                or row.get('currency') != 'TRY'
                or row.get('availability') != 'in_stock'
                or row.get('source_title') != offer['source_title']
                or row.get('observed_at') != offer['observed_at']
                or row.get('checked_at') != offer['observed_at']):
            return False, 'prepared first-party offer differs from audited observation'
    ingestion = report.get('ingestion') or {}
    if (checked_urls != set(URLS) or ingestion.get('committed') is not True
            or ingestion.get('accepted') != 2
            or ingestion.get('stale_ignored') != 0
            or ingestion.get('variant_rejected') != 0):
        return False, 'both prepared offers were not accepted'
    coverage = report.get('coverage') or {}
    if (coverage.get('products') != 1 or coverage.get('with_purchasable_offer') != 1
            or coverage.get('with_two_merchants') != 1):
        return False, 'two-merchant coverage was not established'
    return True, 'both exact first-party offers accepted for one Fold8 SKU'


def main():
    if len(sys.argv) != 3:
        raise SystemExit('usage: verify_fold8_ultra_scoped_refresh.py AUDIT REPORT')
    try:
        audit = json.loads(Path(sys.argv[1]).read_text())
        report = json.loads(Path(sys.argv[2]).read_text())
        valid, reason = validate_scoped_refresh(audit, report)
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as error:
        valid, reason = False, f'invalid audit or writer report: {type(error).__name__}'
    print(reason)
    return 0 if valid else 1


if __name__ == '__main__':
    raise SystemExit(main())
