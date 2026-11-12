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


def pazarama_selected(html, url, *, verify_product=None):
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
    if not isinstance(seller, str) or not re.fullmatch(r'[0-9a-f-]{36}', listing_id):
        return None
    document = native_script(soup)
    selected = document.split('UI_PRODUCT_DETAIL:{', 1)
    if len(selected) != 2:
        return None
    detail = selected[1][:30000]
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
    if evidence is None:
        return None
    return {**cash, **evidence, 'merchant': seller, 'platform': 'Pazarama', 'gtin13': gtin,
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
