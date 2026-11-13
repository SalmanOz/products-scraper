"""Read-only, same-run proof of the single Vatan vivo X300 selected PDP."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir

from offer_ingestion_client import OfferIngestionClient
from retailer_offer_refresh import RetailerOfferRefresher, VIVO_X300_VATAN_PDP
from tr_price_scraper import TRPriceScraper
from update_prices import PriceUpdater
from vatan_reviewed_source import VatanReviewedSource


PRODUCT = {'id': 286, 'slug': 'vivo-x300', 'name': 'vivo X300',
           'ram_gb': 16, 'storage_gb': 512, 'network_generation': '5g'}
PROFILE = PRODUCT
SPECS = {key: PRODUCT[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
URL = VIVO_X300_VATAN_PDP
SELECTED_H1 = 'Vivo X300 16/512GB Akıllı Telefon Pembe'
MAX_OBSERVATION_AGE_SECONDS = 300
OUT = Path(os.environ.get('VATAN_VIVO_AUDIT_OUTPUT_DIR', gettempdir()))


def _fresh(observed_at, now):
    if not isinstance(observed_at, str):
        return False
    try:
        observed = datetime.fromisoformat(observed_at.replace('Z', '+00:00'))
        age = (now - observed).total_seconds()
        return observed.tzinfo is not None and 0 <= age <= MAX_OBSERVATION_AGE_SECONDS
    except ValueError:
        return False


def exact_catalog_product(products):
    if not isinstance(products, list) or len(products) > 1000:
        return None
    matches = [product for product in products
               if isinstance(product, dict) and product.get('id') == PRODUCT['id']]
    if len(matches) != 1:
        return None
    product = matches[0]
    specs = PriceUpdater.get_expected_specs(product.get('attributes'))
    matcher = TRPriceScraper()
    if (product.get('slug') != PRODUCT['slug']
            or product.get('name') != PRODUCT['name']
            or any(matcher._spec_int(specs.get(key)) != PRODUCT[key]
                   for key in ('ram_gb', 'storage_gb'))
            or specs.get('network_generation') != '5g'):
        return None
    return product


def run_audit(reader=None, catalog_client=None, now=None):
    if now is not None and now.tzinfo is None:
        raise ValueError('now must include timezone')
    reader = reader or RetailerOfferRefresher(vatan_source=VatanReviewedSource(
        additional_reviewed_urls=(VIVO_X300_VATAN_PDP,)))
    catalog_status = 'ready'
    try:
        catalog = (catalog_client or OfferIngestionClient.from_env()).get_published_products()
        if not isinstance(catalog, list) or len(catalog) > 1000:
            raise ValueError('invalid_catalog_snapshot')
    except Exception as error:
        catalog, catalog_status = [], 'unavailable_' + type(error).__name__
    product = exact_catalog_product(catalog)
    catalog_confirmed = product is not None
    specs = PriceUpdater.get_expected_specs(product.get('attributes')) if product else {}
    targets = [{'source_url': VIVO_X300_VATAN_PDP}]
    offers = reader.refresh_existing_offer_targets(PRODUCT['name'], targets, SPECS)
    attempts = reader.target_report(PRODUCT['name'], targets, SPECS)
    source = reader.vatan_reviewed_source.report()
    checked_at = now or datetime.now(timezone.utc)
    attempt_confirmed = (len(attempts) == 1
                         and attempts[0].get('source_url') == VIVO_X300_VATAN_PDP
                         and attempts[0].get('merchant') == 'Vatan Bilgisayar'
                         and attempts[0].get('reason') == 'observed_exact_offer'
                         and attempts[0].get('http_status') == 200)
    offer_confirmed = (len(offers) == 1
                       and offers[0].get('merchant') == 'Vatan Bilgisayar'
                       and offers[0].get('selected_vatan_variant_verified') is True
                       and offers[0].get('source_title') == SELECTED_H1
                       and offers[0].get('url') == VIVO_X300_VATAN_PDP
                       and offers[0].get('currency') == 'TRY'
                       and offers[0].get('availability') == 'in_stock'
                       and isinstance(offers[0].get('price'), (int, float))
                       and not isinstance(offers[0].get('price'), bool)
                       and 0 < offers[0]['price'] <= 5_000_000
                       and _fresh(offers[0].get('observed_at'), checked_at))
    status = ('passed' if catalog_confirmed and attempt_confirmed and offer_confirmed else 'partial')
    return {'purpose': 'read_only_vatan_vivo_exact_selected_pdp_proof',
            'checked_at': checked_at.isoformat().replace('+00:00', 'Z'),
            'status': status, 'catalog_status': catalog_status,
            'catalog_confirmed': catalog_confirmed,
            'catalog_variant': {'id': PRODUCT['id'], 'slug': PRODUCT['slug'],
                                'name': PRODUCT['name'], 'specs': SPECS},
            'catalog_observed': ({'id': product.get('id'), 'slug': product.get('slug'),
                                  'name': product.get('name'), 'specs': specs} if product else None),
            'product_id': PRODUCT['id'], 'url': VIVO_X300_VATAN_PDP,
            'attempt_confirmed': attempt_confirmed, 'source_attempts': attempts,
            'offer_confirmed': offer_confirmed,
            'offer': ({key: offers[0].get(key) for key in
                       ('merchant', 'price', 'currency', 'availability',
                        'source_title', 'url', 'observed_at',
                        'selected_vatan_variant_verified')} if offer_confirmed else None),
            'vatan_source': source, 'writes': False}


def summary(report):
    attempt = report['source_attempts'][0] if report['source_attempts'] else {}
    offer = report['offer'] or {}
    return ('## Vatan vivo X300 selected PDP audit (read only)\n'
            f"Status: {report['status']}; catalog {report['catalog_confirmed']}; "
            f"selected PDP {report['offer_confirmed']}; "
            f"{attempt.get('reason', 'not_attempted')} HTTP {attempt.get('http_status')}; "
            f"cash TRY {offer.get('price')}; stock {offer.get('availability')}.\n"
            'No catalog, price, schema, R2 or IndexNow writes.\n')


def main():
    report = run_audit()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'vatan-vivo-exact.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    text = summary(report)
    (OUT / 'vatan-vivo-exact.md').write_text(text)
    print(text, end='')
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
