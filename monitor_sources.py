"""Read selected new monitor offers using the retailer's public buy box."""
from collections import defaultdict
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP
from html import unescape
import json
from math import isfinite
import re
from urllib.parse import parse_qs, urljoin, urlparse

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
from trendyol_catalog_source import _parse_selected_listing as trendyol_offer, other_seller_links
from trendyol_basket_price import amount, cash
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
PCKOLIK_HOST = 'pckolik.com.tr'
PCKOLIK_SELLER = 'https://pckolik.com.tr/mesafeli-satis-sozlesmesi'
PCKOLIK_LEGAL = 'tektekal sanal magazacilik ve bilisim teknolojileri pazarlama'
ASEMPI_HOST = 'www.asempi.com'
ASEMPI_SELLER = f'https://{ASEMPI_HOST}/mesafeli-satis-sozlesmesi.php'
ASEMPI_LEGAL = 'asempi bilisim tekstil sanayi ve dis ticaret limited sirketi'
ASEMPI_CATEGORY = f'https://{ASEMPI_HOST}/kategori.php?ana_kid=1810&stokta=1'
OZAY_HOST = 'ozaybilgisayar.com'
OZAY_SELLER = f'https://{OZAY_HOST}/sayfa/mesafeli-satis'
OVERGAME_HOST = 'www.overgameweb.com'
OVERGAME_SELLER = f'https://{OVERGAME_HOST}/iletisim'
GPN_HOST = 'www.gpnteknoloji.com'
GPN_SELLER = f'https://{GPN_HOST}/sayfa/mesafeli-satis-sozlesmesi'
AVANSAS_HOST = 'www.avansas.com'
AVANSAS_SELLER = f'https://{AVANSAS_HOST}/iletisim'
ENSONU_HOST = 'www.ensonu.com'
ENSONU_CATEGORY = f'https://{ENSONU_HOST}/urun-kategori/bilgisayar/cevre-birimleri/monitor/'
ENSONU_SELLER = f'https://{ENSONU_HOST}/mesafeli-satis-sozlesmesi/'
COMPAZAR_HOST = 'www.compazar.com'
COMPAZAR_SELLER = f'https://{COMPAZAR_HOST}/on-bilgilendirme-formu'
NETHOUSE_HOST = 'www.nethouse.com.tr'
NETHOUSE_SELLER = f'https://{NETHOUSE_HOST}/kvkk-aydinlatma-metni'
NETHOUSE_IMAGE_HOST = 'static.ticimax.cloud'
RAMPAGE_HOST = 'www.rampage.com.tr'
RAMPAGE_SELLER = f'https://{RAMPAGE_HOST}/en/pages/mesafeli-satis-sozlesmesi'
DIRECT_MONITOR_STORES = frozenset({'www.gaming.gen.tr', 'www.incehesap.com', 'www.sinerji.gen.tr',
                                   'www.teknostim.com', 'pitrap.com', 'www.uzmanlarpc.com', 'www.netsiparis.com'})
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


def selected_trendyol(html, url, code, *, seller_links=None, expected_gtin=None):
    def variant(product, title, brand):
        category = product.get('category')
        if not isinstance(category, dict) or WORDS(category.get('name')) not in {'monitor', 'bilgisayar ekrani', 'oyuncu monitor'}:
            return None, 'not_monitor_category'
        evidence = _evidence(title, brand.get('name'), code, _attrs(product.get('attributes'), trendyol=True),
                             product.get('images'), ('cdn.dsmcdn.com',), category_verified=True)
        if not evidence:
            return None, 'selected_monitor_identity_missing'
        listing = product.get('merchantListing')
        merchant = listing.get('merchant') if isinstance(listing, dict) else None
        winner = listing.get('winnerVariant') if isinstance(listing, dict) else None
        if (seller_links is not None and expected_gtin and isinstance(winner, dict) and isinstance(merchant, dict)
                and retailer_gtin13(winner.get('barcode')) == expected_gtin):
            links = other_seller_links(url, listing.get('otherMerchants'), merchant.get('id'),
                                       winner.get('itemNumber'), expected_gtin)
            seller_links.extend(target for target, _ in links[:2])
        return {**evidence, **_legal(merchant, 'officialName')}, None
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
    return selected_direct_laptop(html, url, read_facts=lambda title, brand, attrs: monitor_facts(title, brand, code, attrs))


def asempi_product_id(url):
    match = re.fullmatch(r'https://www\.asempi\.com/urun\.php\?id=([1-9][0-9]{0,8})', url)
    return match[1] if match else None


def asempi_seller_identity(html):
    soup = BeautifulSoup(html, 'html.parser')
    if [tag.get('href') for tag in soup.select('link[rel="canonical"]')] != [ASEMPI_SELLER]:
        return None
    boxes = [node.parent for node in soup.select('.kutu > h2') if WORDS(node.get_text(' ', strip=True)) == '1 satici bilgileri']
    if len(boxes) != 1:
        return None
    fields = [WORDS(node.get_text(' ', strip=True)) for node in boxes[0].select(':scope > p')]
    tax = [field.rsplit(' ', 1)[-1] for field in fields if re.fullmatch(r'vergi dairesi no .+ [0-9]{10}', field)]
    if fields.count('unvan ' + ASEMPI_LEGAL) != 1 or tax != ['4940833646']:
        return None
    return {'_legal_name': ASEMPI_LEGAL, '_tax_key': tax[0]}


