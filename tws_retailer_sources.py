"""Exact selected TWS listings from n11 and Vatan public pages."""
from collections import defaultdict
from datetime import datetime, timezone
from html import unescape
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from n11_catalog_source import _parse_selected_listing, discover_product_sellers, public_search_card_document
from retailer_barcode import retailer_gtin13
from retailer_offer_refresh import _is_conditional_price, _schema_nodes, _type_is
from teknosa_reviewed_source import _amount, _cash_price
from tr_price_scraper import TRPriceScraper
from vatan_category_source import BOT, VatanCategorySource

N11_CATEGORY_PATH = '/telefon-ve-aksesuarlari/cep-telefonu-aksesuarlari/bluetooth-kulaklik'
VATAN_HOME = 'https://www.vatanbilgisayar.com'
VATAN_CATEGORY = VATAN_HOME + '/vatan/bluetooth-kulaklik/'
STOP_REASONS = {'http_403', 'http_429', 'source_circuit_open', 'challenge_page', 'transport_error', 'request_error'}


def _words(value):
    return TRPriceScraper._normalize_words(value)


def selected_photo(url, hosts):
    if not isinstance(url, str) or len(url) > 500:
        return None
    route = urlsplit(url)
    if (route.scheme == 'https' and route.netloc in hosts and not route.query
            and not route.fragment and route.path):
        return url
    return None


def selected_n11_tws(html, url, name, model_name):
    def verify_product(product):
        category = product.get('category')
        seller = product.get('seller')
        brand = product.get('productBrand')
        if (not isinstance(category, dict) or category.get('id') != 1000479
                or category.get('seoUrl') != N11_CATEGORY_PATH
                or not isinstance(brand, str) or not isinstance(seller, dict)):
            return lambda title: False, {}
        model_codes = {attribute.get('value') for attribute in product.get('productAttributes') or []
                       if isinstance(attribute, dict) and attribute.get('key') == 'Model'
                       and isinstance(attribute.get('value'), str)}
        model_code = next(iter(model_codes)) if len(model_codes) == 1 else ''
        def matches(title):
            return model_name(title, brand) == name or (
                name in ('Apple AirPods 4', 'Apple AirPods 4 ANC')
                and model_name(f'{title} {model_code}', brand) == name)
        tax = seller.get('taxNumber')
        photo = next((photo for row in product.get('images') or []
                      if isinstance(row, dict) and row.get('isVideoBannerImage') is False
                      and isinstance(row.get('path'), str)
                      and (photo := selected_photo(row['path'].replace('{0}', '640'),
                                                   {'n11scdn.akamaized.net', 'n11scdn2-im.akamaized.net'}))), None)
        return matches, {'model': name, '_image': photo, '_legal_name': _words(seller.get('companyName')),
                         '_tax_key': tax if isinstance(tax, str) and re.fullmatch(r'\d{10,11}', tax) else ''}
    return _parse_selected_listing(html, url, name, {}, verify_tws=verify_product)


def selected_hepsiburada_tws(html, url, name, model_name):
    """Read an exact earbud SKU from Hepsiburada's public buy box."""
    from audit_hepsiburada_iphone16pm_readonly import parse_exact_buy_box, _nodes
    from hepsiburada_catalog_source import _identity

    soup = BeautifulSoup(html, 'html.parser')
    scripts = soup.select('script#reduxStore')
    if len(scripts) != 1 or urlsplit(url).query:
        return None, 'selected_earbud_state_missing'
    try:
        product = json.loads(scripts[0].get_text())['productState']['product']
        sku, product_id, listing_id = (product[key] for key in ('sku', 'productId', 'listingId'))
        seller = product['merchant']
        variants = [row for row in product['variants'] if row.get('isDefault') is True]
        category_ids = {row.get('categoryId') for row in product['categories']}
    except (TypeError, KeyError, ValueError, AttributeError):
        return None, 'selected_earbud_state_missing'
    brand = product.get('brand')
    seller_name, seller_slug = seller.get('name'), seller.get('urlPostfix')
    first_party = (seller_name, seller_slug) == ('Hepsiburada', 'hepsiburada')
    # Exact reviewed marketplace listing. The legal seller is corroborated by
    # Türkiye's ETBİS business register and the matching public store profile.
    reviewed_seller = (
        name == 'JBL Endurance Race 2' and sku == 'HBCV000095WJQ4'
        and (seller_name, seller_slug, seller.get('id')) ==
        ('Umut İnternet', 'umut-internet', '2e6b5bf8-196f-4afb-aa57-4af8dc8c0f6b')
    )
    if (not _identity(url, product_id, sku) or not urlsplit(url).path.endswith('-p-' + sku)
            or product.get('isBundle') is not False or '16218' not in category_ids or len(variants) != 1
            or not isinstance(brand, str) or not (first_party or reviewed_seller)):
        return None, 'selected_earbud_variant_mismatch'
    selected = variants[0]
    gtin = retailer_gtin13(selected.get('barcode'))
    barcodes = {retailer_gtin13(value) for value in selected.get('barcodes', [])}
    schemas = [node for node in _nodes(soup) if node.get('@type') == 'Product' and node.get('sku') == sku]
    if (selected.get('sku') != sku or selected.get('isInStock') is not True or selected.get('isPreOrder') is not False
            or not gtin or barcodes != {gtin} or len(schemas) != 1 or retailer_gtin13(schemas[0].get('gtin')) != gtin
            or reviewed_seller and gtin != '1200130014291'):
        return None, 'selected_earbud_gtin_or_stock_unverified'
    schema = schemas[0]
    scope = {'URL': url, 'SCHEMA_URL': url, 'PRODUCT_ID': product_id, 'SKU': sku, 'LISTING_ID': listing_id,
             'MERCHANT_ID': seller['id'], 'MERCHANT_POSTFIX': seller_slug, 'SELLER': seller_name,
             'SELLER_URL': 'https://www.hepsiburada.com/magaza/' + seller_slug, 'GTIN13': gtin, 'generic_catalog': True}

    def verify(_soup, _product, title):
        return selected if all(model_name(text, brand) == name for text in (title, selected.get('name'), schema.get('name'))) else None

    offer, reason = parse_exact_buy_box(html, url, scope=scope, verify_laptop=verify)
    if offer:
        photo = next((photo for raw in schema.get('image', [])
                      if (photo := selected_photo(raw, {'productimages.hepsiburada.net'}))), None)
        company = ('umut internet hizmetleri sanayi ve ticaret limited sirketi' if reviewed_seller
                   else 'd market elektronik hizmetler ve ticaret anonim sirketi')
        offer.update(platform='Hepsiburada', model=name, _image=photo, _legal_name=company)
    return offer, reason


