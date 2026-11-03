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

# The legacy audit retains its exact identity. Catalog readers supply a fresh,
# URL-bound selection and reuse the same cross-layer proof instead of a SKU list.
DEFAULT_REVIEWED_TARGET = {
    'CATALOG': CATALOG,
    'COLOR': COLOR,
    'GTIN13': GTIN13,
    'LISTING_ID': LISTING_ID,
    'MERCHANT_ID': MERCHANT_ID,
    'MERCHANT_POSTFIX': 'bsz-elektronik',
    'PRODUCT_ID': PRODUCT_ID,
    'SCHEMA_URL': SCHEMA_URL,
    'SELLER': SELLER,
    'SELLER_URL': SELLER_URL,
    'SKU': SKU,
    'SPECS': SPECS,
    'URL': URL,
}


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


def _selected_variant(product, scope=None):
    scope = DEFAULT_REVIEWED_TARGET if scope is None else scope
    variants = product.get('variants')
    if not isinstance(variants, list):
        return None
    if sum(isinstance(item, dict) and item.get('isDefault') is True
           for item in variants) != 1:
        return None
    selected = [variant for variant in variants if isinstance(variant, dict)
                and variant.get('sku') == scope['SKU'] and variant.get('isDefault') is True]
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
    # Synthetic barcodes may coexist; the selected variant must have exactly
    # one numeric EAN13 and it must match the caller's checksum-validated proof.
    gtins = [barcode for barcode in barcodes if isinstance(barcode, str)
             and re.fullmatch(r'\d{13}', barcode)] if isinstance(barcodes, list) else []
    if (values != {'Renk': scope['COLOR'], 'Kapasite': str(scope['SPECS']['storage_gb']) + ' GB'}
            or gtins != [scope['GTIN13']]
            or not _valid_gtin13(scope['GTIN13'])
            or variant.get('isInStock') is not True
            or variant.get('isPreOrder') is not False
            or _amount(variant.get('price')) is None
            or not TRPriceScraper().is_strict_match(scope['CATALOG']['name'],
                                                   str(variant.get('name') or ''), scope['SPECS'])
            or re.search(r'yenilenmi[sş]|refurbished|ikinci\s*el',
                         str(variant.get('name') or ''), re.I)):
        return None
    return variant