def selected_asempi(html, url, code):
    soup = BeautifulSoup(html, 'html.parser')
    product_id = asempi_product_id(url)
    if not product_id or [tag.get('href') for tag in soup.select('link[rel="canonical"]')] != [url]:
        return None, 'asempi_canonical_mismatch'
    nodes = _schema_nodes(soup, url)
    products = [node for node in nodes if node.get('@type') == 'Product']
    headings = soup.select('.urun-detay h1.urun-baslik')
    if len(products) != 1 or len(headings) != 1:
        return None, 'asempi_selected_product_missing'
    product = products[0]
    title = headings[0].get_text(' ', strip=True)
    brand = product.get('brand')
    gtin = retailer_gtin13(product.get('gtin13'))
    barcodes = [retailer_gtin13(node.get_text(strip=True)) for node in soup.select('.urun-detay .urun-meta-satir span.mono')
                if WORDS(node.parent.get_text(' ', strip=True)).startswith('barkod ')]
    photos = [node['src'] for node in soup.select('.urun-detay img#anaGorsel[src]')]
    category = any(isinstance(item, dict) and item.get('item') == f'https://{ASEMPI_HOST}/kategori.php?id=1810'
                   for node in nodes if node.get('@type') == 'BreadcrumbList' for item in node.get('itemListElement') or [])
    evidence = _evidence(title, brand.get('name') if isinstance(brand, dict) else None, code, {}, photos,
                         (ASEMPI_HOST,), category_verified=category)
    if (not gtin or barcodes != [gtin] or not category or not evidence
            or product.get('sku') != 'ASM-' + product_id or WORDS(str(product.get('name', ''))) != WORDS(title)):
        return None, 'asempi_selected_variant_unproven'
    offer = product.get('offers')
    if (not isinstance(offer, dict) or offer.get('@type') != 'Offer' or offer.get('url') != url
            or offer.get('priceCurrency') != 'TRY' or offer.get('availability') != 'https://schema.org/InStock'
            or offer.get('itemCondition') not in (None, 'https://schema.org/NewCondition')
            or any(key in offer for key in ('validForMemberTier', 'eligibleCustomerType', 'eligibleTransactionVolume'))):
        return None, 'asempi_new_stock_unproven'
    warranties = [n.get_text(' ', strip=True) for n in soup.select('.urun-detay .urun-meta-satir > span')]
    stock = soup.select('.urun-detay .urun-fiyat-kutu .urun-stok-satir.stok-var')
    quantity = re.fullmatch(r'([1-9][0-9]*) adet stokta(?:\s*[—–-]\s*Son \1 adet!)?', stock[0].get_text(' ', strip=True)) if len(stock) == 1 else None
    cart = soup.select('.urun-detay form#sepeteEkleForm[method="post"] button.sepete-ekle-btn[type="submit"]')
    amounts = soup.select('.urun-detay form#sepeteEkleForm input[name="adet"][min="1"][value="1"]')
    if (not any(re.fullmatch(r'[1-9][0-9]* ay garanti', text) for text in warranties) or not quantity
            or len(amounts) != 1 or amounts[0].get('max') != quantity[1]
            or len(cart) != 1 or cart[0].has_attr('disabled') or cart[0].get('aria-disabled') == 'true'):
        return None, 'asempi_selected_cart_unproven'
    price = amount(offer.get('price'))
    prices = [cash(node.get_text(' ', strip=True)) for node in soup.select('.urun-detay .urun-fiyat-buyuk')]
    card = [cash(row.select_one('span.mono').get_text(' ', strip=True)) for row in soup.select('.urun-detay .taksit-satir')
            if row.select_one('span') and row.select_one('span').get_text(strip=True) == 'Tek Çekim' and row.select_one('span.mono')]
    if not price or prices != [price] or card != [price]:
        return None, 'asempi_selected_cash_unproven'
    return {**evidence, 'platform': 'Asempi', 'merchant': 'Asempi', 'merchant_id': ASEMPI_HOST,
            'seller_url': ASEMPI_SELLER, 'gtin13': gtin, 'price': float(price), 'currency': 'TRY',
            'condition': 'new', 'availability': 'in_stock', 'url': url, 'source_title': title,
            'observed_at': datetime.now(timezone.utc).isoformat()}, 'observed'


def ozay_seller_identity(html):
    soup = BeautifulSoup(html, 'html.parser')
    if [tag.get('href') for tag in soup.select('link[rel="canonical"]')] != [OZAY_SELLER]:
        return None
    fields = [WORDS(node.get_text(' ', strip=True)) for node in soup.select('.card .pad > p')]
    if (fields.count('satici ozay teknoloji san tic ltd sti') != 1
            or fields.count('vergi dairesi no duzce 6640851380') != 1):
        return None
    return {'_legal_name': 'ozay teknoloji sanayi ticaret limited sirketi', '_tax_key': '6640851380'}


def selected_ozay(html, url, code):
    soup = BeautifulSoup(html, 'html.parser')
    if [tag.get('href') for tag in soup.select('link[rel="canonical"]')] != [url]:
        return None, 'ozay_canonical_mismatch'
    products = [node for node in _schema_nodes(soup, url) if node.get('@type') == 'Product']
    headings = soup.select('.pdp h1')
    if len(products) != 1 or len(headings) != 1:
        return None, 'ozay_selected_product_missing'
    product, title = products[0], headings[0].get_text(' ', strip=True)
    brand = product.get('brand')
    gtin = retailer_gtin13(product.get('gtin'))
    barcodes, attrs, cash_prices = [], {}, []
    for row in soup.select('.pdp table.spec tr, .pdp .price-box .inst tr'):
        cells = row.find_all('td', recursive=False)
        if len(cells) != 2:
            continue
        label = WORDS(cells[0].get_text(' ', strip=True))
        value = cells[1].get_text(' ', strip=True)
        if label == 'ean':
            barcodes.append(retailer_gtin13(value))
        elif label == 'tek cekim':
            cash_prices.append(cash(value))
        else:
            attrs[label] = value
    evidence = _evidence(title, brand.get('name') if isinstance(brand, dict) else None, code, attrs,
                         product.get('image'), (OZAY_HOST,))
    skus = [node.get_text(' ', strip=True).removeprefix('Ürün kodu: ') for node in soup.select('.pdp .pdp-meta .mono')]
    if (not gtin or barcodes != [gtin] or not evidence or not product.get('sku') or skus != [product['sku']]
            or WORDS(str(product.get('name', ''))) != WORDS(title)):
        return None, 'ozay_selected_variant_unproven'
    offer = product.get('offers')
    if (not isinstance(offer, dict) or offer.get('@type') != 'Offer' or offer.get('url') != url
            or offer.get('priceCurrency') != 'TRY' or offer.get('availability') != 'https://schema.org/InStock'
            or offer.get('itemCondition') != 'https://schema.org/NewCondition'
            or any(key in offer for key in ('validForMemberTier', 'eligibleCustomerType', 'eligibleTransactionVolume'))):
        return None, 'ozay_new_stock_unproven'
    price = amount(offer.get('price'))
    prices = [cash(node.get_text(' ', strip=True)) for node in soup.select('.pdp .price-box .big')]
    if not price or prices != [price] or cash_prices != [price]:
        return None, 'ozay_selected_cash_unproven'
    stock = soup.select('.pdp .pdp-meta .chip-stock')
    cart = soup.select('.pdp .price-box form.buy-row[action="/sepet/ekle"][method="post"] button[type="submit"]')
    if (len(stock) != 1 or not re.fullmatch(r'stokta|son [1-9][0-9]* adet', WORDS(stock[0].get_text(' ', strip=True)))
            or len(cart) != 1 or WORDS(cart[0].get_text(' ', strip=True)) != 'sepete ekle'
            or cart[0].has_attr('disabled') or cart[0].get('aria-disabled') == 'true'):
        return None, 'ozay_selected_cart_unproven'
    return {**evidence, 'platform': 'Özay Bilgisayar', 'merchant': 'Özay Bilgisayar', 'merchant_id': OZAY_HOST,
            'seller_url': OZAY_SELLER, 'gtin13': gtin, 'price': float(price), 'currency': 'TRY',
            'condition': 'new', 'availability': 'in_stock', 'url': url, 'source_title': title,
            'observed_at': datetime.now(timezone.utc).isoformat()}, 'observed'


