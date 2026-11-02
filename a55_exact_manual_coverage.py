"""Manual-only, exact-two-seller Galaxy A55 MediaMarkt coverage.

Audit is read-only. Publication requires an explicit workflow dispatch, a
same-run authenticated catalog/target/seller proof, legal seller identities,
and a second live PDP read.
The two reviewed prices are exact equality gates, not saved offers.
"""

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re

from bs4 import BeautifulSoup

from coverage_two_marketplace import _offer_matches, _read_selected
from mediamarkt_marketplace_preflight import no_merchant_collision
from mediamarkt_source import BOT, MAX_HTML_BYTES, MediaMarktSource
from offer_ingestion_client import OfferIngestionClient
from update_prices import PriceUpdater


PROFILES = {
    'https://www.mediamarkt.com.tr/tr/product/_samsung-galaxy-a55-5g-8-gb-128-gb-akilli-telefon-mavi-166438357.html': {
        'id': 32, 'slug': 'samsung-galaxy-a55-5g',
        'name': 'Samsung Galaxy A55 5G',
        'ram_gb': 8, 'storage_gb': 128, 'network_generation': '5g',
        'sku': '166438357', 'gtin13': '8806095518817',
        'source_title': 'SAMSUNG Galaxy A55 5G 8 GB 128 GB Akıllı Telefon Mavi',
        'seller_name': 'Güzel Teknoloji', 'seller_id': '12069',
        'price': Decimal('37499'),
    },
    'https://www.mediamarkt.com.tr/tr/product/_samsung-galaxy-a55-5g-8-gb-128-gb-akilli-telefon-siyah-166438300.html': {
        'id': 32, 'slug': 'samsung-galaxy-a55-5g',
        'name': 'Samsung Galaxy A55 5G',
        'ram_gb': 8, 'storage_gb': 128, 'network_generation': '5g',
        'sku': '166438300', 'gtin13': '8806095517490',
        'source_title': 'SAMSUNG Galaxy A55 5G 8 GB 128 GB Akıllı Telefon Siyah',
        'seller_name': 'Başaran Ticaret', 'seller_id': '12140',
        'price': Decimal('44999'),
    },
}
SELLER_IDENTITIES = {
    'Güzel Teknoloji': {
        'seller_id': '12069',
        'legal_name': ('Güzel Teknoloji İletişim İnşaat Gayrimenkul Sanayi '
                       've Ticaret Limited Şirketi'),
        'mersis': '453145023500001', 'tax': '4531450235',
    },
    'Başaran Ticaret': {
        'seller_id': '12140', 'legal_name': 'İDRİS BAŞARAN',
        'mersis': '2508736931400010', 'tax': '25087369314',
    },
}
PURPOSE = 'read_only_a55_exact_two_seller_marketplace_offers'
MAX_AGE_SECONDS = 300
OFFER_IDENTITY_FIELDS = ('url', 'platform', 'merchant', 'seller_name',
                         'seller_id', 'sku', 'gtin13', 'selected_variant_gtin13',
                         'source_title', 'ram_gb',
                         'storage_gb', 'network_generation', 'condition',
                         'availability', 'currency', 'offer_id', 'price')


def _utc(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('timezone missing')
    return stamp.astimezone(timezone.utc)


def _fresh(stamp, now):
    return -60 <= (now - _utc(stamp)).total_seconds() <= MAX_AGE_SECONDS


def _exact_offer(offer, url, profile):
    try:
        return (_offer_matches(offer, url, profile)
                and not isinstance(offer.get('price'), bool)
                and Decimal(str(offer['price'])) == profile['price'])
    except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError):
        return False


