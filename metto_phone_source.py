"""Read selected phone offers and merchant identity from Metto Market."""

from datetime import datetime, timezone
import json
import re

from bs4 import BeautifulSoup

from ayaz_phone_source import only, value
from retailer_barcode import retailer_gtin13
from retailer_offer_refresh import _schema_nodes
from teknosa_reviewed_source import _amount, _cash_price
from tr_price_scraper import TRPriceScraper


METTO_HOST = 'www.mettomarket.com'
METTO_BASE = 'https://' + METTO_HOST
METTO_TERMS = METTO_BASE + '/mesafeli-satis-sozlesmesi'
METTO_CATEGORY = '/cep-telefonlari'
METTO_PATH = re.compile(r'/([a-z0-9-]{3,180}(?:/\d{2,4}-?gb)?[a-z0-9-]*-(?:akilli-telefon|cep-telefonu)(?:-[a-z0-9]+)*)')
COMPANY = 'berling group teknoloji limited sirketi'
MERCHANT = 'Metto Market'


def metto_phone_url(url):
    return (isinstance(url, str) and url.startswith(METTO_BASE) and len(url) <= 270
            and METTO_PATH.fullmatch(url.removeprefix(METTO_BASE)) is not None)


def page_offer_data(soup):
    found = []
    for script in soup.select('script:not([src])'):
        body = script.get_text()
        start = re.search(r"\bconst PRODUCT_PAGE_DATA\s*=\s*\{\s*price:\s*'([^']{1,40})'", body)
        if not start:
            continue
        match = re.match(r"\s*,\s*(?:eftPrice:\s*'[^']{1,40}'\s*,\s*)?variants:\s*(?=\{)", body[start.end():])
        try:
            variants = json.JSONDecoder().raw_decode(body[start.end() + match.end():])[0] if match else None
        except ValueError:
            return None
        found.append((_cash_price(start[1]), variants))
    return found[0] if len(found) == 1 else None


def verified_product_selection(soup, price_box, cart, title, specs, barcode, price):
    data = page_offer_data(soup)
    if not data or data[0] != price:
        return False
    variants = data[1]
    selectors = price_box.select('.variant-box select[data-variant-name]')
    if not selectors:
        return variants is None and not re.search(r'\b(?:checkVariant|variantCount)\b', cart['onclick'])
    storage = TRPriceScraper._spec_int(specs.get('storage_gb'))
    if not storage or len(selectors) != 2 or not isinstance(variants, dict) or variants.get('count') != 2:
        return False
    storage_label = f'{storage} GB'
    fields = {select['data-variant-name']: select for select in selectors}
    try:
        summary = variants['summary']
        if tuple(fields) == ('Depolama', 'Mobil Telefon Standardı'):
            radio = {'4g': '4G (LTE)', '5g': '5G'}.get(specs.get('network_generation'))
            labels = (storage_label, radio)
        elif tuple(fields) == ('Renk', 'Depolama') and isinstance(summary, dict):
            normalize = TRPriceScraper._normalize_words
            colors = [color for color, by_storage in summary.items() if isinstance(by_storage, dict)
                      and isinstance(candidate := by_storage.get(storage_label), dict)
                      and retailer_gtin13(candidate.get('barcode')) == barcode
                      and f' {normalize(color)} ' in f' {normalize(title)} ']
            if len(colors) != 1:
                return False
            labels = (colors[0], storage_label)
        else:
            return False
        for select, label in zip(fields.values(), labels, strict=True):
            choices = [option for option in select.select('option[value]:not([disabled])') if option['value'] == label]
            selected = [option.get('value') for option in select.select('option[selected]')]
            if len(choices) != 1 or selected not in ([], [label]):
                return False
        row = summary[labels[0]][labels[1]]
        prices = row['price_list']
        discounts = prices['indirimli_fiyatlar']
        quantity = row['quantity']
        return (row['in_stock'] is True and isinstance(quantity, str)
                and re.fullmatch(r'[1-9]\d{0,7}', quantity) is not None
                and retailer_gtin13(row['barcode']) == barcode
                and prices['ID'] == '_'.join(labels) and _amount(prices['fiyat_format_0']) == price
                and _cash_price(prices['fiyat']) == price
                and all(discounts[key]['aktif'] is False for key in ('sabit_indirim', 'promosyon')))
    except (KeyError, TypeError):
        return False


def metto_offer(html, legal_html, *, url, name, specs):
    if not metto_phone_url(url):
        return None
    matcher = TRPriceScraper()
    normalize = matcher._normalize_words
    specs = {**specs, 'network_generation': specs.get('network_generation') or matcher._radio_generation(name)}
    legal = BeautifulSoup(legal_html, 'html.parser')
    companies = [normalize(match[1]) for line in legal.stripped_strings
                 if (match := re.fullmatch(r'Ünvan:\s*(.+)', line, re.I))]
    if companies != [COMPANY] or value(legal, 'link[rel=canonical]') != METTO_TERMS:
        return None

    soup = BeautifulSoup(html, 'html.parser')
    root = only(soup, '.product-profile-1[data-product-id]')
    if not root or value(soup, 'link[rel=canonical]') != url:
        return None
    products = [node for node in _schema_nodes(soup, url) if node.get('@type') == 'Product']
    if len(products) != 1:
        return None
    product = products[0]
    title, sku, offer = value(root, 'h1.title'), product.get('sku'), product.get('offers')
    barcode = retailer_gtin13(product.get('gtin'))
    if (not title or title != product.get('name') or not barcode or not isinstance(offer, dict)
            or not isinstance(sku, str) or not re.fullmatch(r'[1-9]\d{3,11}', sku) or product.get('mpn') != sku
            or any(key in product and retailer_gtin13(product[key]) != barcode for key in ('gtin12', 'gtin13', 'gtin14'))
            or not matcher.has_observed_offer_evidence(name, {'source_title': title, 'url': url}, specs)):
        return None

    facts = {}
    for row in root.select('.product-profile-info > li:has(> .value)'):
        label = row.get_text(' ', strip=True).split(':', 1)[0]
        if label in facts:
            return None
        facts[label] = value(row, ':scope > .value')
    price_box = only(root, '.product-price-group')
    cart = only(price_box, '.price-group button.btn-cart[onclick]') if price_box else None
    price = _amount(offer.get('price'))
    seller = offer.get('seller')
    if (facts.get('Ürün Kodu') != sku or facts.get('Barkod') != barcode
            or offer.get('@type') != 'Offer' or offer.get('url') != url or offer.get('priceCurrency') != 'TRY'
            or offer.get('availability') != 'https://schema.org/InStock'
            or offer.get('itemCondition') != 'https://schema.org/NewCondition'
            or not isinstance(seller, dict) or normalize(seller.get('name')) != COMPANY
            or not cart or cart.has_attr('disabled') or cart.get('aria-disabled') == 'true'
            or cart.get_text(' ', strip=True) != 'Sepete Ekle' or not re.fullmatch(r'[1-9]\d{0,10}', root['data-product-id'])
            or not re.match(r"^\s*addCart\(\s*" + root['data-product-id'] + r"\s*,\s*'product'\s*,", cart['onclick'])
            or price is None or _cash_price(value(price_box, '.price-group .sale-variant-price')) != price
            or not verified_product_selection(soup, price_box, cart, title, specs, barcode, price)):
        return None
    return {'platform': MERCHANT, 'merchant': MERCHANT, '_legal_name': COMPANY,
            'price': float(price), 'currency': 'TRY', 'availability': 'in_stock', 'gtin13': barcode,
            'source_title': title, 'url': url, 'observed_at': datetime.now(timezone.utc).isoformat()}
