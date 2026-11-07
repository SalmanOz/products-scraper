"""Selected offers for exact new laptop configurations from public retailer pages."""
from datetime import datetime, timezone
from html import unescape
import re
from urllib.parse import urlparse, urljoin

from bs4 import BeautifulSoup

from mediamarkt_source import _selected_offer as media_offer
from mediamarkt_marketplace_audit import selected_marketplace_offer
from n11_catalog_source import _parse_selected_listing as n11_offer
from retailer_barcode import retailer_gtin13
from retailer_offer_refresh import _vatan_review_jsonld
from teknosa_marketplace_source import _selected_marketplace_commerce
from trendyol_catalog_source import _parse_selected_listing as trendyol_offer
from tr_price_scraper import TRPriceScraper
from tws_offer_pilot import _selected_group_root
from tws_retailer_sources import selected_vatan_tws

BRANDS = {brand.casefold(): brand for brand in
          ('Lenovo', 'ASUS', 'Apple', 'MSI', 'Acer', 'Dell', 'HP', 'Huawei', 'Casper', 'Gigabyte', 'Monster')}
MODEL_PATTERNS = {
    'Lenovo': r'(?:8[1-3]|2[01])[A-Z0-9]{6}(?:TR|TX)',
    'ASUS': r'[A-Z]{1,3}\d{3,4}[A-Z0-9]{0,4}-[A-Z]{1,3}\d{2,5}[A-Z0-9]{0,3}',
    'Acer': r'N[HX]\.[A-Z0-9]{5}\.\d{3}',
    'Apple': r'M[A-Z0-9]{4}(?:TU|ZE|LL)/A',
    'HP': r'(?=[A-Z0-9]*\d)[A-Z0-9]{5}EA(?:#AB8)?',
    'MSI': r'[AB]\d{2}[A-Z0-9]{2,9}-\d{2,4}X?TR',
    'Dell': r'(?:[A-Z]{1,3}\d{5}-[A-Z]{2,5}-\d{2,4}-[A-Z]|[A-Z]{0,3}\d{4}[A-Z]{2,6}\d{2,5})',
}
BAD = re.compile(r'\b(?:yenilenmis|ikinci el|outlet|teshir|refurbished|renewed|open box|upgrade|yukselt(?:ilmis|meli))\b')
WORDS = TRPriceScraper._normalize_words
LAPTOP_LABEL = re.compile(r'\b(?:laptop|notebook|macbook|dizustu|tasinabilir bilgisayar)\b')
RAM_SIZES = (4, 8, 12, 16, 18, 24, 32, 36, 48, 64, 96, 128)
GPU = r'(RTX|GTX|RX|ARC)\s*(?:TM\s*)?(\d{3,4}[A-Z]{0,2})(\s+TI\b)?'


def _brand(value):
    return BRANDS.get(str(value or '').strip().casefold())


def model_codes(value, brand=None):
    patterns = [MODEL_PATTERNS.get(brand, '')] if brand else MODEL_PATTERNS.values()
    return set().union(*(set(re.findall(r'(?<![A-Z0-9])(?:' + pattern + r')(?![A-Z0-9#-])',
                                       str(value or '').upper())) for pattern in patterns if pattern))


def model_code(title, brand, native=''):
    native_codes = model_codes(native, brand)
    title_codes = model_codes(title, brand)
    if native_codes and title_codes and native_codes != title_codes:
        return None
    found = native_codes or title_codes
    return next(iter(found)) if len(found) == 1 else None


def _gb(text):
    match = re.fullmatch(r'(\d{1,4})\s*(GB|TB)', str(text or '').strip().upper())
    return int(match[1]) * (1024 if match[2] == 'TB' else 1) if match else None


def _one(values):
    return next(iter(values)) if len(values) == 1 else None