def _seller_profiles(source):
    """Pin both independent public legal identities; never use a PDP as proof."""
    if ({profile['seller_name']: profile['seller_id']
         for profile in PROFILES.values()} !=
            {seller: identity['seller_id']
             for seller, identity in SELLER_IDENTITIES.items()}):
        raise ValueError('seller_profile_mapping_changed')
    source._load_robots()
    if source.report().get('robots_status') != 'ready' or source._rules is None:
        raise ValueError('seller_robots_unavailable')
    verified = {}
    for seller, expected in SELLER_IDENTITIES.items():
        url = ('https://www.mediamarkt.com.tr/tr/marketplace/seller/'
               + expected['seller_id'])
        if not source._rules.can_fetch(BOT, url):
            raise ValueError('seller_robots_disallowed')
        body, reason, status = source._direct(url, MAX_HTML_BYTES, 'text/html')
        if reason != 'ready' or status != 200 or not isinstance(body, bytes):
            raise ValueError('seller_identity_unavailable')
        text = ' '.join(BeautifulSoup(body, 'html.parser').get_text(' ', strip=True).split())
        if not all(field in text for field in (
                'Firma adı: ' + expected['legal_name'],
                'MERSİS No: ' + expected['mersis'],
                'Vergi No: ' + expected['tax'])):
            raise ValueError('seller_identity_changed')
        verified[seller] = {'seller_url': url, **expected}
    return verified


def _catalog(client):
    """Require complete authenticated safe targets and independent raw sellers."""
    products = client.get_published_products()
    prices = client.get_public_price_priority()
    if not isinstance(products, list) or not isinstance(prices, dict):
        raise ValueError('catalog_unavailable')
    selected = {}
    for url, profile in PROFILES.items():
        rows = [row for row in products if isinstance(row, dict)
                and row.get('id') == profile['id']]
        if len(rows) != 1:
            raise ValueError(profile['slug'] + '_catalog_identity_missing_or_duplicate')
        product = rows[0]
        specs = PriceUpdater.get_expected_specs(product.get('attributes'))
        expected = {key: profile[key] for key in
                    ('ram_gb', 'storage_gb', 'network_generation')}
        if (product.get('slug') != profile['slug']
                or product.get('name') != profile['name']
                or not isinstance(product.get('attributes'), dict)
                or any(specs.get(key) != value for key, value in expected.items()
                       if key != 'network_generation')
                or specs.get('network_generation') not in
                    (None, profile['network_generation'])
                or prices.get(profile['id']) != (profile['slug'], False)):
            raise ValueError(profile['slug'] + '_catalog_variant_or_unpriced_status_changed')
        default_targets = product.get('offer_targets')
        if not isinstance(default_targets, list):
            raise ValueError(profile['slug'] + '_catalog_targets_unavailable')
        raw = client.get_raw_seller_collision_proof(
            profile['id'], profile['seller_name'], url)
        total = raw['refresh_targets_total']
        invalid = raw['refresh_targets_invalid_count']
        duplicate = raw['refresh_targets_duplicate_count']
        if (raw.get('complete') is not True or raw.get('blocked') is not False
                or raw.get('reasons') != [] or duplicate != 0
                or invalid and total > 12
                or raw.get('stored_rows_checked') != total + invalid
                or raw.get('unknown_seller_rows') != 0
                or raw.get('invalid_url_rows') != 0
                or raw.get('invalid_row_identity_rows') != 0
                or raw.get('proposed_seller_other_url_rows') != 0):
            raise ValueError(profile['slug'] + '_raw_seller_or_target_rejection')
        if total <= 12:
            inspected = client.get_offer_target_inspection(profile['id'])
            targets = inspected.get('offer_targets')
            if (inspected.get('id') != profile['id']
                    or inspected.get('slug') != profile['slug']
                    or inspected.get('name') != profile['name']
                    or inspected.get('attributes') != product['attributes']
                    or inspected.get('offer_targets_total') != total
                    or inspected.get('offer_targets_invalid_count') != invalid
                    or inspected.get('offer_targets_duplicate_count') != 0
                    or inspected.get('offer_targets_truncated') is not bool(invalid)
                    or inspected.get('offer_targets_snapshot') !=
                        raw['refresh_targets_snapshot']
                    or inspected.get('offer_targets_invalid_reasons') !=
                        raw['refresh_targets_invalid_reasons']):
                raise ValueError(profile['slug'] + '_target_inspection_changed')
        else:
            complete = client.get_complete_offer_targets(profile['id'])
            targets = complete.get('offer_targets')
            if (complete.get('id') != profile['id']
                    or complete.get('slug') != profile['slug']
                    or complete.get('name') != profile['name']
                    or complete.get('offer_targets_total') != total
                    or complete.get('offer_targets_truncated') is not False
                    or complete.get('offer_targets_snapshot') !=
                        raw['refresh_targets_snapshot']):
                raise ValueError(profile['slug'] + '_complete_targets_changed')
        if (not isinstance(targets, list) or len(targets) != total
                or default_targets != targets[:len(default_targets)]
                or len(default_targets) != min(12, total)
                or product.get('offer_targets_truncated') is not
                    (total > 12 or bool(invalid))):
            raise ValueError(profile['slug'] + '_target_page_changed')
        complete_product = {**product, 'offer_targets': targets,
                            'offer_targets_total': total,
                            'offer_targets_snapshot': raw['refresh_targets_snapshot'],
                            'offer_targets_truncated': False}
        if not no_merchant_collision(complete_product, profile, url):
            raise ValueError(profile['slug'] + '_seller_on_other_url_or_platform')
        evidence = {
            'id': profile['id'], 'slug': profile['slug'], 'name': profile['name'],
            'expected_specs': expected, 'public_unpriced': True,
            'target_count': total, 'target_snapshot': raw['refresh_targets_snapshot'],
            'invalid_targets': invalid, 'duplicate_targets': duplicate,
            'raw_rows_checked': raw['stored_rows_checked'],
            'invalid_reasons': raw['refresh_targets_invalid_reasons'],
            'raw_seller_collision': False,
        }
        selected[url] = (product, evidence)
    return selected


