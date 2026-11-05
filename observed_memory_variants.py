"""Read-only recovery of another physical memory variant on a fetched PDP.

These observations never become an offer on the original catalog SKU. A
separate existing-model publication flow may independently verify them again.
"""
import json
import re

from bs4 import BeautifulSoup

from tr_price_scraper import TRPriceScraper
from trendyol_catalog_source import (_product_id, parse_catalog_listing, catalog_offer_matches,
                                    selected_attribute_capacity_gb)


def inspect_teknosa_memory_variant(html, url, name, catalog_specs, technical_observation=None, *, diagnostic=None):
    """Read selected source capacities, then prove that entire offer.

    A cart title is only a capacity hint here. The ordinary Teknosa reader
    must bind it to the visible heading, selected SKU, GTIN, seller, stock,
    schema and cash price. The later publisher also reads an independent
    technical document; neither stage fills omitted RAM/radio from the parent.
    """
    from priority_retailer_transport import product_host
    from teknosa_reviewed_source import parse_catalog_offer
    from teknosa_marketplace_source import selected_technical_specs
    if isinstance(diagnostic, dict):
        diagnostic.update(status='selected_memory_source_incomplete', writes=False,
                          scope='diagnostic_only_not_offer_evidence')
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
    # The selected PDP can omit RAM in its heading while explicitly listing
    # physical RAM/storage/5G/model in its own complete technical table. Use
    # the same independent extractor as the ordinary offer reader, without
    # passing a catalog profile. Never replace a contradictory title fact.
    technical = selected_technical_specs(mains[0], title, carts[0], technical_observation)
    if technical is not None:
        if (ambiguous or ram is not None and ram != technical['ram_gb']
                or storage is not None and storage != technical['storage_gb']
                or radios and radios != {technical['network_generation']}):
            if isinstance(diagnostic, dict):
                diagnostic['status'] = 'selected_title_technical_conflict'
            return None
        ram, storage = technical['ram_gb'], technical['storage_gb']
        radios = {technical['network_generation']}
    if (ambiguous or ram is None or storage is None or len(radios) != 1
            or not 1 <= ram <= 64 or not 32 <= storage <= 4096):
        if isinstance(diagnostic, dict):
            diagnostic.update(status='selected_physical_specs_incomplete',
                missing_or_invalid_fields=[key for key, valid in (
                    ('ram_gb', ram is not None and 1 <= ram <= 64),
                    ('storage_gb', storage is not None and 32 <= storage <= 4096),
                    ('network_generation', len(radios) == 1)) if not valid],
                ambiguous_title_capacities=ambiguous,
                independent_technical_document_read=technical_observation is not None)
        return None
    actual = {'ram_gb': ram, 'storage_gb': storage, 'network_generation': radios.pop()}
    expected = tuple(matcher._spec_int(catalog_specs.get(k)) for k in ('ram_gb', 'storage_gb'))
    radio = catalog_specs.get('network_generation') or matcher._radio_generation(name)
    if (not all(expected) or actual['network_generation'] != radio
            or (ram, storage) == expected):
        if isinstance(diagnostic, dict):
            diagnostic.update(status='catalog_variant_scope_mismatch',
                reason=('catalog_memory_incomplete' if not all(expected) else
                        'catalog_radio_missing' if radio is None else
                        'selected_radio_differs' if actual['network_generation'] != radio else
                        'selected_memory_already_cataloged'))
        return None
    model = matcher._catalog_model_name(name, catalog_specs)
    if not model:
        return None
    offer, reason = parse_catalog_offer(html, url, {'name': model, 'specs': actual}, technical_observation)
    if offer is None or not matcher.has_observed_offer_evidence(model, offer, actual):
        if isinstance(diagnostic, dict):
            diagnostic.update(status='selected_variant_parse_rejected',
                              reader_reason=reason if offer is None else 'selected_offer_evidence_incomplete',
                              observed_specs=actual)
        return None
    return {'scope': 'other_memory_variant_preview_only', 'writes': False,
            'catalog_memory': dict(zip(('ram_gb', 'storage_gb'), expected)),
            'observed_specs': actual, 'verification': reason, 'offer': offer}