def overgame_seller_identity(html):
    soup = BeautifulSoup(html, 'html.parser')
    if [tag.get('href') for tag in soup.select('link[rel="canonical"]')] != [OVERGAME_SELLER]:
        return None
    fields = [WORDS(node.get_text(' ', strip=True)) for node in soup.select('p.col > span.row')]
    if (fields.count('firma adi eloy elektronik oyunlar san ve tic ltd sti') != 1
            or fields.count('vergi numarasi 3330702675') != 1):
        return None
    return {'_legal_name': 'eloy elektronik oyunlar sanayi ve ticaret limited sirketi', '_tax_key': '3330702675'}


def selected_overgame(html, url, code):
    soup = BeautifulSoup(html, 'html.parser')
    if [tag.get('href') for tag in soup.select('link[rel="canonical"]')] != [url]:
        return None, 'overgame_canonical_mismatch'
    products = [node for node in _schema_nodes(soup, url) if node.get('@type') == 'Product']
    headings = soup.select('h1')
    states = []
    for script in soup.find_all('script'):
        match = re.fullmatch(r"PRODUCT_DATA\.push\(JSON\.parse\('((?:\\.|[^'\\])*)'\)\);", script.get_text().strip())
        if match:
            try:
                states.append(json.loads(json.loads('"' + match[1].replace("\\'", "'") + '"')))
            except (ValueError, TypeError):
                return None, 'overgame_selected_state_invalid'
    if len(products) != 1 or len(headings) != 1 or len(states) != 1 or not isinstance(states[0], dict):
        return None, 'overgame_selected_product_missing'
    product, state, title = products[0], states[0], headings[0].get_text(' ', strip=True)
    brand = product.get('brand')
    gtin = retailer_gtin13(product.get('gtin13'))
    product_id = product.get('productID')
    evidence = _evidence(title, brand.get('name') if isinstance(brand, dict) else None, code, {},
                         product.get('image'), (OVERGAME_HOST,))
    if (not gtin or retailer_gtin13(state.get('barcode')) != gtin or not evidence
            or not isinstance(product_id, str) or not re.fullmatch(r'[1-9][0-9]{0,9}', product_id)
            or state.get('id') != product_id or state.get('url') != urlparse(url).path.removeprefix('/')
            or not product.get('sku') or product['sku'] != state.get('code') or state.get('subproduct_id') != 0
            or WORDS(state.get('name')) != WORDS(title) or WORDS(product.get('name')) != WORDS(title)):
        return None, 'overgame_selected_variant_unproven'
    offer = product.get('offers')
    if (not isinstance(offer, dict) or offer.get('@type') != 'Offer' or offer.get('url') != url
            or offer.get('priceCurrency') != 'TRY' or offer.get('availability') != 'https://schema.org/InStock'
            or offer.get('itemCondition') != 'https://schema.org/NewCondition' or state.get('available') is not True
            or type(state.get('quantity')) is not int or state['quantity'] < 1
            or any(key in offer for key in ('validForMemberTier', 'eligibleCustomerType', 'eligibleTransactionVolume'))):
        return None, 'overgame_new_stock_unproven'
    price = amount(offer.get('price'))
    displayed = [cash(node.get_text(' ', strip=True)) for node in soup.select('.mainPrices .discountPrice')]
    if (not price or state.get('currency_target') != 'TL' or amount(state.get('total_sale_price')) != price
            or displayed not in ([price], [price.quantize(1, rounding=ROUND_HALF_UP)])):
        return None, 'overgame_selected_cash_unproven'
    regular = amount(state.get('total_price'))
    old_prices = [cash(node.get_text(' ', strip=True)) for node in soup.select('.mainPrices .discountedPrice')]
    if (not regular or regular < price or (regular == price and old_prices)
            or (regular > price and (amount(state.get('total_base_price')) != regular
                                     or old_prices not in ([regular], [regular.quantize(1, rounding=ROUND_HALF_UP)])))):
        return None, 'overgame_selected_cash_unproven'
    cart = soup.select('#mobileBuyBtn #addCartBtn.addCartBtn')
    if (len(cart) != 1 or WORDS(cart[0].get_text(' ', strip=True)) != 'sepete ekle'
            or not cart[0].get('onclick', '').startswith(f'Add2Cart({product_id},')
            or cart[0].has_attr('disabled') or cart[0].get('aria-disabled') == 'true'):
        return None, 'overgame_selected_cart_unproven'
    return {**evidence, 'platform': 'OverGame', 'merchant': 'OverGame', 'merchant_id': OVERGAME_HOST,
            'seller_url': OVERGAME_SELLER, 'gtin13': gtin, 'price': float(price), 'currency': 'TRY',
            'condition': 'new', 'availability': 'in_stock', 'url': url, 'source_title': title,
            'observed_at': datetime.now(timezone.utc).isoformat()}, 'observed'


def gpn_seller_identity(html):
    soup = BeautifulSoup(html, 'html.parser')
    name = 'gpn bilgi teknolojileri sanayi ve ticaret anonim sirketi'
    if [tag.get('href') for tag in soup.select('link[rel="canonical"]')] != [GPN_SELLER]:
        return None
    fields = [WORDS(node.get_text(' ', strip=True)) for node in soup.select('p')]
    if (fields.count('ticaret unvani ' + name) != 1
            or sum(field.startswith('adres ') and 'vergi kimlik no 4110738196 ' in field for field in fields) != 1):
        return None
    return {'_legal_name': name, '_tax_key': '4110738196'}


