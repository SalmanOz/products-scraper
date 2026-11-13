"""The public selected PDP's lazy technical tab; never price or seller proof.

Observed native tab code uses GET /getClassificationData?productCode=... from
#js-psh-btn. No account state or private endpoint is involved. The original
HTML and response must bind the same selected code, model and physical facts.
"""
from datetime import datetime, timezone
import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

MAX_BYTES = 64 * 1024
MAX_AGE_SECONDS = 60
FIELDS = {
    'Bellek Kapasitesi (RAM)': 'teknosaClassificationCatalog/1.0/CLS-2001.bellek_kapasitesi_100',
    'Dahili Hafıza': 'teknosaClassificationCatalog/1.0/CLS-2001.dahili_hafiza_100',
    '5G Desteği': 'teknosaClassificationCatalog/1.0/CLS-2001.5g_100',
    'Model Kodu': 'teknosaClassificationCatalog/1.0/CLS-2000.uretici_kodu_100',
}


def classification_url(document_url, html):
    from priority_retailer_transport import product_host
    if (product_host(document_url) != 'www.teknosa.com' or urlparse(document_url).query
            or not isinstance(html, str) or len(html.encode()) > 5 * 1024 * 1024):
        return None
    soup = BeautifulSoup(html, 'html.parser')
    mains = soup.select('#pdp-main')
    triggers = soup.select('button#js-psh-btn[data-product-code]')
    if len(mains) != 1 or len(triggers) != 1:
        return None
    carts = mains[0].select('button#addToCartButton')
    sku = urlparse(document_url).path.rsplit('-p-', 1)[-1]
    if (len(carts) != 1 or triggers[0].get('data-product-code') != sku
            or carts[0].get('data-product-id') != sku
            or carts[0].get('data-product-url') != urlparse(document_url).path):
        return None
    # Only the observed, still empty native tab may require another read.
    roots = mains[0].find_parent(class_='pdp')
    if (roots is None or len(roots.select('#pdp-main')) != 1
            or triggers[0].find_parent(class_='pdp') is not roots):
        return None
    sections = roots.select('#pdp-technical[data-title="Teknik Özellikler"]')
    if len(sections) != 1 or len(sections[0].select('#tableContainer')) != 1:
        return None
    headings = sections[0].select(':scope > button.pdp-acc-trg > h2')
    if (len(headings) != 1 or headings[0].get_text(' ', strip=True) != 'Teknik Özellikler'
            or sections[0].select('#tableContainer table')):
        return None
    return 'https://www.teknosa.com/getClassificationData?productCode=' + sku


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_field')
        result[key] = value
    return result


def classification_values(observation, main, cart):
    """Read raw physical fields from the selected SKU's bound classification response."""
    if not isinstance(observation, dict):
        return None
    document_url, source_url = observation.get('document_url'), observation.get('source_url')
    from priority_retailer_transport import product_host
    sku = cart.get('data-product-id')
    if (not isinstance(document_url, str) or not isinstance(source_url, str)
            or product_host(document_url) != 'www.teknosa.com' or urlparse(document_url).query
            or not isinstance(sku, str) or not urlparse(document_url).path.endswith('-p-' + sku)
            or cart.get('data-product-url') != urlparse(document_url).path
            or source_url != 'https://www.teknosa.com/getClassificationData?productCode=' + sku
            or classification_url(document_url, str(main.find_parent(class_='pdp'))) != source_url):
        return None
    try:
        stamp = datetime.fromisoformat(observation['observed_at'])
        if not 0 <= (datetime.now(timezone.utc) - stamp).total_seconds() <= MAX_AGE_SECONDS:
            return None
        body = observation['body']
        if not isinstance(body, str) or len(body.encode()) > MAX_BYTES:
            return None
        # Chromium may wrap a JSON document in a single PRE. Nothing else may
        # contribute data, scripts, forms or text to that representation.
        if body.lstrip().startswith('<'):
            soup = BeautifulSoup(body, 'html.parser')
            pre = soup.find_all('pre')
            if (len(pre) != 1 or soup.find(['script', 'form', 'iframe'])
                    or soup.get_text(' ', strip=True) != pre[0].get_text(' ', strip=True)):
                return None
            body = pre[0].get_text()
        data = json.loads(body, object_pairs_hook=_unique_object)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return None
    if (not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict)
            or data[0].get('code') != '' or data[0].get('name') != ''):
        return None
    rows = data[0].get('features')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        return None
    fields = {}
    for row in rows:
        key = row.get('name') if isinstance(row, dict) else None
        if not isinstance(key, str):
            return None
        if key not in FIELDS:
            continue
        values = row.get('featureValues')
        if (key in fields or row.get('code') != FIELDS[key] or row.get('range') is not False
                or not isinstance(values, list) or len(values) != 1
                or not isinstance(values[0], dict) or not isinstance(values[0].get('value'), str)):
            return None
        fields[key] = values[0]['value']
    return fields


def response_fields(observation, main, title, cart):
    """Independent tab fields; no catalog expectations or inferred capacities."""
    fields = classification_values(observation, main, cart)
    if fields is None or set(fields) != set(FIELDS):
        return None
    ram = re.fullmatch(r'([1-9]\d?) GB', fields['Bellek Kapasitesi (RAM)'])
    storage = re.fullmatch(r'([1-9]\d{1,3}) GB', fields['Dahili Hafıza'])
    brand = cart.get('data-product-brand')
    from tr_price_scraper import TRPriceScraper
    model_words = set(TRPriceScraper._identity_words(fields['Model Kodu']))
    generic = {'galaxy', 'iphone', 'phone', 'smartphone', 'akilli', 'telefon', 'cep',
               'pro', 'max', 'ultra', 'lite', 'plus', 'note', 'fe', 'se', 'mini', 'edge', '4g', '5g'}
    brand_words = set(TRPriceScraper._identity_words(brand)) if isinstance(brand, str) else set()
    if (ram is None or storage is None or fields['5G Desteği'] != 'Evet'
            or not isinstance(brand, str) or not brand.strip()
            or not any(len(word) > 1 for word in model_words - generic - brand_words)
            or not 1 <= int(ram[1]) <= 64 or not 32 <= int(storage[1]) <= 4096
            or not TRPriceScraper().is_strict_match(brand + ' ' + fields['Model Kodu'], title, {})):
        return None
    return {'ram_gb': int(ram[1]), 'storage_gb': int(storage[1]), 'network_generation': '5g',
            'selected_teknosa_physical_specs_verified': True}


def read_observation(transport, html, document_url, *, fresh=False):
    """Read once through the caller's existing circuit and request budget."""
    target = classification_url(document_url, html)
    read = getattr(type(transport), 'read_teknosa_classification', None)
    if target is None or not callable(read):
        return None, 'selected_public_technical_tab_unavailable'
    body, reason, status = (transport.read_teknosa_classification(document_url, target, fresh=True)
                            if fresh else transport.read_teknosa_classification(document_url, target))
    if body is None or reason != 'document_read' or status != 200:
        return None, reason
    return {'document_url': document_url, 'source_url': target, 'body': body,
            'observed_at': datetime.now(timezone.utc).isoformat()}, reason
