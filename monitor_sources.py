"""Read selected new monitor offers using the retailer's public buy box."""
from datetime import datetime, timezone
import json
import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from audit_hepsiburada_iphone16pm_readonly import parse_exact_buy_box
from hepsiburada_catalog_source import _identity, selected_technical_text
from hepsiburada_s25_fe_source import _nodes
from laptop_direct_source import selected_direct_laptop
from laptop_retailer_sources import (
    WORDS, _attrs, _legal, _mediamarkt_url, _photo, _vatan_review_jsonld,
)
from mediamarkt_source import _selected_offer as media_offer
from mediamarkt_marketplace_audit import selected_marketplace_offer
from n11_catalog_source import _parse_selected_listing as n11_offer
from retailer_barcode import retailer_gtin13
from retailer_offer_refresh import _schema_nodes
from teknosa_marketplace_source import _selected_marketplace_commerce
from trendyol_catalog_source import _parse_selected_listing as trendyol_offer
from tws_offer_pilot import _selected_group_root
from tws_retailer_sources import selected_vatan_tws

BRANDS = {'asus': 'ASUS', 'acer': 'Acer', 'aoc': 'AOC', 'benq': 'BenQ', 'dell': 'Dell',
          'gigabyte': 'Gigabyte', 'hp': 'HP', 'lenovo': 'Lenovo', 'lg': 'LG', 'msi': 'MSI',
          'philips': 'Philips', 'samsung': 'Samsung', 'viewsonic': 'ViewSonic', 'xiaomi': 'Xiaomi',
          'dahua': 'Dahua', 'huawei': 'Huawei', 'gamepower': 'GamePower', 'gamebooster': 'GameBooster',
          'fazeon': 'Fazeon', 'james donkey': 'James Donkey',
          'casper': 'Casper', 'rampage': 'Rampage', 'monster': 'Monster', 'aiwa': 'Aiwa',
          'frisby': 'Frisby', 'everest': 'Everest', 'arzopa': 'Arzopa', 'twisted minds': 'Twisted Minds',
          'hkc': 'HKC', 'peaq': 'PEAQ', 'koorui': 'KOORUI', 'gameraider': 'GAMERAIDER',
          'lenovo legion': 'Lenovo', 'dell alienware': 'Dell'}
NOT_MONITOR = re.compile(r'\b(?:yenilenmis|ikinci el|outlet|teshir|refurbished|renewed|adaptoru|kablo|karti|yedek parca|monitor standi|aski aparati)\b')
SIZE = re.compile(r'(?<![\d.])((?:1[5-9]|[2-4]\d)(?:[.,]\d)?)\s*(?:"|″|\u201d|\'\'|inç\b|inch\b)', re.I)
RESOLUTION = re.compile(r'\b(\d{4})\s*[x×]\s*(\d{3,4})\b', re.I)
REFRESH = re.compile(r'\b([1-9]\d{1,2})\s*hz\b', re.I)
PANEL = re.compile(r'\b(qd[ -]?oled|oled|ips|va|tn)\b', re.I)
LABELS = {
    'size': ('ekran boyutu', 'ekran boyutu inc', 'ekran boyutu inch', 'ekran boyut araligi'),
    'resolution': ('cozunurluk', 'ekran cozunurlugu', 'maksimum cozunurluk', 'max ekran cozunurlugu'),
    'refresh': ('yenileme hizi', 'ekran yenileme hizi', 'tarama hizi'),
    'panel': ('panel tipi', 'ekran panel tipi', 'panel teknolojisi', 'ekran teknolojisi'),
}


def code_key(value):
    return re.sub(r'[^a-z0-9]', '', WORDS(value))


def mentions(title, code):
    words = re.findall(r'[a-z0-9]+', WORDS(title))
    return any(''.join(words[i:i+count]) == code for i in range(len(words)) for count in range(1, 5))


def monitor_facts(title, brand, code, attrs=None, *, category_verified=False):
    brand = BRANDS.get(WORDS(brand))
    words = WORDS(title)
    if (not brand or not (category_verified or re.search(r'\bmonitor(?:u|ler)?\b', words))
            or NOT_MONITOR.search(words) or not mentions(title, code)):
        return None
    attrs = attrs or {}

    def read(key, regex, render=lambda match: match[1]):
        values = [attrs[label] for label in LABELS[key] if label in attrs]
        for value in [*values, title]:
            if match := regex.search(value):
                return render(match)
        return None

    size = read('size', SIZE, lambda m: float(m[1].replace(',', '.')))
    resolution = read('resolution', RESOLUTION, lambda m: f'{m[1]} × {m[2]}')
    if not resolution:
        for text, pixels in [(r'\b(?:4k|uhd)\b', '3840 × 2160'), (r'\b(?:qhd|wqhd|1440p)\b', '2560 × 1440'),
                             (r'\b(?:fhd|full ?hd|1080p)\b', '1920 × 1080')]:
            if re.search(text, words) and not re.search(r'\b(?:ultrawide|uwqhd|dqhd|wuxga)\b', words):
                resolution = pixels
                break
    if not resolution and re.search(r'\buwqhd\b', words):
        resolution = '3440 × 1440'
    refresh = read('refresh', REFRESH, lambda m: int(m[1]))
    panel = read('panel', PANEL, lambda m: re.sub(r'[- ]', '', m[1].upper()).replace('QDOLED', 'QD-OLED'))
    return {'brand': brand, 'size': size, 'resolution': resolution, 'refresh': refresh, 'panel': panel,
            'hdmi': bool(re.search(r'\b(?:\d+x)?hdmi\b', words)),
            'dp': bool(re.search(r'\b(?:dp|display ?port)\b', words)),
            'usb_c': bool(re.search(r'\b(?:usb|type) c\b', words))}


