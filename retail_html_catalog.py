"""Build a catalog from selected products on public retailer HTML pages."""
import argparse
import asyncio
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from html import unescape
import json
import math
from pathlib import Path
import re
import sqlite3
from unicodedata import normalize
from threading import local
from urllib.parse import urlencode, urljoin, urlsplit

from curl_cffi import CurlError
from curl_cffi.requests import Session, RequestsError
from lxml import html as lxml
from lxml.etree import ParserError

from mediamarkt_marketplace_audit import product_features as mediamarkt_product_features
from retailer_barcode import retailer_gtin13

HB, MM, N11, TY, VATAN = ('www.hepsiburada.com', 'www.mediamarkt.com.tr', 'www.n11.com',
                          'www.trendyol.com', 'www.vatanbilgisayar.com')
STORES = {HB: 'Hepsiburada', MM: 'MediaMarkt', N11: 'n11', TY: 'Trendyol', VATAN: 'Vatan Bilgisayar'}
PAGING = {HB: 'sayfa', MM: 'page', N11: 'pg', TY: 'pi', VATAN: 'page'}
PDP = {
    HB: r'/[a-zA-Z0-9-]+-pm?-(HBCV?[A-Z0-9]{5,24}|[A-Z0-9]{6,24})',
    MM: r'/tr/product/_[a-z0-9-]+-([0-9]{6,12})\.html',
    N11: r'/urun/[a-z0-9-]+-([1-9][0-9]{3,12})',
    TY: r'/[a-z0-9-]+/[a-z0-9-]+-p-([1-9][0-9]{3,11})',
    VATAN: r'/([a-z0-9-]+)\.html',
}
IMAGE_HOSTS = {
    HB: {'productimages.hepsiburada.net'}, MM: {'assets.mmsrg.com'}, N11: {
        'n11scdn.akamaized.net', 'n11scdn2-im.akamaized.net'}, TY: {'cdn.dsmcdn.com'},
    VATAN: {'cdn.vatanbilgisayar.com'},
}
KIND_WORDS = {
    'laptop': r'\b(?:dizustu|laptop|notebook|macbook)\b',
    'tablet': r'\b(?:tablet|ipad)\b',
    'television': r'\b(?:televizyon|television|tv|oled tv|led tv)\b',
    'smartwatch': r'\b(?:akilli (?:kol |cocuk )?saat(?:ler)?|smartwatch|apple watch)\b',
    'monitor': r'\b(?:monitor(?:ler|u)?|monitor ve ekran|bilgisayar ekrani)\b',
    'earbuds': r'\b(?:kulaklik(?:lar)?|earbuds|headphones|tws)\b',
    'pc-parts': r'\b(?:bilesenler|bilgisayar parcalari|islemci(?:ler)?|anakart(?:lar)?|ekran kart(?:i|lari)|ram|ssd|bilgisayar kasasi|bilgisayar kasalari|guc kaynagi|guc kaynaklari|islemci sogutucu|sivi sogutma)\b',
    'desktop': r'\b(?:masaustu bilgisayar(?:lar)?|hazir sistem|all in one|mini pc|imac)\b',
    'keyboard': r'\b(?:klavye(?:ler)?|keyboard)\b',
    'mouse': r'\b(?:mouse|fare)\b',
    'printer': r'\b(?:yazici(?:lar)?|printer)\b',
    'speaker': r'\b(?:hoparlor(?:ler)?|speaker|soundbar)\b',
    'robot-vacuum': r'\b(?:robot supurge(?:ler)?|robot vacuum)\b',
    'camera': r'\b(?:fotograf makine(?:si|leri)|kamera(?:si|lar)?|camera)\b',
    'drone': r'\b(?:drone|dron|multikopter)\b',
    'projector': r'\b(?:projeksiyon|projector)\b',
    'power-bank': r'\b(?:power ?bank|tasinabilir (?:pilli )?sarj cihaz(?:i|lari))\b',
    'network': r'\b(?:modem(?:ler)?|router(?:lar)?|yonlendirici|ag anahtari|network urunleri|switch(?:ler)?|wi fi menzil genisletici)\b',
    'storage': r'\b(?:harici (?:disk|harddisk|hard disk)|usb bellek|hafiza kart(?:i|lari)|tasinabilir ssd)\b',
    'security-camera': r'\b(?:guvenlik kamera(?:si|lari)|ip kamera|network kamera)\b',
    'microphone': r'\b(?:mikrofon(?:lar)?|microphone)\b',
    'air-purifier': r'\b(?:hava temizleyici|hava temizleme cihaz(?:i|lari)|air purifier)\b',
    'air-conditioner': r'\b(?:klima(?:lar|si)?|air conditioner)\b',
    'refrigerator': r'\b(?:buzdolabi|buzdolaplari|derin dondurucu|refrigerator|freezer)\b',
    'washing-machine': r'\b(?:camasir makine(?:si|leri)|washing machine)\b',
    'dishwasher': r'\b(?:bulasik makine(?:si|leri)|dishwasher)\b',
}
USED = re.compile(r'\b(?:ikinci el|2 el|teshir|yenilenmis|refurbished|outlet|kullanilmis)\b')
ACCESSORY = re.compile(r'\b(?:on panel|anakart tamir|kilif|ekran koruyucu|uyumlu sarj|sarj kablosu|aski aparati|yedek parca|toner|kartus)\b')
OTHER_KIND = {'laptop', 'tablet', 'television', 'monitor', 'smartwatch', 'robot-vacuum'}
SOURCES = json.loads(Path(__file__).with_name('retail_html_sources.json').read_text())
DECODER = json.JSONDecoder()


