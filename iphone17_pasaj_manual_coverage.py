"""One-off iPhone 17 Pasaj freshness gate; default workflow mode only audits.

The existing Pasaj reader proves the selected cash option and Turkcell seller.
This module pins the reviewed SKU, price and one existing seller destination.
It never adds a daily seed or updates another merchant's offer.
"""

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re

from offer_ingestion_client import OfferIngestionClient
from pasaj_source import IPHONE17_256_URL, PasajSource
from reviewed_offer_targets import with_reviewed_radio
from update_prices import PriceUpdater


CATALOG = {'id': 235, 'slug': 'apple-iphone-17', 'name': 'Apple iPhone 17',
           'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'}
URL = IPHONE17_256_URL
MERCHANT = 'Pasaj'
TITLE = 'iPhone 17 256 GB'
PRICE = Decimal('86999')
PURPOSE = 'manual_exact_iphone17_pasaj_cash_refresh'
MAX_AGE_SECONDS = 300
OFFER_FIELDS = ('merchant', 'price', 'currency', 'availability',
                'source_title', 'url')


class CatalogStageError(ValueError):
    """Do not leak private endpoint responses in workflow artifacts."""


def _read(stage, callback):
    try:
        return callback()
    except Exception:
        raise CatalogStageError(stage) from None


def _utc(raw):
    value = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('timezone_missing')
    return value.astimezone(timezone.utc)


def _fresh(raw, now):
    return -60 <= (now - _utc(raw)).total_seconds() <= MAX_AGE_SECONDS


def _identity_rows(identity, now):
    if (not isinstance(identity, dict)
            or identity.get('evidenceVersion') != 'product-offer-match-v1'
            or identity.get('mode') != 'shadow_audit_only'
            or not isinstance(identity.get('productCoverage'), list)
            or not _fresh(identity.get('auditedAt'), now)):
        raise ValueError('commercial_identity_unavailable_or_stale')
    matched = [row for row in identity['productCoverage']
               if isinstance(row, dict) and row.get('slug') == CATALOG['slug']]
    if len(matched) != 1 or not isinstance(matched[0].get('matchedOffers'), list):
        raise ValueError('commercial_product_identity_missing')
    row = matched[0]
    offers = row['matchedOffers']
    if (len(offers) not in (3, 4) or row.get('matchedMerchants') != len(offers)):
        raise ValueError('commercial_four_seller_baseline_changed')
    normalized = []
    for offer in offers:
        if (not isinstance(offer, dict) or
                not isinstance(offer.get('merchant_name'), str) or
                not isinstance(offer.get('source_url'), str) or
                not offer['source_url'].startswith('https://')):
            raise ValueError('commercial_offer_identity_invalid')
        try:
            price = Decimal(str(offer['price']))
        except (KeyError, InvalidOperation, TypeError, ValueError):
            raise ValueError('commercial_offer_price_invalid') from None
        if not price.is_finite() or price <= 0:
            raise ValueError('commercial_offer_price_invalid')
        normalized.append((offer['merchant_name'].strip(),
                           offer['source_url'], str(price)))
    if (len(set(normalized)) != len(normalized)
            or len({entry[0].casefold() for entry in normalized}) != len(normalized)):
        raise ValueError('commercial_offer_duplicate_seller')
    pasaj = [entry for entry in normalized if entry[0].casefold() == MERCHANT.casefold()]
    others = [entry for entry in normalized if entry[0].casefold() != MERCHANT.casefold()]
    if (len(others) != 3 or len(pasaj) > 1 or
            pasaj and (pasaj[0][1] != URL or Decimal(pasaj[0][2]) != PRICE) or
            any(entry[1] == URL for entry in others)):
        raise ValueError('commercial_seller_or_url_collision')
    lowest = min(Decimal(entry[2]) for entry in normalized)
    if Decimal(str(row.get('matchedLowestPrice'))) != lowest:
        raise ValueError('commercial_lowest_price_changed')
    return {'other_offers': sorted(others), 'pasaj_current': bool(pasaj),
            'lowest_price': str(lowest)}


