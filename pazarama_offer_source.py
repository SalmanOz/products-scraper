"""Verify the selected public Pazarama offer and its seller profile."""

import json
import re

from bs4 import BeautifulSoup

from pazarama_category_source import HOST, pazarama_product_url
from retailer_barcode import retailer_gtin13
from retailer_offer_refresh import _product_offer, _schema_nodes
from tr_price_scraper import TRPriceScraper


NORMALIZE = TRPriceScraper._normalize_words
SELLER_ROOT = f'https://{HOST}/magaza/'
NUXT_IDENTIFIER = r'[A-Za-z_$][A-Za-z0-9_$]{0,20}'
UUID = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
NUXT_STRING = r'"(?:[^"\\]|\\.)*"'


def native_script(soup):
    scripts = [node.get_text() for node in soup.select('script')
               if node.get_text().startswith('window.__NUXT__=(function(')]
    return scripts[0] if len(scripts) == 1 else ''


def native_string(token, document):
    if token.startswith('"'):
        try:
            return json.loads(token)
        except ValueError:
            return None
    # Nuxt's serializer may pass repeated strings as positional arguments.
    # Decode the JSON argument list without evaluating the inline script.
    header = re.match(r'window\.__NUXT__=\(function\(([^)]{1,8000})\)\{', document)
    end = document.rfind('}(')
    if not header or not document.endswith('));') or end < header.end():
        return None
    arguments = document[end + 2:-3]
    keys = header[1].split(',')
    if (len(arguments) > 64_000 or len(keys) != len(set(keys)) or
            any(not re.fullmatch(NUXT_IDENTIFIER, key) for key in keys) or token not in keys):
        return None
    prefix = document[header.end():].split('UI_PRODUCT_DETAIL:{', 1)[0]
    if re.search(r'(?<![\w$])' + re.escape(token) + r'\s*(?:[+*/%-]?=(?!=)|\+\+|--)', prefix):
        return None
    try:
        values = json.loads('[' + arguments + ']')
    except (ValueError, RecursionError):
        return None
    if len(values) != len(keys):
        return None
    value = values[keys.index(token)]
    return value if isinstance(value, str) else None


def _phone_facts(soup, detail, document, token):
    names = {'ram': 'ram_gb', 'ram kapasitesi': 'ram_gb', 'dahili hafiza': 'storage_gb',
             'ag baglanti': 'network_generation', 'urun tipi': 'category'}
    visible, native = {}, {}

    def collect(destination, label, value):
        key = names.get(NORMALIZE(label))
        if key:
            destination.setdefault(key, set()).add(NORMALIZE(value))

    for row in soup.select('.product-detail .feature > div'):
        values = row.find_all('span', recursive=False)
        if len(values) == 2:
            collect(visible, *(value.get_text(' ', strip=True) for value in values))
    if visible.get('category') != {'akilli telefon'}:
        return {}
    attributes = re.search(r',attributes:\[([^\[\]]{1,18000})\],', detail)
    if not attributes:
        return {}
    for pair in re.finditer(r'\{attributeName:(' + token + r'),attributeValue:(' + token + r'),', attributes[1]):
        label, value = (native_string(raw, document) for raw in pair.groups())
        if label:
            collect(native, label, value)
    if (any(len(values) != 1 for table in (visible, native) for values in table.values())
            or any(visible[key] != native[key] for key in visible.keys() & native.keys())):
        return None
    if visible != native or set(native) != set(names.values()):
        return {}
    ram = re.fullmatch(r'([1-9]\d?) gb', next(iter(native['ram_gb'])))
    storage = re.fullmatch(r'([1-9]\d{0,3}) (gb|tb)', next(iter(native['storage_gb'])))
    radio = next(iter(native['network_generation']))
    product_ids = re.findall(r',productId:(' + token + r'),', detail)
    product_id = native_string(product_ids[0], document) if len(product_ids) == 1 else None
    if not (ram and storage and radio in {'4g', '5g'} and product_id and re.fullmatch(UUID, product_id)):
        return None
    return {'product_id': product_id, 'ram_gb': int(ram[1]),
            'storage_gb': int(storage[1]) * (1024 if storage[2] == 'tb' else 1),
            'network_generation': radio}