def words(value):
    folded = normalize('NFKD', unescape(str(value or '')).casefold().replace('ı', 'i'))
    return ' '.join(re.findall(r'[a-z0-9]+', ''.join(c for c in folded if not '\u0300' <= c <= '\u036f')))


def clean(value, limit=250):
    if not isinstance(value, str):
        return ''
    result = ' '.join(unescape(value).split())
    return result if 0 < len(result) <= limit and not re.search(r'[\x00-\x1f\x7f]', result) else ''


def text(node):
    return clean(' '.join(node.itertext()), 600)


def as_dict(value):
    return value if isinstance(value, dict) else {}


def select_one(rows):
    return rows[0] if len(rows) == 1 else None


def document(raw):
    return lxml.fromstring(raw, parser=lxml.HTMLParser(encoding='utf-8', no_network=True, recover=True))


def clean_url(raw, base):
    if not isinstance(raw, str) or len(raw) > 900 or re.search(r'[\x00-\x20\\]', raw):
        return None
    try:
        url = urlsplit(urljoin(base, raw))
        if url.scheme == 'https' and url.netloc == urlsplit(base).netloc and not url.fragment:
            return url._replace(query='').geturl()
    except ValueError:
        pass
    return None


def product_url(raw, base):
    url = clean_url(raw, base)
    if url:
        route = urlsplit(url)
        if (rule := PDP.get(route.netloc)) and re.fullmatch(rule, route.path):
            return url
    return None


def jsonld(doc):
    for raw in doc.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            node = json.loads(raw.strip().removesuffix(';'))
        except ValueError:
            continue
        node = node if isinstance(node, list) else as_dict(node).get('@graph', [node])
        yield from (row for row in node if isinstance(row, dict))


def js_state(doc, marker):
    found = []
    for raw in doc.xpath('//script[contains(text(), $marker)]/text()', marker=marker):
        match = re.search(re.escape(marker) + r'\s*=\s*', raw)
        if match:
            try:
                found.append(as_dict(DECODER.raw_decode(raw, match.end())[0]))
            except ValueError:
                continue
    return select_one(found) or {}


def matches_category(kind, category):
    return any(re.search(KIND_WORDS[kind], words(leaf)) for leaf in category if leaf)


def valid_title(title, kind):
    current = words(title)
    if USED.search(current) or ACCESSORY.search(current):
        return False
    if kind == 'network' and re.search(r'\b(?:kablo|konnektor|pense|crimp|fiber adaptor|anten soketi)\b', current):
        return False
    if kind in OTHER_KIND:
        # Category path is verified on the PDP. Reject named add-ons even
        # when a retailer includes them in a finished-device category.
        return not re.search(r'\b(?:yedek|uyumlu|compat(?:ible)?|standi|askisi|'
                             r'adaptor(?:u)?|kumanda(?:si)?|klavyesi|cantasi)\b', current)
    return True


def technical_field(label, value):
    key = words(label)
    return ('@' not in value and not re.search(
        r'\b(?:adres|uretici bilgisi|uretici iletisim|ithalatci|yetkili temsilci|uygunluk|mensei|garanti)\b', key))


