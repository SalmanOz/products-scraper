"""Verify that the scoped writer consumed and accepted both audited sellers."""

from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys

from mediamarkt_marketplace_source import PROFILES
from mediamarkt_marketplace_preflight import validated_offers


def validate_scoped_refresh(audit, report):
    observed = validated_offers(audit)
    if not observed:
        return False, 'two-offer audit missing or stale'
    if not isinstance(report, dict):
        return False, 'invalid writer report'
    products = report.get('products')
    if (report.get('status') != 'completed' or report.get('mode') != 'apply'
            or report.get('known_pdp_only') is not True
            or not isinstance(products, list) or len(products) != len(PROFILES)):
        return False, 'writer scope differs from two exact models'
    profiles_by_slug = {p['slug']: (url, p) for url, p in PROFILES.items()}
    if {row.get('slug') for row in products if isinstance(row, dict)} != set(profiles_by_slug):
        return False, 'writer model identity differs from audit'
    for row in products:
        url, profile = profiles_by_slug[row['slug']]
        targets = row.get('targets')
        attempts = row.get('source_attempts')
        prepared = row.get('prepared_offers')
        if (not isinstance(targets, list) or len(targets) != 1
                or targets[0].get('source_url') != url
                or not isinstance(attempts, list) or len(attempts) != 1
                or attempts[0].get('source_url') != url
                or attempts[0].get('merchant') != profile['seller_name']
                or attempts[0].get('reason') != 'observed_exact_new_marketplace_offer_from_preflight'
                or attempts[0].get('http_status') != 200
                or not isinstance(prepared, list) or len(prepared) != 1):
            return False, 'writer did not use one exact audited seller for each model'
        record = prepared[0]
        offer = observed[url]
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
            return False, 'prepared seller/price/variant differs from audited observation'
    ingestion = report.get('ingestion') or {}
    coverage = report.get('coverage') or {}
    if (ingestion.get('committed') is not True or ingestion.get('accepted') != len(PROFILES)
            or ingestion.get('stale_ignored') != 0 or ingestion.get('variant_rejected') != 0
            or coverage.get('products') != len(PROFILES)
            or coverage.get('with_purchasable_offer') != len(PROFILES)):
        return False, 'both exact marketplace offers were not accepted'
    return True, 'two exact new marketplace sellers accepted'


def main():
    if len(sys.argv) != 3:
        raise SystemExit('usage: verify_mediamarkt_marketplace_two.py AUDIT REPORT')
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