def pazarama_selected(html, url, *, verify_product=None, phone=False):
    if not pazarama_product_url(url):
        return None
    soup = BeautifulSoup(html, 'html.parser')
    cash = _product_offer(soup, url)
    nodes = _schema_nodes(soup, url)
    products = [p for p in nodes if p.get('@type') == 'Product' and p.get('url') == url]
    if not cash or len(products) != 1 or cash['availability'] != 'in_stock':
        return None
    product = products[0]
    seller = product['offers'].get('seller')
    listing_id = str(product.get('sku', ''))
    if not isinstance(seller, str) or not re.fullmatch(UUID, listing_id):
        return None
    document = native_script(soup)
    selected = document.split('UI_PRODUCT_DETAIL:{', 1)
    if len(selected) != 2:
        return None
    detail = re.split(r'\},UI_[A-Z_]+:', selected[1][:30000], maxsplit=1)[0]
    token = NUXT_STRING + '|' + NUXT_IDENTIFIER
    identities = re.match(r'name:\w+,displayName:\w+,sellerName:(' + token + r'),'
                          r'sellerSeoName:(' + token + r'),[^{}]{0,1000}?'
                          r'\bcode:(' + token + r'),stockCount:', detail)
    if not identities:
        return None
    selected_name, seller_slug, code = (native_string(value, document) for value in identities.groups())
    if (not selected_name or NORMALIZE(selected_name) != NORMALIZE(seller)
            or not seller_slug or not re.fullmatch(r'[a-z0-9-]+', seller_slug) or not code):
        return None
    gtin = retailer_gtin13(code)
    if not gtin or not re.fullmatch(r'\d{12,13}', code) or not url.endswith('-p-' + code):
        return None
    if re.search(r'\bproductSellerId:"' + re.escape(listing_id) + r'"', detail) is None:
        return None
    seller_path = '/magaza/' + seller_slug
    anchors = soup.select('.product-detail a[href]')
    if not any(a['href'] == seller_path and
               NORMALIZE(a.get_text(' ', strip=True)).startswith('satici ' + NORMALIZE(seller))
               for a in anchors):
        return None
    evidence = verify_product(product, nodes) if verify_product else {}
    facts = _phone_facts(soup, detail, document, token) if phone else {}
    if evidence is None or facts is None:
        return None
    if facts:
        facts.update(selected_pazarama_variant_verified=True, condition='new', sku=listing_id, listing_id=listing_id)
    return {**cash, **evidence, **facts, 'merchant': seller, 'platform': 'Pazarama', 'gtin13': gtin,
            'merchant_id': seller_slug, 'seller_url': SELLER_ROOT + seller_slug}


def pazarama_seller_identity(html, offer):
    slug = offer.get('merchant_id')
    if not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9-]{1,100}', slug):
        return None
    url = SELLER_ROOT + slug
    if offer.get('seller_url') != url:
        return None
    soup = BeautifulSoup(html, 'html.parser')
    canonical = [node.get('href') for node in soup.select('link[rel=canonical]')]
    headers = soup.select('h1')
    if (canonical != [url] or len(headers) != 1
            or NORMALIZE(headers[0].get_text(' ', strip=True)) != NORMALIZE(offer['merchant'])):
        return None
    states = native_script(soup).split('UI_STORE_INFO:{')
    if len(states) != 2:
        return None
    profile = re.match(r'(sellerId:\w+,[^{}]{1,1800}?),sellerAddress:', states[1])
    if not profile:
        return None
    names = re.findall(r'\bsellerTradeName:("(?:[^"\\]|\\.){3,200}")', profile[1])
    registrations = re.findall(r'\bsellerMersisNumber:"(\d{16})"', profile[1])
    if len(names) != 1 or len(registrations) > 1:
        return None
    name = NORMALIZE(json.loads(names[0]))
    return (name, registrations[0][1:11] if registrations else '') if len(name.split()) >= 2 else None