def properties(rows):
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = row.get('name') or row.get('key')
        value = row.get('unitText') or row.get('value')
        label = clean(as_dict(key).get('name') if isinstance(key, dict) else key, 80)
        value = clean(as_dict(value).get('name') if isinstance(value, dict) else str(value or ''), 110)
        if label and value and technical_field(label, value) and words(label) not in {
            'marka', 'brand', 'barkod', 'ean', 'gtin', 'garanti', 'garanti suresi',
            'garanti tipi', 'urun tipi', 'urun durumu', 'renk', 'color', 'mensei',
        }:
            result.setdefault(label, value)
    return result


def table_properties(doc, expression):
    rows = []
    for row in doc.xpath(expression):
        cells = row.xpath('./th | ./td | ./dt | ./dd | ./div')
        if len(cells) == 2:
            rows.append({'name': text(cells[0]), 'value': text(cells[1])})
    return properties(rows)


def images(host, items):
    if isinstance(items, str):
        items = [items]
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        if not isinstance(item, str) or len(item) > 500 or re.search(r'[\x00-\x20\\]', item):
            continue
        route = urlsplit(item)
        if route.scheme == 'https' and route.netloc in IMAGE_HOSTS[host] and not route.query and not route.fragment:
            result.append(item)
    return list(dict.fromkeys(result))


def exact_barcode(*values):
    valid = {barcode for value in values if (barcode := retailer_gtin13(value))}
    return next(iter(valid)) if len(valid) == 1 else None


def brand_name(node):
    return clean(as_dict(node).get('name') if isinstance(node, dict) else node, 80)


def identifier(node):
    return clean(node.get('mpn'), 80)


def model_from_specs(specs):
    candidates = {value for key, value in specs.items()
                  if words(key) in {'uretici kodu', 'uretici parca numarasi', 'model kodu', 'model numarasi', 'mpn'}}
    return select_one(list(candidates))


def price_value(offer, url):
    if (offer.get('@type') != 'Offer' or offer.get('url') != url or offer.get('priceCurrency') != 'TRY'
            or str(offer.get('availability', '')).rsplit('/', 1)[-1] not in {'InStock', 'LimitedAvailability'}
            or str(offer.get('itemCondition', '')).rsplit('/', 1)[-1] not in {'', 'NewCondition'}
            or any(k in offer for k in ('eligibleCustomerType', 'eligibleTransactionVolume', 'validForMemberTier'))):
        return None
    return amount(offer.get('price'))


def amount(raw):
    if isinstance(raw, bool) or not re.fullmatch(r'\d+(?:\.\d{1,2})?', str(raw)):
        return None
    value = float(raw)
    return value if math.isfinite(value) and 1 <= value <= 5_000_000 else None


def make_row(host, kind, url, title, brand, gtin, mpn, photos, specs, price=None, seller=None):
    title = clean(title, 350)
    photos = images(host, photos)
    if not (title and brand and gtin and photos and valid_title(title, kind)):
        return None
    checked = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return {'gtin': gtin, 'name': title, 'brand': brand, 'kind': kind, 'model': mpn or None,
            'image': photos[0], 'specs': dict(list(specs.items())[:20]), 'source': url,
            'checkedAt': checked, 'offer': {'url': url, 'price': price, 'merchant': seller, 'platform': STORES[host],
                                           'checkedAt': checked} if price and seller else None}


