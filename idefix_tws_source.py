"""Verify İdefix earbuds and the seller of the selected price record."""

from datetime import datetime, timezone
import json
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from nethouse_offer_source import LEGAL_NAME as NETHOUSE_LEGAL_NAME
from retailer_barcode import retailer_gtin13
from retailer_offer_refresh import _schema_nodes
from teknosa_reviewed_source import _amount, _cash_price
from tr_price_scraper import TRPriceScraper
from tws_retailer_sources import selected_photo


HOST = 'www.idefix.com'
API_HOST = 'ecomapi.idefix.com'
BASE = 'https://' + HOST
PRODUCT_PATH = re.compile(r'/[a-z0-9-]{3,250}-p-([1-9]\d{4,11})')
SELLER_PATH = re.compile(r'/satici/[a-z0-9-]{1,100}-([1-9]\d{0,9})')
IMAGE_HOSTS = {'image02.idefix.com', 'image03.idefix.com'}
WORDS = TRPriceScraper._normalize_words
KNOWN_SELLERS = {
    # https://uyelik.dr.com.tr/uyelik-sozlesmesi
    (33, 'D&R', '/satici/dr-33'): 'turkuvaz muzik kitap magazacilik ve pazarlama anonim sirketi',
    # https://www.nethouse.com.tr/Gizlilik-Politikasi
    (347, 'Nethouse', '/satici/nethouse-347'): NETHOUSE_LEGAL_NAME,
}


def idefix_product_url(url):
    if not isinstance(url, str) or len(url) > 300 or not url.startswith(BASE + '/'):
        return False
    route = urlsplit(url)
    return (url == BASE + route.path + ('?' + route.query if route.query else '')
            and PRODUCT_PATH.fullmatch(route.path) is not None
            and (not route.query or re.fullmatch(r'vendorId=[1-9]\d{0,9}', route.query) is not None))


def page_state(soup):
    nodes = soup.select('script#__NEXT_DATA__[type="application/json"]')
    if len(nodes) != 1:
        return None, None
    try:
        data = json.loads(nodes[0].get_text())
        props = data['props']['pageProps']
        return (data, props) if isinstance(props, dict) else (None, None)
    except (KeyError, TypeError, ValueError):
        return None, None


def selected_idefix_offer(html, url, name, model_name, *, discovery=None):
    if not idefix_product_url(url):
        return None, 'invalid_idefix_destination'
    route = urlsplit(url)
    canonical = BASE + route.path
    product_id = PRODUCT_PATH.fullmatch(route.path)[1]
    vendor_id = int(route.query.split('=')[1]) if route.query else None
    soup = BeautifulSoup(html, 'html.parser')
    data, props = page_state(soup)
    if not data or data.get('page') != '/[slug]':
        return None, 'idefix_selected_document_missing'
    expected_query = {'slug': route.path[1:]}
    if vendor_id:
        expected_query['vendorId'] = str(vendor_id)
    product = props.get('productDetail')
    initial = props.get('initialState')
    state = initial.get('pdp') if isinstance(initial, dict) else None
    if (data.get('query') != expected_query or [node['href'] for node in soup.select('link[rel=canonical][href]')] != [canonical]
            or not isinstance(product, dict) or not isinstance(state, dict)
            or str(product.get('id')) != product_id or str(state.get('slugId')) != product_id
            or product.get('url') != canonical or product.get('slug') != url.removeprefix(BASE)
            or product.get('state') != 'published' or product.get('pageState') != 'available'):
        return None, 'idefix_selected_identity_mismatch'
    native = product.get('currentPrice')
    schema = [node for node in _schema_nodes(soup, canonical) if node.get('@type') == 'Product']
    if not isinstance(native, dict) or len(schema) != 1:
        return None, 'idefix_selected_offer_missing'
    gtin = retailer_gtin13(product.get('barcode'))
    schema_product = schema[0]
    brand = product.get('brandName')
    title = product.get('title')
    headings = soup.select('h1')
    if (not gtin or retailer_gtin13(schema_product.get('gtin13')) != gtin
            or retailer_gtin13(schema_product.get('sku')) != gtin
            or not isinstance(title, str) or not isinstance(brand, str)
            or WORDS(schema_product.get('name')) != WORDS(title) or len(headings) != 1
            or WORDS(headings[0].get_text(' ', strip=True)) not in (WORDS(title), WORDS(brand + ' ' + title))
            or model_name(title, brand) != name):
        return None, 'idefix_selected_variant_unverified'
    price_id = native.get('id')
    stock = native.get('inventoryQuantity')
    offer = schema_product.get('offers')
    cart = [button for button in soup.select('[aria-label="Add To Basket Section"] button:not([disabled])')
            if button.get_text(' ', strip=True) == 'Sepete Ekle']
    if (type(price_id) is not int or not 0 < price_id < 10**12 or state.get('priceId') != price_id
            or type(stock) is not int or stock <= 0 or len(cart) != 1
            or not isinstance(offer, dict) or offer.get('@type') != 'Offer' or offer.get('url') != canonical
            or offer.get('availability') != 'https://schema.org/InStock'
            or offer.get('itemCondition') != 'https://schema.org/NewCondition'):
        return None, 'idefix_selected_stock_unverified'
    if discovery is not None:
        discovery.update(url=url, gtin13=gtin, price_id=price_id)
    cash = _amount(native.get('price'))
    visible = soup.select('span.text-title-xl-semibold')
    if (cash is None or native.get('effectivePriceType') != 'price' or native.get('isZoneSale') is not False
            or native.get('minBasketQuantity') not in (None, 1) or offer.get('priceCurrency') != 'TRY'
            or _amount(native.get('effectivePrice')) != cash or _amount(offer.get('price')) != cash
            or len(visible) != 1 or _cash_price(visible[0].get_text(strip=True)) != cash):
        return None, 'idefix_selected_cash_unverified'
    images = schema_product.get('image')
    photos = images if isinstance(images, list) else [images]
    photo = next((valid for image in photos if (valid := selected_photo(image, IMAGE_HOSTS))), None)
    return {'platform': 'idefix', 'model': name, 'url': url, 'price': float(cash), 'currency': 'TRY',
            'availability': 'in_stock', 'condition': 'new', 'gtin13': gtin, 'source_title': title,
            'price_id': price_id, 'requested_vendor_id': vendor_id, '_image': photo,
            'observed_at': datetime.now(timezone.utc).isoformat()}, 'verified'