def _catalog(client):
    products = _read('catalog_get_failed', client.get_published_products)
    prices = _read('public_status_get_failed', client.get_public_price_priority)
    if not isinstance(products, list) or not isinstance(prices, dict):
        raise ValueError('catalog_unavailable')
    rows = [row for row in products if isinstance(row, dict)
            and row.get('id') == CATALOG['id']]
    if len(rows) != 1:
        raise ValueError('catalog_identity_missing_or_duplicate')
    product = rows[0]
    specs = with_reviewed_radio(product, PriceUpdater.get_expected_specs(
        product.get('attributes')))
    if (product.get('slug') != CATALOG['slug']
            or product.get('name') != CATALOG['name']
            or any(specs.get(key) != CATALOG[key] for key in
                   ('ram_gb', 'storage_gb', 'network_generation'))
            or prices.get(CATALOG['id']) != (CATALOG['slug'], True)):
        raise ValueError('catalog_variant_or_current_price_status_changed')
    first_targets = product.get('offer_targets')
    if not isinstance(first_targets, list):
        raise ValueError('catalog_targets_unavailable')
    complete = _read('complete_targets_get_failed',
                     lambda: client.get_complete_offer_targets(CATALOG['id']))
    targets = complete.get('offer_targets') if isinstance(complete, dict) else None
    total = complete.get('offer_targets_total') if isinstance(complete, dict) else None
    snapshot = complete.get('offer_targets_snapshot') if isinstance(complete, dict) else None
    if (complete.get('id') != CATALOG['id'] or
            complete.get('slug') != CATALOG['slug'] or
            complete.get('name') != CATALOG['name'] or
            complete.get('offer_targets_truncated') is not False or
            type(total) is not int or not 4 <= total <= 60 or
            not isinstance(targets, list) or len(targets) != total or
            not isinstance(snapshot, str) or
            re.fullmatch(r'[0-9a-f]{64}', snapshot) is None or
            targets[:len(first_targets)] != first_targets or
            len(first_targets) != min(total, 12) or
            product.get('offer_targets_truncated') is not (total > 12)):
        raise ValueError('complete_target_snapshot_changed')
    pasaj = [row for row in targets if isinstance(row, dict) and
             str(row.get('merchant_name') or '').strip().casefold() ==
             MERCHANT.casefold()]
    same_url = [row for row in targets if isinstance(row, dict) and
                row.get('source_url') == URL]
    if (len(pasaj) != 1 or len(same_url) != 1 or pasaj[0] != same_url[0]
            or pasaj[0].get('source_url') != URL):
        raise ValueError('pasaj_seller_or_source_url_collision')
    try:
        prior_checked = _utc(pasaj[0]['checked_at'])
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError('pasaj_prior_checked_at_missing') from None
    now = datetime.now(timezone.utc)
    if (now - prior_checked).total_seconds() < -60:
        raise ValueError('pasaj_prior_checked_at_in_future')
    identity = _read('commercial_identity_get_failed',
                     client.get_offer_identity_readiness)
    commercial = _identity_rows(identity, now)
    return product, {'target_count': total, 'target_snapshot': snapshot,
                     'prior_pasaj_checked_at': prior_checked.isoformat(),
                     **commercial}


def _exact_offer(offer, now=None):
    now = now or datetime.now(timezone.utc)
    if not isinstance(offer, dict):
        return False
    try:
        return (all(offer.get(key) == value for key, value in (
                    ('merchant', MERCHANT), ('currency', 'TRY'),
                    ('availability', 'in_stock'), ('source_title', TITLE),
                    ('url', URL)))
                and not isinstance(offer.get('price'), bool)
                and Decimal(str(offer['price'])) == PRICE
                and _fresh(offer['observed_at'], now))
    except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError):
        return False


def _source(source):
    offer, reason, status = source.read_offer(URL)
    state = source.report()
    if (reason != 'observed_exact_cash_offer' or status != 200
            or state.get('robots_status') != 'allowed'
            or state.get('blocked') is not False
            or not _exact_offer(offer)):
        return None, {'reason': reason, 'http_status': status, 'source': state}
    return offer, {'reason': reason, 'http_status': status, 'source': state}