def selected_hb(doc, url, kind, nodes, title):
    raw = select_one(doc.xpath('//script[@id="reduxStore"]/text()'))
    if raw is None:
        return None
    state = json.loads(raw)['productState']
    product = state['product']
    category = [c['categoryName'] for c in product.get('categories', [])][-2:]
    if (product.get('isBundle') is not False or not matches_category(kind, category)
            or not url.endswith(('-p-' + product['sku'], '-pm-' + product['productId']))):
        return None
    schemas = [n for n in nodes if n.get('@type') == 'Product' and n.get('sku') == product['sku']]
    schema = select_one(schemas) or {}
    brand = brand_name(product.get('brand'))
    name = clean(product.get('name'), 350)
    if not name or not brand or words(title) not in {words(name), words(brand + ' ' + name)}:
        return None
    schema_brand = brand_name(schema.get('brand'))
    if schema_brand and words(schema_brand) != words(brand):
        return None
    gtin = exact_barcode(product.get('barcode'), schema.get('gtin'), schema.get('gtin13'))
    galleries = [row['url'].replace('{size}', '600') for row in product.get('media', [])
                 if row.get('isVideo') is False and isinstance(row.get('url'), str)]
    specs = properties(schema.get('additionalProperty', []))
    specs.update(table_properties(doc, '//*[@id="techSpecs"]//*[*[contains(@class,"OXP5AzPvafgN_i3y6wGp")]]'))
    offer = as_dict(schema.get('offers'))
    merchant = as_dict(product.get('merchant'))
    live = (merchant.get('id') and merchant['id'] == product.get('merchantId')
            and merchant.get('name') == product.get('merchantName') and product.get('listingId')
            and product.get('isProductLive') is True and product.get('quantity', 0) > 0
            and product.get('isInStock') is True and product.get('isPreOrder') is False)
    price = price_value(offer, url) if live else None
    public_price = select_one(product.get('prices', []))
    if not public_price or public_price.get('value') != price:
        price = None
    return make_row(HB, kind, url, title, brand, gtin, identifier(schema) or model_from_specs(specs), galleries, specs,
                    price, clean(product.get('merchantName'), 100))


def selected_n11(doc, url, kind, nodes, title):
    product = as_dict(as_dict(js_state(doc, 'window.__INITIAL_STATE__').get('productStore')).get('product'))
    category = as_dict(product.get('category'))
    if (product.get('bundleProduct') is True or product.get('secondHand') is not False
            or not matches_category(kind, [category.get('name'), category.get('seoUrl')])
            or clean_url(product.get('productUrlWithoutSellerShop'), url) != url
            or words(product.get('title')) != words(title)):
        return None
    schema = select_one([n for n in nodes if n.get('@type') == 'Product'
                         and as_dict(n.get('offers')).get('url') == url]) or {}
    sku = product.get('defaultSkuId')
    if not sku or str(schema.get('sku')) != str(sku):
        return None
    variants = product.get('skus', [])
    matched = select_one([n for n in variants if n.get('id') == sku]) if isinstance(variants, list) else None
    gtin = exact_barcode(product.get('defaultGtin'), as_dict(matched).get('gtin'), schema.get('gtin13'))
    brand = brand_name(schema.get('brand'))
    gallery = [row['path'].replace('{0}', '640') for row in product.get('images', [])
               if row.get('isVideoBannerImage') is False and isinstance(row.get('path'), str)]
    specs = properties(schema.get('additionalProperty', []))
    for raw in product.get('productAttributes', []) or []:
        if isinstance(raw, dict):
            specs.update(properties([raw]))
    facts = properties(doc_rows_n11(doc))
    specs.update(facts)
    price = price_value(as_dict(schema.get('offers')), url) if product.get('availableToBuy') is True else None
    if not price or amount(product.get('displayPriceNumber')) != price or product.get('hasInstantDiscount') is True:
        price = None
    return make_row(N11, kind, url, title, brand, gtin, clean(product.get('defaultSkuOem'), 80) or model_from_specs(specs),
                    gallery, specs, price, clean(as_dict(product.get('seller')).get('nickName'), 100))


def doc_rows_n11(doc):
    result = []
    for row in doc.xpath('//*[contains(@class,"property-item") or contains(@class,"attribute-item")]'):
        key = row.xpath('./*[contains(@class,"name") or contains(@class,"key")]')
        value = row.xpath('./*[contains(@class,"value") or contains(@class,"attribute-val")]')
        if len(key) == len(value) == 1:
            result.append({'name': text(key[0]), 'value': text(value[0])})
    return result