def _evidence(title, brand, code, attrs, images, hosts, *, category_verified=False):
    facts = monitor_facts(title, brand, code, attrs, category_verified=category_verified)
    if not facts:
        return None
    if isinstance(images, str):
        images = [images]
    photo = next((p for raw in images or [] if (p := _photo(raw, *hosts))), None)
    return {'facts': facts, '_image': photo}


def selected_n11(html, url, code):
    def verify(product):
        category = product.get('category')
        title = product.get('title', '')
        if not isinstance(category, dict) or category.get('seoUrl') != '/bilgisayar/cevre-birimleri/monitor-ve-ekran':
            return lambda value: False, {}
        photos = [row['path'].replace('{0}', '640') for row in product.get('images') or []
                  if isinstance(row, dict) and isinstance(row.get('path'), str) and row.get('isVideoBannerImage') is False]
        evidence = _evidence(title, product.get('productBrand'), code, _attrs(product.get('productAttributes')),
                             photos, ('n11scdn.akamaized.net', 'n11scdn2-im.akamaized.net'), category_verified=True)
        if not evidence:
            return lambda value: False, {}
        return (lambda value: WORDS(value.removesuffix(' Fiyatları ve Özellikleri')) == WORDS(title)), {
            **evidence, **_legal(product.get('seller'), 'companyName')}
    return n11_offer(html, url, '', {}, verify_laptop=verify)


def selected_trendyol(html, url, code):
    def variant(product, title, brand):
        category = product.get('category')
        if not isinstance(category, dict) or WORDS(category.get('name')) not in {'monitor', 'bilgisayar ekrani', 'oyuncu monitor'}:
            return None, 'not_monitor_category'
        evidence = _evidence(title, brand.get('name'), code, _attrs(product.get('attributes'), trendyol=True),
                             product.get('images'), ('cdn.dsmcdn.com',), category_verified=True)
        if not evidence:
            return None, 'selected_monitor_identity_missing'
        return {**evidence, **_legal(product['merchantListing']['merchant'], 'officialName')}, None
    return trendyol_offer(html, url, variant, _selected_group_root)


def selected_vatan(html, url, code):
    soup = BeautifulSoup(html, 'html.parser')
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            item = _vatan_review_jsonld(script.get_text())
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict) and item.get('@type') == 'Product' and script.string:
            script.string = json.dumps(item, ensure_ascii=False).replace('<', r'\u003c')
    selected = [p for p in _schema_nodes(soup, url) if p.get('@type') == 'Product'
                and isinstance(p.get('offers'), dict) and p['offers'].get('url') == url]
    if len(selected) != 1:
        return None, 'selected_monitor_schema_missing'
    product = selected[0]
    brand = product.get('brand', {})
    heading = soup.select_one('.product-detail h1.product-list__product-name')
    if not heading or not isinstance(brand, dict):
        return None, 'selected_monitor_identity_missing'
    attrs = {}
    for row in soup.select('.product-table tr'):
        cells = row.find_all(['th', 'td'])
        if len(cells) == 2:
            attrs[WORDS(cells[0].get_text(' ', strip=True))] = cells[1].get_text(' ', strip=True)
    title = heading.get_text(' ', strip=True)
    facts = monitor_facts(title, brand.get('name'), code, attrs)
    if not facts:
        return None, 'selected_monitor_identity_missing'
    offer = selected_vatan_tws(str(soup), url, code, lambda value, _: code if mentions(value, code) else None)
    return ({**offer, 'facts': facts}, 'observed') if offer else (None, 'vatan_selected_offer_unproven')


def selected_mediamarkt(html, url, code):
    def verify(product, title, brand, features=None):
        attrs = _attrs([{'key': row.get('name'), 'value': row.get('value')} for row in features or []])
        evidence = _evidence(title, brand, code, attrs, product.get('image'), ('assets.mmsrg.com',))
        gtin = retailer_gtin13(str(product.get('gtin13', '')))
        return {**evidence, 'gtin13': gtin, 'sku': str(product.get('sku'))} if evidence and gtin else None

    first = media_offer(html, url, allowed_url=_mediamarkt_url, verify_product=verify)
    if first:
        return {**first, 'platform': 'MediaMarkt', 'condition': 'new',
                '_legal_name': 'mediamarkt turkey ticaret limited sirketi'}, 'observed'
    radio = 'name="pdp-condition-selector"' in html
    offer = selected_marketplace_offer(html, url, allowed_url=_mediamarkt_url, verify_product=verify,
        allow_cents=True, allow_refurbished_alternatives=radio, allow_delivery_card=radio,
        require_selected_condition_radio=radio)
    if not offer:
        return None, 'mediamarkt_selected_offer_unproven'
    return {**offer, 'merchant': offer['seller_name'], 'merchant_id': offer['seller_id'],
            'seller_url': 'https://www.mediamarkt.com.tr/tr/marketplace/seller/' + offer['seller_id'],
            'observed_at': datetime.now(timezone.utc).isoformat()}, 'observed'