def audit(*, client=None, source=None):
    client = client or OfferIngestionClient.from_env()
    source = source or MediaMarktSource()
    report = {'purpose': PURPOSE, 'writes': False, 'status': 'failed',
              'github_run_id': os.environ.get('GITHUB_RUN_ID'),
              'github_run_attempt': os.environ.get('GITHUB_RUN_ATTEMPT'),
              'checked_at': None, 'catalog_status': 'failed',
              'catalog_error': None, 'robots_status': None,
              'seller_status': 'failed', 'seller_error': None,
              'seller_profiles': None, 'rows': [], 'source': None}
    try:
        catalog = _catalog(client)
        report['catalog_status'] = 'passed'
    except Exception as error:
        catalog = {}
        report['catalog_error'] = (str(error) if isinstance(error, ValueError)
                                   else type(error).__name__)
    try:
        report['seller_profiles'] = _seller_profiles(source)
        report['seller_status'] = 'passed'
    except Exception as error:
        report['seller_error'] = (str(error) if isinstance(error, ValueError)
                                  else type(error).__name__)
    for url, profile in PROFILES.items():
        offer, attempt = _read_selected(source, url)
        verified = offer if _exact_offer(offer, url, profile) else None
        product, evidence = catalog.get(url, (None, None))
        report['rows'].append({
            'url': url,
            'product': {'id': profile['id'], 'slug': profile['slug'],
                        'name': profile['name'], 'expected_specs': {
                            key: profile[key] for key in
                            ('ram_gb', 'storage_gb', 'network_generation')}},
            'catalog_evidence': evidence,
            'source_attempt': attempt, 'verified_offer': verified,
            'status': 'passed' if product and verified and
                      report['seller_status'] == 'passed' else 'failed',
        })
    report['robots_status'] = source.report().get('robots_status')
    if (report['catalog_status'] == 'passed'
            and report['seller_status'] == 'passed'
            and report['robots_status'] == 'ready'
            and len(report['rows']) == len(PROFILES)
            and all(row['status'] == 'passed' for row in report['rows'])):
        report['status'] = 'passed'
    report['checked_at'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    report['source'] = source.report()
    return report


def verified_offers(report, now=None):
    try:
        now = now or datetime.now(timezone.utc)
        if (not isinstance(report, dict) or report.get('purpose') != PURPOSE
                or report.get('writes') is not False
                or report.get('status') != 'passed'
                or report.get('catalog_status') != 'passed'
                or report.get('catalog_error') is not None
                or report.get('seller_status') != 'passed'
                or report.get('seller_error') is not None
                or report.get('seller_profiles') != {
                    seller: {'seller_url':
                             'https://www.mediamarkt.com.tr/tr/marketplace/seller/'
                             + identity['seller_id'], **identity}
                    for seller, identity in SELLER_IDENTITIES.items()}
                or report.get('robots_status') != 'ready'
                or report.get('github_run_id') != os.environ.get('GITHUB_RUN_ID')
                or report.get('github_run_attempt') !=
                    os.environ.get('GITHUB_RUN_ATTEMPT')
                or now.tzinfo is None or not _fresh(report['checked_at'], now)):
            return None
        rows = report.get('rows')
        if not isinstance(rows, list) or len(rows) != len(PROFILES):
            return None
        selected = {}
        for row in rows:
            if not isinstance(row, dict):
                return None
            url = row.get('url')
            profile = PROFILES.get(url)
            evidence = row.get('catalog_evidence')
            attempt = row.get('source_attempt')
            offer = row.get('verified_offer')
            if (url in selected or profile is None or row.get('status') != 'passed'
                    or row.get('product') != {
                        'id': profile['id'], 'slug': profile['slug'],
                        'name': profile['name'], 'expected_specs': {
                            key: profile[key] for key in
                            ('ram_gb', 'storage_gb', 'network_generation')}}
                    or not isinstance(evidence, dict)
                    or evidence.get('id') != profile['id']
                    or evidence.get('slug') != profile['slug']
                    or evidence.get('name') != profile['name']
                    or evidence.get('expected_specs') != {
                        key: profile[key] for key in
                        ('ram_gb', 'storage_gb', 'network_generation')}
                    or evidence.get('public_unpriced') is not True
                    or type(evidence.get('target_count')) is not int
                    or type(evidence.get('invalid_targets')) is not int
                    or evidence['target_count'] < 0
                    or evidence['invalid_targets'] < 0
                    or evidence['invalid_targets'] and evidence['target_count'] > 12
                    or evidence.get('duplicate_targets') != 0
                    or evidence.get('raw_rows_checked') != evidence['target_count']
                       + evidence['invalid_targets']
                    or not isinstance(evidence.get('invalid_reasons'), dict)
                    or sum(evidence['invalid_reasons'].values()) !=
                        evidence['invalid_targets']
                    or not isinstance(evidence.get('target_snapshot'), str)
                    or not re.fullmatch(r'[0-9a-f]{64}', evidence['target_snapshot'])
                    or evidence.get('raw_seller_collision') is not False
                    or not isinstance(attempt, dict)
                    or attempt.get('source_url') != url
                    or attempt.get('merchant') not in
                        (None, profile['seller_name'])
                    or attempt.get('reason') != 'observed_exact_new_marketplace_offer'
                    or attempt.get('http_status') != 200
                    or not _exact_offer(offer, url, profile)
                    or not _fresh(offer['observed_at'], now)
                    or not -60 <= (_utc(report['checked_at']) -
                                   _utc(offer['observed_at'])).total_seconds()
                       <= MAX_AGE_SECONDS):
                return None
            selected[url] = offer
        return selected if set(selected) == set(PROFILES) else None
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None


def _readback_matches(identity, offers, now):
    if (not isinstance(identity, dict)
            or identity.get('evidenceVersion') != 'product-offer-match-v1'
            or identity.get('mode') != 'shadow_audit_only'
            or not isinstance(identity.get('productCoverage'), list)):
        return False
    try:
        if not _fresh(identity['auditedAt'], now):
            return False
        for url, offer in offers.items():
            profile = PROFILES[url]
            rows = [row for row in identity['productCoverage']
                    if isinstance(row, dict) and row.get('slug') == profile['slug']]
            if len(rows) != 1 or not isinstance(rows[0].get('matchedOffers'), list):
                return False
            exact = [row for row in rows[0]['matchedOffers']
                     if isinstance(row, dict) and row.get('source_url') == url]
            if (len(exact) != 1
                    or exact[0].get('merchant_name') != profile['seller_name']
                    or Decimal(str(exact[0].get('price'))) != profile['price']
                    or _utc(identity['auditedAt']) < _utc(offer['observed_at'])):
                return False
        return True
    except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError):
        return False