def selected_ty(doc, url, kind, nodes, title):
    product = as_dict(js_state(doc, 'window["__envoy__SHARED_PROPS"]').get('product'))
    category = as_dict(product.get('category'))
    brand = brand_name(product.get('brand'))
    name = clean(product.get('name'), 350)
    if (not name or not brand or not url.endswith('-p-' + str(product.get('id')))
            or words(title) != words(brand + ' ' + name)
            or not matches_category(kind, [category.get('name'), str(category.get('hierarchy', '')).rsplit('/', 1)[-1]])):
        return None
    variant = select_one([row for row in product.get('variants', []) if row.get('isSelected') is True]) or {}
    listing = as_dict(product.get('merchantListing'))
    winner = as_dict(listing.get('winnerVariant'))
    if (not variant.get('itemNumber') or winner.get('itemNumber') != variant['itemNumber']
            or winner.get('barcode') != variant.get('barcode')):
        return None
    gtin = exact_barcode(variant.get('barcode'), product.get('productCode'))
    schema = select_one([r for r in nodes if r.get('@type') == 'Product' and r.get('@id') == url]) or {}
    offer = as_dict(schema.get('offers'))
    price = price_value(offer, url) if winner.get('sellable') is True and winner.get('inStock') is True else None
    cash = as_dict(winner.get('price'))
    if (not price or cash.get('currency') != 'TRY' or as_dict(cash.get('sellingPrice')).get('value') != price
            or product.get('inStock') is not True or doc.xpath('//*[contains(@class,"campaign-price-content")]')):
        price = None
    specs = properties(product.get('attributes', []) + schema.get('additionalProperty', []))
    return make_row(TY, kind, url, title, brand, gtin, identifier(schema) or model_from_specs(specs),
                    product.get('images', []), specs, price, clean(as_dict(listing.get('merchant')).get('name'), 100))


def selected_mm(doc, url, kind, nodes, title):
    product = select_one([p for node in nodes if node.get('@type') == 'BuyAction'
                          and (p := as_dict(node.get('object'))).get('url') == url and p.get('name') == title])
    if not product or not url.endswith('-' + str(product.get('sku')) + '.html'):
        return None
    crumbs = [c.get('name') for n in nodes if n.get('@type') == 'BreadcrumbList'
              for c in n.get('itemListElement', []) if c.get('item') != url]
    if not matches_category(kind, crumbs[-3:]):
        return None
    sku = str(product['sku'])
    states = doc.xpath('//script[contains(text(),"window.__PRELOADED_STATE__")]/text()')
    state = select_one(states) or ''
    features = []
    for match in re.finditer(r'"CofrCoreFeatureProductFeature:Media:tr:' + sku + r':[^"]+"\s*:\s*', state):
        try:
            row = as_dict(DECODER.raw_decode(state, match.end())[0])
        except ValueError:
            continue
        if str(row.get('id', '')).startswith('Media:tr:' + sku + ':'):
            features.append({'name': row.get('name'), 'value': row.get('values')})
    state_match = re.match(r'window\.__PRELOADED_STATE__\s*=\s*', state)
    if state_match:
        try:
            hydrated = json.loads(re.sub(r':undefined(?=[,}])', ':null', state[state_match.end():].strip().rstrip(';')))
        except ValueError:
            hydrated = {}
        apollo = as_dict(hydrated.get('apolloState'))
        graph = as_dict(apollo.get('GraphqlProductAggregate:Media:tr-TR:' + sku))
        features += mediamarkt_product_features(apollo, graph, sku) or []
    specs = properties(product.get('additionalProperty', []) + features)
    offer = product.get('offers')
    if isinstance(offer, list):
        offer = select_one(offer)
    offer = as_dict(offer)
    price = price_value(offer, url)
    values = doc.xpath('//*[@data-test="branded-price-whole-value"]')
    raw = re.sub(r'[^0-9]', '', text(values[0])) if len(values) == 1 else ''
    decimals = doc.xpath('//*[@data-test="branded-price-decimal-value"]')
    decimal = re.sub(r'[^0-9]', '', text(decimals[0])) if len(decimals) == 1 else ''
    decimal = decimal or '00'
    if not raw.isdigit() or len(decimal) != 2 or float(raw + '.' + decimal) != price:
        price = None
    sink = brand_name(offer.get('seller'))
    if not sink:
        first_party = re.search(r'"id":"Media:tr:' + sku + r'","currency":"TRY","isProductOfTypeMarketplace":false',
                                state)
        sink = STORES[MM] if first_party else None
    return make_row(MM, kind, url, title, brand_name(product.get('brand')),
                    exact_barcode(product.get('gtin13'), product.get('gtin')), identifier(product) or model_from_specs(specs),
                    product.get('image', []), specs, price, sink)


