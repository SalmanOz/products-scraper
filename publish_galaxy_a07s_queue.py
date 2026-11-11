"""Verify and publish the Galaxy A07s 4G with the exact Turkish 4/128 SKU."""

import argparse
from datetime import datetime, timezone
import json
import re
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from automatic_memory_variants import variant_selected_evidence
from daily_discovery import PublicReader, model_key, product_names
from priority_retailer_transport import PriorityRetailerTransport, compatible_get
from publish_s26_fe_queue import publish_verified
from retailer_offer_refresh import _schema_nodes
from teknosa_reviewed_source import _amount, _cash_price
from tr_price_scraper import TRPriceScraper
from trendyol_catalog_source import parse_catalog_listing, selected_product_state
from tws_offer_pilot import independent_sellers
from tws_retailer_sources import selected_photo

NAME = 'Samsung Galaxy A07s'
SLUG = 'samsung-galaxy-a07s'
GTIN = '8806099448929'
MPN = 'SM-A077FLVGTUR'
SPECS = {'ram_gb': 4, 'storage_gb': 128, 'network_generation': '4g'}
MANUFACTURER = ('https://www.samsung.com/tr/smartphones/galaxy-a/'
                'galaxy-a07s-light-violet-128gb-sm-a077flvgtur/')
SPEC_DATABASE = 'https://www.kimovil.com/en/where-to-buy-samsung-galaxy-a07s'
OZGUR_BASE = 'https://ozguriletisimankara.com'
OZGUR = OZGUR_BASE + '/urun/samsung-galaxy-a07s-4-gb-ram-acik-mor-128-gb'
OZGUR_TERMS = OZGUR_BASE + '/mesafeli-satis'
TRENDYOL = 'https://www.trendyol.com/samsung/galaxy-a07s-4-128-gb-acik-mor-cep-telefonu-p-1210300556'
CANDIDATE = {'name': NAME, 'slug': SLUG, 'brandSlug': 'samsung', 'segment': 'new_release',
             'evidenceUrl': MANUFACTURER}


def specifications():
    observed = datetime.now(timezone.utc).isoformat()
    physical = {
        'ram_gb': 4, 'storage_gb': 128, 'battery_mah': 5000, 'screen_size_inch': 6.7,
        'Design & Materials': {'Screen': '6.7" PLS LCD', 'Resolution': '720 × 1600',
                               'Refresh rate': '90 Hz', 'Weight': '188 g', 'Resistance': 'IP54'},
        'Performance & Hardware': {'Processor': 'MediaTek Helio G99+', 'RAM': '4 GB', 'Storage': '128 GB'},
        'Camera': {'Main camera': '50 MP', 'Depth': '2 MP', 'Selfie': '8 MP'},
        'Connectivity': {'5G': False, '4G': 'LTE FDD / TDD', 'Bluetooth': '5.3'},
        'Batarya': {'Capacity': '5000 mAh'}, 'Software': {'Operating system': 'Android'},
    }
    sources = [(key, value, 'manufacturer', MANUFACTURER) for key, value in physical.items()]
    sources.extend([
        ('Technical sheet', {'Release date': '2026-09'}, 'spec_database', SPEC_DATABASE),
        ('partials', {'design': 5.4, 'hardware': 5.9, 'camera': 6.2, 'connectivity': 5.4, 'battery': 6.7},
         'benchmark_source', SPEC_DATABASE),
    ])
    return [{'product_slug': SLUG, 'attribute_key': key, 'value': value, 'value_origin': origin,
             'source_url': url, 'observed_at': observed, 'confidence': 'medium', 'is_estimate': False}
            for key, value, origin, url in sources]