def selected_gpn(html, url, code):
    soup = BeautifulSoup(html, 'html.parser')
    if [tag.get('href') for tag in soup.select('link[rel="canonical"]')] != [url]:
        return None, 'gpn_canonical_mismatch'
    sections = soup.select('#product-detail-container')
    if len(sections) != 1:
        return None, 'gpn_selected_product_missing'
    section = sections[0]
    headings = section.select('.product-title h1')
    titles = [node.get_text(' ', strip=True) for node in section.select('span[itemprop="name"]')]
    if len(headings) != 1 or len(titles) != 1 or WORDS(headings[0].get_text(' ', strip=True)) != WORDS(titles[0]):
        return None, 'gpn_selected_title_unproven'
    title = headings[0].get_text(' ', strip=True)
    barcodes = [retailer_gtin13(node.get('content')) for node in section.select('meta[itemprop="gtin13"]')]
    brands = [node.get('content') for node in section.select('[itemprop="brand"] meta[itemprop="name"]')]
    photo = section.select_one('#primary-image[src]')
    evidence = _evidence(title, brands[0] if len(brands) == 1 else None, code, {},
                         urljoin(url, photo['src']) if photo else None, (GPN_HOST,))
    if len(barcodes) != 1 or not barcodes[0] or not evidence:
        return None, 'gpn_selected_variant_unproven'
    sku = [node.get_text(' ', strip=True) for node in section.select('[itemprop="sku"]')]
    visible_sku = [row.select_one('.product-list-content').get_text(' ', strip=True)
                   for row in section.select('.product-list-row') if row.select_one('.product-list-title')
                   and WORDS(row.select_one('.product-list-title').get_text(' ', strip=True)) == 'stok kodu'
                   and row.select_one('.product-list-content')]
    if len(sku) != 1 or not sku[0] or visible_sku != sku:
        return None, 'gpn_selected_variant_unproven'
    offers = section.select('[itemprop="offers"][itemtype="https://schema.org/Offer"], [itemprop="offers"][itemtype="http://schema.org/Offer"]')
    if (len(offers) != 1 or [node.get('href') for node in offers[0].select('link[itemprop="availability"]')] != ['https://schema.org/InStock']
            or [node.get_text(' ', strip=True) for node in offers[0].select('[itemprop="url"]')] != [url]
            or [node.get('content') for node in offers[0].select('meta[itemprop="priceCurrency"]')] != ['TRY']
            or [node.get('content') for node in section.select('meta[itemprop="itemCondition"]')]
            not in (['http://schema.org/NewCondition'], ['https://schema.org/NewCondition'])):
        return None, 'gpn_new_stock_unproven'
    prices = [amount(node.get('content')) for node in offers[0].select('meta[itemprop="price"]')]
    box = section.select_one('.product-right .product-price')
    display = box.select('.product-price-new') or box.select('.product-price-old') if box else []
    if (len(prices) != 1 or not prices[0] or len(display) != 1
            or cash(display[0].get_text(' ', strip=True)) != prices[0]
            or [WORDS(node.get_text(' ', strip=True)) for node in box.select('.product-price-title')] != ['kdv dahil']):
        return None, 'gpn_selected_cash_unproven'
    cart = section.select('.product-right a.add-to-cart-button[data-selector="add-to-cart"][data-context="detail"]')
    ids = [node.get('data-product-id') for node in section.select('.product-detail-tab[data-product-id]')]
    if (len(cart) != 1 or not re.fullmatch(r'[1-9][0-9]{0,9}', cart[0].get('data-product-id', ''))
            or ids != [cart[0]['data-product-id']] or WORDS(cart[0].get_text(' ', strip=True)) != 'sepete ekle'
            or cart[0].has_attr('disabled') or cart[0].get('aria-disabled') == 'true'):
        return None, 'gpn_selected_cart_unproven'
    return {**evidence, 'platform': 'GPN Teknoloji', 'merchant': 'GPN Teknoloji', 'merchant_id': GPN_HOST,
            'seller_url': GPN_SELLER, 'gtin13': barcodes[0], 'price': float(prices[0]), 'currency': 'TRY',
            'condition': 'new', 'availability': 'in_stock', 'url': url, 'source_title': title,
            'observed_at': datetime.now(timezone.utc).isoformat()}, 'observed'


def avansas_seller_identity(html):
    soup = BeautifulSoup(html, 'html.parser')
    if [tag.get('href') for tag in soup.select('link[rel="canonical"]')] != [AVANSAS_SELLER]:
        return None
    fields = WORDS(soup.get_text(' ', strip=True))
    if 'avansas ofis malzemeleri tic a s' not in fields or 'vergi no 104 099 6132' not in fields:
        return None
    return {'_legal_name': 'avansas ofis malzemeleri ticaret anonim sirketi', '_tax_key': '1040996132'}


def selected_avansas(html, url, code):
    soup = BeautifulSoup(html, 'html.parser')
    if [tag.get('href') for tag in soup.select('link[rel="canonical"]')] != [url]:
        return None, 'avansas_canonical_mismatch'
    products = [node for node in _schema_nodes(soup, url) if node.get('@type') == 'Product']
    boxes = soup.select('.product-detail .product-summary .product-detail-content')
    if len(products) != 1 or len(boxes) != 1:
        return None, 'avansas_selected_product_missing'
    product, box = products[0], boxes[0]
    headings = [node.get_text(' ', strip=True) for node in box.select('h1.product-name')]
    title = unescape(str(product.get('name', '')))
    if len(headings) != 1 or WORDS(headings[0]) != WORDS(title):
        return None, 'avansas_selected_title_unproven'
    barcode = [retailer_gtin13(node.find_next_sibling('dd').get_text(strip=True))
               for node in box.select('dl.product-definitions dt') if WORDS(node.get_text()) == 'urun barkodu'
               and node.find_next_sibling('dd')]
    sku = urlparse(url).path.rsplit('-p-', 1)[-1]
    if len(barcode) != 1 or not barcode[0] or product.get('sku') != sku:
        return None, 'avansas_selected_variant_unproven'
    state = [node.get_text() for node in soup.find_all('script')
             if node.get_text().strip().startswith('window.initialProductDetailPageData = {')]
    if len(state) != 1:
        return None, 'avansas_selected_state_missing'
    fields = defaultdict(list)
    for key, value in re.findall(r"^\s*(code|stockLevelStatus|approvalStatus|formattedPrice): '([^'\r\n]*)',?\s*$", state[0], re.M):
        fields[key].append(value)
    offer = product.get('offers')
    if (not isinstance(offer, dict) or offer.get('@type') != 'Offer' or offer.get('url') != url
            or offer.get('priceCurrency') != 'TRY' or offer.get('availability') != 'https://schema.org/InStock'
            or offer.get('itemCondition') not in (None, 'https://schema.org/NewCondition')
            or fields['code'] != [sku] or fields['stockLevelStatus'] != ['inStock'] or fields['approvalStatus'] != ['APPROVED']):
        return None, 'avansas_selected_stock_unproven'
    attrs = {}
    for row in soup.select('#ProductReferencesTabComponent table tr'):
        cells = row.find_all('td', recursive=False)
        if len(cells) == 2:
            attrs[WORDS(cells[0].get_text(' ', strip=True))] = cells[1].get_text(' ', strip=True)
    brand = product.get('brand')
    images = [image for image in product.get('image', []) if isinstance(image, str)
              and code in code_key(urlparse(image).path.rsplit('/', 1)[-1])]
    evidence = _evidence(title, brand.get('name') if isinstance(brand, dict) else None, code, attrs,
                         images, ('cdnsta.avansas.com',))
    if not evidence or not (offer.get('itemCondition') or re.fullmatch(r'[1-9][0-9]* ay', WORDS(attrs.get('garanti')))):
        return None, 'avansas_new_variant_unproven'
    carts = box.select('add-to-cart-button[list-name="Product Page"][product-code="{{product.code}}"]')
    prices = [cash(node.get_text(' ', strip=True)) for node in box.select('.current-price .price-number')]
    tax_labels = [WORDS(node.get_text(' ', strip=True)) for node in box.select('.current-price .price-tax-included')]
    price = amount(offer.get('price'))
    if (not price or prices != [price] or [cash(value) for value in fields['formattedPrice']] != [price]
            or tax_labels != ['kdv dahil']
            or len(carts) != 1 or carts[0].has_attr('disabled') or carts[0].get('aria-disabled') == 'true'):
        return None, 'avansas_selected_cash_or_cart_unproven'
    return {**evidence, 'platform': 'Avansas', 'merchant': 'Avansas', 'merchant_id': AVANSAS_HOST,
            'seller_url': AVANSAS_SELLER, 'gtin13': barcode[0], 'price': float(price), 'currency': 'TRY',
            'condition': 'new', 'availability': 'in_stock', 'url': url, 'source_title': title,
            'observed_at': datetime.now(timezone.utc).isoformat()}, 'observed'