def _cpu(text):
    text = WORDS(text).upper()
    for pattern, fmt in [
        (r'\b(?:INTEL )?(?:CORE )?ULTRA\s*([579])\s*(\d{3}[A-Z]{0,3})\b', 'Intel Core Ultra {} {}'),
        (r'\b(?:INTEL )?CORE\s*(?:U\s*)?([357])\s*(\d{3}[A-Z]{0,3})\b', 'Intel Core {} {}'),
        (r'\bI([3579])[- ]?(\d{4,5}[A-Z]{0,3})\b', 'Intel Core i{}-{}'),
        (r'\b(?:AMD )?RYZEN AI\s*([579])\s*(\d{3}[A-Z]{0,3})\b', 'AMD Ryzen AI {} {}'),
        (r'\b(?:AMD )?(?:RYZEN\s*|R)([3579])\s*(\d{3,4}[A-Z]{0,3})\b', 'AMD Ryzen {} {}'),
        (r'\b(CELERON|PENTIUM)\s+([A-Z]\d{3,4}[A-Z]?)\b', 'Intel {} {}'),
        (r'\bINTEL\s+(N\d{2,3})\b', 'Intel {}'),
        (r'\b(M[1-9])(?:\s+(PRO|MAX|ULTRA)\b)?', 'Apple {} {}'),
        (r'\bSNAPDRAGON\s+X\s+(PLUS|ELITE)\s+(X\d[A-Z]?\s+\d{2}\s+\d{3})\b', 'Snapdragon X {} {}'),
    ]:
        if match := re.search(pattern, text):
            return fmt.format(*(x or '' for x in match.groups())).strip()
    return None


def _os(text):
    normalized = WORDS(text)
    if re.search(r'\b(?:free\s?dos|fdos|isletim sistemi yok|no os|os yok|without os)\b', normalized):
        return 'FreeDOS'
    if re.search(r'\bubuntu\b', normalized):
        return 'Ubuntu'
    if re.search(r'\b(?:macos|mac os)\b', normalized):
        return 'macOS'
    if re.search(r'\b(?:windows\s*11|win\s*11|w11)[hp]?\b', normalized):
        if re.search(r'\b(?:windows\s*11\s*pro|win\s*11\s*pro|w11\s*pro|w11p|win11p)\b', normalized):
            return 'Windows 11 Pro'
        if re.search(r'\b(?:home|w11h|win11h)\b', normalized):
            return 'Windows 11 Home'
        return 'Windows 11'
    return None


def _label(attributes, *keys):
    return next((attributes.get(key) for key in keys if attributes.get(key)), '')


def _attrs(rows, *, trendyol=False):
    result, collisions = {}, set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        key, value = row.get('key'), row.get('value')
        if trendyol:
            key = key.get('name') if isinstance(key, dict) else None
            value = value.get('name') if isinstance(value, dict) else None
        if isinstance(key, str) and isinstance(value, str) and value.strip():
            key = WORDS(key)
            if key in result and result[key] != value.strip():
                collisions.add(key)
            result[key] = value.strip()
    return {k: v for k, v in result.items() if k not in collisions}


def _photo(url, *hosts):
    if not isinstance(url, str):
        return None
    p = urlparse(url)
    return url if (p.scheme == 'https' and p.netloc in hosts and not p.username
                   and not p.query and not p.fragment and len(url) <= 500) else None


def _size(value):
    match = re.search(r'\b(1[0-8])(?:[.,](\d))?\s*(?:"|inç|inch|inc|″)', str(value or ''), re.I)
    return (match[1] + ('.' + match[2] if match[2] else '')) if match else None


