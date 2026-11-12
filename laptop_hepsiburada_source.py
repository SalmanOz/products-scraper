"""Read a Hepsiburada laptop's selected configuration and buy box."""
import json
import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from audit_hepsiburada_iphone16pm_readonly import parse_exact_buy_box
from hepsiburada_catalog_source import _identity, selected_technical_text
from hepsiburada_s25_fe_source import MAX_HTML_BYTES, _nodes
from laptop_retailer_sources import LAPTOP_LABEL, WORDS, _brand, _gb, _photo, laptop_facts
from priority_retailer_transport import product_host
from retailer_barcode import retailer_gtin13

# Reviewed marketplace account links. Both public profiles must still agree
# with these identities before an offer can count as an independent seller.
TEKNOSA_SELLER_PROFILES = {
    'notebookstore': ('f21b5d6e-acee-4b69-85c2-1633598a0115', 'notebookstore', '2272',
                      'victory bilgisayar yazilim temizlik gida ve emlak otomotiv sanayi limited sirketi'),
}


def selected_hepsiburada(html, url):
    if (product_host(url) != 'www.hepsiburada.com' or not isinstance(html, str)
            or len(html.encode()) > MAX_HTML_BYTES):
        return None, 'invalid_laptop_pdp'
    soup = BeautifulSoup(html, 'html.parser')
    scripts = soup.select('script#reduxStore')
    try:
        product = json.loads(scripts[0].get_text())['productState']['product'] if len(scripts) == 1 else {}
        sku, product_id, listing = (product[k] for k in ('sku', 'productId', 'listingId'))
        merchant = product['merchant']
        seller, merchant_id, slug = (merchant[k] for k in ('name', 'id', 'urlPostfix'))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None, 'selected_laptop_state_missing'
    uuid = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
    if (not isinstance(sku, str) or not re.fullmatch(r'(?:HBCV|HBV)[A-Z0-9]{5,30}', sku)
            or not isinstance(product_id, str) or not re.fullmatch(r'(?:HBC|HB)[A-Z0-9]{5,30}', product_id)
            or not _identity(url, product_id, sku)
            or not all(isinstance(value, str) and re.fullmatch(uuid, value) for value in (merchant_id, listing))
            or not isinstance(seller, str) or not 0 < len(seller) <= 100
            or not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9-]{1,100}', slug)
            or product.get('isBundle') is not False):
        return None, 'selected_laptop_sku_or_seller_mismatch'
    if any(WORDS(value) not in {WORDS(slug), WORDS(seller)} for value in
           parse_qs(urlparse(url).query).get('magaza', [])):
        return None, 'selected_laptop_seller_changed'
    categories = product.get('categories')
    variants = product.get('variants')
    if not (isinstance(categories, list) and any(isinstance(row, dict) and row.get('categoryId') == '98'
            and row.get('urlKeyword') == 'laptop-notebook-dizustu-bilgisayarlar' for row in categories)
            and isinstance(variants, list)):
        return None, 'not_selected_laptop_category'
    default = [row for row in variants if isinstance(row, dict) and row.get('isDefault') is True]
    if len(default) != 1 or default[0].get('sku') != sku:
        return None, 'selected_laptop_variant_missing'
    variant = default[0]
    gtin = retailer_gtin13(variant.get('barcode'))
    barcodes = variant.get('barcodes')
    products = [node for node in _nodes(soup) if node.get('@type') == 'Product' and node.get('sku') == sku]
    if (not gtin or not isinstance(barcodes, list) or not barcodes
            or {retailer_gtin13(code.strip()) if isinstance(code, str) else None for code in barcodes} != {gtin}
            or retailer_gtin13(product.get('barcode')) != gtin or len(products) != 1
            or retailer_gtin13(products[0].get('gtin')) != gtin):
        return None, 'selected_laptop_barcode_unproven'
    schema = products[0]
    schema_offer = schema.get('offers')
    schema_url = schema_offer.get('url') if isinstance(schema_offer, dict) else None
    if not schema_url or not _identity(schema_url, product_id, sku):
        return None, 'selected_laptop_schema_url_mismatch'
    brand = _brand(product.get('brand'))
    schema_brand = schema.get('brand')
    props = variant.get('properties')
    keys = {'Ram (Sistem Belleği)': 'ram', 'SSD Kapasitesi': 'ssd'}
    if (not isinstance(schema_brand, dict) or _brand(schema_brand.get('name')) != brand
            or not isinstance(props, list) or not 2 <= len(props) <= 3):
        return None, 'selected_laptop_config_missing'
    attrs, controls = {}, {}
    for prop in props:
        if not isinstance(prop, dict) or not isinstance(prop.get('valueObject'), dict):
            return None, 'selected_laptop_config_missing'
        label, value = prop.get('name'), prop['valueObject'].get('actualValue')
        if label not in keys.keys() | {'Renk'} or label in controls or not isinstance(value, str):
            return None, 'selected_laptop_config_missing'
        controls[label] = value
        if label in keys:
            if not _gb(value):
                return None, 'selected_laptop_config_missing'
            attrs[keys[label]] = value
    if set(attrs) != {'ram', 'ssd'} or variant.get('isInStock') is not True or variant.get('isPreOrder') is not False:
        return None, 'selected_laptop_variant_unavailable'
    detail = selected_technical_text(soup, sku, product_id, listing, merchant_id)
    if not re.search(r'\bStok Kodu\s+' + sku + r'\b', detail):
        return None, 'selected_laptop_technical_sku_missing'
    for label, value in controls.items():
        buttons = soup.find_all('button', attrs={'data-test-id': f'variant-box-{label}-{value}'})
        if len(buttons) != 1 or buttons[0].get('aria-label') != f'Seçili {label} {value}':
            return None, 'selected_laptop_control_mismatch'
    heading = soup.select_one('h1[data-test-id="title"]')
    title = heading.get_text(' ', strip=True) if heading else ''
    facts = laptop_facts(title, attrs, brand)
    if not facts or not LAPTOP_LABEL.search(WORDS(title)):
        return None, 'selected_laptop_config_missing'
    processor_vendors = re.findall(r'\bislemci(?: tipi)? (amd ryzen|intel core)\b', WORDS(detail))
    if any(not WORDS(facts['cpu']).startswith(vendor) for vendor in processor_vendors):
        return None, 'selected_laptop_processor_conflict'
    for title_to_verify, native in ((schema.get('name'), facts['mpn']),
                                    (variant.get('name'), ''), (product.get('name'), '')):
        # This schema shortens the heading but is independently tied to the
        # same selected SKU and GTIN. An explicitly different MPN still fails.
        other = laptop_facts(title_to_verify, attrs, brand, native)
        if not other or any(facts[key] != other[key] for key in ('mpn', 'cpu', 'ram', 'ssd', 'discrete', 'os')):
            return None, 'selected_laptop_config_mismatch'
    seller_url = 'https://www.hepsiburada.com/magaza/' + slug
    scope = dict(URL=url, SCHEMA_URL=schema_url, PRODUCT_ID=product_id, SKU=sku,
                 LISTING_ID=listing, MERCHANT_ID=merchant_id, MERCHANT_POSTFIX=slug,
                 SELLER=seller, SELLER_URL=seller_url, GTIN13=gtin, generic_catalog=True)
    offer, reason = parse_exact_buy_box(html, url, scope=scope, verify_laptop=lambda *_: variant)
    if not offer:
        return None, reason
    return {**offer, 'platform': 'Hepsiburada', 'facts': facts,
            '_image': _photo(variant.get('thumbnail'), 'productimages.hepsiburada.net'),
            'seller_url': seller_url}, reason


