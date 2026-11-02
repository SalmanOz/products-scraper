"""Read-only audit for one reviewed iPhone 16 Pro Max 8/256 buy box.

Hepsiburada's JSON-LD currently calls Apple the seller and gives the selected
product a synthetic GTIN. Neither field identifies the selected offer. This
audit instead reconciles the visible buy box, the selected Redux product and
variant, and two buy-box events. It is deliberately not an ingestion source.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
import requests

from hepsiburada_s25_fe_source import (
    BOT, MAX_HTML_BYTES, MAX_ROBOTS_BYTES, ROBOTS_URL,
    HepsiburadaS25FeSource, _nodes, _normalized,
)
from retailer_offer_refresh import _challenge_page, _is_conditional_price
from tr_price_scraper import TRPriceScraper


CATALOG = {'id': 24, 'slug': 'apple-iphone-16-pro-max',
           'name': 'Apple iPhone 16 Pro Max', 'ram_gb': 8,
           'storage_gb': 256, 'network_generation': '5g'}
URL = 'https://www.hepsiburada.com/iphone-16-pro-max-256gb-col-titanyum-pm-HBC00006Y4HC4'
SCHEMA_URL = ('https://www.hepsiburada.com/'
              'apple-iphone-16-pro-max-256gb-col-titanyum-pm-HBC00006Y4HC4')
SELLER_URL = 'https://www.hepsiburada.com/magaza/bsz-elektronik'
PRODUCT_ID = 'HBC00006Y4HC4'
SKU = 'HBCV00006Y4HC5'
GTIN13 = '0195949806179'
COLOR = 'Çöl Titanyum'
SELLER = 'Bsz Elektronik'
MERCHANT_ID = 'c9df3cfc-5c33-49a0-a730-3c2e5bf0007d'
LISTING_ID = '72c6883a-067c-4819-b62b-07ccaaee7a08'
SPECS = {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'}


def _amount(value):
    if isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if amount.is_finite() and 0 < amount <= 5_000_000 else None


def _valid_gtin13(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{13}', value):
        return False
    digits = [int(digit) for digit in value]
    return (10 - (sum(digits[:12:2]) + 3 * sum(digits[1:12:2])) % 10) % 10 == digits[12]


def _selected_variant(product):
    variants = product.get('variants')
    if not isinstance(variants, list):
        return None
    if sum(isinstance(item, dict) and item.get('isDefault') is True
           for item in variants) != 1:
        return None
    selected = [variant for variant in variants if isinstance(variant, dict)
                and variant.get('sku') == SKU and variant.get('isDefault') is True]
    if len(selected) != 1:
        return None
    variant = selected[0]
    properties = variant.get('properties')
    values = {}
    if not isinstance(properties, list):
        return None
    for prop in properties:
        if not isinstance(prop, dict) or not isinstance(prop.get('valueObject'), dict):
            return None
        name = prop.get('name')
        if name in values:
            return None
        values[name] = prop['valueObject'].get('actualValue')
    barcodes = variant.get('barcodes')
    # A synthetic barcode may also appear, but only the selected variant's
    # independently reviewed, checksum-valid Apple GTIN is used as evidence.
    gtins = [barcode for barcode in barcodes if isinstance(barcode, str)
             and re.fullmatch(r'\d{13}', barcode)] if isinstance(barcodes, list) else []
    if (values != {'Renk': COLOR, 'Kapasite': '256 GB'}
            or gtins != [GTIN13]
            or not _valid_gtin13(GTIN13)
            or variant.get('isInStock') is not True
            or variant.get('isPreOrder') is not False
            or _amount(variant.get('price')) is None
            or not TRPriceScraper().is_strict_match(CATALOG['name'],
                                                   str(variant.get('name') or ''), SPECS)
            or re.search(r'yenilenmi[sş]|refurbished|ikinci\s*el',
                         str(variant.get('name') or ''), re.I)):
        return None
    return variant


def parse_exact_buy_box(html: str, final_url: str = URL):
    """Return (observed offer, reason) or fail closed on a changed buy box."""
    if (final_url != URL or not isinstance(html, str)
            or len(html.encode('utf-8')) > MAX_HTML_BYTES):
        return None, 'wrong_or_oversized_pdp'
    soup = BeautifulSoup(html, 'html.parser')
    if _challenge_page(soup):
        return None, 'challenge_page'

    headings = soup.select('h1[data-test-id="title"]')
    if len(headings) != 1:
        return None, 'selected_title_missing'
    title = _normalized(headings[0].get_text(' ', strip=True))
    matcher = TRPriceScraper()
    if (not matcher.is_strict_match(CATALOG['name'], title, SPECS)
            or COLOR.casefold() not in title.casefold()
            or re.search(r'yenilenmi[sş]|refurbished|ikinci\s*el', title, re.I)):
        return None, 'selected_variant_mismatch'

    scripts = soup.select('script#reduxStore')
    if len(scripts) != 1:
        return None, 'redux_missing'
    try:
        state = json.loads(scripts[0].get_text())
    except (TypeError, ValueError):
        return None, 'redux_invalid'
    product_state = state.get('productState') if isinstance(state, dict) else None
    product = product_state.get('product') if isinstance(product_state, dict) else None
    if not isinstance(product, dict):
        return None, 'redux_product_missing'
    stock = product.get('stockInformation')
    if (product.get('productId') != PRODUCT_ID or product.get('sku') != SKU
            or product.get('listingId') != LISTING_ID
            or product.get('merchantName') != SELLER
            or product.get('merchantId') != MERCHANT_ID
            or product.get('isInStock') is not True
            or not isinstance(stock, dict) or stock.get('isInStock') is not True
            or product.get('isProductLive') is not True
            or product.get('isClosedProduct') is not False
            or product.get('isPreOrder') is not False):
        return None, 'selected_redux_offer_mismatch'
    merchant = product.get('merchant')
    if (not isinstance(merchant, dict) or merchant.get('id') != MERCHANT_ID
            or merchant.get('name') != SELLER
            or merchant.get('urlPostfix') != 'bsz-elektronik'):
        return None, 'selected_redux_merchant_mismatch'
    variant = _selected_variant(product)
    if variant is None:
        return None, 'selected_redux_variant_mismatch'

    prices = product.get('prices')
    if not isinstance(prices, list) or len(prices) != 1 or not isinstance(prices[0], dict):
        return None, 'selected_redux_price_missing'
    price = _amount(prices[0].get('value'))
    if (price is None or _amount(variant['price']) != price
            or prices[0].get('formattedPrice') !=
               f'{price:,.2f}'.replace(',', '_').replace('.', ',').replace('_', '.')):
        return None, 'selected_redux_price_mismatch'

    schema_products = [node for node in _nodes(soup)
                       if node.get('@type') == 'Product' and node.get('sku') == SKU]
    if len(schema_products) != 1 or schema_products[0].get('color') != COLOR:
        return None, 'schema_selected_variant_mismatch'
    schema_offer = schema_products[0].get('offers')
    if (not isinstance(schema_offer, dict) or schema_offer.get('@type') != 'Offer'
            or schema_offer.get('url') != SCHEMA_URL
            or schema_offer.get('priceCurrency') != 'TRY'
            or _amount(schema_offer.get('price')) != price
            or not str(schema_offer.get('availability', '')).rstrip('/').endswith('/InStock')
            or not str(schema_offer.get('itemCondition', '')).rstrip('/').endswith('/NewCondition')):
        return None, 'schema_cash_new_stock_mismatch'
    # JSON-LD seller and gtin are intentionally not read: both are wrong on
    # this reviewed PDP. The visible/Redux/event seller and variant GTIN rule.

    sellers = [link for link in soup.select('a[href]')
               if link.get('href') == SELLER_URL and link.get('title') == SELLER
               and _normalized(link.get_text(' ', strip=True)) == SELLER
               and link.parent is not None
               and re.search(r'\bSatıcı\s*:', _normalized(link.parent.get_text(' ', strip=True)))]
    if len(sellers) != 1:
        return None, 'selected_visible_seller_mismatch'
    controls = (('Kapasite', '256 GB'), ('Renk', COLOR))
    for kind, value in controls:
        buttons = soup.select(f'button[data-test-id="variant-box-{kind}-{value}"]')
        if len(buttons) != 1 or buttons[0].get('aria-label') != f'Seçili {kind} {value}':
            return None, 'selected_controls_mismatch'
    technical = soup.select('#techSpecs[data-test-id="KeyFeaturesTable"]')
    detail = _normalized(technical[0].get_text(' ', strip=True)) if len(technical) == 1 else ''
    if (not re.search(r'\bDahili Hafıza\s+256 GB\b', detail)
            or not re.search(r'\bRAM Kapasitesi\s+8 GB RAM\b', detail)
            or not re.search(r'\bMobil Bağlantı Hızı\s+5G\b', detail)
            or not re.search(r'\bStok Kodu\s+' + SKU + r'\b', detail)):
        return None, 'technical_variant_mismatch'

    price_regions = soup.select('[data-test-id="default-price"]')
    if len(price_regions) != 1:
        return None, 'selected_price_region_missing'
    visible = price_regions[0].find('span')
    match = re.fullmatch(r'(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*TL',
                         _normalized(visible.get_text(' ', strip=True)) if visible else '', re.I)
    visible_price = _amount(match[1].replace('.', '').replace(',', '.')) if match else None
    if visible_price != price:
        return None, 'visible_cash_price_mismatch'
    if _is_conditional_price(soup, schema_offer, float(price)):
        return None, 'conditional_price'

    events = {}
    for node in soup.select('[data-hbus]'):
        try:
            payload = json.loads(node['data-hbus'])
        except (TypeError, ValueError):
            continue
        event = payload.get('data') if isinstance(payload, dict) else None
        if (isinstance(event, dict) and event.get('placement') == 'buybox'
                and event.get('component') in {'variant', 'Key Features'}):
            events.setdefault(event['component'], []).append(event)
    for component in ('variant', 'Key Features'):
        found = events.get(component, [])
        if len(found) != 1:
            return None, 'selected_buy_box_event_missing'
        event = found[0]
        if (event.get('page_type') != 'pdp'
                or event.get('page_value') != SKU
                or event.get('product_id') != PRODUCT_ID
                or event.get('listing_id') != LISTING_ID
                or event.get('merchant_id') != MERCHANT_ID
                or event.get('merchant_name') != SELLER
                or _amount(event.get('price')) != price):
            return None, 'selected_buy_box_event_mismatch'
    carts = soup.select('button[data-test-id="addToCart"]')
    if (len(carts) != 1 or carts[0].has_attr('disabled')
            or carts[0].get('aria-disabled') == 'true'
            or _normalized(carts[0].get_text(' ', strip=True)).casefold() != 'sepete ekle'):
        return None, 'selected_cart_unavailable'

    offer = {'merchant': SELLER, 'merchant_id': MERCHANT_ID,
             'listing_id': LISTING_ID, 'product_id': PRODUCT_ID, 'sku': SKU,
             'gtin13': GTIN13, 'price': float(price), 'currency': 'TRY',
             'availability': 'in_stock', 'condition': 'new',
             'source_title': title, 'url': URL,
             'observed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}
    if not matcher.has_observed_offer_evidence(CATALOG['name'], offer, SPECS):
        return None, 'catalog_variant_mismatch'
    return offer, 'selected_new_cash_offer_observed'


class HepsiburadaIphone16PmAuditSource:
    """Robots preflight followed by one bounded, exact public PDP read."""

    def __init__(self, get=None, sleep=time.sleep):
        self._get = get
        self._sleep = sleep

    def read_offer(self):
        # Use only direct public GETs. This audit does not call the local
        # FlareSolverr POST endpoint or a production ingestion endpoint.
        reader = HepsiburadaS25FeSource(get=self._get, sleep=self._sleep)
        try:
            robots, reason, status = reader._read(
                ROBOTS_URL, MAX_ROBOTS_BYTES, accept='text/plain')
            if reason != 'ready':
                return None, 'robots_' + reason, status
            text = reader._robots_text(robots)
            if not re.search(r'^\s*user-agent\s*:', text, re.I | re.M) or '<html' in text.lower():
                return None, 'robots_invalid', status
            rules = RobotFileParser()
            rules.parse(text.splitlines())
            if not rules.can_fetch(BOT, URL):
                return None, 'robots_disallowed', None
            delay = rules.crawl_delay(BOT) or 0
            if delay > 30:
                return None, 'robots_delay_exceeds_budget', None
            if delay:
                self._sleep(delay)
            body, reason, status = reader._read(
                URL, MAX_HTML_BYTES, accept='text/html')
            if reason != 'ready':
                return None, reason, status
            offer, reason = parse_exact_buy_box(body.decode('utf-8-sig', errors='strict'))
            return offer, reason, status
        except (requests.RequestException, ValueError, TypeError, UnicodeError):
            return None, 'transport_error', None


def run_audit(source=None):
    offer, reason, status = (source or HepsiburadaIphone16PmAuditSource()).read_offer()
    return {'purpose': 'read_only_exact_iphone16pm_hepsiburada_marketplace_audit',
            'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
            'catalog': CATALOG, 'url': URL, 'seller_url': SELLER_URL,
            'http_status': status, 'verified': offer is not None, 'reason': reason,
            'offer': offer, 'writes': False}


if __name__ == '__main__':
    report = run_audit()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0 if report['verified'] else 1)