def inspect_n11_memory_variant(html, url, name, catalog_specs, diagnostic=None):
    from n11_catalog_source import parse_catalog_listing as parse, catalog_offer_matches as matches
    from n11_capacity import storage_gb
    if isinstance(diagnostic, dict):
        diagnostic.update(status='selected_memory_source_incomplete', writes=False,
                          scope='diagnostic_only_not_offer_evidence')
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
    value = fields.get('Bellek Kapasitesi')
    found = re.fullmatch(r'([1-9]\d{0,3}) GB', value) if isinstance(value, str) else None
    if found is None:
        return None
    actual = {'ram_gb': int(found[1]), 'storage_gb': storage_gb(storage[0])}
    if actual['storage_gb'] is None:
        return None
    if not 1 <= actual['ram_gb'] <= 64 or not 32 <= actual['storage_gb'] <= 4096:
        return None
    if fields.get('5G Desteği') == 'Var':
        actual['network_generation'] = '5g'
    elif fields.get('5G Desteği') == 'Yok' and fields.get('4.5G Desteği') == 'Var':
        actual['network_generation'] = '4g'
    else:
        return None
    if isinstance(diagnostic, dict):
        diagnostic.update(status='catalog_variant_scope_mismatch', observed_specs=dict(actual))
    matcher = TRPriceScraper()
    expected = tuple(matcher._spec_int(catalog_specs.get(k)) for k in ('ram_gb', 'storage_gb'))
    radio = catalog_specs.get('network_generation') or matcher._radio_generation(name)
    if (not all(expected) or actual['network_generation'] != radio
            or tuple(actual[k] for k in ('ram_gb', 'storage_gb')) == expected):
        return None
    model = matcher._catalog_model_name(name, catalog_specs)
    offer, reason = parse(html, url, model, actual)
    if isinstance(diagnostic, dict):
        diagnostic.update(status='selected_variant_parse_rejected' if offer is None else 'selected_variant_match_rejected',
                          reader_reason=reason)
    if offer is None or not matches(offer, model, actual):
        return None
    if isinstance(diagnostic, dict):
        diagnostic['status'] = 'preview_verified'
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
        if key == 'storage_gb' and not values:
            from hepsiburada_capacity import selected_tb_storage
            storage = selected_tb_storage(soup, text)
            if storage is not None:
                actual[key] = storage
                continue
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


def inspect_trendyol_memory_variant(html, url, name, catalog_specs, diagnostic=None):
    if isinstance(diagnostic, dict):
        diagnostic.update(status='selected_memory_source_incomplete', writes=False,
                          scope='diagnostic_only_not_offer_evidence')
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
    soup = BeautifulSoup(html, 'html.parser')
    for script in soup.select('script'):
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
    if not isinstance(product, dict) or str(product.get('id')) != identity:
        return None
    if product.get('inStock') is not True:
        if isinstance(diagnostic, dict):
            diagnostic['status'] = 'selected_product_unavailable'
        return None
    if not isinstance(product.get('attributes'), list):
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
    headings = soup.select('h1[data-testid="product-title"]')
    heading_ram, heading_ambiguous = None, True
    if len(headings) == 1:
        heading_ram, _, heading_ambiguous = matcher._unambiguous_listing_capacities(
            headings[0].get_text(' ', strip=True))
    for key, label in (('ram_gb', 'RAM Kapasitesi'), ('storage_gb', 'Dahili Hafıza')):
        amount = selected_attribute_capacity_gb(fields.get(label), key)
        # The normal selected-offer reader already proves this native A16
        # template's literal "Yok" RAM through its explicit selected heading.
        # Discover that same physical amount here, without a parent default;
        # the complete ordinary proof below must independently accept it too.
        if amount is None and key == 'ram_gb' and fields.get(label) == 'Yok' and not heading_ambiguous:
            amount = heading_ram
        if amount is None:
            return None
        actual[key] = amount
    network = str(fields.get('Mobil Bağlantı Hızı') or '').casefold()
    if (network not in {'4g', '4.5g', '4,5g', '5g'} or not 1 <= actual['ram_gb'] <= 64
            or not 32 <= actual['storage_gb'] <= 4096):
        return None
    actual['network_generation'] = '5g' if network == '5g' else '4g'
    if isinstance(diagnostic, dict):
        diagnostic['observed_specs'] = dict(actual)
    if actual['network_generation'] != radio:
        if isinstance(diagnostic, dict):
            diagnostic['status'] = 'selected_radio_mismatch'
        return None
    if tuple(actual[k] for k in ('ram_gb', 'storage_gb')) == expected:
        if isinstance(diagnostic, dict):
            diagnostic['status'] = 'same_catalog_memory'
        return None
    # Re-run the complete ordinary selected-listing proof against ONLY the
    # capacities actually observed above. No expected catalog amount is copied
    # into the listing, and the original raw title remains untouched.
    offer, reason = parse_catalog_listing(html, url, model, actual)
    if offer is None or not catalog_offer_matches(offer, model, actual):
        if isinstance(diagnostic, dict):
            diagnostic['status'] = 'selected_variant_parse_rejected'
            # Only this parser's bounded internal reason; never selected state,
            # unrelated attributes, seller data or a response body.
            if isinstance(reason, str) and re.fullmatch(r'[a-z_]{1,80}', reason):
                diagnostic['reader_reason'] = reason
        return None
    if isinstance(diagnostic, dict):
        diagnostic['status'] = 'selected_variant_verified'
    return {'scope': 'other_memory_variant_preview_only', 'writes': False,
            'catalog_memory': dict(zip(('ram_gb', 'storage_gb'), expected)),
            'observed_specs': actual, 'verification': reason, 'offer': offer}