def parse_exact_buy_box(html: str, final_url: str = URL, *, scope=None):
    """Reconcile all selected buy-box layers; default to the reviewed audit SKU."""
    scope = DEFAULT_REVIEWED_TARGET if scope is None else scope
    if (final_url != scope['URL'] or not isinstance(html, str)
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
    if (not matcher.is_strict_match(scope['CATALOG']['name'], title, scope['SPECS'])
            or scope['COLOR'].casefold() not in title.casefold()
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
    generic = scope.get('generic_catalog') is True
    if (product.get('productId') != scope['PRODUCT_ID'] or product.get('sku') != scope['SKU']
            or product.get('listingId') != scope['LISTING_ID']
            or product.get('merchantName') not in ((None, scope['SELLER']) if generic else (scope['SELLER'],))
            or product.get('merchantId') not in ((None, scope['MERCHANT_ID']) if generic else (scope['MERCHANT_ID'],))
            or (product.get('isInStock') is not True and not (generic and product.get('isInStock') is None))
            or (not (generic and stock is None) and (not isinstance(stock, dict) or
                stock.get('isInStock') is not True and not (generic and stock.get('isInStock') is None)))
            or (product.get('isProductLive') is not True and not (generic and product.get('isProductLive') is None))
            or (product.get('isClosedProduct') is not False and not (generic and product.get('isClosedProduct') is None))
            or (product.get('isPreOrder') is not False and not (generic and product.get('isPreOrder') is None))):
        return None, 'selected_redux_offer_mismatch'
    merchant = product.get('merchant')
    if (not isinstance(merchant, dict) or merchant.get('id') != scope['MERCHANT_ID']
            or merchant.get('name') != scope['SELLER']
            or merchant.get('urlPostfix') != scope['MERCHANT_POSTFIX']):
        return None, 'selected_redux_merchant_mismatch'
    variant = _selected_variant(product, scope)
    if variant is None:
        return None, 'selected_redux_variant_mismatch'

    prices = product.get('prices')
    if generic:
        # The installment/loyalty price container is not the selected cash
        # offer. Its layout differs across categories. The SKU's own price
        # must still agree with JSON-LD, visible cash and both buy-box events.
        price = _amount(variant['price'])
    else:
        if not isinstance(prices, list) or len(prices) != 1 or not isinstance(prices[0], dict):
            return None, 'selected_redux_price_missing'
        price = _amount(prices[0].get('value'))
        if (price is None or _amount(variant['price']) != price
                or prices[0].get('formattedPrice') !=
                   f'{price:,.2f}'.replace(',', '_').replace('.', ',').replace('_', '.')):
            return None, 'selected_redux_price_mismatch'

    schema_products = [node for node in _nodes(soup)
                       if node.get('@type') == 'Product' and node.get('sku') == scope['SKU']]
    if len(schema_products) != 1 or schema_products[0].get('color') != scope['COLOR']:
        return None, 'schema_selected_variant_mismatch'
    schema_offer = schema_products[0].get('offers')
    if (not isinstance(schema_offer, dict) or schema_offer.get('@type') != 'Offer'
            or schema_offer.get('url') != scope['SCHEMA_URL']
            or schema_offer.get('priceCurrency') != 'TRY'
            or _amount(schema_offer.get('price')) != price
            or not str(schema_offer.get('availability', '')).rstrip('/').endswith('/InStock')
            or not str(schema_offer.get('itemCondition', '')).rstrip('/').endswith('/NewCondition')):
        return None, 'schema_cash_new_stock_mismatch'
    # Some PDPs label the manufacturer as seller and expose a synthetic GTIN.
    # The visible/Redux/event seller and selected variant barcode identify it.

    sellers = [link for link in soup.select('a[href]')
               if link.get('href') == scope['SELLER_URL'] and link.get('title') == scope['SELLER']
               and _normalized(link.get_text(' ', strip=True)) == scope['SELLER']
               and link.parent is not None
               and re.search(r'\bSatıcı\s*:', _normalized(link.parent.get_text(' ', strip=True)))]
    if len(sellers) != 1:
        return None, 'selected_visible_seller_mismatch'
    controls = (('Kapasite', str(scope['SPECS']['storage_gb']) + ' GB'), ('Renk', scope['COLOR']))
    for kind, value in controls:
        buttons = soup.select(f'button[data-test-id="variant-box-{kind}-{value}"]')
        if len(buttons) != 1 or buttons[0].get('aria-label') != f'Seçili {kind} {value}':
            return None, 'selected_controls_mismatch'
    technical = soup.select('#techSpecs[data-test-id="KeyFeaturesTable"]')
    detail = _normalized(technical[0].get_text(' ', strip=True)) if len(technical) == 1 else ''
    if (not re.search(rf"\bDahili Hafıza\s+{scope['SPECS']['storage_gb']} GB\b", detail)
            or not re.search(rf"\bRAM Kapasitesi\s+{scope['SPECS']['ram_gb']} GB(?: RAM)?\b", detail)
            or not re.search((r'\bMobil Bağlantı Hızı\s+5G\b' if scope['SPECS']['network_generation'] == '5g' else r'\bMobil Bağlantı Hızı\s+4(?:[.,]5)?G\b'), detail)
            or not re.search(r'\bStok Kodu\s+' + scope['SKU'] + r'\b', detail)):
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
                or event.get('page_value') != scope['SKU']
                or event.get('product_id') != scope['PRODUCT_ID']
                or event.get('listing_id') != scope['LISTING_ID']
                or event.get('merchant_id') != scope['MERCHANT_ID']
                or event.get('merchant_name') != scope['SELLER']
                or _amount(event.get('price')) != price):
            return None, 'selected_buy_box_event_mismatch'
    carts = soup.select('button[data-test-id="addToCart"]')
    if (len(carts) != 1 or carts[0].has_attr('disabled')
            or carts[0].get('aria-disabled') == 'true'
            or _normalized(carts[0].get_text(' ', strip=True)).casefold() != 'sepete ekle'):
        return None, 'selected_cart_unavailable'

    offer = {'merchant': scope['SELLER'], 'merchant_id': scope['MERCHANT_ID'],
             'listing_id': scope['LISTING_ID'], 'product_id': scope['PRODUCT_ID'], 'sku': scope['SKU'],
             'gtin13': scope['GTIN13'], 'price': float(price), 'currency': 'TRY',
             'availability': 'in_stock', 'condition': 'new',
             'source_title': title, 'url': scope['URL'],
             'observed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}
    from mediamarkt_marketplace_audit import catalog_variant_matches
    matches = (catalog_variant_matches({'source_title': title, **scope['SPECS']},
                    scope['CATALOG']['name'], scope['SPECS']) if scope.get('generic_catalog') is True else
               matcher.has_observed_offer_evidence(scope['CATALOG']['name'], offer, scope['SPECS']))
    if not matches:
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