def ensonu_seller_identity(html):
    soup = BeautifulSoup(html, 'html.parser')
    if [tag.get('href') for tag in soup.select('link[rel="canonical"]')] != [ENSONU_SELLER]:
        return None
    paragraphs = soup.select('main #content .row-main .col-inner > p')
    seller = [node.find_next_sibling('p') for node in paragraphs
              if WORDS(node.get_text(' ', strip=True)) == '1 1 satici']
    if len(seller) != 1 or not seller[0]:
        return None
    name = WORDS(seller[0].get_text(' ', strip=True))
    if not name.startswith('unvani ahsen teknoloji san tic ltd sti adresi '):
        return None
    return {'_legal_name': 'ahsen teknoloji sanayi ve ticaret limited sirketi'}


def rampage_seller_identity(html):
    soup = BeautifulSoup(html, 'html.parser')
    if [tag.get('href') for tag in soup.select('link[rel="canonical"]')] != [RAMPAGE_SELLER]:
        return None
    fields = [WORDS(tag.get_text(' ', strip=True)) for tag in soup.select('main .page.rte > p strong')]
    if (fields.count('title segment computer foreign trade joint stock company') != 1
            or fields.count('mersis number 0758062367100001') != 1):
        return None
    return {'_legal_name': 'segment bilgisayar dis ticaret anonim sirketi'}


def compazar_seller_identity(html):
    soup = BeautifulSoup(html, 'html.parser')
    if [tag.get('href') for tag in soup.select('link[rel="canonical"]')] != [COMPAZAR_SELLER]:
        return None
    for table in soup.select('main table'):
        fields = [WORDS(row.get_text(' ', strip=True)) for row in table.select('tr')]
        if ('satici compazar' in fields and 'web sitesi https www compazar com' in fields
                and 'vergi dairesi vergi no mecidiyekoy 1031526956' in fields):
            return {'_legal_name': 'compazar', '_tax_key': '1031526956'}
    return None


def nethouse_seller_identity(html):
    soup = BeautifulSoup(html, 'html.parser')
    if [tag.get('href') for tag in soup.select('link[rel="canonical"]')] != [NETHOUSE_SELLER]:
        return None
    names = soup.select('#divCenterBlock .wpb_wrapper > p:first-child > strong')
    expected = 'nethouse bilgisayar sistemleri pazarlama sanayi ve ticaret limited sirketi'
    if [WORDS(node.get_text(' ', strip=True)) for node in names] != [expected]:
        return None
    return {'_legal_name': expected}


def _english_lira(text, pattern):
    match = re.fullmatch(pattern, text)
    return amount(match[1].replace(',', '')) if match else None


def _enabled_purchase(button, *labels):
    return (not button.has_attr('disabled') and 'disabled' not in button.get('class', [])
            and button.get('aria-disabled', '').lower() != 'true'
            and WORDS(button.get_text(' ', strip=True)) in labels)


def selected_ensonu(html, url, code):
    soup = BeautifulSoup(html, 'html.parser')
    if [tag.get('href') for tag in soup.select('link[rel="canonical"]')] != [url]:
        return None, 'ensonu_canonical_mismatch'
    products = [node for node in _schema_nodes(soup, url) if node.get('@type') == 'Product']
    boxes = soup.select('main .product-info.summary')
    descriptions = soup.select('main .product-page-sections .product-section .panel.entry-content')
    if len(products) != 1 or len(boxes) != 1 or not descriptions:
        return None, 'ensonu_selected_product_missing'
    product, box = products[0], boxes[0]
    headings = box.select('h1.product_title')
    title = headings[0].get_text(' ', strip=True) if len(headings) == 1 else ''
    detail = ' '.join(node.get_text(' ', strip=True) for node in descriptions)
    gtin = retailer_gtin13(product.get('gtin'))
    category = box.select(f'.woocommerce-breadcrumb a[href="{ENSONU_CATEGORY}"]')
    evidence = _evidence(title, WORDS(title).split(' ')[0], code, {}, product.get('image'), (ENSONU_HOST,),
                         category_verified=len(category) == 1)
    if (not evidence or not category or product.get('url') != url
            or WORDS(product.get('name')) != WORDS(title)
            or not gtin or re.findall(r'\bEAN\s*:\s*(\d{8,14})\b', detail, re.I) != [gtin]):
        return None, 'ensonu_selected_variant_unproven'
    offers = product.get('offers')
    offer = offers[0] if isinstance(offers, list) and len(offers) == 1 and isinstance(offers[0], dict) else {}
    seller = offer.get('seller')
    stock = [WORDS(node.get_text(' ', strip=True)) for node in box.select('p.stock.in-stock')]
    warranty = re.search(r'\bgaranti(?:si|li)?\b', WORDS(detail))
    if (offer.get('@type') != 'Offer' or offer.get('url') != url
            or not isinstance(seller, dict) or seller.get('name') != 'EnSonu'
            or seller.get('url') != f'https://{ENSONU_HOST}'
            or offer.get('availability') != 'https://schema.org/InStock' or stock != ['stokta']
            or not warranty or re.search(r'\b(?:yenilenmis urun|ikinci el urun|teshir urunu|refurbished)\b', WORDS(detail))
            or any(node.get('itemCondition', 'https://schema.org/NewCondition') != 'https://schema.org/NewCondition'
                   for node in (product, offer))):
        return None, 'ensonu_new_stock_unproven'
    specs = offer.get('priceSpecification')
    spec = specs[0] if isinstance(specs, list) and len(specs) == 1 and isinstance(specs[0], dict) else {}
    price = amount(spec.get('price'))
    visible = [node.get_text(' ', strip=True) for node in box.select('p.product-page-price')]
    single_payment = [cells[1].get_text(' ', strip=True) for row in box.select('table tr')
                      if len(cells := row.find_all('td', recursive=False)) == 2
                      and WORDS(cells[0].get_text(' ', strip=True)) == 'banka ve kredi karti ile odeme tek cekim']
    forms = box.select('form.cart')
    carts = forms[0].select('button.single_add_to_cart_button[name="add-to-cart"]') if len(forms) == 1 else []
    lira = r'₺\s*((?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)\.[0-9]{2})'
    if (not price or spec.get('@type') != 'UnitPriceSpecification' or spec.get('priceCurrency') != 'TRY'
            or spec.get('valueAddedTaxIncluded') is not True
            or any(key in node for node in (offer, spec) for key in (
                'validForMemberTier', 'eligibleCustomerType', 'eligibleTransactionVolume', 'eligibleQuantity'))
            or [_english_lira(text, lira) for text in visible] != [price]
            or [_english_lira(text, lira) for text in single_payment] != [price]
            or len(carts) != 1 or forms[0].get('action') != url or forms[0].get('method') != 'post'
            or not re.fullmatch(r'[1-9][0-9]{0,12}', carts[0].get('value', ''))
            or not _enabled_purchase(carts[0], 'sepete ekle')):
        return None, 'ensonu_selected_cash_or_cart_unproven'
    return {**evidence, 'platform': 'EnSonu', 'merchant': 'EnSonu', 'merchant_id': ENSONU_HOST,
            'seller_url': ENSONU_SELLER, 'gtin13': gtin, 'price': float(price), 'currency': 'TRY',
            'condition': 'new', 'availability': 'in_stock', 'url': url, 'source_title': title,
            'observed_at': datetime.now(timezone.utc).isoformat()}, 'observed'


