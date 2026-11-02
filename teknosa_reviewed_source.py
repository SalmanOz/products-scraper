"""Read only a reviewed, exact first-party Teknosa buy box.

Teknosa is a marketplace: the canonical PDP may select another seller even
when the page URL and product title are unchanged. Every observation therefore
requires the visible seller, selected cart, Product schema and cash price to
agree on Teknosa and the same SKU. No offer is persisted by this module.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
import requests

from hepsiburada_s25_fe_source import HepsiburadaS25FeSource
from tr_price_scraper import TRPriceScraper


BOT = 'TeknoskorPriceBot'
ROBOTS_URL = 'https://www.teknosa.com/robots.txt'
URL = 'https://www.teknosa.com/apple-iphone-17e-256gb-beyaz-akilli-telefon-p-100000061663'
PRODUCT_ID = '100000061663'
EAN = '195951030753'
NAME = 'Apple iPhone 17e'
SPECS = {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'}
MAX_ROBOTS_BYTES = 64 * 1024
MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_PREFLIGHT_AGE_SECONDS = 10 * 60


def _amount(value):
    try:
        price = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return price if price.is_finite() and 0 < price <= 5_000_000 else None


def _cash_price(text):
    match = re.fullmatch(r'(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*TL',
                         ' '.join(text.split()), re.I)
    return _amount(match[1].replace('.', '').replace(',', '.')) if match else None


def parse_exact_first_party_offer(html: str, final_url: str = URL):
    """Return an offer only when the selected first-party buy box is provable."""
    if (final_url != URL or not isinstance(html, str)
            or len(html.encode('utf-8')) > MAX_HTML_BYTES):
        return None, 'wrong_or_oversized_pdp'
    soup = BeautifulSoup(html, 'html.parser')
    if re.search(r'captcha|just a moment|access denied|you have been blocked',
                 soup.title.get_text(' ', strip=True) if soup.title else '', re.I):
        return None, 'challenge_page'
    headings = soup.select('h1.pdp-title')
    if len(headings) != 1:
        return None, 'selected_title_missing'
    title = headings[0].get_text(' ', strip=True)
    matcher = TRPriceScraper()
    if (not matcher.is_strict_match(NAME, title, SPECS)
            or not re.search(r'\bBeyaz\b', title, re.I)
            or re.search(r'outlet|teşhir|teshir|yenilen|ikinci el', title, re.I)):
        return None, 'selected_variant_mismatch'

    pages = []
    products = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            root = json.loads(script.get_text())
        except (TypeError, ValueError):
            continue
        for node in (root if isinstance(root, list) else [root]):
            if not isinstance(node, dict):
                continue
            if node.get('@type') == 'WebPage':
                pages.append(node)
            graph = node.get('@graph')
            if isinstance(graph, dict):
                graph = [graph]
            if isinstance(graph, list):
                products.extend(item for item in graph if isinstance(item, dict)
                                and item.get('@type') == 'Product')
    products = [node for node in products if node.get('sku') == PRODUCT_ID]
    if (len(pages) != 1 or pages[0].get('url') != URL
            or len(products) != 1):
        return None, 'schema_identity_missing'
    product = products[0]
    if (product.get('name') != title
            or not matcher.is_strict_match(NAME, str(product.get('name') or ''), SPECS)):
        return None, 'schema_variant_mismatch'
    offer = product.get('offers')
    seller = offer.get('seller') if isinstance(offer, dict) else None
    if (not isinstance(offer, dict) or offer.get('@type') != 'Offer'
            or offer.get('url') != URL or offer.get('priceCurrency') != 'TRY'
            or not str(offer.get('availability', '')).rstrip('/').endswith('/InStock')
            or not str(offer.get('itemCondition', '')).rstrip('/').endswith('/NewCondition')
            or not isinstance(seller, dict) or seller.get('name') != 'TEKNOSA'):
        return None, 'schema_first_party_offer_missing'
    price = _amount(offer.get('price'))
    if price is None:
        return None, 'schema_price_invalid'

    visible_sellers = soup.select('.pdp-seller-info')
    if (len(visible_sellers) != 1
            or ' '.join(visible_sellers[0].get_text(' ', strip=True).split()) != 'Satıcı: TEKNOSA'):
        return None, 'selected_visible_seller_mismatch'
    visible_prices = soup.select('.pdp-prices .pdp-prc2 .prc')
    if not 1 <= len(visible_prices) <= 2 or any(
            _cash_price(node.get_text(' ', strip=True)) != price
            for node in visible_prices):
        return None, 'visible_cash_price_mismatch'
    from retailer_offer_refresh import _is_conditional_price
    if _is_conditional_price(soup, offer, float(price)):
        return None, 'conditional_price'
    carts = soup.select('#addToCartButton')
    if len(carts) != 1:
        return None, 'selected_cart_missing'
    cart = carts[0]
    if (cart.has_attr('disabled') or cart.get('aria-disabled') == 'true'
            or cart.get_text(' ', strip=True) != 'Sepete Ekle'
            or cart.get('data-product-id') != PRODUCT_ID
            or cart.get('data-product-ean') != EAN
            or cart.get('data-product-name') != title
            or cart.get('data-product-url') != '/' + URL.split('/', 3)[3]
            or cart.get('data-shop-name') != 'TEKNOSA'
            or cart.get('data-product-shop-id') != '1001'
            or cart.get('data-product-stock') != 'Y'
            or cart.get('data-product-stock-status') != 'Satışta'
            or _amount(cart.get('data-product-price')) != price
            or _amount(cart.get('data-product-discounted-price')) != price):
        return None, 'selected_cart_offer_mismatch'
    result = {'merchant': 'Teknosa', 'price': float(price), 'currency': 'TRY',
              'availability': 'in_stock', 'source_title': title, 'url': URL,
              'observed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}
    if not matcher.has_observed_offer_evidence(NAME, result, SPECS):
        return None, 'catalog_variant_mismatch'
    return result, 'observed_exact_first_party_cash_offer'


class TeknosaReviewedSource:
    """A two-browser-read budget: robots, then only the exact reviewed PDP."""

    def __init__(self, get=None, post=None, sleep=time.sleep,
                 audit_file=None, now=None):
        self._get = get or requests.get
        self._post = post or requests.post
        self._sleep = sleep
        self._audit_file = (audit_file if audit_file is not None else
                            os.environ.get('TEKNOSA_VERIFIED_AUDIT_FILE'))
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _preflight_offer(self):
        """Reuse only a fresh, exact observation from this runner's preflight."""
        try:
            report = json.loads(Path(self._audit_file).read_text())
            if (not isinstance(report, dict)
                    or report.get('purpose') != 'read_only_exact_first_party_teknosa_offer'
                    or report.get('verified') is not True
                    or report.get('writes') is not False
                    or report.get('reason') != 'observed_exact_first_party_cash_offer'
                    or report.get('http_status') != 200
                    or report.get('catalog_id') != 248
                    or report.get('slug') != 'apple-iphone-17e'
                    or report.get('name') != NAME
                    or report.get('specs') != SPECS
                    or report.get('retailer_product_id') != PRODUCT_ID
                    or report.get('url') != URL):
                return None
            offer = report.get('offer')
            if (not isinstance(offer, dict)
                    or offer.get('merchant') != 'Teknosa'
                    or _amount(offer.get('price')) is None
                    or offer.get('currency') != 'TRY'
                    or offer.get('availability') != 'in_stock'
                    or offer.get('url') != URL):
                return None
            title = offer.get('source_title')
            if (not isinstance(title, str)
                    or not TRPriceScraper().is_strict_match(NAME, title, SPECS)
                    or not re.search(r'\bBeyaz\b', title, re.I)
                    or re.search(r'outlet|teşhir|teshir|yenilen|ikinci el', title, re.I)):
                return None
            checked = datetime.fromisoformat(report['checked_at'].replace('Z', '+00:00'))
            observed = datetime.fromisoformat(offer['observed_at'].replace('Z', '+00:00'))
            now = self._now()
            if (checked.tzinfo is None or observed.tzinfo is None or now.tzinfo is None
                    or not -60 <= (now - checked).total_seconds() <= MAX_PREFLIGHT_AGE_SECONDS
                    or not -60 <= (now - observed).total_seconds() <= MAX_PREFLIGHT_AGE_SECONDS
                    or not -60 <= (checked - observed).total_seconds() <= MAX_PREFLIGHT_AGE_SECONDS):
                return None
            return offer
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None

    def read_offer(self, url=URL):
        if url != URL:
            return None, 'unreviewed_pdp', None
        if self._audit_file:
            offer = self._preflight_offer()
            return (offer, 'observed_exact_first_party_cash_offer_from_preflight', 200) if offer else (
                None, 'preflight_invalid_or_stale', None)
        # Reuse the proven bounded GET transport; it never writes or follows
        # changed URLs. One instance keeps its two browser requests isolated.
        transport = HepsiburadaS25FeSource(
            get=self._get, post=self._post, sleep=self._sleep)
        try:
            robots, reason, status = transport._read_with_fallback(
                ROBOTS_URL, MAX_ROBOTS_BYTES, accept='text/plain')
            if reason != 'ready':
                return None, 'robots_' + reason, status
            policy = transport._robots_text(robots)
            if not re.search(r'^\s*user-agent\s*:', policy, re.I | re.M) or '<html' in policy.lower():
                return None, 'robots_invalid', status
            rules = RobotFileParser()
            rules.parse(policy.splitlines())
            if not rules.can_fetch(BOT, url):
                return None, 'robots_disallowed', status
            delay = float(rules.crawl_delay(BOT) or 0)
            rate = rules.request_rate(BOT)
            if rate:
                delay = max(delay, rate.seconds / max(1, rate.requests))
            if delay > 30:
                return None, 'robots_delay_exceeds_budget', status
            if delay:
                self._sleep(delay)
            body, reason, status = transport._read_with_fallback(
                url, MAX_HTML_BYTES, accept='text/html')
            if reason != 'ready':
                return None, reason, status
            result, reason = parse_exact_first_party_offer(
                body.decode('utf-8-sig', errors='strict'), url)
            return result, reason, status
        except (requests.RequestException, ValueError, UnicodeError):
            return None, 'transport_error', None