def hepsiburada_company_identity(html):
    text = WORDS(BeautifulSoup(html, 'html.parser').get_text(' ', strip=True))
    if re.search(r'\bunvani d market elektronik hizmetler ve (?:ticaret|tic)\b', text):
        return {'_legal_name': 'd market elektronik hizmetler ve ticaret anonim sirketi',
                '_tax_key': ''}
    return None


def hepsiburada_seller_profile_matches(html, offer, slug):
    seller_url = f'https://www.hepsiburada.com/magaza/{slug}'
    if offer['seller_url'] != seller_url or WORDS(offer['merchant']) != WORDS(slug):
        return False
    soup = BeautifulSoup(html, 'html.parser')
    try:
        scripts = soup.select('script#reduxStore')
        detail = json.loads(scripts[0].get_text())['merchantState']['merchantDetail'] if len(scripts) == 1 else None
    except (ValueError, KeyError, TypeError):
        return False
    return (isinstance(detail, dict) and detail.get('id') == offer['merchant_id']
            and detail.get('urlPostfix') == slug and detail.get('name') == offer['merchant']
            and [tag.get('href') for tag in soup.select('link[rel="canonical"]')] == [seller_url]
            and [tag.get_text(' ', strip=True) for tag in soup.select('h1')] == [offer['merchant']])