def ozgur_offer(html, legal_html):
    soup = BeautifulSoup(html, 'html.parser')
    canonical = soup.select_one('link[rel="canonical"]')
    roots = soup.select('.oi-product-detail[data-product-id][data-oi-view-variant]')
    products = [item for item in _schema_nodes(soup, OZGUR) if item.get('@type') == 'Product']
    if not canonical or canonical.get('href') != OZGUR or len(products) != 1 or len(roots) != 1:
        raise ValueError('ozgur_selected_page_unverified')
    product, root = products[0], roots[0]
    offer = product.get('offers') or {}
    box = root.select_one('.oi-pd-buybox')
    if box is None:
        raise ValueError('ozgur_buybox_missing')
    heading = box.select_one('h1.oi-pd-title')
    title = heading.get_text(' ', strip=True) if heading else ''
    model = box.select_one('.oi-pd-model span')
    stock = box.select_one('.oi-pd-stockline.is-in')
    visible = box.select_one('[data-oi-price]')
    cart = box.select_one('[data-oi-add-cart]')
    photo = root.select_one('img#oi-product-main-img[src]')
    photo_url = selected_photo(urljoin(OZGUR_BASE + '/', photo['src']), {OZGUR_BASE.split('//')[1]}) if photo else None
    legal = BeautifulSoup(legal_html, 'html.parser').get_text(' ', strip=True)
    company = re.search(r'Satıcı:\s*(.+?)\s+Vergi dairesi:', legal)
    price = _amount(offer.get('price'))
    if (not company or not title or product.get('name') != title or product.get('url') != OZGUR
            or not TRPriceScraper().is_strict_match(NAME, title, SPECS) or product.get('mpn') != MPN
            or not model or model.get_text(strip=True) != MPN or product.get('gtin13') != GTIN
            or (product.get('brand') or {}).get('name') != 'Samsung' or not photo_url
            or photo_url not in product.get('image', []) or offer.get('url') != OZGUR
            or (offer.get('seller') or {}).get('name') != 'Özgür İletişim'
            or offer.get('availability') != 'https://schema.org/InStock'
            or offer.get('itemCondition') != 'https://schema.org/NewCondition' or offer.get('priceCurrency') != 'TRY'
            or not stock or stock.get_text(' ', strip=True) != 'Stokta' or not cart
            or cart.get('data-id') != root['data-product-id'] or cart.get_text(' ', strip=True) != 'Sepete Ekle'
            or cart.has_attr('disabled') or cart.get('aria-disabled') == 'true'
            or price is None or not visible or _cash_price(visible.get_text(' ', strip=True)) != price):
        raise ValueError('ozgur_exact_sku_cash_offer_unverified')
    merchant = 'Özgür İletişim'
    proof = {'version': 'selected-retailer-sku-v1', 'platform': merchant, 'merchant_id': 'ozgur-iletisim',
             'merchant_name': merchant, 'product_id': root['data-product-id'], 'listing_id': root['data-oi-view-variant'],
             'sku': product['sku'], 'gtin13': GTIN, **SPECS}
    return {'merchant': merchant, 'platform': merchant, '_legal_name': TRPriceScraper._normalize_words(company[1]),
            'price': float(price), 'currency': 'TRY', 'availability': 'in_stock', 'gtin13': GTIN,
            'source_title': title, 'url': OZGUR, 'observed_at': datetime.now(timezone.utc).isoformat(),
            'source_variant_evidence': proof}, photo_url


def prepare():
    reader = PublicReader()
    page = reader.read(MANUFACTURER)
    official = BeautifulSoup(page, 'html.parser')
    option_data = official.select_one('#BV-buyingOptionData[value]')
    options = json.loads(option_data['value']).get(MPN) if option_data else None
    if (not any(model_key(name) == model_key(NAME) for name in product_names(page, MANUFACTURER))
            or options != {'Renk': 'Açık Mor', 'Depolama': '128 GB', 'Hafıza': '4 GB'}
            or '2G GSM, 3G WCDMA, 4G LTE FDD, 4G LTE TDD' not in official.get_text(' ', strip=True)):
        raise ValueError('manufacturer_4g_variant_unverified')
    robots = RobotFileParser()
    robots.parse(reader.read(OZGUR_BASE + '/robots.txt').splitlines())
    if any(not robots.can_fetch('TeknoskorPriceBot', url) for url in (OZGUR, OZGUR_TERMS)):
        raise ValueError('ozgur_robots_disallowed')
    direct, photo = ozgur_offer(reader.read(OZGUR), reader.read(OZGUR_TERMS))

    transport = PriorityRetailerTransport(maximum_per_host=4, browser_fallback=False, get=compatible_get)
    html, reason, _ = transport.read_document(TRENDYOL)
    if not html or transport.resolved_url(TRENDYOL) != TRENDYOL:
        raise ValueError(f'trendyol_{reason}')
    second, reason = parse_catalog_listing(html, TRENDYOL, NAME, SPECS)
    product, _ = selected_product_state(BeautifulSoup(html, 'html.parser'))
    seller = (product or {}).get('merchantListing', {}).get('merchant', {})
    if (not second or second['merchant'] != 'Trendyol' or second['gtin13'] != GTIN
            or second['availability'] != 'in_stock' or second['currency'] != 'TRY'
            or str(second['merchant_id']) != str(seller.get('id'))):
        raise ValueError(f'trendyol_exact_4g_offer_unverified_{reason}')
    second['_legal_name'] = TRPriceScraper._normalize_words(seller.get('officialName'))
    second['source_variant_evidence'] = variant_selected_evidence(second, NAME, SPECS)
    if not second['source_variant_evidence'] or not independent_sellers(direct, second):
        raise ValueError('two_independent_in_stock_offers_required')
    return [direct, second], photo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    rows, photo = prepare()
    print(json.dumps({'gtin': GTIN, 'offers': [{key: row[key] for key in ('merchant', 'price', 'url', 'observed_at')}
                     for row in rows], 'photo': photo}, ensure_ascii=False), flush=True)
    if args.publish:
        publish_verified(CANDIDATE, specifications(), rows, photo)


if __name__ == '__main__':
    main()