def audit(*, client=None, source=None):
    client = client or OfferIngestionClient.from_env()
    source = source or PasajSource()
    report = {'purpose': PURPOSE, 'writes': False, 'status': 'failed',
              'workflow_run_id': os.environ.get('GITHUB_RUN_ID'),
              'workflow_run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'),
              'checked_at': None, 'catalog_error': None,
              'catalog_evidence': None, 'source_attempt': None,
              'verified_offer': None}
    try:
        _, report['catalog_evidence'] = _catalog(client)
        report['verified_offer'], report['source_attempt'] = _source(source)
        if report['verified_offer'] is not None:
            report['status'] = 'passed'
    except Exception as error:
        report['catalog_error'] = (str(error) if isinstance(error, ValueError)
                                   else type(error).__name__)
    report['checked_at'] = datetime.now(timezone.utc).isoformat()
    return report


def verified_offer(report, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        if (not isinstance(report, dict) or report.get('purpose') != PURPOSE
                or report.get('writes') is not False
                or report.get('status') != 'passed'
                or report.get('catalog_error') is not None
                or report.get('workflow_run_id') != os.environ.get('GITHUB_RUN_ID')
                or report.get('workflow_run_attempt') !=
                    os.environ.get('GITHUB_RUN_ATTEMPT')
                or not _fresh(report['checked_at'], now)
                or not isinstance(report.get('catalog_evidence'), dict)
                or not isinstance(report.get('source_attempt'), dict)
                or report['source_attempt'].get('reason') !=
                    'observed_exact_cash_offer'
                or report['source_attempt'].get('http_status') != 200
                or report['source_attempt'].get('source', {}).get('robots_status')
                    != 'allowed'
                or not _exact_offer(report.get('verified_offer'), now)):
            return None
        checked = _utc(report['checked_at'])
        observed = _utc(report['verified_offer']['observed_at'])
        return report['verified_offer'] if 0 <= (checked - observed).total_seconds() <= MAX_AGE_SECONDS else None
    except (KeyError, TypeError, ValueError, AttributeError, InvalidOperation):
        return None


def _readback(client, before, now=None):
    now = now or datetime.now(timezone.utc)
    identity = _read('commercial_readback_get_failed',
                     client.get_offer_identity_readiness)
    after = _identity_rows(identity, now)
    if (after['other_offers'] != before['other_offers'] or
            after['pasaj_current'] is not True or
            after['lowest_price'] != before['lowest_price']):
        raise RuntimeError('commercial_four_seller_readback_changed')
    return after


def publish(report, *, client=None, source=None, now=None):
    """One existing Pasaj seller row, after another exact live PDP GET."""
    audited = verified_offer(report, now)
    if audited is None:
        raise ValueError('exact_pasaj_audit_missing_or_stale')
    source = source or PasajSource()
    live, _ = _source(source)
    if (live is None or any(live.get(key) != audited.get(key)
                            for key in OFFER_FIELDS)):
        raise ValueError('exact_pasaj_live_offer_changed')
    client = client or OfferIngestionClient.from_env()
    product, proof = _catalog(client)
    if proof != report.get('catalog_evidence'):
        raise ValueError('catalog_or_prior_seller_snapshot_changed')
    if _utc(live['observed_at']) <= _utc(proof['prior_pasaj_checked_at']):
        raise ValueError('pasaj_observation_not_newer_than_stored_row')
    prepared = PriceUpdater(ingestion_client=client).prepare_product_offers(
        product, [live], live['observed_at'], live['observed_at'])
    if len(prepared) != 1:
        raise ValueError('prepared_offer_count_changed')
    row = prepared[0]
    if (row.get('product_id') != CATALOG['id'] or
            row.get('product_slug') != CATALOG['slug'] or
            row.get('seller') != MERCHANT or row.get('source_url') != URL or
            row.get('source_title') != TITLE or
            Decimal(str(row.get('price'))) != PRICE or
            row.get('currency') != 'TRY' or
            row.get('availability') != 'in_stock' or
            _utc(row.get('observed_at')) != _utc(live['observed_at']) or
            _utc(row.get('checked_at')) != _utc(live['observed_at']) or
            row.get('is_official') is not False):
        raise ValueError('prepared_offer_changed')
    before_ingest = datetime.now(timezone.utc)
    if (verified_offer(report, before_ingest) is None or
            not _fresh(live['observed_at'], before_ingest)):
        raise ValueError('pasaj_evidence_stale_before_ingest')
    # An uncertain POST response must not trigger a second network submission.
    # The authenticated readback can be inspected manually if that happens.
    retry_budget = client.max_attempts if isinstance(client, OfferIngestionClient) else None
    try:
        if retry_budget is not None:
            client.max_attempts = 1
        result = client.ingest_offers([row], require_all_accepted=True)
    finally:
        if retry_budget is not None:
            client.max_attempts = retry_budget
    if (not isinstance(result, dict) or result.get('committed') is not True
            or result.get('accepted') != 1 or result.get('stale_ignored') != 0
            or result.get('variant_rejected') != 0):
        raise RuntimeError('exact_pasaj_offer_not_accepted')
    _readback(client, proof)
    return {'status': 'passed', 'submitted': 1, 'accepted': 1,
            'slug': CATALOG['slug'], 'seller': MERCHANT,
            'source_url': URL, 'price': float(PRICE),
            'readback': 'same_four_sellers_and_exact_pasaj_price'}


def summary(report, publication=None, publish_requested=False):
    offer = report.get('verified_offer') or {}
    attempt = report.get('source_attempt') or {}
    catalog = report.get('catalog_evidence') or {}
    if not publish_requested:
        outcome = 'Yayın istenmedi; fiyat yazımı yok.'
    elif report.get('status') != 'passed' and publication is None:
        outcome = 'Denetim başarısız; yayın adımı çalışmadı.'
    elif (isinstance(publication, dict) and
          publication.get('readback') ==
          'same_four_sellers_and_exact_pasaj_price' and
          publication.get('accepted') == 1):
        outcome = 'Bir Pasaj teklifi kabul edildi; dört satıcı readback doğrulandı.'
    else:
        outcome = 'Yayın sonucu belirsiz; ingestion yanıtını inceleyin.'
    return (f"iPhone 17 8/256 Pasaj denetimi: {report.get('status')}; "
            f"kaynak {attempt.get('reason') or 'okunmadı'}; "
            f"{offer.get('price') or '?'} TRY. "
            f"{report.get('catalog_error') or ''}\n"
            f"Pasaj satırının doğrulanmış son kontrolü: "
            f"{catalog.get('prior_pasaj_checked_at') or 'bilinmiyor'}; "
            f"denetim anında güncel: {catalog.get('pasaj_current')}.\n"
            f"{outcome}\n")


def main(argv=None):
    parser = argparse.ArgumentParser()
    actions = parser.add_subparsers(dest='action', required=True)
    read = actions.add_parser('audit')
    read.add_argument('--audit-file', type=Path, required=True)
    write = actions.add_parser('publish')
    write.add_argument('--audit-file', type=Path, required=True)
    write.add_argument('--output-file', type=Path, required=True)
    write.add_argument('--publish-exact-one', action='store_true', required=True)
    render = actions.add_parser('summarize')
    render.add_argument('--audit-file', type=Path, required=True)
    render.add_argument('--publish-file', type=Path, required=True)
    render.add_argument('--output-file', type=Path, required=True)
    render.add_argument('--publish-requested', choices=('true', 'false'), required=True)
    args = parser.parse_args(argv)
    if args.action == 'audit':
        report = audit()
        args.audit_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        args.audit_file.with_suffix('.md').write_text(summary(report))
        print(summary(report), end='')
        return 0 if report['status'] == 'passed' else 1
    if args.action == 'summarize':
        report = json.loads(args.audit_file.read_text())
        try:
            publication = json.loads(args.publish_file.read_text())
        except (OSError, ValueError):
            publication = None
        rendered = summary(report, publication,
                           args.publish_requested == 'true')
        args.output_file.write_text(rendered)
        print(rendered, end='')
        return 0
    report = json.loads(args.audit_file.read_text())
    result = publish(report)
    args.output_file.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