def selected_vatan(doc, url, kind, nodes, title):
    product = select_one([p for p in nodes if p.get('@type') == 'Product'
                          and as_dict(p.get('offers')).get('url') == url and words(p.get('name')) == words(title)])
    barcode = select_one(doc.xpath('//input[@ean and @brand and contains(@class,"flixinpage")]'))
    if not product or barcode is None or not matches_category(
            kind, [text(x) for x in doc.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," breadcrumb ")]//a')][:-1]):
        return None
    brand = brand_name(product.get('brand'))
    if words(barcode.get('brand')) != words(brand):
        return None
    photos = product.get('image', [])
    specs = properties(product.get('additionalProperty', []))
    specs.update(table_properties(doc, '//*[contains(@class,"product-table")]//tr'))
    offer = as_dict(product.get('offers'))
    price = price_value(offer, url)
    price_region = doc.xpath('//*[contains(@class,"product-detail")]//*[contains(@class,"product-list__price")]')
    visible = [amount(text(node).replace('.', '').replace(',', '.')) for node in price_region]
    checkout = doc.xpath('//*[@id="add-to-cart-button" and not(@disabled) and @onclick="addBasket()"]')
    if not checkout or not price or price not in visible or brand_name(offer.get('seller')) != STORES[VATAN]:
        price = None
    return make_row(VATAN, kind, url, title, brand, exact_barcode(barcode.get('ean'), product.get('gtin13')),
                    identifier(product), photos, specs, price, STORES[VATAN])


PARSERS = {HB: selected_hb, N11: selected_n11, TY: selected_ty, MM: selected_mm, VATAN: selected_vatan}


def read_product(raw, url, kind):
    host = urlsplit(url).netloc
    doc = document(raw)
    canonical = product_url(select_one(doc.xpath('//link[@rel="canonical"]/@href')), url)
    if canonical != url:
        incoming = re.fullmatch(PDP[host], urlsplit(url).path)
        target = re.fullmatch(PDP[host], urlsplit(canonical).path) if canonical else None
        # Hepsiburada category cards often carry a selected SKU URL while its
        # product page declares the parent group as canonical. The selected
        # page's SKU and group must both be present in retailer state.
        if host != HB or not incoming or not target:
            return None
        raw_state = select_one(doc.xpath('//script[@id="reduxStore"]/text()'))
        product = as_dict(as_dict(json.loads(raw_state).get('productState')).get('product')) if raw_state else {}
        if (incoming[1] not in (product.get('sku'), product.get('productId')) or
                target[1] not in (product.get('sku'), product.get('productId'))):
            return None
    headings = doc.xpath('//h1')
    if not headings:
        return None
    title = text(headings[0])
    if not title or not valid_title(title, kind):
        return None
    return PARSERS[host](doc, url, kind, list(jsonld(doc)), title)


def read_category(raw, url, host):
    doc = document(raw)
    products = {}
    for node in jsonld(doc):
        if node.get('@type') != 'ItemList':
            continue
        for entry in node.get('itemListElement', []):
            item = as_dict(as_dict(entry).get('item'))
            if (dest := product_url(item.get('url'), url)) and (name := clean(item.get('name'), 350)):
                products[dest] = name
    if host == HB:
        for a in doc.xpath('//a[@href and (contains(@href, "-p-HBCV") or contains(@href, "-pm-HBC"))]'):
            names = a.xpath('.//h2 | .//h3')
            if names and (dest := product_url(a.get('href'), url)) and (name := text(names[0])):
                products.setdefault(dest, name)
    if host == VATAN:
        for a in doc.xpath('//a[contains(concat(" ",normalize-space(@class)," ")," product-list-link ") and @data-page]'):
            if dest := product_url(a.get('href'), url):
                names = a.xpath('.//*[contains(@class,"product-list__product-name")]')
                if names and (name := text(names[0])):
                    products[dest] = name
    return products


class Host:
    def __init__(self, spacing):
        self.spacing = spacing
        self.clients = local()
        self.schedule = asyncio.Lock()
        self.next_start = 0
        self.rejections = Counter()
        self.denials = 0
        self.stopped = False

    async def get(self, url):
        async with self.schedule:
            if self.stopped:
                return 0, b''
            clock = asyncio.get_running_loop()
            delay = self.next_start - clock.time()
            if delay > 0:
                await asyncio.sleep(delay)
            self.next_start = clock.time() + self.spacing
        status, data = await asyncio.to_thread(self.request, url)
        if status in (403, 429, 503):
            self.denials += 1
            if self.denials >= 4:
                self.stopped = True
        elif status == 200:
            self.denials = 0
        if status != 200:
            self.rejections[str(status)] += 1
        return status, data

    def request(self, url):
        if not hasattr(self.clients, 'session'):
            self.clients.session = Session(impersonate='chrome', discard_cookies=True)
        try:
            with self.clients.session.stream('GET', url, headers={'Accept': 'text/html'}, timeout=18,
                                             allow_redirects=False) as response:
                code = response.status_code
                if code != 200:
                    return code, b''
                if 'html' not in response.headers.get('content-type', ''):
                    return 415, b''
                data = bytearray()
                for chunk in response.iter_content():
                    data.extend(chunk)
                    if len(data) > 6_000_000:
                        return 413, b''
                return code, bytes(data)
        except (CurlError, RequestsError, OSError, TimeoutError):
            return 0, b''


