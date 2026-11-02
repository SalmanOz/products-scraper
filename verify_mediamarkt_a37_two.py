"""Require both A37 sellers to be accepted from the same fresh audit."""

from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from mediamarkt_a37_preflight import validated_offers
from mediamarkt_marketplace_source import A37_PROFILES
from offer_ingestion_client import OfferIngestionClient


def _current_matched_offers(identity, observed, now=None):
    """Confirm the authenticated commercial filter exposes both exact rows."""
    now = now or datetime.now(timezone.utc)
    if (not isinstance(identity, dict)
            or identity.get('evidenceVersion') != 'product-offer-match-v1'
            or identity.get('mode') != 'shadow_audit_only'
            or not isinstance(identity.get('productCoverage'), list)):
        return False
    try:
        checked = datetime.fromisoformat(identity['auditedAt'].replace('Z', '+00:00'))
        if (checked.tzinfo is None or now.tzinfo is None
                or not -60 <= (now - checked).total_seconds() <= 300):
            return False
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
    rows = [row for row in identity['productCoverage']
            if isinstance(row, dict) and row.get('slug') == 'samsung-galaxy-a37-5g']
    if len(rows) != 1 or not isinstance(rows[0].get('matchedOffers'), list):
        return False
    if (type(rows[0].get('matchedMerchants')) is not int
            or rows[0]['matchedMerchants'] < len(A37_PROFILES)):
        return False
    matched = rows[0]['matchedOffers']
    for url, profile in A37_PROFILES.items():
        exact = [row for row in matched if isinstance(row, dict)
                 and row.get('source_url') == url]
        if len(exact) != 1 or exact[0].get('merchant_name') != profile['seller_name']:
            return False
        try:
            if Decimal(str(exact[0]['price'])) != Decimal(str(observed[url]['price'])):
                return False
        except (KeyError, TypeError, ValueError, InvalidOperation):
            return False
    return True


def validate_scoped_refresh(audit, report, identity=None):
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
    if not _current_matched_offers(identity, observed):
        return False, 'authenticated commercial readback lacks both exact A37 seller offers'
    return True, 'two exact A37 marketplace sellers accepted'


def main():
    if len(sys.argv) != 3:
        raise SystemExit('usage: verify_mediamarkt_a37_two.py AUDIT REPORT')
    try:
        audit = json.loads(Path(sys.argv[1]).read_text())
        report = json.loads(Path(sys.argv[2]).read_text())
        identity = OfferIngestionClient.from_env().get_offer_identity_readiness()
        valid, reason = validate_scoped_refresh(audit, report, identity)
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError, KeyError) as error:
        valid, reason = False, f'invalid audit or writer report: {type(error).__name__}'
    print(reason)
    return 0 if valid else 1


if __name__ == '__main__':
    raise SystemExit(main())
