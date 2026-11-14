"""Read PTTAVM's selected public listing and its registered seller."""

from datetime import datetime, timezone
import json
import re

from bs4 import BeautifulSoup

from ayaz_phone_source import value
from retailer_barcode import retailer_gtin13
from retailer_offer_refresh import _schema_nodes
from teknosa_reviewed_source import _amount, _cash_price
from tr_price_scraper import TRPriceScraper


HOST = 'www.pttavm.com'
BASE = 'https://' + HOST
PRODUCT_PATH = re.compile(r'/[a-z0-9-]{3,250}-p-([1-9]\d{5,11})')
SELLER_PATH = re.compile(r'/magaza/([a-z0-9-]{2,100})')
CATEGORIES = {'phone': {'cep-telefonu', 'android-telefonlar', 'iphone-ios-telefonlar'},
              'tws': {'bluetooth-kulaklik'}, 'laptop': {'dizustu-bilgisayar'},
              'television': {'televizyon', 'televizyonlar'},
              'monitor': {'monitor', 'led-monitor', 'oyuncu-monitoru'}}
USED = re.compile(r'\b(?:yenilenmis|ikinci el|refurbished|outlet|teshir|muadil|replika|yedek)\b')
CONDITIONAL = re.compile(r'\b(?:sepete ozel|sepette|kuponla|uyeye ozel|uyelere ozel|kart ile)\b')
STATE_SCRIPT = re.compile(r'\s*window\.__staticRouterHydrationData = JSON\.parse\((".*")\);?\s*', re.S)
NORMALIZE = TRPriceScraper._normalize_words


def ptt_product_url(url):
    if not isinstance(url, str) or not url.startswith(BASE):
        return False
    path = url.removeprefix(BASE)
    return PRODUCT_PATH.fullmatch(path) is not None and USED.search(NORMALIZE(path)) is None


