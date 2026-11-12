"""Read Nethouse's selected in-stock earbud offer."""

from datetime import datetime, timezone
import json
import math
import re

from bs4 import BeautifulSoup

from retailer_barcode import retailer_gtin13
from retailer_offer_refresh import _is_conditional_price, _schema_nodes
from teknosa_reviewed_source import _amount, _cash_price
from tr_price_scraper import TRPriceScraper
from tws_retailer_sources import selected_photo


HOST = 'www.nethouse.com.tr'
BASE = 'https://' + HOST
PRODUCT_PATH = re.compile(r'/([a-z0-9]+(?:[.-][a-z0-9]+){2,40})')
CATEGORY = '/kulak-ici-kulaklik'
LEGAL_NAME = 'nethouse bilgisayar sistemleri pazarlama sanayi ve ticaret limited sirketi'
IMAGE_HOST = 'static.ticimax.cloud'
USED = re.compile(r'\b(?:yenilenmis|ikinci el|outlet|teshir|refurbished|muadil|replika|yedek)\b')
WORDS = TRPriceScraper._normalize_words


def nethouse_product_url(url):
    return (isinstance(url, str) and len(url) <= 275 and url.startswith(BASE)
            and PRODUCT_PATH.fullmatch(url.removeprefix(BASE)) is not None
            and not USED.search(WORDS(url.removeprefix(BASE))))


def selected_nethouse_offer(html, url, name, model_name):
    if not nethouse_product_url(url):
        return None, 'invalid_nethouse_destination'
    soup = BeautifulSoup(html, 'html.parser')
    canonical = soup.select('link[rel=canonical][href]')
    nodes = [row for row in _schema_nodes(soup, url) if row.get('@type') == 'Product']
    assignment = 'var productDetailModel = '
    models = [text.split(assignment, 1)[1].lstrip() for script in soup.select('script:not([src])')
              if assignment in (text := script.get_text())]
    if len(canonical) != 1 or canonical[0]['href'] != url or len(nodes) != 1 or len(models) != 1:
        return None, 'nethouse_selected_document_mismatch'
    try:
        data, end = json.JSONDecoder().raw_decode(models[0])
    except ValueError:
        return None, 'nethouse_selected_document_mismatch'
    if not isinstance(data, dict) or not models[0][end:].lstrip().startswith(';'):
        return None, 'nethouse_selected_document_mismatch'
    product = nodes[0]
    selected = data.get('product')
    offer = product.get('offers')
    brand = product.get('brand')
    brand = brand.get('name') if isinstance(brand, dict) else None
    heading = soup.select('h1:has(> #divUrunKodu) > span:first-child')
    title = heading[0].get_text(' ', strip=True) if len(heading) == 1 else ''
    if (not title or not brand or USED.search(WORDS(title))
            or WORDS(title) != WORDS(product.get('name')) or WORDS(title) != WORDS(data.get('productName'))
            or WORDS(brand) != WORDS(data.get('brandName')) or model_name(title, brand) != name
            or not isinstance(selected, dict) or not isinstance(offer, dict)
            or data.get('productIsAsorti') is not False or data.get('productVariantData') is not None
            or type(data.get('productId')) is not int or selected.get('urunKartiId') != data['productId']
            or type(data.get('mainVariantId')) is not int or selected.get('id') != data['mainVariantId']):
        return None, 'nethouse_selected_identity_mismatch'

    barcodes = soup.select('#spnBarkod')
    gtin = retailer_gtin13(selected.get('barkod'))
    if (not gtin or len(barcodes) != 1 or retailer_gtin13(barcodes[0].get_text(strip=True)) != gtin
            or any(retailer_gtin13(value) != gtin
                   for value in (product.get('sku'), data.get('stockCode'), selected.get('stokKodu')))):
        return None, 'nethouse_selected_gtin_unverified'
    stock = selected.get('stokAdedi')
    cart = soup.select('#divSatinAl input.Addtobasket[value="Sepete Ekle"]')
    if (data.get('productActive') is not True or type(stock) not in (int, float) or not 0 < stock < math.inf
            or stock != data.get('totalStockAmount') or len(cart) != 1 or cart[0].has_attr('disabled')
            or offer.get('availability') not in ('InStock', 'https://schema.org/InStock')
            or offer.get('itemCondition') not in ('NewCondition', 'https://schema.org/NewCondition')):
        return None, 'nethouse_selected_stock_unverified'

    visible = soup.select('#divIndirimliFiyat #indirimliFiyat .spanFiyat')
    if not visible:
        visible = soup.select('#divIndirimsizFiyat .spanFiyat, #divIndirimliFiyat #fiyat .spanFiyat')
    seller = offer.get('seller')
    price = _amount(offer.get('price'))
    cash = visible[0].get_text(strip=True) if len(visible) == 1 else ''
    if (offer.get('@type') != 'Offer' or offer.get('url') != url
            or data.get('productCurrency') != 'TRY' or str(offer.get('priceCurrency')).upper() != 'TRY'
            or not isinstance(seller, dict) or WORDS(seller.get('name')) != 'nethouse'
            or price is None or price != _amount(data.get('productPriceStr'))
            or not cash.startswith('₺') or _cash_price(cash.removeprefix('₺') + ' TL') != price
            or _is_conditional_price(visible[0].parent.parent, offer, float(price))):
        return None, 'nethouse_selected_cash_unverified'
    photos = product.get('image')
    photo = next((valid for image in photos if (valid := selected_photo(image, {IMAGE_HOST}))), None) \
        if isinstance(photos, list) else selected_photo(photos, {IMAGE_HOST})
    return {'platform': 'Nethouse', 'merchant': 'Nethouse', 'model': name,
            'gtin13': gtin, 'sku': str(data['productId']), 'source_title': title, 'url': url,
            'price': float(price), 'currency': 'TRY', 'condition': 'new', 'availability': 'in_stock',
            'observed_at': datetime.now(timezone.utc).isoformat(), '_image': photo,
            '_legal_name': LEGAL_NAME}, 'verified'
