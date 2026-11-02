"""Strict, reviewed Hepsiburada first-party Apple PDP reader.

The three exact URLs below were observed with a selected Hepsiburada buy box
on 2026-11-02. JSON-LD names Apple as the Offer seller even when the visible
buy box is Hepsiburada, so neither the schema nor a query-string seller hint
is sufficient by itself. Every read rechecks the selected SKU, merchant,
ordinary cash price, stock, and robots policy. No offer is retained here.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from threading import Lock
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
import requests

from hepsiburada_s25_fe_source import (
    BOT, MAX_HTML_BYTES, MAX_ROBOTS_BYTES, ROBOTS_URL, SELLER_URL,
    HepsiburadaS25FeSource, _nodes, _normalized,
)
from tr_price_scraper import TRPriceScraper


@dataclass(frozen=True)
class ReviewedApplePdp:
    catalog_id: int
    slug: str
    name: str
    ram_gb: int
    url: str
    product_id: str
    sku: str
    color: str

    @property
    def schema_url(self):
        return self.url.split('?', 1)[0]

    @property
    def specs(self):
        return {'ram_gb': self.ram_gb, 'storage_gb': 256,
                'network_generation': '5g'}


REVIEWED_PDPS = (
    ReviewedApplePdp(
        248, 'apple-iphone-17e', 'Apple iPhone 17e', 8,
        'https://www.hepsiburada.com/apple-iphone-17e-256-gb-beyaz-p-'
        'HBCV0000D58K87?magaza=Hepsiburada&',
        'HBC0000D58AG4', 'HBCV0000D58K87', 'Beyaz'),
    ReviewedApplePdp(
        296, 'apple-iphone-18-pro', 'Apple iPhone 18 Pro', 12,
        'https://www.hepsiburada.com/apple-iphone-18-pro-256-gb-siyah-p-'
        'HBCV0000IOFEJU',
        'HBC0000IOFEJI', 'HBCV0000IOFEJU', 'Siyah'),
    ReviewedApplePdp(
        297, 'apple-iphone-18-pro-max', 'Apple iPhone 18 Pro Max', 12,
        'https://www.hepsiburada.com/apple-iphone-18-pro-max-256-gb-siyah-p-'
        'HBCV0000IOFEKB',
        'HBC0000IOFEJZ', 'HBCV0000IOFEKB', 'Siyah'),
)
BY_URL = {target.url: target for target in REVIEWED_PDPS}


def _amount(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() and 0 < result <= 5_000_000 else None


def parse_exact_buy_box(html: str, url: str, final_url: str | None = None):
    """Return (offer, reason) only for a freshly selected reviewed Apple SKU."""
    target = BY_URL.get(url)
    if (target is None or (final_url or url) != url or not isinstance(html, str)
            or len(html.encode('utf-8')) > MAX_HTML_BYTES):
        return None, 'unreviewed_or_oversized_pdp'
    soup = BeautifulSoup(html, 'html.parser')
    if re.search(r'captcha|just a moment|access denied|you have been blocked',
                 _normalized(soup.title.get_text() if soup.title else ''), re.I):
        return None, 'challenge_page'
    headings = soup.select('h1[data-test-id="title"]')
    if len(headings) != 1:
        return None, 'selected_title_missing'
    title = _normalized(headings[0].get_text(' ', strip=True))
    matcher = TRPriceScraper()
    if (not matcher.is_strict_match(target.name, title, target.specs)
            or not re.search(r'\b' + re.escape(target.color) + r'\b', title, re.I)):
        return None, 'selected_variant_mismatch'

    nodes = list(_nodes(soup))
    pages = [node for node in nodes if node.get('@type') == 'WebPage'
             and node.get('url') == target.schema_url]
    products = [node for node in nodes if node.get('@type') == 'Product'
                and node.get('sku') == target.sku]
    if len(pages) != 1 or len(products) != 1:
        return None, 'schema_identity_missing'
    product = products[0]
    if (product.get('color') != target.color
            or not matcher.is_strict_match(target.name, str(product.get('name') or ''),
                                           target.specs)):
        return None, 'schema_selected_variant_mismatch'
    offer = product.get('offers')
    if (not isinstance(offer, dict) or offer.get('@type') != 'Offer'
            or offer.get('url') != target.schema_url
            or offer.get('priceCurrency') != 'TRY'
            or not str(offer.get('availability', '')).rstrip('/').endswith('/InStock')
            or not str(offer.get('itemCondition', '')).rstrip('/').endswith('/NewCondition')):
        return None, 'schema_exact_offer_missing'
    schema_seller = offer.get('seller')
    if (not isinstance(schema_seller, dict)
            or schema_seller.get('name') not in {'Apple', 'Hepsiburada'}):
        return None, 'unexpected_schema_seller'
    price = _amount(offer.get('price'))
    if price is None:
        return None, 'schema_price_invalid'

    sellers = [link for link in soup.select('a[href]')
               if link.get('href') == SELLER_URL
               and link.get('title') == 'Hepsiburada'
               and _normalized(link.get_text(' ', strip=True)) == 'Hepsiburada'
               and link.parent is not None
               and re.search(r'\bSatıcı\s*:', _normalized(link.parent.get_text(' ', strip=True)))]
    if len(sellers) != 1:
        return None, 'selected_visible_seller_mismatch'
    capacity = soup.select('button[data-test-id="variant-box-Kapasite-256 GB"]')
    color = soup.select(f'button[data-test-id="variant-box-Renk-{target.color}"]')
    if (len(capacity) != 1 or capacity[0].get('aria-label') != 'Seçili Kapasite 256 GB'
            or len(color) != 1 or color[0].get('aria-label') != f'Seçili Renk {target.color}'):
        return None, 'selected_controls_mismatch'
    # HB exposes this section as id="techSpecs"; its generated class name
    # changes. Keep the evidence anchored to the selected product table.
    technical = soup.select('#techSpecs[data-test-id="KeyFeaturesTable"]')
    detail = _normalized(technical[0].get_text(' ', strip=True)) if len(technical) == 1 else ''
    if (not re.search(r'\bDahili Hafıza\s+256 GB\b', detail)
            or not re.search(rf'\bRAM Kapasitesi\s+{target.ram_gb} GB RAM\b', detail)
            or not re.search(r'\bMobil Bağlantı Hızı\s+5G\b', detail)):
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
    from retailer_offer_refresh import _is_conditional_price
    if _is_conditional_price(soup, offer, float(price)):
        return None, 'conditional_price'

    events = {}
    for node in soup.select('[data-hbus]'):
        try:
            payload = json.loads(node['data-hbus'])
        except (TypeError, ValueError):
            continue
        data = payload.get('data') if isinstance(payload, dict) else None
        if isinstance(data, dict) and data.get('component') in {'variant', 'Key Features'}:
            events.setdefault(data['component'], []).append(data)
    for component in ('variant', 'Key Features'):
        candidates = events.get(component, [])
        if len(candidates) != 1:
            return None, 'selected_buy_box_merchant_mismatch'
        event = candidates[0]
        if (event.get('page_type') != 'pdp' or event.get('placement') != 'buybox'
                or event.get('merchant_name') != 'Hepsiburada'
                or event.get('merchant_id') != 'e750130c-d0ff-469f-8767-66a63af8ea0c'
                or event.get('product_id') != target.product_id
                or event.get('page_value') != target.sku
                or _amount(event.get('price')) != price):
            return None, 'selected_buy_box_merchant_mismatch'
    carts = soup.select('button[data-test-id="addToCart"]')
    if (len(carts) != 1 or carts[0].has_attr('disabled')
            or carts[0].get('aria-disabled') == 'true'
            or _normalized(carts[0].get_text(' ', strip=True)).casefold() != 'sepete ekle'):
        return None, 'selected_cart_unavailable'
    result = {'merchant': 'Hepsiburada', 'price': float(price), 'currency': 'TRY',
              'availability': 'in_stock', 'source_title': title, 'url': url,
              'observed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}
    if not matcher.has_observed_offer_evidence(target.name, result, target.specs):
        return None, 'catalog_variant_mismatch'
    return result, 'observed_exact_first_party_cash_offer'


class HepsiburadaAppleSource:
    """One robots check and one bounded exact PDP read per requested target."""

    def __init__(self, get=None, post=None, sleep=time.sleep):
        self._get = get or requests.get
        self._post = post or requests.post
        self._sleep = sleep
        self._lock = Lock()

    def read_offer(self, url):
        if url not in BY_URL:
            return None, 'unreviewed_apple_pdp', None
        # The daily refresher may check multiple Apple SKUs in parallel. A
        # fresh transport per SKU keeps its two-browser-read budget isolated.
        with self._lock:
            transport = HepsiburadaS25FeSource(
                get=self._get, post=self._post, sleep=self._sleep)
            try:
                robots, reason, status = transport._read_with_fallback(
                    ROBOTS_URL, MAX_ROBOTS_BYTES, accept='text/plain')
                if reason != 'ready':
                    return None, 'robots_' + reason, status
                text = transport._robots_text(robots)
                if not re.search(r'^\s*user-agent\s*:', text, re.I | re.M) or '<html' in text.lower():
                    return None, 'robots_invalid', status
                rules = RobotFileParser()
                rules.parse(text.splitlines())
                if not rules.can_fetch(BOT, url):
                    return None, 'robots_disallowed', None
                delay = rules.crawl_delay(BOT) or 0
                if delay > 30:
                    return None, 'robots_delay_exceeds_budget', None
                if delay:
                    self._sleep(delay)
                html, reason, status = transport._read_with_fallback(
                    url, MAX_HTML_BYTES, accept='text/html')
                if reason != 'ready':
                    return None, reason, status
                result, reason = parse_exact_buy_box(
                    html.decode('utf-8-sig', errors='strict'), url)
                return result, reason, status
            except (requests.RequestException, ValueError, UnicodeError):
                return None, 'transport_error', None