def n11_offers(transport, models, model_name, failures):
    results = defaultdict(list)
    visited = set()
    alternatives = defaultdict(list)
    remaining = defaultdict(list)
    def read(url, name):
        if (name, url) in visited:
            return None, None, None
        visited.add((name, url))
        html, reason, _ = transport.read_document(url)
        if html is None:
            failures[f'n11_{reason}'] += 1
            return None, None, reason
        offer, reason = selected_n11_tws(html, transport.resolved_url(url), name, model_name)
        if offer is None:
            failures[f'n11_{reason}'] += 1
        return html, offer, reason

    def read_candidate(url, name):
        html, offer, reason = read(url, name)
        if offer:
            results[name].append(offer)
            if not alternatives[name]:
                alternatives[name].extend((offer['gtin13'], hint)
                    for hint in discover_product_sellers(html, offer['url'], maximum=2)
                    if hint['discovery_scope'] == 'public_other_seller_selector')
        return reason

    for name in models:
        html, reason, _ = transport.read_n11_search(name)
        if html is None:
            failures[f'n11_{reason}'] += 1
            if reason in STOP_REASONS:
                break
            continue
        cards = BeautifulSoup(public_search_card_document(html), 'html.parser')
        brand = name.split()[0]
        matches = []
        for card in cards.select('a[href]'):
            title = card.get_text(' ', strip=True)
            model = model_name(title, brand)
            apple_hint = (model is None and name in ('Apple AirPods 4', 'Apple AirPods 4 ANC')
                          and re.search(r'\bapple airpods 4\b', _words(title)))
            if model == name or apple_hint:
                matches.append(card['href'])
        if matches:
            if read_candidate(matches[0], name) in STOP_REASONS:
                return results
            remaining[name] = matches[1:5]
    for index in range(4):
        for name, urls in remaining.items():
            if len(urls) > index and read_candidate(urls[index], name) in STOP_REASONS:
                return results
    for name, hints in alternatives.items():
        for gtin, hint in hints:
            _, second, reason = read(hint['source_url'], name)
            if reason in STOP_REASONS:
                return results
            if second and second['merchant'] == hint['seller_name'] and second['gtin13'] == gtin:
                results[name].append(second)
            elif second:
                failures['n11_alternative_seller_or_gtin_mismatch'] += 1
    return results


def _vatan_url(url):
    return bool(re.fullmatch(r'https://www\.vatanbilgisayar\.com/[a-z0-9-]+\.html', url))