def selected_rampage(html, url, code):
    soup = BeautifulSoup(html, 'html.parser')
    if [tag.get('href') for tag in soup.select('link[rel="canonical"]')] != [url]:
        return None, 'rampage_canonical_mismatch'
    roots = [node for node in _schema_nodes(soup, url) if node.get('@type') in ('Product', 'ProductGroup')]
    boxes = soup.select('main product-info.product__info')
    if len(roots) != 1 or len(boxes) != 1 or roots[0].get('url') != url:
        return None, 'rampage_selected_product_missing'
    root, box = roots[0], boxes[0]
    forms = box.select('form.product-form[action="/en/cart/add"][method="post"]')
    variant_ids = [node.get('value') for node in forms[0].select('input[name="id"]')] if len(forms) == 1 else []
    if len(variant_ids) != 1 or not re.fullmatch(r'[1-9][0-9]{0,19}', variant_ids[0] or ''):
        return None, 'rampage_selected_variant_missing'
    selected_url = f'{url}?variant={variant_ids[0]}'
    variants = root.get('hasVariant') if root.get('@type') == 'ProductGroup' else [root]
    selected = [node for node in variants if isinstance(node, dict) and node.get('@type') == 'Product'
                and isinstance(node.get('offers'), dict) and node['offers'].get('url') == selected_url
                ] if isinstance(variants, list) else []
    if len(selected) != 1:
        return None, 'rampage_selected_variant_unproven'
    product = selected[0]
    offer, gtin, brand = product['offers'], retailer_gtin13(product.get('gtin')), root.get('brand')
    title = product.get('name')
    headings = box.select('.product__title h1')
    heading = WORDS(headings[0].get_text(' ', strip=True)) if len(headings) == 1 else ''
    image = product.get('image')
    evidence = _evidence(title, brand.get('name') if isinstance(brand, dict) else None, code, {},
                         image.split('?', 1)[0] if isinstance(image, str) else None, (RAMPAGE_HOST,),
                         category_verified=bool(re.search(r'\bmonitor\b', heading)))
    if (not evidence or evidence['facts']['brand'] != 'Rampage' or not gtin
            or not heading or heading not in WORDS(title) or NOT_MONITOR.search(WORDS(title))
            or re.search(r'\b(?:open box|used|renewed|second hand)\b', WORDS(title))):
        return None, 'rampage_selected_variant_unproven'
    price = amount(offer.get('price'))
    visible = [node.get_text(' ', strip=True) for node in box.select('.product__title .product__price')]
    carts = forms[0].select('button[type="submit"][name="add"]')
    lira = r'((?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)\.[0-9]{2})\s*TL'
    if (offer.get('@type') != 'Offer' or offer.get('priceCurrency') != 'TRY'
            or offer.get('availability') not in ('http://schema.org/InStock', 'https://schema.org/InStock')
            or any(node.get('itemCondition', 'https://schema.org/NewCondition') not in
                   ('http://schema.org/NewCondition', 'https://schema.org/NewCondition') for node in (product, offer))
            or any(key in offer for key in ('validForMemberTier', 'eligibleCustomerType',
                                            'eligibleTransactionVolume', 'priceSpecification'))
            or not price or [_english_lira(text, lira) for text in visible] != [price]
            or len(carts) != 1 or not forms[0].get('id') or carts[0].get('form') != forms[0]['id']
            or not _enabled_purchase(carts[0], 'sepete ekle', 'add to cart')):
        return None, 'rampage_selected_cash_or_cart_unproven'
    return {**evidence, 'platform': 'Rampage', 'merchant': 'Rampage', 'merchant_id': RAMPAGE_HOST,
            'seller_url': RAMPAGE_SELLER, 'gtin13': gtin, 'price': float(price), 'currency': 'TRY',
            'condition': 'new', 'availability': 'in_stock', 'url': url, 'source_title': title,
            'observed_at': datetime.now(timezone.utc).isoformat()}, 'observed'


