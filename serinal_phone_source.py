"""Verify a Serinal Samsung phone and its selected first-party offer."""

from datetime import datetime, timezone
import re

from bs4 import BeautifulSoup

from ayaz_phone_source import only, value
from retailer_barcode import retailer_gtin13
from retailer_offer_refresh import _schema_nodes
from teknosa_reviewed_source import _amount, _cash_price
from tr_price_scraper import TRPriceScraper


SERINAL_HOST = 'www.serinal.com.tr'
SERINAL_BASE = 'https://' + SERINAL_HOST
SERINAL_CATEGORY = '/samsung-galaxy-akilli-telefonlar'
SERINAL_TERMS = SERINAL_BASE + '/mesafeli-satis-sozlesmesi'
SERINAL_PATH = re.compile(r'/samsung-galaxy-((?:[asfm][0-9]{1,3}|z-(?:fold|flip)[0-9]{1,2})'
                          r'(?:-[a-z0-9]+)+(?:/[0-9]{2,4}gb(?:-[a-z0-9]+)+)?)')
COMPANY = 'serinal iletisim hiz ve iklimlendirme sist san tic ltd sti'
SCHEMA_COMPANY = 'serinal ilesim hiz ve iklimlendirme sist san tic ldt sti'


def serinal_phone_url(url):
    return (isinstance(url, str) and url.startswith(SERINAL_BASE) and len(url) <= 270
            and SERINAL_PATH.fullmatch(url.removeprefix(SERINAL_BASE)) is not None)


def serinal_offer(html, legal_html, *, url, name, specs):
    if not serinal_phone_url(url):
        return None
    normalize = TRPriceScraper._normalize_words
    legal = BeautifulSoup(legal_html, 'html.parser')
    # Parent wrappers also contain the terms. Use the seller line itself once.
    companies = [normalize(match[1]) for line in legal.stripped_strings
                 if (match := re.fullmatch(r'AD- SOYAD:\s*(.+)', line, re.I))]
    if value(legal, 'link[rel=canonical]') != SERINAL_TERMS or companies != [COMPANY]:
        return None

    soup = BeautifulSoup(html, 'html.parser')
    root = only(soup, '.product-profile-1[data-product-id]')
    if not root or value(soup, 'link[rel=canonical]') != url:
        return None
    if value(root, f'a[href="{SERINAL_BASE}{SERINAL_CATEGORY}"]') != SERINAL_BASE + SERINAL_CATEGORY:
        return None
    products = [row for row in _schema_nodes(soup, url) if row.get('@type') == 'Product']
    if len(products) != 1:
        return None
    product = products[0]
    title = value(root, 'h1.title')
    brand = product.get('brand')
    offer = product.get('offers')
    if (title != product.get('name') or not title or not isinstance(brand, dict) or brand.get('name') != 'Samsung'
            or not isinstance(offer, dict) or not TRPriceScraper().has_observed_offer_evidence(
                name, {'source_title': title, 'url': url}, specs)):
        return None

    sku = product.get('sku')
    barcode = retailer_gtin13(product.get('gtin'))
    identifiers = {}
    for row in root.select('.product-profile-info > li:has(> .value)'):
        label = row.get_text(' ', strip=True).split(':', 1)[0]
        if label in identifiers:
            return None
        identifiers[label] = value(row, ':scope > .value')
    cart = only(root, 'button.btn-cart[onclick]')
    seller = offer.get('seller')
    price_box = only(root, '.product-price-group')
    if not price_box or not cart or not re.fullmatch(r'[1-9]\d{0,11}', root['data-product-id']):
        return None
    price = _amount(offer.get('price'))
    if (not isinstance(sku, str) or not re.fullmatch(r'SM-[A-Z0-9]{6,20}', sku) or sku != product.get('mpn')
            or not barcode or any(key in product and retailer_gtin13(product[key]) != barcode
                                  for key in ('gtin12', 'gtin13', 'gtin14'))
            or identifiers != {'Ürün Kodu': sku, 'Barkod': barcode}
            or offer.get('@type') != 'Offer' or offer.get('url') != url or offer.get('priceCurrency') != 'TRY'
            or offer.get('availability') != 'https://schema.org/InStock'
            or offer.get('itemCondition') != 'https://schema.org/NewCondition'
            or not isinstance(seller, dict) or normalize(seller.get('name')) not in (COMPANY, SCHEMA_COMPANY)
            or cart.get_text(' ', strip=True) != 'Sepete Ekle' or cart.has_attr('disabled')
            or cart.get('aria-disabled') == 'true' or 'disabled' in cart.get('class', [])
            or not re.match(r"^\s*addCart\(\s*" + root['data-product-id'] + r"\s*,\s*'product'\s*,", cart['onclick'])
            or price is None or _cash_price(value(price_box, '.sale-variant-price')) != price
            or re.search(r'\b(?:sepette|kuponla|uye fiyati|uyelere ozel|sepete ozel)\b',
                         normalize(price_box.get_text(' ', strip=True)))):
        return None

    return {'platform': 'Serinal', 'merchant': 'Serinal', '_legal_name': COMPANY,
            'price': float(price), 'currency': 'TRY', 'availability': 'in_stock', 'gtin13': barcode,
            'source_title': title, 'url': url, 'observed_at': datetime.now(timezone.utc).isoformat()}
