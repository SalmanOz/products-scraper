"""Read a selected Evofone SKU from its public checkout and seller terms."""

from datetime import datetime, timezone
from decimal import Decimal
import json
import re

from bs4 import BeautifulSoup

from ayaz_phone_source import only, value
from retailer_barcode import retailer_gtin13
from retailer_offer_refresh import _schema_nodes
from teknosa_reviewed_source import _amount, _cash_price
from tr_price_scraper import TRPriceScraper
from tws_retailer_sources import selected_photo


HOST = 'evofone.com'
BASE = 'https://' + HOST
TERMS = BASE + '/satis-sozlesmesi'
CATEGORIES = {'phone': '/akilli-telefon', 'tws': '/nothing-kulak-ici-kulakliklar'}
PRODUCT = re.compile(r'/((?:xiaomi|redmi|poco|tecno|nothing|cmf)-[a-z0-9-]{4,175})')
IMAGE_HOST = 'static.ticimax.cloud'
MERCHANT = 'Evofone'
COMPANY = 'evofone teknoloji anonim sirketi'
NORMALIZE = TRPriceScraper._normalize_words
NON_NEW = re.compile(r'\b(?:outlet|yenilenmis|ikinci el|teshir|yedek|kilif|replika)\b')


def product_url(url):
    return (isinstance(url, str) and url.startswith(BASE + '/') and
            PRODUCT.fullmatch(url.removeprefix(BASE)) is not None and not NON_NEW.search(NORMALIZE(url)))


def seller_identity(html):
    soup = BeautifulSoup(html, 'html.parser')
    names = [NORMALIZE(node.get_text(' ', strip=True)) for node in soup.select('.html-context strong ~ span')
             if (previous := node.find_previous_sibling('strong'))
             and NORMALIZE(previous.get_text(' ', strip=True)) == 'unvani']
    if value(soup, 'link[rel=canonical]') != TERMS or COMPANY not in names or set(names) - {COMPANY, 'evofone teknoloji a s'}:
        return None
    return COMPANY


def checkout_model(soup):
    declarations = [script.get_text().split('var productDetailModel = ', 1)[1].lstrip()
                    for script in soup.select('script:not([src])')
                    if 'var productDetailModel = ' in script.get_text()]
    if len(declarations) != 1:
        return None
    try:
        model, end = json.JSONDecoder().raw_decode(declarations[0])
    except ValueError:
        return None
    return model if isinstance(model, dict) and declarations[0][end:].lstrip().startswith(';') else None


def displayed(node):
    return (node is not None and not node.has_attr('hidden') and not node.has_attr('disabled')
            and node.get('aria-hidden') != 'true' and node.get('aria-disabled') != 'true'
            and not re.search(r'(?:display\s*:\s*none|visibility\s*:\s*hidden)', node.get('style', ''), re.I))


def selected_offer(html, url, company, *, kind):
    if company != COMPANY or not product_url(url):
        return None, 'evofone_url_or_seller_unverified'
    soup = BeautifulSoup(html, 'html.parser')
    box = only(soup, '.RightDetail')
    products = [item for item in _schema_nodes(soup, url) if item.get('@type') == 'Product']
    if not box or value(soup, 'link[rel=canonical]') != url or len(products) != 1:
        return None, 'evofone_selected_page_unverified'
    title = value(box, '.ProductName h1')
    product = products[0]
    model = checkout_model(soup)
    sku = model.get('product') if model else None
    if not isinstance(sku, dict) or model.get('products') != [sku]:
        return None, 'evofone_selected_sku_unverified'
    brand = product.get('brand')
    brand = brand.get('name') if isinstance(brand, dict) else None
    barcode = retailer_gtin13(sku.get('barkod'))
    title_key = NORMALIZE(title)
    if (not title_key or NON_NEW.search(title_key) or brand != model.get('brandName') or not brand
            or any(NORMALIZE(item) != title_key for item in (product.get('name'), model.get('productName'), sku.get('urunAdi')))
            or not barcode or any(retailer_gtin13(product[key]) != barcode for key in ('gtin', 'gtin12', 'gtin13', 'gtin14') if key in product)
            or type(model.get('productId')) is not int or model['productId'] < 1
            or type(sku.get('id')) is not int or sku['id'] < 1 or model.get('mainVariantId') != sku['id']
            or sku.get('urunKartiId') != model['productId'] or not sku.get('stokKodu') or sku['stokKodu'] != model.get('stockCode')
            or model.get('productIsAsorti') is not False or model.get('isPackageProduct') is not False
            or model.get('productType') != 'Urun' or (model.get('productCategoryId') == 123) != (kind == 'tws')):
        return None, 'evofone_selected_sku_unverified'

    offer = product.get('offers')
    if not isinstance(offer, dict):
        return None, 'evofone_selected_offer_unverified'
    seller = offer.get('seller')
    cart_box = only(box, '#divSatinAl')
    cart = only(cart_box, 'input.btnAddBasketOnDetail') if cart_box else None
    stock = _amount(sku.get('stokAdedi'))
    if (offer.get('@type') != 'Offer' or offer.get('url') != url or
            offer.get('availability') not in ('InStock', 'https://schema.org/InStock')
            or offer.get('itemCondition') not in ('NewCondition', 'https://schema.org/NewCondition')
            or not isinstance(seller, dict) or NORMALIZE(seller.get('name')) != 'evofone teknoloji a s'
            or sku.get('aktif') is not True or model.get('productActive') is not True or stock is None
            or stock != _amount(model.get('totalStockAmount')) or not displayed(cart_box) or not displayed(cart)
            or cart.get('value') != 'Sepete Ekle'):
        return None, 'evofone_selected_stock_or_seller_unverified'

    price = _amount(offer.get('price'))
    checkout_price = _amount(model.get('productPriceKDVIncluded'))
    visible = [node.get_text(' ', strip=True) for node in box.select('#divFiyatAlanlari .spanFiyat')
               if displayed(node) and displayed(node.parent) and displayed(node.parent.parent) and node.get_text(strip=True)]
    if (price is None or offer.get('priceCurrency') != 'TRY' or model.get('productCurrency') != 'TRY'
            or model.get('fiyatGoster') is not True
            or checkout_price is None or checkout_price.quantize(Decimal('.01')) != price
            or [_cash_price(text.removeprefix('₺') + ' TL') for text in visible] != [price]
            or displayed(only(box, '#divUrunKampanyaliFiyat'))):
        return None, 'evofone_selected_cash_unverified'
    brand = 'Nothing' if NORMALIZE(brand) in ('nothing r', 'cmf by nothing') else brand
    photo = selected_photo(value(soup, 'meta[property="og:image"]'), {IMAGE_HOST})
    return {'platform': MERCHANT, 'merchant': MERCHANT, 'merchant_id': HOST, '_legal_name': company,
            'product_id': str(model['productId']), 'sku': str(sku['id']),
            'price': float(price), 'currency': 'TRY', 'availability': 'in_stock', 'condition': 'new',
            'brand': brand, 'gtin13': barcode, 'source_title': title, 'url': url, '_image': photo,
            'observed_at': datetime.now(timezone.utc).isoformat()}, 'verified'