def hepsiburada_seller_slug(url):
    if not isinstance(url, str):
        return None
    match = re.fullmatch(r'https://www\.hepsiburada\.com/magaza/([a-z0-9-]{1,100})', url)
    return match[1] if match else None


def read_hepsiburada_seller_identity(offer, transport):
    url = offer.get('seller_url')
    if not hepsiburada_seller_slug(url):
        return None
    target = url + '?tab=profile'
    html, _, _ = transport._read_document(target, 'www.hepsiburada.com', category=True)
    if html and transport.resolved_url(target) == target:
        return hepsiburada_seller_identity(html, offer)
    return None


def hepsiburada_seller_identity(html, offer):
    """Bind the public seller profile's trade name to the selected merchant."""
    url = offer.get('seller_url')
    slug = hepsiburada_seller_slug(url)
    if not slug or not isinstance(html, str) or len(html.encode()) > MAX_HTML_BYTES:
        return None
    soup = BeautifulSoup(html, 'html.parser')
    canonical = [node.get('href') for node in soup.select('link[rel="canonical"]')]
    profiles = soup.select('div.MerchantProfile[id^="MerchantProfile_"]')
    scripts = soup.select('script#reduxStore')
    if canonical != [url] or len(profiles) != 1 or len(scripts) != 1:
        return None
    profile = profiles[0]
    script = profile.find_previous_sibling('script')
    if script is None:
        return None
    component = profile['id'].removeprefix('MerchantProfile_')
    match = re.search(
        r"window\.MCONTENT\.MERCHANTPROFILE\s*=\s*Object\.assign\("
        r"window\.MCONTENT\.MERCHANTPROFILE\s*\|\|\s*\{\},\s*\{\s*'"
        + re.escape(component) + r"':\s*\{\s*'STATE':\s*", script.get_text(),
    )
    if match is None:
        return None
    try:
        selected = json.loads(scripts[0].get_text())['merchantState']['merchantDetail']
        detail = json.JSONDecoder().raw_decode(script.get_text()[match.end():])[0]['data']['merchantDetail']
        if any(row['id'] != offer['merchant_id'] or row['urlPostfix'] != slug
               for row in (selected, detail)) or selected['name'] != offer['merchant']:
            return None
        properties = detail['properties']
        fields = {}
        for row in properties:
            key = WORDS(row['key'])
            if key in fields:
                return None
            fields[key] = row['val']
        name = fields.get('ticaret unvani')
        tax = fields.get('vkn')
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if not isinstance(name, str) or len(WORDS(name).split()) < 2 or len(name) > 240:
        return None
    return {'_legal_name': name, '_tax_key': tax if isinstance(tax, str) and re.fullmatch(r'\d{10,11}', tax) else ''}