def selected_vatan_tws(html, url, name, model_name):
    if not _vatan_url(url):
        return None
    soup = BeautifulSoup(html, 'html.parser')
    selected = [product for product in _schema_nodes(soup, url) if _type_is(product, 'Product')
                and isinstance(product.get('offers'), dict) and product['offers'].get('url') == url]
    if len(selected) != 1:
        return None
    product = selected[0]
    offer = product['offers']
    seller = offer.get('seller')
    price = _amount(offer.get('price'))
    if (not _type_is(offer, 'Offer') or offer.get('itemCondition') != 'https://schema.org/NewCondition'
            or offer.get('availability') != 'https://schema.org/InStock' or offer.get('priceCurrency') != 'TRY'
            or not isinstance(seller, dict) or seller.get('name') != 'Vatan Bilgisayar'
            or seller.get('@id') != VATAN_HOME + '/#organization' or price is None):
        return None
    heading = soup.select('.product-detail h1.product-list__product-name')
    code = soup.select('.product-detail .product-id[data-productid][data-productcode]')
    cart = soup.select('.product-detail button#add-to-cart-button.basketBTN')
    barcode = soup.select('.detailPartialPage > input.flixinpage[ean][brand]')
    brand = product.get('brand')
    brand = brand.get('name') if isinstance(brand, dict) else None
    sku = str(product.get('sku', ''))
    if (not len(heading) == len(code) == len(cart) == len(barcode) == 1
            or not isinstance(brand, str) or _words(brand) != _words(barcode[0]['brand'])
            or code[0]['data-productid'] != sku or not re.fullmatch(r'\d{5,7}', sku)
            or not product.get('mpn') or code[0]['data-productcode'] != product['mpn']
            or cart[0].has_attr('disabled') or 'disabled' in cart[0].get('class', [])
            or cart[0].get('aria-disabled') == 'true' or cart[0].get_text(' ', strip=True) != 'Sepete Ekle'):
        return None
    title = ' '.join(heading[0].get_text(' ', strip=True).split())
    if (' '.join(unescape(str(product.get('name') or '')).split()) != title
            or model_name(f"{title} {product['mpn']}", brand) != name):
        return None
    visible = soup.select('.product-detail .web-ozel-prod .web-ozel-fiyat') if soup.select('.product-detail .web-ozel-prod') else soup.select('.product-detail .product-detail-price')
    gtin = retailer_gtin13(barcode[0]['ean'])
    if (not gtin or len(visible) != 1 or _cash_price(visible[0].get_text(' ', strip=True)) != price
            or _is_conditional_price(visible[0], offer, float(price))):
        return None
    photo = next((value for anchor in soup.select('.product-detail a[href*="/Upload/PRODUCT/"]')
                  if (value := selected_photo(urljoin(url, anchor['href']), {'cdn.vatanbilgisayar.com'}))), None)
    return {'platform': 'Vatan Bilgisayar', 'merchant': 'Vatan Bilgisayar', 'model': name,
            'gtin13': gtin, 'sku': sku, 'retailer_model_code': product['mpn'], 'source_title': title,
            'url': url, 'price': float(price), 'currency': 'TRY', 'condition': 'new', 'availability': 'in_stock',
            'observed_at': datetime.now(timezone.utc).isoformat(),
            '_image': photo, '_legal_name': 'vatan bilgisayar sanayi ve ticaret anonim sirketi'}


def vatan_offers(source, models, model_name, failures):
    source._load_robots()
    if source._rules is None:
        failures[f'vatan_{source._robots_status}'] += 1
        return {}
    last = None
    def read(url, limit):
        nonlocal last
        if not source._rules.can_fetch(BOT, url):
            return None, 'robots_disallowed'
        if last is not None:
            source._sleep(max(0, max(0.5, source._delay) - (time.monotonic() - last)))
        last = time.monotonic()
        raw, reason, _ = source._read(url, limit, 'text/html')
        return raw, reason

    categories = [VATAN_CATEGORY]
    brands = {name.split()[0].casefold() for name in models}
    candidates = defaultdict(list)
    for url in categories:
        raw, reason = read(url, 3 * 1024 * 1024)
        if raw is None:
            failures[f'vatan_{reason}'] += 1
            if reason in STOP_REASONS:
                return {}
            continue
        soup = BeautifulSoup(raw, 'html.parser')
        if url == VATAN_CATEGORY:
            linked = {urljoin(VATAN_HOME, a['href']) for a in soup.select('a[href]')}
            categories.extend(target for brand in sorted(brands)
                              if (target := f'{VATAN_HOME}/{brand}/bluetooth-kulaklik/') in linked)
        for card in soup.select('#productsLoad a.product-list-link[href]'):
            title = card.select_one('.product-list__product-name h3')
            if title is None:
                continue
            title = title.get_text(' ', strip=True)
            if not title:
                continue
            name = model_name(title, title.split()[0])
            target = urljoin(VATAN_HOME, card['href'])
            if name in models and _vatan_url(target) and target not in candidates[name]:
                candidates[name].append(target)
    results = defaultdict(list)
    for name, urls in candidates.items():
        for url in urls[:5]:
            raw, reason = read(url, 5 * 1024 * 1024)
            if raw is None:
                failures[f'vatan_{reason}'] += 1
                if reason in STOP_REASONS:
                    return results
                continue
            offer = selected_vatan_tws(raw, url, name, model_name)
            if offer:
                results[name].append(offer)
            else:
                failures['vatan_selected_offer_unproven'] += 1
    return results
