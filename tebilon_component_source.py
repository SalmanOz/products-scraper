"""Bind Tebilon's published GTIN to the same selected component checkout."""

from datetime import datetime, timezone
import re

from bs4 import BeautifulSoup

from laptop_direct_source import DIRECT_STORES
from laptop_retailer_sources import WORDS
from retailer_barcode import retailer_gtin13
from retailer_offer_refresh import _is_conditional_price, _schema_nodes
from trendyol_basket_price import cash

TEBILON = 'www.tebilon.com'


def selected_tebilon_component(html, url, feed_row, kind):
    if (not feed_row or feed_row.get('source') != TEBILON or feed_row.get('url') != url
            or feed_row.get('category') != kind or not (gtin := retailer_gtin13(feed_row.get('feedGtin')))):
        return None, 'tebilon_feed_identity_unverified'
    platform, cash_selector, cart_selector, legal_path, _ = DIRECT_STORES[TEBILON]
    soup = BeautifulSoup(html, 'html.parser')
    canonicals = [node.get('href') for node in soup.select('link[rel="canonical"]')]
    headings = soup.select('.productDetail__productName h1')
    codes = soup.select('.productDetail__productName h2')
    products = [node for node in _schema_nodes(soup, url) if node.get('@type') == 'Product']
    if canonicals != [url] or len(headings) != 1 or len(codes) != 1 or len(products) != 1:
        return None, 'tebilon_selected_product_unverified'
    title = headings[0].get_text(' ', strip=True)
    code = codes[0].get_text(' ', strip=True)
    product = products[0]
    brand = product.get('brand')
    brand = brand.get('name') if isinstance(brand, dict) else None
    if (not brand or not title or not code or WORDS(code) != WORDS(feed_row.get('feedMpn'))
            or WORDS(title) != WORDS(feed_row.get('title')) or WORDS(title) != WORDS(product.get('name'))
            or re.search(r'\b(?:outlet|yenilenmis|teshir|ikinci el|refurbished)\b', WORDS(title))):
        return None, 'tebilon_selected_model_unverified'
    if any(retailer_gtin13(product[key]) != gtin for key in ('gtin', 'gtin12', 'gtin13', 'gtin14') if key in product):
        return None, 'tebilon_selected_barcode_conflict'
    offer = product.get('offers')
    if (not isinstance(offer, dict) or offer.get('@type') != 'Offer' or offer.get('url') != url
            or offer.get('priceCurrency') != 'TRY'
            or offer.get('itemCondition') not in (None, 'https://schema.org/NewCondition', 'http://schema.org/NewCondition')
            or offer.get('availability') not in (None, 'https://schema.org/InStock', 'http://schema.org/InStock')):
        return None, 'tebilon_selected_offer_unverified'
    price = cash(str(offer.get('price')) + ' TL')
    prices = soup.select(cash_selector)
    carts = soup.select(cart_selector)
    if not price or len(prices) != 1 or cash(prices[0].get_text(' ', strip=True)) != price:
        return None, 'tebilon_visible_cash_mismatch'
    if _is_conditional_price(prices[0].find_parent(class_='productDetail__topPrice'), offer, float(price)):
        return None, 'tebilon_conditional_price'
    if (len(carts) != 1 or not re.fullmatch(r'[1-9]\d{0,12}', carts[0]['data-id'])
            or carts[0].has_attr('disabled') or carts[0].has_attr('hidden')
            or carts[0].get('aria-disabled') == 'true' or 'disabled' in carts[0].get('class', [])
            or WORDS(carts[0].get_text(' ', strip=True)) != 'sepete ekle'):
        return None, 'tebilon_selected_stock_unverified'
    return {'gtin13': gtin, 'brand': brand, 'source_title': title, 'price': float(price), 'platform': platform,
            'merchant': platform, 'merchant_id': TEBILON, 'seller_url': f'https://{TEBILON}{legal_path}',
            'observed_at': datetime.now(timezone.utc).isoformat()}, 'verified'