def database(path):
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS pages (
            url TEXT PRIMARY KEY, status INT NOT NULL, items INT NOT NULL, checked_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS queue (
            url TEXT PRIMARY KEY, host TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', data TEXT, checked_at TEXT
        );
        CREATE INDEX IF NOT EXISTS queue_host_status ON queue (host, status);
    ''')
    return db


async def crawl(args, db):
    hosts = {host: Host(args.spacing) for host in (args.host or args.sources)}

    async def discover(host, reader):
        for kind, paths in args.sources.get(host, {}).items():
            if args.kind and kind not in args.kind:
                continue
            for path in paths:
                repeated = set()
                for page in range(args.start_page, args.start_page + args.pages):
                    if reader.stopped:
                        return
                    url = f'https://{host}{path}'
                    if page > 1:
                        url += '?' + urlencode({PAGING[host]: page})
                    saved = db.execute(
                        "SELECT items FROM pages WHERE url=? AND status=200 AND checked_at >= datetime('now', ?)",
                        (url, f'-{args.rediscover_after} hours')).fetchone()
                    if saved:
                        if not saved[0]:
                            break
                        continue
                    status, raw = await reader.get(url)
                    rows = {}
                    if raw:
                        try:
                            rows = await asyncio.to_thread(read_category, raw, url, host)
                        except (ParserError, ValueError, TypeError):
                            status = 422
                    signature = tuple(sorted(rows))
                    if signature in repeated:
                        break
                    repeated.add(signature)
                    db.execute('INSERT OR REPLACE INTO pages (url,status,items) VALUES (?,?,?)', (url, status, len(rows)))
                    db.executemany('INSERT OR IGNORE INTO queue (url,host,kind,title) VALUES (?,?,?,?)',
                                   [(u, host, kind, title) for u, title in rows.items() if valid_title(title, kind)])
                    db.commit()
                    if not rows:
                        break
                print(f'{host} {kind} discovery complete', flush=True)

    async def verify(host, reader, queue, counters):
        while not reader.stopped:
            try:
                url, kind = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            status, raw = await reader.get(url)
            if not raw and status in (0, 403, 429, 503):
                continue
            result = None
            if raw:
                try:
                    result = await asyncio.to_thread(read_product, raw, url, kind)
                except (KeyError, AttributeError, ValueError, TypeError, ParserError):
                    pass
            outcome = 'verified' if result else f'http_{status}' if not raw else 'unverified'
            counters[outcome] += 1
            if args.refresh and not result:
                db.execute('UPDATE queue SET checked_at=CURRENT_TIMESTAMP WHERE url=?', (url,))
                db.commit()
                continue
            db.execute('UPDATE queue SET status=?, data=?, checked_at=CURRENT_TIMESTAMP WHERE url=?',
                       (outcome, json.dumps(result, ensure_ascii=False) if result else None, url))
            db.commit()
            if sum(counters.values()) % 200 == 0:
                print(host, 'products', dict(counters), flush=True)

    async def work(host, reader):
        if not (args.verify_only or args.refresh or args.retry_unverified):
            await discover(host, reader)
        if args.discovery_only:
            return
        # Category and product requests share one host pacing budget.
        queue, counters = asyncio.Queue(), Counter()
        status = 'verified' if args.refresh else 'unverified' if args.retry_unverified else 'pending'
        order = 'checked_at, rowid' if args.refresh else 'rowid'
        rows = db.execute(f'SELECT url,kind FROM queue WHERE host=? AND status=? ORDER BY {order} LIMIT ?',
                          (host, status, args.maximum)).fetchall()
        for row in rows:
            queue.put_nowait(row)
        if reader.stopped and rows:
            # A stopped origin stays deferred until the next invocation.
            print(host, 'source stopped', dict(reader.rejections), flush=True)
        await asyncio.gather(*(verify(host, reader, queue, counters) for _ in range(args.workers)))
        print(host, 'complete', dict(counters), dict(reader.rejections), flush=True)

    await asyncio.gather(*(work(host, reader) for host, reader in hosts.items()))


def export(db, destination):
    products = {}
    conflicts = set()
    for raw, in db.execute("SELECT data FROM queue WHERE status='verified' ORDER BY checked_at DESC, url"):
        row = json.loads(raw)
        key = row['gtin']
        if key not in products:
            offer = row.pop('offer')
            row['offers'] = [offer] if offer else []
            products[key] = row
            continue
        seen = products[key]
        if (words(row['brand']) != words(seen['brand']) or row['kind'] != seen['kind']):
            conflicts.add(key)
            continue
        if row['model'] and seen['model'] and words(row['model']) != words(seen['model']):
            conflicts.add(key)
            continue
        offer = row['offer']
        if offer and not any(p['platform'] == offer['platform'] or words(p['merchant']) == words(offer['merchant'])
                             for p in seen['offers']):
            seen['offers'].append(offer)
        if len(row['specs']) > len(seen['specs']):
            seen.update(specs=row['specs'], source=row['source'], name=row['name'],
                        image=row['image'], model=row['model'] or seen['model'])
    for gtin in conflicts:
        del products[gtin]
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps([products[key] for key in sorted(products)], ensure_ascii=False) + '\n')
    temporary.replace(destination)
    print('exported', len(products), 'identity conflicts', len(conflicts), dict(Counter(p['kind'] for p in products.values())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--attach', type=Path, action='append', default=[], help='Merge audited HTML queues')
    parser.add_argument('--host', choices=SOURCES, action='append')
    parser.add_argument('--kind', choices=KIND_WORDS, action='append')
    parser.add_argument('--start-page', type=int, default=1)
    parser.add_argument('--rediscover-after', type=int, default=168, metavar='HOURS')
    parser.add_argument('--pages', type=int, default=38)
    parser.add_argument('--sources', type=Path, help='Override category paths for the built-in stores')
    parser.add_argument('--maximum', type=int, default=5000, help='Maximum selected product pages per host')
    parser.add_argument('--spacing', type=float, default=.5)
    parser.add_argument('--workers', type=int, default=4)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--discovery-only', action='store_true')
    mode.add_argument('--verify-only', action='store_true')
    mode.add_argument('--retry-unverified', action='store_true')
    mode.add_argument('--export-only', action='store_true')
    mode.add_argument('--refresh', action='store_true')
    args = parser.parse_args()
    if args.spacing < .35 or not 1 <= args.workers <= 8 or not 1 <= args.pages <= 200 \
            or args.start_page < 1 or args.rediscover_after < 1 or args.maximum < 1 or args.export_only and not args.output:
        parser.error('Invalid page, product or request limits')
    args.sources = json.loads(args.sources.read_text()) if args.sources else SOURCES
    if any(host not in STORES or any(kind not in KIND_WORDS or any(
            not re.fullmatch(r'/[a-z0-9./-]+', path) for path in paths)
            for kind, paths in categories.items()) for host, categories in args.sources.items()):
        parser.error('Invalid retailer category paths')
    args.db.parent.mkdir(parents=True, exist_ok=True)
    with closing(database(args.db)) as db:
        for path in args.attach:
            db.execute('ATTACH DATABASE ? AS part', (str(path),))
            db.execute('INSERT OR IGNORE INTO pages SELECT * FROM part.pages')
            db.execute(
                "INSERT INTO queue SELECT * FROM part.queue WHERE 1 "
                "ON CONFLICT(url) DO UPDATE SET status=excluded.status,data=excluded.data,checked_at=excluded.checked_at "
                "WHERE excluded.status='verified' AND "
                "(queue.status!='verified' OR json_extract(excluded.data, '$.checkedAt') >= json_extract(queue.data, '$.checkedAt'))"
            )
            db.commit()
            db.execute('DETACH DATABASE part')
        if not args.export_only:
            asyncio.run(crawl(args, db))
        if args.output:
            export(db, args.output)


if __name__ == '__main__':
    main()