def selected_compazar(html, url, code):
    soup = BeautifulSoup(html, 'html.parser')
    if [tag.get('href') for tag in soup.select('link[rel="canonical"]')] != [url]:
        return None, 'compazar_canonical_mismatch'
    products = [node for node in _schema_nodes(soup, url) if node.get('@type') == 'Product']
    boxes = soup.select('main.vb-hb-info-column')
    if len(products) != 1 or len(boxes) != 1 or products[0].get('url') != url:
        return None, 'compazar_selected_product_missing'
    product, box = products[0], boxes[0]
    fields = defaultdict(list)
    for node in soup.select('.vb-product-feature-row > div'):
        label, value = node.select(':scope > span'), node.select(':scope > strong')
        if len(label) == len(value) == 1:
            fields[WORDS(label[0].get_text(' ', strip=True))].append(value[0].get_text(' ', strip=True))
    title, brand = product.get('name'), product.get('brand')
    brand_name = brand.get('name') if isinstance(brand, dict) else None
    headings = [WORDS(node.get_text(' ', strip=True)) for node in box.select('.vb-hb-product-title')]
    gtin, sku = retailer_gtin13(product.get('gtin13')), product.get('sku')
    evidence = _evidence(title, brand_name, code, {}, product.get('image'), (COMPAZAR_HOST,), category_verified=True)
    if (not evidence or headings != [WORDS(title)] or not gtin
            or [retailer_gtin13(value) for value in fields['barkod']] != [gtin]
            or not isinstance(sku, str) or fields['urun kodu'] != [sku]
            or list(map(WORDS, fields['kategori'])) != ['monitorler']
            or list(map(WORDS, fields['marka'])) != [WORDS(brand_name)]):
        return None, 'compazar_selected_variant_unproven'
    offer = product.get('offers')
    seller = offer.get('seller') if isinstance(offer, dict) else None
    if (not isinstance(seller, dict) or seller.get('name') != 'Compazar'
            or [WORDS(node.get_text(' ', strip=True)) for node in box.select('.vb-hb-seller-line strong')] != ['compazar']
            or offer.get('@type') != 'Offer' or offer.get('url') != url or offer.get('priceCurrency') != 'TRY'
            or offer.get('availability') != 'https://schema.org/InStock'
            or offer.get('itemCondition') != 'https://schema.org/NewCondition'
            or list(map(WORDS, fields['stok durumu'])) != ['stokta var']
            or list(map(WORDS, fields['satis durumu'])) != ['online satisa acik']):
        return None, 'compazar_new_stock_unproven'
    forms = box.select('form.vb-hb-cart-form')
    prices = [cash(node.get_text(' ', strip=True)) for node in box.select('.vb-hb-main-price-wrap > strong')]
    mobile = [cash(node.get_text(' ', strip=True)) for node in soup.select('.vb-mobile-sticky-buy-price > strong')]
    old_price = [cash(node.get_text(' ', strip=True)) for node in box.select('.vb-hb-price-line > del')]
    taxes = [WORDS(node.get_text(' ', strip=True)) for node in box.select('.vb-hb-main-price-wrap > span')]
    if len(forms) != 1 or len(prices) != 1 or not prices[0] or mobile != prices or taxes != ['kdv dahil']:
        return None, 'compazar_selected_cash_unproven'
    price, form = prices[0], forms[0]
    structured = amount(offer.get('price'))
    if (not structured or (structured != price and not (structured > price and old_price == [structured]))
            or any(key in offer for key in ('validForMemberTier', 'eligibleCustomerType',
                                            'eligibleTransactionVolume', 'priceSpecification'))
            or amount(form.get('data-product-price')) != price or form.get('data-product-currency') != 'TRY'
            or form.get('data-product-code') != sku or WORDS(form.get('data-product-title')) != WORDS(title)
            or WORDS(form.get('data-product-category')) != 'monitorler'
            or form.get('action') != f'https://{COMPAZAR_HOST}/sepet/ekle' or form.get('method', '').lower() != 'post'):
        return None, 'compazar_selected_cash_unproven'
    ids = [node.get('value') for node in form.select('input[name="product_id"]')]
    quantity = form.select('input[name="quantity"][min="1"][value="1"]')
    stock = re.fullmatch(r'([1-9][0-9]*) adet', WORDS(fields['stok miktari'][0])) if len(fields['stok miktari']) == 1 else None
    carts = form.select('button[type="submit"][data-action="add-cart"]')
    if (len(ids) != 1 or not re.fullmatch(r'[1-9][0-9]{0,12}', ids[0] or '') or ids != [form.get('data-product-id')]
            or len(quantity) != 1 or not stock or quantity[0].get('max') != stock[1]
            or len(carts) != 1 or not _enabled_purchase(carts[0], 'sepete ekle')):
        return None, 'compazar_selected_cart_unproven'
    return {**evidence, 'platform': 'Compazar', 'merchant': 'Compazar', 'merchant_id': COMPAZAR_HOST,
            'seller_url': COMPAZAR_SELLER, 'gtin13': gtin, 'price': float(price), 'currency': 'TRY',
            'condition': 'new', 'availability': 'in_stock', 'url': url, 'source_title': title,
            'observed_at': datetime.now(timezone.utc).isoformat()}, 'observed'


def selected_nethouse(html, url, code):
    soup = BeautifulSoup(html, 'html.parser')
    if [tag.get('href') for tag in soup.select('link[rel="canonical"]')] != [url]:
        return None, 'nethouse_canonical_mismatch'
    products = [node for node in _schema_nodes(soup, url) if node.get('@type') == 'Product']
    boxes = soup.select('#ProductDetailMain')
    states = []
    for script in soup.select('script:not([src])'):
        raw = script.get_text()
        for match in re.finditer(r'(?:^|\n)\s*var productDetailModel\s*=\s*(?=\{)', raw):
            try:
                state, end = json.JSONDecoder().raw_decode(raw, match.end())
            except ValueError:
                return None, 'nethouse_selected_state_invalid'
            if not raw[end:].lstrip().startswith(';'):
                return None, 'nethouse_selected_state_invalid'
            states.append(state)
    if len(products) != 1 or len(boxes) != 1 or len(states) != 1:
        return None, 'nethouse_selected_product_missing'
    product, box, state = products[0], boxes[0], states[0]
    selected = state.get('product')
    if not isinstance(selected, dict):
        return None, 'nethouse_selected_variant_missing'
    title, brand = product.get('name'), product.get('brand')
    brand_name = brand.get('name') if isinstance(brand, dict) else None
    sku = product.get('sku')
    headings = [WORDS(node.get_text(' ', strip=True).removesuffix(f' ({sku})'))
                for node in box.select('.ProductName h1')]
    gtin = retailer_gtin13(selected.get('barkod'))
    visible_gtins = [retailer_gtin13(node.get_text(' ', strip=True)) for node in box.select('#spnBarkod')]
    category = state.get('breadCrumb')
    monitor_category = isinstance(category, list) and any(
        isinstance(node, dict) and node.get('urlKod') == '/monitor'
        and node.get('id') == state.get('productCategoryId') for node in category)
    evidence = _evidence(title, brand_name, code, {}, product.get('image'), (NETHOUSE_IMAGE_HOST,),
                         category_verified=monitor_category)
    if (not monitor_category or not evidence or not gtin or visible_gtins != [gtin] or retailer_gtin13(sku) != gtin
            or state.get('stockCode') != sku or selected.get('stokKodu') != sku
            or headings != [WORDS(title)] or WORDS(state.get('productName')) != WORDS(title)
            or WORDS(selected.get('urunAdi')) != WORDS(title) or WORDS(state.get('brandName')) != WORDS(brand_name)
            or type(state.get('mainVariantId')) is not int or state['mainVariantId'] <= 0
            or state['mainVariantId'] != selected.get('id')
            or type(state.get('productId')) is not int or state['productId'] <= 0
            or state['productId'] != selected.get('urunKartiId') or selected.get('anaUrun') is not True
            or state.get('productIsAsorti') is not False or state.get('isPackageProduct') is not False):
        return None, 'nethouse_selected_variant_unproven'
    offer = product.get('offers')
    seller = offer.get('seller') if isinstance(offer, dict) else None
    stock = selected.get('stokAdedi')
    if (not isinstance(seller, dict) or WORDS(seller.get('name')) != 'nethouse'
            or offer.get('@type') != 'Offer' or offer.get('url') != url
            or offer.get('itemCondition') not in ('NewCondition', 'https://schema.org/NewCondition')
            or offer.get('availability') not in ('InStock', 'https://schema.org/InStock')
            or state.get('productActive') is not True or selected.get('aktif') is not True
            or type(stock) not in (float, int) or not isfinite(stock) or stock < 1):
        return None, 'nethouse_new_stock_unproven'
    price = amount(offer.get('price'))
    normal = [node.get_text(' ', strip=True) for node in box.select('#divIndirimsizFiyat #fiyat2')]
    single = [node.get_text(' ', strip=True) for node in soup.select('#divTekCekimFiyati #ltrTekCekim')]
    lira = r'₺\s*((?:[0-9]{1,3}(?:\.[0-9]{3})+|[0-9]+),[0-9]{2})'
    def read_lira(raw):
        match = re.fullmatch(lira, raw)
        return cash(match[1] + ' TL') if match else None
    carts = box.select('#divSatinAl input.btnAddBasketOnDetail[type="button"][value="Sepete Ekle"]')
    if (not price or str(offer.get('priceCurrency')).upper() != 'TRY' or state.get('productCurrency') != 'TRY'
            or selected.get('paraBirimiKodu') != 'TRY' or state.get('fiyatGoster') is not True
            or amount(state.get('productPriceStr')) != price
            or selected.get('urunKampanyaAktif') is not False
            or any(key in offer for key in ('validForMemberTier', 'eligibleCustomerType',
                                            'eligibleTransactionVolume', 'priceSpecification'))
            or [read_lira(text.removesuffix(' (KDV Dahil)')) for text in normal if text.endswith(' (KDV Dahil)')] != [price]
            or len(normal) != 1 or [read_lira(text) for text in single] != [price] or len(carts) != 1
            or any(node.has_attr('hidden') or node.has_attr('disabled') or node.get('aria-disabled') == 'true'
                   or re.search(r'display\s*:\s*none|visibility\s*:\s*hidden', node.get('style', ''), re.I)
                   for node in (carts[0], *carts[0].parents) if node is not box)):
        return None, 'nethouse_selected_cash_or_cart_unproven'
    return {**evidence, 'platform': 'Nethouse', 'merchant': 'Nethouse', 'merchant_id': NETHOUSE_HOST,
            'seller_url': NETHOUSE_SELLER, 'gtin13': gtin, 'price': float(price), 'currency': 'TRY',
            'condition': 'new', 'availability': 'in_stock', 'url': url, 'source_title': title,
            'observed_at': datetime.now(timezone.utc).isoformat()}, 'observed'


