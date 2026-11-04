"""Read-only recovery of another physical memory variant on a fetched PDP.

These observations never become an offer on the original catalog SKU. A
separate existing-model publication flow may independently verify them again.
"""
import json
import re

from bs4 import BeautifulSoup

from tr_price_scraper import TRPriceScraper
from trendyol_catalog_source import _product_id, parse_catalog_listing, catalog_offer_matches


def inspect_hepsiburada_memory_variant(html, url, name, catalog_specs):
    from hepsiburada_catalog_source import selected_technical_text, parse_catalog_buy_box
    from hepsiburada_catalog_source import catalog_offer_matches as matches
    if not isinstance(html, str) or len(html.encode()) > 5 * 1024 * 1024:
        return None
    soup = BeautifulSoup(html, 'html.parser')
    scripts = soup.select('script#reduxStore')
    if len(scripts) != 1:
        return None
    try:
        product = json.loads(scripts[0].get_text())['productState']['product']
        text = selected_technical_text(soup, product['sku'], product['productId'],
                                       product['listingId'], product['merchant']['id'])
    except (ValueError, KeyError, TypeError):
        return None
    actual = {}
    for key, label in (('ram_gb', 'RAM Kapasitesi'), ('storage_gb', 'Dahili Hafıza')):
        values = re.findall(r'\b' + label + r'\s+([1-9]\d{0,3})\s+GB\b', text)
        if len(values) != 1 or len(re.findall(r'\b' + label + r'\b', text)) != 1:
            return None
        actual[key] = int(values[0])
    radios = re.findall(r'\bMobil Bağlantı Hızı\s+(5G|4[.,]5G|4G)\b', text, re.I)
    if (len(radios) != 1 or not 1 <= actual['ram_gb'] <= 64 or not 32 <= actual['storage_gb'] <= 4096
            or not isinstance(catalog_specs, dict)):
        return None
    actual['network_generation'] = '5g' if radios[0].casefold() == '5g' else '4g'
    matcher = TRPriceScraper()
    expected = tuple(matcher._spec_int(catalog_specs.get(k)) for k in ('ram_gb', 'storage_gb'))
    radio = catalog_specs.get('network_generation') or matcher._radio_generation(name)
    if (not all(expected) or actual['network_generation'] != radio
            or tuple(actual[k] for k in ('ram_gb', 'storage_gb')) == expected):
        return None
    model = matcher._catalog_model_name(name, catalog_specs)
    offer, reason = parse_catalog_buy_box(html, url, model, actual)
    if offer is None or not matches(offer, model, actual):
        return None
    return {'scope': 'other_memory_variant_preview_only', 'writes': False,
            'catalog_memory': dict(zip(('ram_gb', 'storage_gb'), expected)),
            'observed_specs': actual, 'verification': reason, 'offer': offer}


def inspect_trendyol_memory_variant(html, url, name, catalog_specs):
    identity = _product_id(url)
    if (identity is None or not isinstance(html, str) or len(html.encode()) > 5 * 1024 * 1024
            or not isinstance(catalog_specs, dict)):
        return None
    matcher = TRPriceScraper()
    expected = tuple(matcher._spec_int(catalog_specs.get(k)) for k in ('ram_gb', 'storage_gb'))
    radio = catalog_specs.get('network_generation') or matcher._radio_generation(name)
    model = matcher._catalog_model_name(name, catalog_specs)
    if not all(expected) or radio not in {'4g', '5g'} or not model:
        return None
    states = []
    for script in BeautifulSoup(html, 'html.parser').select('script'):
        body = script.get_text()
        for match in re.finditer(r'window\["__envoy__SHARED_PROPS"\]\s*=\s*', body):
            try:
                state, end = json.JSONDecoder().raw_decode(body[match.end():])
                if body[match.end() + end:].strip() not in ('', ';'):
                    return None
                states.append(state)
            except (ValueError, TypeError):
                return None
    if len(states) != 1 or not isinstance(states[0], dict):
        return None
    product = states[0].get('product')
    if (not isinstance(product, dict) or str(product.get('id')) != identity
            or product.get('inStock') is not True or not isinstance(product.get('attributes'), list)):
        return None
    fields = {}
    for row in product['attributes']:
        if not isinstance(row, dict) or not isinstance(row.get('key'), dict) or not isinstance(row.get('value'), dict):
            continue
        key = row['key'].get('name')
        if key in {'RAM Kapasitesi', 'Dahili Hafıza', 'Mobil Bağlantı Hızı'}:
            if key in fields:
                return None
            fields[key] = row['value'].get('name')
    actual = {}
    for key, label in (('ram_gb', 'RAM Kapasitesi'), ('storage_gb', 'Dahili Hafıza')):
        value = fields.get(label)
        match = re.fullmatch(r'([1-9]\d{0,3})\s*GB', value, re.I) if isinstance(value, str) else None
        if match is None:
            return None
        actual[key] = int(match[1])
    network = str(fields.get('Mobil Bağlantı Hızı') or '').casefold()
    if (network not in {'4g', '4.5g', '4,5g', '5g'} or not 1 <= actual['ram_gb'] <= 64
            or not 32 <= actual['storage_gb'] <= 4096):
        return None
    actual['network_generation'] = '5g' if network == '5g' else '4g'
    if actual['network_generation'] != radio or tuple(actual[k] for k in ('ram_gb', 'storage_gb')) == expected:
        return None
    # Re-run the complete ordinary selected-listing proof against ONLY the
    # capacities actually observed above. No expected catalog amount is copied
    # into the listing, and the original raw title remains untouched.
    offer, reason = parse_catalog_listing(html, url, model, actual)
    if offer is None or not catalog_offer_matches(offer, model, actual):
        return None
    return {'scope': 'other_memory_variant_preview_only', 'writes': False,
            'catalog_memory': dict(zip(('ram_gb', 'storage_gb'), expected)),
            'observed_specs': actual, 'verification': reason, 'offer': offer}