def laptop_facts(title, attributes, brand, native=''):
    if not brand or not isinstance(title, str) or BAD.search(WORDS(title)):
        return None
    mpn = model_code(title, brand, native)
    if not mpn:
        return None
    label = lambda *keys: _label(attributes, *keys)
    raw_cpu = label('islemci modeli', 'islemci numarasi')
    cpu_attr = _cpu(f"{label('islemci tipi', 'islemci', 'islemci teknolojisi')} {raw_cpu}") or _cpu(raw_cpu)
    cpu_title = _cpu(title)
    if cpu_attr and cpu_title and cpu_attr != cpu_title:
        return None
    cpu = cpu_attr or cpu_title
    ram_attr = _gb(label('bellek kapasitesi', 'ram sistem bellegi', 'ram bellek', 'ram'))
    drive_type = WORDS(label('disk turu'))
    ssd_attr = _gb(label('ssd kapasitesi', 'ssd', 'disk kapasitesi')) if drive_type not in ('hdd', 'hard disk') else None
    upper_title = title.upper()
    drives = {int(value) * (1024 if unit == 'TB' else 1)
              for value, unit in re.findall(r'\b(\d{1,4})\s*(GB|TB)?\s*SSD\b', upper_title)}
    ssd_title = _one(drives)
    explicit_ram = {int(value) for value in re.findall(r'\b(\d{1,3})\s*GB\s*(?:RAM|LPDDR[345X]*|DDR[345])\b', upper_title)}
    # A GPU's immediately following capacity describes its VRAM, unless it is explicitly system RAM.
    without_vram = re.sub(r'\b' + GPU + r'[- ]*\d{1,2}\s*GB\b(?!\s*(?:RAM|LPDDR|DDR))',
                          '', WORDS(title).upper())
    unlabelled_ram = {int(value) for value in re.findall(
        r'\b(\d{1,3})\s*GB\b(?!\s*(?:SSD|HDD|GDDR))', without_vram) if int(value) in RAM_SIZES}
    if not explicit_ram and len(unlabelled_ram) > 1:
        front_vram = {int(match[0]) for match in re.findall(
            r'\b([468])\s*GB\s+(?:NVIDIA\s+)?' + GPU, without_vram)}
        unlabelled_ram -= front_vram
    ram_values = explicit_ram or unlabelled_ram
    if len(drives) > 1 or len(ram_values) > 1:
        return None
    ram_title = _one(ram_values)
    if (ram_attr and ram_title and ram_attr != ram_title or
            ssd_attr and ssd_title and ssd_attr != ssd_title):
        return None
    ram, ssd = ram_attr or ram_title, ssd_attr or ssd_title
    os_attr, os_title = _os(label('isletim sistemi')), _os(title)
    if os_attr and os_title and not (os_attr.startswith(os_title) or os_title.startswith(os_attr)):
        return None
    os_name = max((os_attr or '', os_title or ''), key=len) or ('macOS' if brand == 'Apple' and cpu else None)
    if not (cpu and ram in RAM_SIZES
            and ssd in (128, 256, 512, 1024, 2048, 4096, 8192) and os_name):
        return None
    gpu_attr = label('ekran karti modeli', 'ekran karti', 'grafik karti', 'grafik islemcisi')
    gpu_match = lambda value: re.search(r'\b' + GPU, WORDS(value).upper())
    def discrete(value):
        match = gpu_match(value)
        return ' '.join(x.strip() for x in match.groups() if x) if match else ''
    gpu_a, gpu_b = discrete(gpu_attr), discrete(title)
    if gpu_a and gpu_b and gpu_a != gpu_b:
        return None
    resolution = label('ekran cozunurlugu', 'cozunurluk piksel', 'cozunurluk')
    resolution = re.fullmatch(r'(\d{4})\s*[x×]\s*(\d{3,4})', resolution, re.I)
    size_attr, size_title = _size(label('ekran boyutu', 'ekran boyutu inch')), _size(title)
    if size_attr and size_title and size_attr != size_title:
        return None
    size = size_title or size_attr
    return {'mpn': mpn, 'brand': brand, 'cpu': cpu, 'ram': ram, 'ssd': ssd, 'os': os_name,
            'discrete': gpu_a or gpu_b, 'screen': size.removesuffix('.0') if size else None,
            'resolution': f'{resolution[1]} × {resolution[2]}' if resolution else None,
            'graphics': gpu_a or gpu_b or (gpu_attr if 2 < len(gpu_attr) < 45 and WORDS(gpu_attr) not in
                                          ('diger', 'dahili', 'harici', 'belirtilmemis', 'paylasimli') else None),
            'refresh': label('ekran yenileme hizi', 'yenileme hizi'),
            'memory_type': label('bellek turu', 'ram tipi', 'ram sistem bellegi tipi')}


def _legal(seller, field):
    if not isinstance(seller, dict):
        return {}
    name = WORDS(seller.get(field))
    tax = seller.get('taxNumber')
    return {'_legal_name': name, '_tax_key': tax if isinstance(tax, str) and re.fullmatch(r'\d{10,11}', tax) else ''}


