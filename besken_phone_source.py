"""Read the selected new phone offer on Beşken Elektronik's own storefront."""

from datetime import datetime, timezone
import re

from bs4 import BeautifulSoup

from ayaz_phone_source import only, value
from retailer_barcode import retailer_gtin13
from retailer_offer_refresh import _schema_nodes
from teknosa_reviewed_source import _amount, _cash_price
from tr_price_scraper import TRPriceScraper


BESKEN_HOST = 'www.besken.com.tr'
BESKEN_BASE = 'https://' + BESKEN_HOST
BESKEN_CATEGORY = '/cep-telefonu'
BESKEN_TERMS = BESKEN_BASE + '/iptal-ve-iade-kosullari'
BESKEN_PATH = re.compile(r'/[a-z0-9-]{3,175}-cep-telefonu(?:-[a-z0-9]+)*-([1-9]\d{2,7})')
COMPANY = 'besken elektronik san ve dis tic ltd sti'
MERCHANT = 'Beşken Elektronik'


def besken_phone_url(url):
    return (isinstance(url, str) and url.startswith(BESKEN_BASE) and len(url) <= 270
            and BESKEN_PATH.fullmatch(url.removeprefix(BESKEN_BASE)) is not None)


def besken_offer(html, legal_html, *, url):
    if not besken_phone_url(url):
        return None
    normalize = TRPriceScraper._normalize_words
    terms = BeautifulSoup(legal_html, 'html.parser')
    sellers = [normalize(match[1]) for line in terms.stripped_strings
               if (match := re.fullmatch(r'ADI/UNVANI:\s*(.+)', line))]
    if value(terms, 'link[rel=canonical]') != BESKEN_TERMS or sellers != [COMPANY]:
        return None

    soup = BeautifulSoup(html, 'html.parser')
    detail = only(soup, 'div.productDetails')
    if not detail or value(soup, 'link[rel=canonical]') != url:
        return None
    products = [node for node in _schema_nodes(soup, url) if node.get('@type') == 'Product']
    if len(products) != 1:
        return None
    product = products[0]
    title = ' '.join(value(detail, 'h1.product-title').split())
    schema_title = product.get('name')
    offer = product.get('offers')
    barcode = retailer_gtin13(product.get('gtin13'))
    sku = product.get('sku')
    if (not title or not isinstance(schema_title, str) or title != ' '.join(schema_title.split())
            or not isinstance(offer, dict) or not isinstance(sku, str) or not sku or not barcode
            or any(key in product and retailer_gtin13(product[key]) != barcode
                   for key in ('gtin', 'gtin8', 'gtin12', 'gtin14'))):
        return None

    facts = {}
    for row in soup.select('#collapseProductInformation tr'):
        cells = row.find_all(['th', 'td'], recursive=False)
        if len(cells) != 2:
            continue
        label = cells[0].get_text(' ', strip=True)
        if label in facts:
            return None
        facts[label] = cells[1]
    required = {'Stok Kodu': sku, 'Barkod': barcode, 'EAN': barcode, 'Kullanım Durumu': 'Yeni'}
    category = facts.get('Kategoriler')
    if (any(label not in facts or facts[label].get_text(' ', strip=True) != expected
            for label, expected in required.items())
            or not category or not re.fullmatch(re.escape(BESKEN_BASE) + r'/(?:[a-z0-9-]+-)?cep-telefon(?:u|lar|lari)',
                                                value(category, 'a[href]'))):
        return None

    cart = only(detail, '.product-button-container a.product-cart-add.add-to-cart-btn[data-productid]')
    price_box = only(detail, '.product-price-sale')
    if (not cart or cart.get('role') != 'button' or cart.get('title') != 'Sepete Ekle'
            or ' '.join(cart.get('data-productname', '').split()) != title
            or not re.fullmatch(r'[A-Za-z0-9+/]{16,80}={0,2}', cart['data-productid'])
            or cart.has_attr('disabled') or cart.get('aria-disabled') == 'true'
            or 'disabled' in cart.get('class', []) or not price_box):
        return None
    price = _amount(offer.get('price'))
    if (offer.get('@type') != 'Offer' or offer.get('url') != url or offer.get('priceCurrency') != 'TRY'
            or offer.get('availability') != 'https://schema.org/InStock'
            or offer.get('itemCondition') != 'https://schema.org/NewCondition'
            or price is None or _cash_price(value(price_box, 'span.price')) != price
            or re.search(r'\b(?:sepette|kuponla|uye fiyati|uyelere ozel|sepete ozel)\b',
                         normalize(price_box.parent.get_text(' ', strip=True)))):
        return None
    return {'platform': MERCHANT, 'merchant': MERCHANT, '_legal_name': COMPANY,
            'price': float(price), 'currency': 'TRY', 'availability': 'in_stock', 'gtin13': barcode,
            'source_title': title, 'url': url, 'observed_at': datetime.now(timezone.utc).isoformat()}
