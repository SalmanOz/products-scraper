"""Read-only recovery of another physical memory variant on a fetched PDP.

These observations never become an offer on the original catalog SKU. A
separate existing-model publication flow may independently verify them again.
"""
import json
import re

from bs4 import BeautifulSoup

from tr_price_scraper import TRPriceScraper
from trendyol_catalog_source import _product_id, parse_catalog_listing, catalog_offer_matches


def inspect_teknosa_memory_variant(html, url, name, catalog_specs):
    """Read capacities from the selected cart, then prove that entire offer.

    A cart title is only a capacity hint here. The ordinary Teknosa reader
    must bind it to the visible heading, selected SKU, GTIN, seller, stock,
    schema and cash price. The later publisher also reads an independent
    technical document; neither stage fills omitted RAM/radio from the parent.
    """
    from priority_retailer_transport import product_host
    from teknosa_reviewed_source import parse_catalog_offer
    if (product_host(url) != 'www.teknosa.com' or not isinstance(html, str)
            or len(html.encode()) > 5 * 1024 * 1024 or not isinstance(catalog_specs, dict)):
        return None
    mains = BeautifulSoup(html, 'html.parser').select('#pdp-main')
    if len(mains) != 1:
        return None
    carts = mains[0].select('button#addToCartButton')
    if len(carts) != 1:
        return None
    title = carts[0].get('data-product-name')
    if not isinstance(title, str) or not title:
        return None
    matcher = TRPriceScraper()
    ram, storage, ambiguous = matcher._unambiguous_listing_capacities(title)
    if not ambiguous and ram is None and storage is None:
        model = matcher._catalog_model_name(name, catalog_specs)
        pair = matcher.teknosa_shorthand_capacities(model, title, url)
        if pair is not None:
            ram, storage = pair
    radios = set(matcher._identity_words(title)) & {'4g', '5g', 'lte'}
    if 'lte' in radios:
        radios.discard('lte')
        radios.add('4g')
    if (ambiguous or ram is None or storage is None or len(radios) != 1
            or not 1 <= ram <= 64 or not 32 <= storage <= 4096):
        return None
    actual = {'ram_gb': ram, 'storage_gb': storage, 'network_generation': radios.pop()}
    expected = tuple(matcher._spec_int(catalog_specs.get(k)) for k in ('ram_gb', 'storage_gb'))
    radio = catalog_specs.get('network_generation') or matcher._radio_generation(name)
    if (not all(expected) or actual['network_generation'] != radio
            or (ram, storage) == expected):
        return None
    model = matcher._catalog_model_name(name, catalog_specs)
    if not model:
        return None
    offer, reason = parse_catalog_offer(html, url, {'name': model, 'specs': actual})
    if offer is None or not matcher.has_observed_offer_evidence(model, offer, actual):
        return None
    return {'scope': 'other_memory_variant_preview_only', 'writes': False,
            'catalog_memory': dict(zip(('ram_gb', 'storage_gb'), expected)),
            'observed_specs': actual, 'verification': reason, 'offer': offer}


def inspect_n11_memory_variant(html, url, name, catalog_specs):
    from n11_catalog_source import parse_catalog_listing as parse, catalog_offer_matches as matches
    if (not isinstance(html, str) or len(html.encode()) > 5 * 1024 * 1024
            or not isinstance(catalog_specs, dict)):
        return None
    states = []
    for script in BeautifulSoup(html, 'html.parser').select('script:not([src])'):
        body = script.get_text()
        for found in re.finditer(r'\bwindow\.model\s*=\s*(?=\{)', body):
            try:
                state, end = json.JSONDecoder().raw_decode(body[found.end():])
                if body[found.end() + end:].strip() not in ('', ';'):
                    return None
                states.append(state)
            except (ValueError, TypeError):
                return None
    if len(states) != 1 or not isinstance(states[0], dict):
        return None
    state = states[0]
    product = state.get('product')
    if not isinstance(product, dict):
        return None
    try:
        skus = json.loads(state['skus']) if isinstance(state.get('skus'), str) else state.get('skus')
    except (ValueError, TypeError):
        return None
    if not isinstance(skus, list):
        return None
    selected = [sku for sku in skus if isinstance(sku, dict) and sku.get('productId') == product.get('id')
                and str(sku.get('id')) == str(product.get('defaultSkuId'))]
    if len(selected) != 1 or not isinstance(selected[0].get('skuAttributes'), list):
        return None
    storage = [a.get('name') for a in selected[0]['skuAttributes'] if isinstance(a, dict)
               and isinstance(a.get('skuDefinition'), dict) and a['skuDefinition'].get('name') == 'Dahili Hafıza']
    if len(storage) != 1:
        return None
    fields = {}
    for row in product.get('productAttributes') or []:
        if not isinstance(row, dict) or row.get('key') not in {'Bellek Kapasitesi', '5G Desteği', '4.5G Desteği'}:
            continue
        if row['key'] in fields:
            return None
        fields[row['key']] = row.get('value')
    actual = {}
    for key, value in (('ram_gb', fields.get('Bellek Kapasitesi')), ('storage_gb', storage[0])):
        found = re.fullmatch(r'([1-9]\d{0,3}) GB', value) if isinstance(value, str) else None
        if found is None:
            return None
        actual[key] = int(found[1])
    if not 1 <= actual['ram_gb'] <= 64 or not 32 <= actual['storage_gb'] <= 4096:
        return None
    if fields.get('5G Desteği') == 'Var':
        actual['network_generation'] = '5g'
    elif fields.get('5G Desteği') == 'Yok' and fields.get('4.5G Desteği') == 'Var':
        actual['network_generation'] = '4g'
    else:
        return None
    matcher = TRPriceScraper()
    expected = tuple(matcher._spec_int(catalog_specs.get(k)) for k in ('ram_gb', 'storage_gb'))
    radio = catalog_specs.get('network_generation') or matcher._radio_generation(name)
    if (not all(expected) or actual['network_generation'] != radio
            or tuple(actual[k] for k in ('ram_gb', 'storage_gb')) == expected):
        return None
    model = matcher._catalog_model_name(name, catalog_specs)
    offer, reason = parse(html, url, model, actual)
    if offer is None or not matches(offer, model, actual):
        return None
    return {'scope': 'other_memory_variant_preview_only', 'writes': False,
            'catalog_memory': dict(zip(('ram_gb', 'storage_gb'), expected)),
            'observed_specs': actual, 'verification': reason, 'offer': offer}


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