def selected_n11(html, url):
    def verify(product):
        category, seller = product.get('category'), product.get('seller')
        brand = _brand(product.get('productBrand'))
        if not isinstance(category, dict) or category.get('id') != 1000271 or category.get('seoUrl') != '/bilgisayar/dizustu-bilgisayar':
            return lambda value: False, {}
        attr = _attrs(product.get('productAttributes'))
        title = product.get('title')
        native = attr.get('model', '')
        facts = laptop_facts(title, attr, brand, native)
        if not facts:
            return lambda value: False, {}
        def matches(value):
            return (WORDS(value.removesuffix(' Fiyatları ve Özellikleri')) == WORDS(title)
                    and model_code(value, brand, native) == facts['mpn'])
        photo = None
        for image in product.get('images') or []:
            raw = image.get('path') if isinstance(image, dict) and image.get('isVideoBannerImage') is False else None
            if raw and isinstance(raw, str):
                photo = _photo(raw.replace('{0}', '640'), 'n11scdn.akamaized.net', 'n11scdn2-im.akamaized.net')
                if photo:
                    break
        return matches, {'facts': facts, '_image': photo, **_legal(seller, 'companyName')}
    return n11_offer(html, url, '', {}, verify_laptop=verify)


def selected_trendyol(html, url):
    def variant(product, title, brand):
        category = product.get('category')
        categories = {1583: 'dizustu bilgisayar', 2696: 'oyuncu dizustu bilgisayari'}
        if not isinstance(category, dict) or categories.get(category.get('id')) != WORDS(category.get('name')):
            return None, 'not_selected_laptop_category'
        native = str(product.get('productCode') or '')
        facts = laptop_facts(title, _attrs(product.get('attributes'), trendyol=True), _brand(brand.get('name')), native)
        if not facts:
            return None, 'selected_laptop_config_missing'
        seller = product['merchantListing']['merchant']
        photo = next((value for image in product.get('images') or []
                      if (value := _photo(image, 'cdn.dsmcdn.com'))), None)
        return {'facts': facts, '_image': photo, **_legal(seller, 'officialName')}, None

    offer, reason = trendyol_offer(html, url, variant,
        lambda schema, identity, url, physical, brand: _selected_group_root(schema, identity, url, brand))
    if offer and not offer.get('gtin13'):
        return None, 'selected_laptop_gtin_missing'
    return offer, reason


def _dom_attrs(soup, selector):
    rows = []
    for row in soup.select(selector):
        cells = row.find_all(['th', 'td'], recursive=False)
        if len(cells) == 2:
            rows.append({'key': cells[0].get_text(' ', strip=True), 'value': cells[1].get_text(' ', strip=True)})
    return _attrs(rows)


def selected_vatan(html, url):
    import json
    soup = BeautifulSoup(html, 'html.parser')
    canonical = soup.select('link[rel=canonical]')
    breadcrumb = soup.select_one('.breadcrumb')
    if len(canonical) != 1 or canonical[0].get('href') != url or not breadcrumb or not breadcrumb.select_one('a[href="/notebook/"]'):
        return None, 'not_selected_laptop_category'
    source_code = soup.select('.product-detail .product-id[data-productcode]')
    heading = soup.select('.product-detail h1.product-list__product-name')
    barcode = soup.select('.detailPartialPage > input.flixinpage[ean][brand]')
    if not len(source_code) == len(heading) == len(barcode) == 1:
        return None, 'selected_laptop_config_missing'
    title = heading[0].get_text(' ', strip=True)
    brand = _brand(barcode[0]['brand'])
    native = source_code[0]['data-productcode']
    facts = laptop_facts(title, _dom_attrs(soup, '.product-table tr'), brand, native)
    if not facts:
        return None, 'selected_laptop_config_missing'
    # Escape only malformed customer review bodies. Do not change seller, SKU,
    # GTIN, price or stock; the selected Vatan parser checks those independently.
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            item = _vatan_review_jsonld(script.get_text())
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict) and item.get('@type') == 'Product' and item.get('mpn') == native:
            script.string = json.dumps(item, ensure_ascii=True).replace('<', r'\u003c')
    offer = selected_vatan_tws(str(soup), url, facts['mpn'],
                               lambda full, observed_brand: model_code(full, _brand(observed_brand), native))
    if not offer:
        return None, 'vatan_selected_offer_unproven'
    photo = None
    for a in soup.select('.product-detail a[href*="/Upload/PRODUCT/"]'):
        photo = _photo(urljoin(url, a['href']), 'cdn.vatanbilgisayar.com')
        if photo:
            break
    return {**offer, 'facts': facts, '_image': photo}, 'selected_new_cash_offer_observed'


def _mediamarkt_url(value):
    return bool(re.fullmatch(r'https://www\.mediamarkt\.com\.tr/tr/product/_[a-z0-9-]+-\d{6,12}\.html', value))


