"""Require both A37 sellers to be accepted from the same fresh audit."""

from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys

from mediamarkt_a37_preflight import validated_offers
from mediamarkt_marketplace_source import A37_PROFILES


def validate_scoped_refresh(audit, report):
    observed = validated_offers(audit)
    if not observed:
        return False, 'A37 two-seller audit missing or stale'
    if not isinstance(report, dict):
        return False, 'invalid writer report'
    products = report.get('products')
    if (report.get('status') != 'completed' or report.get('mode') != 'apply'
            or report.get('known_pdp_only') is not True
            or not isinstance(products, list) or len(products) != 1
            or products[0].get('slug') != 'samsung-galaxy-a37-5g'):
        return False, 'writer scope differs from exact A37 model'
    row = products[0]
    targets = row.get('targets')
    attempts = row.get('source_attempts')
    prepared = row.get('prepared_offers')
    if (not isinstance(targets, list) or len(targets) != len(A37_PROFILES)
            or {target.get('source_url') for target in targets} != set(A37_PROFILES)
            or not isinstance(attempts, list) or len(attempts) != len(A37_PROFILES)
            or not isinstance(prepared, list) or len(prepared) != len(A37_PROFILES)):
        return False, 'writer did not use both exact audited targets'
    by_attempt = {attempt.get('source_url'): attempt for attempt in attempts}
    by_prepared = {record.get('source_url'): record for record in prepared}
    if (len(by_attempt) != len(A37_PROFILES) or
            len(by_prepared) != len(A37_PROFILES) or
            set(by_attempt) != set(A37_PROFILES) or
            set(by_prepared) != set(A37_PROFILES)):
        return False, 'writer targets were duplicated or changed'
    for url, profile in A37_PROFILES.items():
        attempt = by_attempt[url]
        record = by_prepared[url]
        offer = observed[url]
        if (attempt.get('merchant') != profile['seller_name']
                or attempt.get('reason') != 'observed_exact_new_marketplace_offer_from_preflight'
                or attempt.get('http_status') != 200):
            return False, 'writer did not use the audited seller source'
        try:
            same_price = Decimal(str(record.get('price'))) == Decimal(str(offer['price']))
        except (TypeError, ValueError, InvalidOperation):
            same_price = False
        if (not same_price or record.get('product_id') != profile['id']
                or record.get('product_slug') != profile['slug']
                or record.get('seller') != profile['seller_name']
                or record.get('source_url') != url
                or record.get('currency') != 'TRY'
                or record.get('availability') != 'in_stock'
                or record.get('source_title') != offer['source_title']
                or record.get('observed_at') != offer['observed_at']
                or record.get('checked_at') != offer['observed_at']):
            return False, 'prepared seller/price/variant differs from audit'
    ingestion = report.get('ingestion') or {}
    coverage = report.get('coverage') or {}
    if (ingestion.get('committed') is not True or ingestion.get('accepted') != len(A37_PROFILES)
            or ingestion.get('stale_ignored') != 0 or ingestion.get('variant_rejected') != 0
            or coverage.get('products') != 1 or coverage.get('with_purchasable_offer') != 1):
        return False, 'both exact A37 offers were not accepted'
    return True, 'two exact A37 marketplace sellers accepted'


def main():
    if len(sys.argv) != 3:
        raise SystemExit('usage: verify_mediamarkt_a37_two.py AUDIT REPORT')
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