def selected_teknosa(html, url, code):
    def verify(cart, title, product):
        return _evidence(title, cart.get('data-product-brand'), code, {}, product.get('image'),
                         ('img-teknosa-sap.mncdn.com',))
    return _selected_marketplace_commerce(html, url, verify_product=verify)


def selected_direct(html, url, code):
    return selected_direct_laptop(html, url, read_facts=lambda title, brand: monitor_facts(title, brand, code))


def selected_hepsiburada(html, url, code):
    soup = BeautifulSoup(html, 'html.parser')
    scripts = soup.select('script#reduxStore')
    try:
        product = json.loads(scripts[0].get_text())['productState']['product'] if len(scripts) == 1 else {}
        sku, group, listing = (product[k] for k in ('sku', 'productId', 'listingId'))
        seller, merchant_id, slug = (product['merchant'][k] for k in ('name', 'id', 'urlPostfix'))
    except (KeyError, ValueError, TypeError):
        return None, 'selected_monitor_state_missing'
    if (not all(isinstance(k, str) and k for k in (sku, group, listing, seller, merchant_id, slug))
            or not re.fullmatch(r'[a-z0-9-]{1,100}', slug) or not _identity(url, group, sku)
            or product.get('isBundle') is not False or not any(
                isinstance(row, dict) and row.get('categoryId') == '57' and row.get('urlKeyword') == 'monitorler'
                for row in product.get('categories', []))
            or any(WORDS(value) not in {WORDS(slug), WORDS(seller)} for value in
                   parse_qs(urlparse(url).query).get('magaza', []))):
        return None, 'selected_monitor_identity_missing'
    default = [v for v in product.get('variants', []) if isinstance(v, dict) and v.get('isDefault') is True]
    schemas = [p for p in _nodes(soup) if p.get('@type') == 'Product' and p.get('sku') == sku]
    if len(default) != 1 or len(schemas) != 1:
        return None, 'selected_monitor_variant_missing'
    variant, schema = default[0], schemas[0]
    gtin = retailer_gtin13(variant.get('barcode'))
    codes = variant.get('barcodes')
    if (not gtin or variant.get('sku') != sku or variant.get('isInStock') is not True
            or variant.get('isPreOrder') is not False or not isinstance(codes, list)
            or {retailer_gtin13(str(value).strip()) for value in codes} != {gtin}
            or retailer_gtin13(product.get('barcode')) != gtin or retailer_gtin13(schema.get('gtin')) != gtin):
        return None, 'selected_monitor_barcode_or_stock_unproven'
    schema_url = (schema.get('offers') or {}).get('url')
    detail = selected_technical_text(soup, sku, group, listing, merchant_id)
    heading = soup.select_one('h1[data-test-id="title"]')
    brand = product.get('brand')
    schema_brand = schema.get('brand')
    if (not schema_url or not _identity(schema_url, group, sku) or not heading
            or not re.search(r'\bStok Kodu\s+' + re.escape(sku) + r'\b', detail)
            or not isinstance(schema_brand, dict) or WORDS(schema_brand.get('name')) != WORDS(brand)
            or not all(mentions(value, code) for value in (schema.get('name'), product.get('name'), variant.get('name')))):
        return None, 'selected_monitor_spec_identity_unproven'
    evidence = _evidence(heading.get_text(' ', strip=True), brand, code, {}, [variant.get('thumbnail')],
                         ('productimages.hepsiburada.net',))
    if not evidence:
        return None, 'selected_monitor_spec_identity_unproven'
    seller_url = 'https://www.hepsiburada.com/magaza/' + slug
    scope = dict(URL=url, SCHEMA_URL=schema_url, PRODUCT_ID=group, SKU=sku, LISTING_ID=listing,
                 MERCHANT_ID=merchant_id, MERCHANT_POSTFIX=slug, SELLER=seller, SELLER_URL=seller_url,
                 GTIN13=gtin, generic_catalog=True)
    offer, reason = parse_exact_buy_box(html, url, scope=scope, verify_laptop=lambda *_: variant)
    return ({**offer, **evidence, 'platform': 'Hepsiburada', 'seller_url': seller_url}, reason) if offer else (None, reason)


PARSERS = {'www.n11.com': selected_n11, 'www.vatanbilgisayar.com': selected_vatan,
           'www.mediamarkt.com.tr': selected_mediamarkt, 'www.trendyol.com': selected_trendyol,
           'www.teknosa.com': selected_teknosa, 'www.gaming.gen.tr': selected_direct,
           'www.incehesap.com': selected_direct, 'www.sinerji.gen.tr': selected_direct,
           'www.hepsiburada.com': selected_hepsiburada}