def selected_state(soup, route, query_key):
    scripts = [m[1] for script in soup.select('script:not([src])')
               if (m := STATE_SCRIPT.fullmatch(script.get_text()))]
    if len(scripts) != 1:
        return None
    try:
        state = json.loads(json.loads(scripts[0]))
        queries = state['loaderData'][route]['dehydratedState']['queries']
        results = [query['state']['data'] for query in queries if query.get('queryKey') == query_key]
        if len(results) != 1 or results[0].get('success') is not True:
            return None
        data = results[0]['data']
        return data if isinstance(data, dict) else None
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def selected_ptt_offer(html, url, category):
    if category not in CATEGORIES or not ptt_product_url(url):
        return None, 'invalid_ptt_destination'
    product_id = PRODUCT_PATH.fullmatch(url.removeprefix(BASE))[1]
    soup = BeautifulSoup(html, 'html.parser')
    data = selected_state(soup, 'slug-page', ['productDetail', product_id, False, None, None])
    if not data or data.get('success') is not True:
        return None, 'ptt_selected_state_missing'
    info = data.get('info')
    if not isinstance(info, dict):
        return None, 'ptt_selected_identity_mismatch'
    source = info.get('externalSource')
    category_data = info.get('category')
    headings = soup.select('h1[class^="productName__"]')
    title = headings[0].get_text(' ', strip=True) if len(headings) == 1 else ''
    if (info.get('id') != int(product_id) or info.get('item_group_id') != int(product_id)
            or info.get('url') != url.removeprefix(BASE + '/')
            or value(soup, 'meta[property="og:url"]') != url
            or not title or NORMALIZE(title) != NORMALIZE(info.get('name')) or USED.search(NORMALIZE(title))
            or not isinstance(category_data, dict) or category_data.get('category_slug') not in CATEGORIES[category]
            or not isinstance(source, dict) or source.get('isVariant') is not False):
        return None, 'ptt_selected_identity_mismatch'
    gtin = retailer_gtin13(source.get('barcode'))
    if not gtin or source.get('catalogBarcode') not in (None, '') and retailer_gtin13(source['catalogBarcode']) != gtin:
        return None, 'ptt_selected_gtin_unverified'
    if (type(info.get('stock')) is not int or info['stock'] <= 0
            or info.get('isPurchasable') is not True or info.get('isVariantPurchasable') is not True):
        return None, 'ptt_selected_stock_unverified'

    nodes = _schema_nodes(soup, url)
    schema = [row for row in nodes if row.get('@type') == 'Product']
    product = schema[0] if len(schema) == 1 else {}
    native_offer = product.get('offers')
    if (product.get('url') != url or product.get('sku') != product_id
            or NORMALIZE(product.get('name')) != NORMALIZE(title) or not isinstance(native_offer, dict)
            or any(key in product and retailer_gtin13(product[key]) != gtin
                   for key in ('gtin', 'gtin12', 'gtin13', 'gtin14'))
            or native_offer.get('@type') != 'Offer' or native_offer.get('priceCurrency') != 'TRY'
            or native_offer.get('availability') != 'https://schema.org/InStock'
            or native_offer.get('itemCondition') not in (None, 'https://schema.org/NewCondition')):
        return None, 'ptt_selected_schema_mismatch'

    brand = info.get('brand')
    schema_brand = product.get('brand')
    if (not isinstance(brand, str) or not brand or not isinstance(schema_brand, dict)
            or NORMALIZE(schema_brand.get('name')) != NORMALIZE(brand)
            or source.get('brand') and NORMALIZE(source['brand']) != NORMALIZE(brand)):
        return None, 'ptt_selected_brand_mismatch'
    selected = headings[0].find_parent('div', class_=re.compile(r'^item__'))
    cart = data.get('cart')
    price_data = cart.get('price') if isinstance(cart, dict) else None
    cart_data = cart.get('addToCart') if isinstance(cart, dict) else None
    if not selected or not isinstance(price_data, dict) or not isinstance(cart_data, dict):
        return None, 'ptt_selected_buy_box_missing'
    price_nodes = selected.select('span[class^="price__"]')
    prices = price_nodes[0].find_parent('div', class_=re.compile(r'^priceRow__')) if len(price_nodes) == 1 else None
    price = _amount(native_offer.get('price'))
    price_text = price_data.get('originalPriceText')
    if (not prices or price is None or price_data.get('currency') != 'TL'
            or price_data.get('isPriceDiscounted') is not False
            or any(_amount(price_data.get(key)) != price for key in ('originalPrice', 'regularPrice', 'discountedPrice'))
            or _cash_price(price_nodes[0].get_text('', strip=True)) != price
            or not isinstance(price_text, str) or _cash_price(price_text) != price
            or CONDITIONAL.search(NORMALIZE(prices.parent.get_text(' ', strip=True)))):
        return None, 'ptt_selected_cash_price_unverified'
    carts = selected.select('button[class*="addToCartButton__"]')
    if (len(carts) != 1 or carts[0].has_attr('disabled') or carts[0].get('aria-disabled') == 'true'
            or carts[0].get_text(' ', strip=True) != 'Sepete Ekle' or cart_data.get('text') != 'Sepete Ekle'):
        return None, 'ptt_selected_cart_unavailable'

    seller = info.get('merchant')
    seller_name = seller.get('name') if isinstance(seller, dict) else None
    seller_id = str(seller.get('id')) if isinstance(seller, dict) else ''
    schema_seller = native_offer.get('seller')
    shops = [(link['href'], link.get_text(' ', strip=True))
             for link in soup.select('a[class^="merchantName__"][href]') if link['href'].startswith(BASE + '/magaza/')]
    if (not isinstance(seller_name, str) or not 1 <= len(seller_name.strip()) <= 100
            or not re.fullmatch(r'[1-9]\d{1,11}', seller_id)
            or not isinstance(schema_seller, dict) or schema_seller.get('@type') != 'Organization'
            or NORMALIZE(schema_seller.get('name')) != NORMALIZE(seller_name)
            or len(shops) != 1 or NORMALIZE(shops[0][1]) != NORMALIZE(seller_name)
            or not (shop := SELLER_PATH.fullmatch(shops[0][0].removeprefix(BASE)))):
        return None, 'ptt_selected_seller_unverified'
    organizations = [node for node in nodes if node.get('@type') == 'Organization'
                     and node.get('url') == BASE + '/' + shop[1] and NORMALIZE(node.get('name')) == NORMALIZE(seller_name)]
    if len(organizations) != 1:
        return None, 'ptt_selected_seller_unverified'
    image = info.get('imageUrl')
    return {'platform': 'PttAVM', 'merchant': seller_name, 'merchant_id': seller_id, 'seller_url': shops[0][0],
            'gtin13': gtin, 'price': float(price), 'currency': 'TRY', 'availability': 'in_stock', 'condition': 'new',
            'source_title': title, 'url': url, 'brand': brand, '_source_image': image,
            'observed_at': datetime.now(timezone.utc).isoformat()}, 'ptt_selected_cash_offer'


def ptt_seller_identity(html, offer):
    url = offer['seller_url']
    shop = SELLER_PATH.fullmatch(url.removeprefix(BASE))
    if not url.startswith(BASE) or not shop:
        return None
    soup = BeautifulSoup(html, 'html.parser')
    data = selected_state(soup, 'shop-page', ['merchantTag', shop[1]])
    if (not data or data.get('url') != shop[1] or str(data.get('id')) != offer['merchant_id']
            or data.get('active') is not True or data.get('on_sale') is not True
            or data.get('is_verified') != 1 or NORMALIZE(data.get('shop_name')) != NORMALIZE(offer['merchant'])):
        return None
    fields = {}
    for row in soup.select('[class^="shopInfoItem__"]'):
        label = value(row, '[class^="shopInfoLabel__"]')
        if label in fields:
            return None
        fields[label] = value(row, '[class^="shopInfoValue__"]')
    legal = NORMALIZE(data.get('name'))
    tax = data.get('taxnumber') or ''
    mersis = data.get('mersis') or '-'
    if (not legal or legal != NORMALIZE(fields.get('Ticaret Ünvanı:')) or tax != fields.get('VKN:')
            or mersis != fields.get('MERSİS No:')):
        return None
    if not isinstance(tax, str) or not isinstance(mersis, str):
        return None
    if not re.fullmatch(r'\d{10,11}', tax):
        tax = mersis[1:11] if re.fullmatch(r'0\d{15}', mersis) else ''
    return legal, tax