def _media_laptop(product, title, brand, attrs=None):
    facts = laptop_facts(title, attrs or {}, _brand(brand), product.get('mpn', ''))
    gtin = retailer_gtin13(str(product.get('gtin13', '')))
    if not facts or not gtin or not LAPTOP_LABEL.search(WORDS(title)):
        return None
    photo = next((x for v in product.get('image', []) if (x := _photo(v, 'assets.mmsrg.com'))), None)
    return {'facts': facts, 'gtin13': gtin, 'sku': str(product['sku']), '_image': photo}


def _media_marketplace_laptop(product, title, brand, features):
    if not features:
        return None
    attrs = _attrs([{'key': row.get('name'), 'value': row.get('value')} for row in features])
    if WORDS(attrs.get('urun tipi')) not in {'laptop', 'gaming laptop', 'notebook', 'dizustu bilgisayar'}:
        return None
    ram = attrs.get('ram bellek boyutu', '')
    if ram.isdigit():
        attrs['ram'] = ram + ' GB'
    drive = re.fullmatch(r'SSD\s*,\s*(\d+\s*(?:GB|TB))', attrs.get('hard disk 1', ''))
    if drive:
        attrs['ssd'] = drive[1]
    return _media_laptop(product, title, brand, attrs)


def selected_mediamarkt(html, url):
    offer = media_offer(html, url, allowed_url=_mediamarkt_url, verify_product=_media_laptop)
    if offer:
        return {**offer, 'platform': 'MediaMarkt', 'condition': 'new',
                '_legal_name': 'mediamarkt turkey ticaret limited sirketi'}, 'selected_new_cash_offer_observed'
    has_selector = 'name="pdp-condition-selector"' in html
    offer = selected_marketplace_offer(html, url, allowed_url=_mediamarkt_url,
        verify_product=_media_marketplace_laptop, allow_cents=True,
        allow_refurbished_alternatives=has_selector, allow_delivery_card=has_selector,
        require_selected_condition_radio=has_selector)
    if not offer:
        return None, 'mediamarkt_selected_offer_unproven'
    return {**offer, 'merchant': offer['seller_name'], 'merchant_id': offer['seller_id'],
            'seller_url': 'https://www.mediamarkt.com.tr/tr/marketplace/seller/' + offer['seller_id'],
            'observed_at': datetime.now(timezone.utc).isoformat()}, 'selected_new_cash_offer_observed'


def selected_teknosa(html, url):
    def verify(cart, title, product):
        brand = _brand(cart.get('data-product-brand'))
        facts = laptop_facts(title, {}, brand, product.get('mpn', ''))
        gtin = retailer_gtin13(cart.get('data-product-ean'))
        if (not facts or not LAPTOP_LABEL.search(WORDS(title)) or not gtin
                or any(product.get(key) is not None and retailer_gtin13(str(product[key])) != gtin
                       for key in ('gtin', 'gtin13'))):
            return None
        images = product.get('image')
        if isinstance(images, str):
            images = [images]
        photo = next((x for image in images or [] if (x := _photo(image, 'img-teknosa-sap.mncdn.com'))), None)
        return {'facts': facts, '_image': photo}

    return _selected_marketplace_commerce(html, url, verify_product=verify)


def marketplace_seller_identity(html, offer):
    soup = BeautifulSoup(html, 'html.parser')
    if offer['platform'] == 'Teknosa':
        names = soup.select('h1.plp-title')
        shops = soup.select('input#SelectedShopId')
        if len(shops) != 1 or shops[0].get('value') != offer['merchant_id']:
            return None
        details = soup.select('.msd-section')
        company_label = 'Firma Resmi Adı'
    else:
        names = soup.select('main h1')
        details = [node.parent for node in soup.select('main h2')
                   if node.get_text(' ', strip=True) == 'Künye & Satıcı Bilgileri']
        company_label = 'Firma adı'
    if len(names) != 1 or WORDS(names[0].get_text(' ', strip=True)) != WORDS(offer['merchant']):
        return None
    text = ' '.join(node.get_text(' ', strip=True) for node in details)
    company = re.findall(r'\b' + company_label + r'\s*:\s*(.{2,250}?)\s+Şehir\s*:', text)
    tax = re.findall(r'\bVergi No\s*:\s*(\d{10,11})(?!\d)', text)
    if len(company) != 1 or len(tax) != 1:
        return None
    return _legal({'companyName': company[0], 'taxNumber': tax[0]}, 'companyName')