def idefix_vendor_document(price_id, document='vendor-info'):
    return f'https://{API_HOST}/api/product-price/{price_id}/detail/{document}'


def idefix_selected_seller(body, offer):
    try:
        vendor = json.loads(body)
        vendor_id, name, slug = (vendor[key] for key in ('vendorId', 'vendorName', 'vendorSlug'))
        match = SELLER_PATH.fullmatch(slug)
        if (type(vendor_id) is not int or not match or int(match[1]) != vendor_id
                or not isinstance(name, str) or not name.strip() or len(name) > 100
                or offer['requested_vendor_id'] not in (None, vendor_id)
                or vendor['vendorBadges']['isVerified'] is not True):
            return None
        return vendor_id, name, slug
    except (KeyError, TypeError, ValueError):
        return None


def idefix_seller_identity(html, seller):
    vendor_id, name, slug = seller
    _, props = page_state(BeautifulSoup(html, 'html.parser'))
    vendor = props.get('vendorDetail') if props else None
    if (not isinstance(vendor, dict) or str(props.get('vendorId')) != str(vendor_id)
            or vendor.get('id') != vendor_id or vendor.get('title') != name
            or vendor.get('isVerified') is not True or vendor.get('isActive') is not True):
        return None
    legal, tax = vendor.get('legalTitle'), vendor.get('taxNumber')
    if not isinstance(legal, str) or not legal.strip():
        return None
    if '*' not in legal and isinstance(tax, str) and re.fullmatch(r'\d{10}', tax):
        return WORDS(legal), tax
    known = KNOWN_SELLERS.get((vendor_id, name, slug))
    return (known, None) if known and re.fullmatch(r'[A-Za-z]\*+', legal) and WORDS(legal[0]) == known[0] else None


def idefix_other_seller_urls(body, offer):
    try:
        options = json.loads(body)
    except (TypeError, ValueError):
        return []
    if not isinstance(options, list):
        return []
    path = urlsplit(offer['url']).path
    urls = []
    for row in options:
        if not isinstance(row, dict):
            continue
        seller = row.get('vendorId')
        stock = row.get('inventoryQuantity')
        slug = row.get('slug')
        if (type(seller) is int and type(stock) is int and stock > 0
                and slug == f'{path}?vendorId={seller}' and idefix_product_url(BASE + slug)):
            urls.append(BASE + slug)
    return list(dict.fromkeys(urls))