def selected_pckolik(html, url, code):
    soup = BeautifulSoup(html, 'html.parser')
    if [tag.get('href') for tag in soup.select('link[rel="canonical"]')] != [url]:
        return None, 'pckolik_canonical_mismatch'
    nodes = _schema_nodes(soup, url)
    products = [node for node in nodes if node.get('@type') == 'Product']
    headings = soup.select('main h1')
    if len(products) != 1 or len(headings) != 1:
        return None, 'pckolik_selected_product_missing'
    product = products[0]
    title = headings[0].get_text(' ', strip=True)
    brand = product.get('brand')
    gtin = retailer_gtin13(product.get('gtin13'))
    category_verified = any(isinstance(item, dict) and item.get('item') in {
        'https://pckolik.com.tr/monitorler', 'https://pckolik.com.tr/oyuncu-monitorleri'
    } for node in nodes if node.get('@type') == 'BreadcrumbList' for item in node.get('itemListElement') or [])
    attrs, barcodes, stock_codes = {}, [], []
    for row in soup.select('main table tr'):
        cells = row.find_all('td', recursive=False)
        if len(cells) != 2:
            continue
        label, value = WORDS(cells[0].get_text(' ', strip=True)), cells[1].get_text(' ', strip=True)
        if label == 'barkod':
            barcodes.append(retailer_gtin13(value))
        elif label == 'stok kodu':
            stock_codes.append(code_key(value))
        elif label in ('monitor cozunurluk', 'tazeleme hizi', 'ekran boyutu', 'panel'):
            attrs[{'monitor cozunurluk': 'cozunurluk', 'tazeleme hizi': 'yenileme hizi',
                   'panel': 'panel tipi'}.get(label, label)] = value
    evidence = _evidence(title, brand.get('name') if isinstance(brand, dict) else None, code, attrs,
                         product.get('image'), (PCKOLIK_HOST,), category_verified=category_verified)
    if (not gtin or barcodes != [gtin] or not evidence
            or WORDS(unescape(str(product.get('name', '')))) != WORDS(title)
            or len(stock_codes) != 1 or not stock_codes[0]
            or stock_codes[0] not in {code_key(product.get('mpn')), code_key(product.get('sku'))}):
        return None, 'pckolik_selected_variant_unproven'
    offer = product.get('offers')
    if (not isinstance(offer, dict) or offer.get('@type') != 'Offer' or offer.get('url') != url
            or offer.get('priceCurrency') != 'TRY' or offer.get('availability') != 'https://schema.org/InStock'
            or offer.get('itemCondition') != 'https://schema.org/NewCondition'
            or any(key in offer for key in ('validForMemberTier', 'eligibleCustomerType', 'eligibleTransactionVolume'))):
        return None, 'pckolik_new_stock_unproven'
    box = soup.select(r'main div[class~="lg:col-start-3"][class~="lg:row-start-1"]')
    prices = box[0].select('.price-now') if len(box) == 1 else []
    carts = [button for button in box[0].select('button') if WORDS(button.get_text(' ', strip=True)) == 'sepete ekle'] if len(box) == 1 else []
    cash_labels = [cash(text.removeprefix('Tek çekim:').strip()) for node in soup.select('main section p')
                   if (text := node.get_text(' ', strip=True)).startswith('Tek çekim:')]
    price = amount(offer.get('price'))
    if (not price or len(prices) != 1 or cash(prices[0].get_text(' ', strip=True)) != price
            or cash_labels != [price] or len(carts) != 1 or carts[0].has_attr('disabled')
            or carts[0].get('aria-disabled') == 'true'):
        return None, 'pckolik_selected_cash_or_cart_unproven'
    return {**evidence, 'platform': 'PcKolik', 'merchant': 'PcKolik', 'merchant_id': PCKOLIK_HOST,
            'seller_url': PCKOLIK_SELLER, 'gtin13': gtin, 'price': float(price), 'currency': 'TRY',
            'condition': 'new', 'availability': 'in_stock', 'url': url, 'source_title': title,
            'observed_at': datetime.now(timezone.utc).isoformat()}, 'observed'


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
           'www.teknosa.com': selected_teknosa, 'www.hepsiburada.com': selected_hepsiburada,
           PCKOLIK_HOST: selected_pckolik, ASEMPI_HOST: selected_asempi, OZAY_HOST: selected_ozay,
           OVERGAME_HOST: selected_overgame, GPN_HOST: selected_gpn, AVANSAS_HOST: selected_avansas,
           ENSONU_HOST: selected_ensonu, RAMPAGE_HOST: selected_rampage, COMPAZAR_HOST: selected_compazar,
           NETHOUSE_HOST: selected_nethouse,
           **dict.fromkeys(DIRECT_MONITOR_STORES, selected_direct)}
