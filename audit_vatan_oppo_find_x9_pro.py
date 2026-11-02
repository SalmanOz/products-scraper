"""Read-only same-run proof for one exact OPPO Find X9 Pro Vatan PDP."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir

from audit_vatan_category_unpriced import PROFILES
from offer_ingestion_client import OfferIngestionClient
from retailer_offer_refresh import RetailerOfferRefresher
from tr_price_scraper import TRPriceScraper
from update_prices import PriceUpdater
from vatan_reviewed_source import VatanReviewedSource


PROFILE = PROFILES[0]
SPECS = {key: PROFILE[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
URL = PROFILE['url']
OUT = Path(os.environ.get('VATAN_OPPO_AUDIT_OUTPUT_DIR', gettempdir()))


def exact_catalog_product(products):
    if not isinstance(products, list) or len(products) > 1000:
        return None
    matches = [product for product in products if isinstance(product, dict)
               and product.get('id') == PROFILE['id']]
    if len(matches) != 1:
        return None
    product = matches[0]
    catalog_specs = PriceUpdater.get_expected_specs(product.get('attributes'))
    matcher = TRPriceScraper()
    if (product.get('slug') != PROFILE['slug'] or product.get('name') != PROFILE['name']
            or any(matcher._spec_int(catalog_specs.get(key)) != PROFILE[key]
                   for key in ('ram_gb', 'storage_gb'))
            or catalog_specs.get('network_generation') != PROFILE['network_generation']):
        return None
    return product


def run_audit(reader=None, catalog_client=None):
    reader = reader or RetailerOfferRefresher(
        vatan_source=VatanReviewedSource(additional_reviewed_urls=(URL,)))
    catalog_status = 'ready'
    try:
        products = (catalog_client or OfferIngestionClient.from_env()).get_published_products()
    except Exception as error:
        products = []
        catalog_status = 'unavailable_' + type(error).__name__
    product = exact_catalog_product(products)
    targets = [{'source_url': URL}]
    offers = reader.refresh_existing_offer_targets(PROFILE['name'], targets, SPECS)
    updater = PriceUpdater(ingestion_client=object(), offer_refresher=reader)
    verified = [offer for offer in offers
                if offer.get('merchant') == 'Vatan Bilgisayar'
                and offer.get('url') == URL
                and offer.get('availability') == 'in_stock'
                and offer.get('selected_vatan_variant_verified') is True
                and product is not None
                and updater.live_offer_is_valid(product, offer, SPECS)]
    attempt = reader.target_report(PROFILE['name'], targets, SPECS)
    return {'purpose': 'read_only_vatan_oppo_find_x9_pro_exact_pdp',
            'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'status': 'passed' if product is not None and len(verified) == 1 else 'partial',
            'catalog_status': catalog_status,
            'catalog_variant': {'id': PROFILE['id'], 'slug': PROFILE['slug'],
                                'name': PROFILE['name'], 'specs': SPECS},
            'catalog_confirmed': product is not None,
            'url': URL,
            'offer_verified': len(verified) == 1,
            'offer': {key: verified[0].get(key) for key in
                      ('merchant', 'price', 'currency', 'availability',
                       'source_title', 'url', 'observed_at')}
                       | {'selected_vatan_variant_verified': True}
                     if len(verified) == 1 else None,
            'source_attempts': attempt,
            'vatan_source': reader.vatan_reviewed_source.report(),
            'writes': False}


def main():
    report = run_audit()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'vatan-oppo-find-x9-pro-audit.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    attempt = report['source_attempts'][0] if report['source_attempts'] else {}
    offer = report['offer'] or {}
    lines = ['## OPPO Find X9 Pro 16/512 exact Vatan PDP (read only)',
             f"Status: {report['status']}; catalog {report['catalog_confirmed']}; "
             f"PDP {report['offer_verified']}; {attempt.get('reason', 'not_attempted')} "
             f"HTTP {attempt.get('http_status')}; cash TRY {offer.get('price')}; "
             f"stock {offer.get('availability')}.",
             'No catalog, price, schema, R2, or IndexNow writes.']
    text = '\n'.join(lines) + '\n'
    (OUT / 'vatan-oppo-find-x9-pro-audit.md').write_text(text)
    print(text, end='')
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
