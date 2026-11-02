"""Require the one exact-GTIN S24 FE seller to be accepted from fresh audit."""

from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys

from mediamarkt_marketplace_source import S24FE_PROFILES, S24FE_URL
from mediamarkt_s24fe_preflight import validated_offer


PROFILE = S24FE_PROFILES[S24FE_URL]


def validate_scoped_refresh(audit, report):
    observed = validated_offer(audit)
    if not observed:
        return False, 'exact-GTIN audit missing or stale'
    if not isinstance(report, dict):
        return False, 'invalid writer report'
    products = report.get('products')
    if (report.get('status') != 'completed' or report.get('mode') != 'apply'
            or report.get('known_pdp_only') is not True
            or not isinstance(products, list) or len(products) != 1
            or products[0].get('slug') != PROFILE['slug']):
        return False, 'writer scope differs from exact S24 FE model'
    row = products[0]
    targets = row.get('targets')
    attempts = row.get('source_attempts')
    prepared = row.get('prepared_offers')
    if (not isinstance(targets, list) or len(targets) != 1
            or targets[0].get('source_url') != S24FE_URL
            or not isinstance(attempts, list) or len(attempts) != 1
            or attempts[0].get('source_url') != S24FE_URL
            or attempts[0].get('merchant') != PROFILE['seller_name']
            or attempts[0].get('reason') != 'observed_exact_new_marketplace_offer_from_preflight'
            or attempts[0].get('http_status') != 200
            or not isinstance(prepared, list) or len(prepared) != 1):
        return False, 'writer did not use the sole audited seller'
    record = prepared[0]
    offer = observed[S24FE_URL]
    try:
        same_price = Decimal(str(record.get('price'))) == Decimal(str(offer['price']))
    except (TypeError, ValueError, InvalidOperation):
        same_price = False
    if (not same_price or record.get('product_id') != PROFILE['id']
            or record.get('product_slug') != PROFILE['slug']
            or record.get('seller') != PROFILE['seller_name']
            or record.get('source_url') != S24FE_URL
            or record.get('currency') != 'TRY'
            or record.get('availability') != 'in_stock'
            or record.get('source_title') != offer['source_title']
            or record.get('observed_at') != offer['observed_at']
            or record.get('checked_at') != offer['observed_at']):
        return False, 'prepared seller/price/variant differs from audited observation'
    ingestion = report.get('ingestion') or {}
    coverage = report.get('coverage') or {}
    if (ingestion.get('committed') is not True or ingestion.get('accepted') != 1
            or ingestion.get('stale_ignored') != 0 or ingestion.get('variant_rejected') != 0
            or coverage.get('products') != 1 or coverage.get('with_purchasable_offer') != 1):
        return False, 'sole exact marketplace offer was not accepted'
    return True, 'one exact-GTIN S24 FE marketplace seller accepted'


def main():
    if len(sys.argv) != 3:
        raise SystemExit('usage: verify_mediamarkt_s24fe_gtin.py AUDIT REPORT')
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