def publish(report, *, client=None, source=None, now=None):
    """One exact two-record batch only after fresh independent rechecks."""
    now = now or datetime.now(timezone.utc)
    audited = verified_offers(report, now)
    if audited is None:
        raise ValueError('exact_two_audit_missing_stale_or_changed')
    source = source or MediaMarktSource()
    current = {}
    for url, profile in PROFILES.items():
        offer, attempt = _read_selected(source, url)
        if (attempt.get('reason') != 'observed_exact_new_marketplace_offer'
                or attempt.get('http_status') != 200
                or source.report().get('robots_status') != 'ready'
                or not _exact_offer(offer, url, profile)
                or any(offer.get(key) != audited[url].get(key)
                       for key in OFFER_IDENTITY_FIELDS)):
            raise ValueError(profile['slug'] + '_live_offer_changed')
        current[url] = offer
    if _seller_profiles(source) != report['seller_profiles']:
        raise ValueError('legal_seller_identity_changed')
    client = client or OfferIngestionClient.from_env()
    catalog = _catalog(client)
    records = []
    for url, profile in PROFILES.items():
        product, evidence = catalog[url]
        audited_evidence = next(row['catalog_evidence'] for row in report['rows']
                                if row['url'] == url)
        if evidence != audited_evidence:
            raise ValueError(profile['slug'] + '_catalog_or_raw_proof_changed')
        prepared = PriceUpdater(ingestion_client=client).prepare_product_offers(
            product, [current[url]], current[url]['observed_at'],
            current[url]['observed_at'])
        if len(prepared) != 1:
            raise ValueError(profile['slug'] + '_prepared_offer_count_changed')
        record = prepared[0]
        if (record.get('product_id') != profile['id']
                or record.get('product_slug') != profile['slug']
                or record.get('seller') != profile['seller_name']
                or record.get('source_url') != url
                or Decimal(str(record.get('price'))) != profile['price']
                or record.get('currency') != 'TRY'
                or record.get('availability') != 'in_stock'
                or record.get('source_title') != profile['source_title']
                or record.get('observed_at') != current[url]['observed_at']
                or record.get('checked_at') != current[url]['observed_at']
                or record.get('is_official') is not False):
            raise ValueError(profile['slug'] + '_prepared_offer_changed')
        records.append(record)
    if (len(records) != 2
            or {row['product_id'] for row in records} != {32}
            or {row['seller'] for row in records} != set(SELLER_IDENTITIES)
            or {row['source_url'] for row in records} != set(PROFILES)):
        raise ValueError('exact_two_seller_record_scope_changed')
    commit_time = datetime.now(timezone.utc)
    if (verified_offers(report, commit_time) is None
            or any(not _fresh(offer['observed_at'], commit_time)
                   for offer in current.values())):
        raise ValueError('exact_two_observation_stale_before_commit')
    result = client.ingest_offers(records, require_all_accepted=True)
    if (not isinstance(result, dict) or result.get('committed') is not True
            or result.get('accepted') != 2 or result.get('stale_ignored') != 0
            or result.get('variant_rejected') != 0):
        raise RuntimeError('exact_two_offers_not_accepted')
    if not _readback_matches(client.get_offer_identity_readiness(), current,
                             datetime.now(timezone.utc)):
        raise RuntimeError('commercial_readback_missing_exact_two_offers')
    return {'status': 'passed', 'submitted': 2, 'accepted': 2,
            'offers': [{'slug': PROFILES[url]['slug'],
                        'seller': PROFILES[url]['seller_name'],
                        'source_url': url, 'price': offer['price']}
                       for url, offer in current.items()],
            'readback': 'two_exact_selected_offers'}


