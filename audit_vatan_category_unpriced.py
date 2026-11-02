"""Read-only runner proof for two unpriced Teknoskor models on Vatan.

Fixed category pages supply URL hints. Prices, seller and stock must be proved
again by the selected Product Offer on each exact PDP in this run.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir

from offer_ingestion_client import OfferIngestionClient
from retailer_offer_refresh import RetailerOfferRefresher
from tr_price_scraper import TRPriceScraper
from update_prices import PriceUpdater
from vatan_category_source import OPPO_CATEGORY, VIVO_CATEGORY, VatanCategorySource
from vatan_reviewed_source import VatanReviewedSource


PROFILES = (
    {'id': 290, 'slug': 'oppo-find-x9-pro', 'name': 'OPPO Find X9 Pro',
     'ram_gb': 16, 'storage_gb': 512, 'network_generation': '5g',
     'category_url': OPPO_CATEGORY,
     'url': 'https://www.vatanbilgisayar.com/oppo-find-x9-pro-16-512-akilli-telefon-titanyum-antrasit.html'},
    {'id': 286, 'slug': 'vivo-x300', 'name': 'vivo X300',
     'ram_gb': 16, 'storage_gb': 512, 'network_generation': '5g',
     'category_url': VIVO_CATEGORY,
     'url': 'https://www.vatanbilgisayar.com/vivo-x300-16-512gb-akilli-telefon-pembe.html'},
)
OUT = Path(os.environ.get('VATAN_CATEGORY_AUDIT_OUTPUT_DIR', gettempdir()))


def run_audit(categories=None, reader=None, catalog_client=None):
    categories = categories or VatanCategorySource()
    reader = reader or RetailerOfferRefresher(vatan_source=VatanReviewedSource(
        additional_reviewed_urls=tuple(profile['url'] for profile in PROFILES)))
    matcher = TRPriceScraper()
    catalog_status = 'ready'
    try:
        products = (catalog_client or OfferIngestionClient.from_env()).get_published_products()
        if not isinstance(products, list) or len(products) > 1000:
            raise ValueError('invalid_catalog_snapshot')
    except Exception as error:
        products = []
        catalog_status = 'unavailable_' + type(error).__name__
    by_id = {product.get('id'): product for product in products if isinstance(product, dict)}
    rows = []
    for profile in PROFILES:
        specs = {key: profile[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
        catalog_product = by_id.get(profile['id'])
        catalog_specs = (PriceUpdater.get_expected_specs(catalog_product.get('attributes'))
                         if isinstance(catalog_product, dict) else {})
        catalog_confirmed = (isinstance(catalog_product, dict)
                             and catalog_product.get('id') == profile['id']
                             and catalog_product.get('slug') == profile['slug']
                             and catalog_product.get('name') == profile['name']
                             and all(matcher._spec_int(catalog_specs.get(key)) == profile[key]
                                     for key in ('ram_gb', 'storage_gb'))
                             and catalog_specs.get('network_generation') == profile['network_generation'])
        cards = categories.read_category(profile['category_url'])
        matching_cards = [card for card in cards if card.get('source_url') == profile['url']
                          and matcher.has_observed_offer_evidence(
                              profile['name'], {'source_title': card.get('title'),
                                                'url': card.get('source_url')}, specs)]
        targets = [{'source_url': profile['url']}]
        # The known URL is still probed if listing access fails, so the report
        # distinguishes runner category access from exact PDP reader access.
        offers = reader.refresh_existing_offer_targets(profile['name'], targets, specs)
        verified = [offer for offer in offers
                    if offer.get('merchant') == 'Vatan Bilgisayar'
                    and offer.get('url') == profile['url']
                    and offer.get('currency') == 'TRY'
                    and offer.get('availability') in {'in_stock', 'limited'}
                    and isinstance(offer.get('price'), (int, float))
                    and offer['price'] > 0]
        row = {'id': profile['id'], 'slug': profile['slug'], 'name': profile['name'],
               'specs': specs, 'category_url': profile['category_url'],
               'catalog_confirmed': catalog_confirmed,
               'catalog_observed': ({'id': catalog_product.get('id'),
                                     'slug': catalog_product.get('slug'),
                                     'name': catalog_product.get('name'),
                                     'specs': catalog_specs}
                                    if isinstance(catalog_product, dict) else None),
               'url': profile['url'], 'category_confirmed': len(matching_cards) == 1,
               'category_title': matching_cards[0]['title'] if len(matching_cards) == 1 else None,
               'offer_verified': len(verified) == 1,
               'offer': {key: verified[0].get(key) for key in
                         ('merchant', 'price', 'currency', 'availability',
                          'source_title', 'url', 'observed_at')}
                        if len(verified) == 1 else None,
               'source_attempts': reader.target_report(profile['name'], targets, specs)}
        rows.append(row)
    return {'purpose': 'read_only_vatan_category_and_selected_pdp_proof',
            'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'status': 'passed' if all(row['catalog_confirmed'] and row['category_confirmed']
                                      and row['offer_verified']
                                      for row in rows) else 'partial',
            'catalog_status': catalog_status,
            'categories': categories.report(), 'products': rows,
            'vatan_source': reader.vatan_reviewed_source.report(), 'writes': False}


def summary(report):
    lines = ['## Vatan unpriced phone category + PDP audit (read only)',
             f"Status: {report['status']}; catalog {report['catalog_status']}; "
             f"robots {report['categories']['robots_status']}."]
    for row in report['products']:
        attempt = row['source_attempts'][0] if row['source_attempts'] else {}
        offer = row['offer'] or {}
        lines.append(f"- {row['slug']}: catalog {row['catalog_confirmed']}; "
                     f"category {row['category_confirmed']}; "
                     f"selected PDP {row['offer_verified']}; "
                     f"{attempt.get('reason', 'not_attempted')} HTTP {attempt.get('http_status')}; "
                     f"cash TRY {offer.get('price')}; stock {offer.get('availability')}.")
    lines.append('No catalog, price, schema, R2, or IndexNow writes. Category card prices are ignored.')
    return '\n'.join(lines) + '\n'


def main():
    report = run_audit()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'vatan-category-unpriced.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    text = summary(report)
    (OUT / 'vatan-category-unpriced.md').write_text(text)
    print(text, end='')
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