def _publication_confirmed(publication):
    if (not isinstance(publication, dict)
            or publication.get('status') != 'passed'
            or publication.get('submitted') != 2
            or publication.get('accepted') != 2
            or publication.get('readback') != 'two_exact_selected_offers'
            or not isinstance(publication.get('offers'), list)
            or len(publication['offers']) != len(PROFILES)):
        return False
    rows = {row.get('source_url'): row for row in publication['offers']
            if isinstance(row, dict)}
    if set(rows) != set(PROFILES):
        return False
    for url, profile in PROFILES.items():
        row = rows[url]
        try:
            if (row.get('slug') != profile['slug']
                    or row.get('seller') != profile['seller_name']
                    or Decimal(str(row.get('price'))) != profile['price']):
                return False
        except (InvalidOperation, TypeError, ValueError):
            return False
    return True


def summary(report, *, publication=None, publish_requested=None):
    if publish_requested is None:
        publication_state = 'Denetim aşaması: yazım yok; koşunun yayın sonucu henüz belirlenmedi.'
    elif publish_requested is False and publication is None:
        publication_state = 'Yayın istenmedi; bu koşuda fiyat yazımı yok.'
    elif publish_requested is True and report.get('status') != 'passed':
        publication_state = 'Denetim başarısız; yayın adımı çalıştırılmadı ve fiyat yazımı yok.'
    elif (publish_requested is True and report.get('status') == 'passed'
          and report.get('catalog_status') == 'passed'
          and _publication_confirmed(publication)):
        publication_state = 'Yayın tamamlandı: iki teklif kabul edildi ve kaynak bazlı readback doğrulandı.'
    else:
        publication_state = ('Yayın sonucu doğrulanamadı; yazım gerçekleşmiş olabilir. '
                             'Ingestion sonucunu kontrol edin.')
    lines = [f"Galaxy A55 5G MediaMarkt iki satıcı: {report['status']}; "
             f"yetkili katalog {report['catalog_status']}; robots "
             f"{report.get('robots_status')}; yasal satıcı "
             f"{report.get('seller_status')}. {publication_state}"]
    for row in report['rows']:
        offer = row.get('verified_offer') or {}
        evidence = row.get('catalog_evidence') or {}
        attempt = row.get('source_attempt') or {}
        lines.append(f"- {row['product']['slug']} [{row['status']}]: "
                     f"{attempt.get('reason')} HTTP {attempt.get('http_status')}; "
                     f"{offer.get('seller_name') or '?'} {offer.get('price') or '?'} TRY; "
                     f"SKU {offer.get('sku') or '?'}, GTIN {offer.get('gtin13') or '?'}, "
                     f"radyo {offer.get('network_generation') or '?'}, "
                     f"stok {offer.get('availability') or '?'}; "
                     f"tam hedef {evidence.get('target_count')}, "
                     f"geçersiz {evidence.get('invalid_targets')}, "
                     f"çift {evidence.get('duplicate_targets')}, "
                     f"ham satır {evidence.get('raw_rows_checked')}, "
                     f"satıcı çakışması {evidence.get('raw_seller_collision')}.")
    if report.get('catalog_error'):
        lines.append('Katalog engeli: ' + report['catalog_error'])
    if report.get('seller_error'):
        lines.append('Satıcı kimliği engeli: ' + report['seller_error'])
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='action', required=True)
    read = sub.add_parser('audit')
    read.add_argument('--audit-file', type=Path, required=True)
    write = sub.add_parser('publish')
    write.add_argument('--audit-file', type=Path, required=True)
    write.add_argument('--output-file', type=Path, required=True)
    write.add_argument('--publish-exact-two', action='store_true', required=True)
    render = sub.add_parser('summarize')
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
        rendered = summary(report, publication=publication,
                           publish_requested=args.publish_requested == 'true')
        args.output_file.write_text(rendered)
        print(rendered, end='')
        return 0
    result = publish(json.loads(args.audit_file.read_text()))
    args.output_file.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
