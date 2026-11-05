"""Read the actual selected Teknosa marketplace offer, not other seller cards."""
from datetime import datetime, timezone
import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from mediamarkt_marketplace_audit import _gtin13
from retailer_barcode import retailer_gtin13
from priority_retailer_transport import product_host
from teknosa_reviewed_source import MAX_HTML_BYTES, _amount, _cash_price
from tr_price_scraper import TRPriceScraper


def selected_technical_specs(main, title, cart, technical_observation=None):
    """Read only the selected PDP's own explicit physical specification table.

    No catalog profile enters this function. A title omission can be filled
    only by independent, complete source facts; recommendations and other
    product tables cannot establish them.
    """
    root = main.find_parent(class_='pdp')
    if root is None or len(root.select('#pdp-main')) != 1:
        return None
    sections = root.select('#pdp-technical[data-title="Teknik Özellikler"]')
    if len(sections) != 1:
        return None
    section = sections[0]
    headings = section.select(':scope > button.pdp-acc-trg > h2')
    tables = section.select('#tableContainer > table.tableFeature')
    if not tables and technical_observation is not None:
        if (len(headings) != 1 or headings[0].get_text(' ', strip=True) != 'Teknik Özellikler'
                or len(section.select('#tableContainer')) != 1 or section.select('#tableContainer table')):
            return None
        from teknosa_public_technical import response_fields
        return response_fields(technical_observation, main, title, cart)
    if (len(headings) != 1 or headings[0].get_text(' ', strip=True) != 'Teknik Özellikler'
            or len(tables) != 1):
        return None
    rows = tables[0].find_all('tr', recursive=False)
    if not rows or len(rows) % 2 or len(rows) > 100:
        return None
    facts = {}
    for headers, values in zip(rows[::2], rows[1::2]):
        labels, cells = headers.find_all('th', recursive=False), values.find_all('td', recursive=False)
        if not labels or len(labels) != len(cells):
            return None
        for label, cell in zip(labels, cells):
            key = label.get_text(' ', strip=True)
            if key not in {'Bellek Kapasitesi (RAM)', 'Dahili Hafıza', '5G Desteği', 'Model Kodu'}:
                continue
            if key in facts or ' '.join(cell.get('class', [])) != 'featureVal ' + key:
                return None
            facts[key] = cell.get_text(' ', strip=True)
    if set(facts) != {'Bellek Kapasitesi (RAM)', 'Dahili Hafıza', '5G Desteği', 'Model Kodu'}:
        return None
    ram = re.fullmatch(r'([1-9]\d?) GB', facts['Bellek Kapasitesi (RAM)'])
    storage = re.fullmatch(r'([1-9]\d{1,3}) GB', facts['Dahili Hafıza'])
    # This observed table explicitly confirms 5G. A missing/negative 5G field
    # never establishes 4G, and no catalog radio can replace source evidence.
    brand = cart.get('data-product-brand')
    if (ram is None or storage is None or facts['5G Desteği'] != 'Evet'
            or not isinstance(brand, str) or not brand.strip()
            or not 1 <= int(ram[1]) <= 64 or not 32 <= int(storage[1]) <= 4096
            or not TRPriceScraper().is_strict_match(brand + ' ' + facts['Model Kodu'], title, {})):
        return None
    return {'ram_gb': int(ram[1]), 'storage_gb': int(storage[1]), 'network_generation': '5g',
            'selected_teknosa_physical_specs_verified': True}


def _selected_marketplace_commerce(html, url, technical_observation=None):
    """Verify the selected store's commercial facts independently of a catalog profile."""
    if (product_host(url) != 'www.teknosa.com' or urlparse(url).query
            or not isinstance(html, str) or len(html.encode()) > MAX_HTML_BYTES):
        return None, 'invalid_catalog_pdp'
    soup = BeautifulSoup(html, 'html.parser')
    mains = soup.select('#pdp-main')
    if len(mains) != 1:
        return None, 'selected_main_missing'
    main = mains[0]
    headings = main.select('h1.pdp-title .replaceName')
    carts = main.select('button#addToCartButton.btn-add-to-cart')
    sellers = main.select('.pdp-seller-info')
    if len(headings) != 1 or len(carts) != 1 or len(sellers) != 1:
        return None, 'selected_marketplace_controls_missing'
    cart = carts[0]
    full_heading = main.select('h1.pdp-title')
    observed_titles = {headings[0].get_text(' ', strip=True)}
    if len(full_heading) == 1:
        observed_titles.add(full_heading[0].get_text(' ', strip=True))
    # Some PDPs put the observed brand in a sibling link, others repeat it
    # inside replaceName. Select only an actual heading spelling agreeing with
    # the selected cart; never prepend the catalog's brand.
    title = cart.get('data-product-name') if cart.get('data-product-name') in observed_titles else ''
    if not title and len(full_heading) == 1:
        # The live template removes every occurrence of its brand from
        # replaceName, including the warranty suffix. Bind that rendering to
        # the original PDP input, visible brand and selected cart; never add a
        # catalog brand or use an arbitrary hidden title as model evidence.
        heading = full_heading[0]
        originals = heading.select('input.productNameReplace.hidden')
        brands = heading.select('input.brandNameReplace.hidden')
        brand_links = heading.select('a.link')
        original, brand = cart.get('data-product-name') or '', cart.get('data-product-brand') or ''
        clean = lambda text: ' '.join(str(text or '').split())
        brand_path = (brand_links[0].get('href') or '') if len(brand_links) == 1 else ''
        # Observed Samsung PDPs use this exact short brand route. It still
        # must bind the visible brand, both original inputs and selected cart.
        observed_brand_route = bool(re.fullmatch(r'/[a-z0-9-]+-b-[1-9]\d*', brand_path)
                                    or brand == 'Samsung' and brand_path == '/samsung')
        if (len(originals) == len(brands) == len(brand_links) == 1
                and originals[0].get('value') == original and brands[0].get('value') == brand
                and brand and original.startswith(brand + ' ') and original.count(brand) >= 2
                and brand_links[0].get_text(' ', strip=True) == brand
                and observed_brand_route
                and clean(original.replace(brand, '')) == clean(headings[0].get_text(' ', strip=True))
                and clean(brand + ' ' + headings[0].get_text(' ', strip=True)) == clean(heading.get_text(' ', strip=True))):
            title = original
    sku = url.rsplit('-p-', 1)[1]
    seller_links = sellers[0].find_all('a', recursive=False)
    seller = seller_links[0].get_text(' ', strip=True) if len(seller_links) == 1 else ''
    shop = cart.get('data-product-shop-id')
    raw_barcode = cart.get('data-product-ean')
    gtin = retailer_gtin13(raw_barcode)
    pending_cart = (cart.has_attr('disabled') and cart.get('disabled') in ('', 'disabled')
                    and 'js-enable-btn' in cart.get('class', [])
                    and cart.get('aria-disabled') != 'true')
    if (not title or not seller or len(seller) > 100 or re.search(r'[\x00-\x1f]', seller)
            or seller.casefold() == 'teknosa' or not re.fullmatch(r'[1-9]\d{2,9}', str(shop or ''))
            or shop == '1001' or not gtin or cart.get('data-shop-name') != seller
            or not re.fullmatch(r'/magaza/[a-z0-9-]+', str(seller_links[0].get('href') or ''))
            or cart.get('data-product-id') != sku or cart.get('data-product-name') != title
            or cart.get('data-product-url') != urlparse(url).path
            or (cart.has_attr('disabled') and not pending_cart) or cart.get('aria-disabled') == 'true'
            or cart.get_text(' ', strip=True) != 'Sepete Ekle'):
        return None, 'selected_marketplace_identity_mismatch'
    shops = main.select('input#shopID')
    listing = re.fullmatch(r'shop_offer_([1-9]\d{2,12})', str(cart.get('data-shop-id') or ''))
    if (len(shops) != 1 or shops[0].get('value') != shop or listing is None
            or 'js-shop_offer_' + listing[1] not in cart.get('class', [])):
        return None, 'selected_shop_or_listing_mismatch'
    price = _amount(cart.get('data-price-with-discount'))
    list_price = _amount(cart.get('data-price-without-discount'))
    # A normal public sale has two different amounts. Keep each bound to
    # its own selected-shop state field; only the sale amount may agree with
    # the visible cash price and schema. Conditional prices still fail below.
    if price is None or list_price is None or list_price < price:
        return None, 'selected_marketplace_cash_price_missing'
    # These fields describe Teknosa's own inventory, not the selected shop's
    # offer. For a marketplace-only PDP they are empty and stock is N. Do not
    # use those unrelated analytics fields as this shop's price/stock proof.
    # Do not compare another merchant's base inventory price with this selected
    # shop. Its SKU+shop insider state, listing fields, visible cash, schema and
    # selected delivery promise must independently agree below.
    states = []
    for script in soup.select('script'):
        for match in re.finditer(r'window\.insider_object\s*=\s*(?=\{)', script.get_text()):
            try:
                root, _ = json.JSONDecoder().raw_decode(script.get_text()[match.end():])
                states.append(root.get('product'))
            except (ValueError, TypeError, AttributeError):
                return None, 'selected_product_state_invalid'
    state = states[0] if len(states) == 1 and isinstance(states[0], dict) else {}
    custom = state.get('custom')
    if (not isinstance(custom, dict) or state.get('id') != sku + '-' + shop
            or state.get('name') != title or state.get('currency') != 'TRY'
            or state.get('url') != url + '?shopId=' + shop
            or custom.get('shop_name') != seller or str(custom.get('shop_id')) != shop
            or _amount(state.get('unit_price')) != list_price or _amount(state.get('unit_sale_price')) != price):
        return None, 'selected_product_state_mismatch'
    from retailer_offer_refresh import _schema_nodes, _type_is, _is_conditional_price
    schemas = [p for p in _schema_nodes(soup, url) if _type_is(p, 'Product')]
    schema = schemas[0] if len(schemas) == 1 else {}
    offer = schema.get('offers')
    schema_seller = offer.get('seller') if isinstance(offer, dict) else None
    # The same observed schema omits literal warranty parentheses and uses
    # spaces for numeric slash pairs. Preserve every word and model qualifier.
    normalize_pair = lambda text: ' '.join(re.sub(r'(?<=\d)/(?=\d)|[()]', ' ', str(text or '')).split())
    if (str(schema.get('sku')) != sku or normalize_pair(schema.get('name')) != normalize_pair(title)
            or not isinstance(offer, dict) or offer.get('@type') != 'Offer'
            or offer.get('url') != url or offer.get('priceCurrency') != 'TRY'
            or not isinstance(schema_seller, dict) or schema_seller.get('name') != seller
            or offer.get('availability') != 'https://schema.org/InStock'
            or offer.get('itemCondition') != 'https://schema.org/NewCondition'
            or _amount(offer.get('price')) != price):
        return None, 'selected_marketplace_schema_mismatch'
    prices = main.select('.pdp-block2 > .pdp-amount-tc .pdp-prices .prd-prc2 .prc-third')
    delivery = main.select('.pdp-info .pds-delivery-detail > span')
    if (len(prices) != 1 or _cash_price(prices[0].get_text(' ', strip=True)) != price
            or len(delivery) != 1 or not re.fullmatch(
                r'[1-9]\d?(?:\s*-\s*[1-9]\d?)?\s+iş günü içinde kargoda',
                delivery[0].get_text(' ', strip=True))
            or _is_conditional_price(soup, offer, float(price))):
        return None, 'selected_marketplace_cash_or_shipping_unavailable'
    result = {'platform': 'Teknosa', 'merchant': seller, 'merchant_id': shop,
              'selected_teknosa_seller': seller, 'selected_teknosa_shop_id': shop,
              'selected_teknosa_variant_verified': True, 'listing_id': listing[1],
              'sku': sku, 'gtin13': gtin, 'observed_barcode': raw_barcode,
              'source_render_state': 'pending_selected_cart' if pending_cart else 'enabled_selected_cart',
              'source_title': title, 'url': url,
              'currency': 'TRY', 'condition': 'new', 'availability': 'in_stock',
              'price': float(price), 'observed_at': datetime.now(timezone.utc).isoformat()}
    technical = selected_technical_specs(main, title, cart, technical_observation)
    if technical is not None:
        result.update(product_id=sku, **technical)
    return result, 'selected_teknosa_marketplace_cash_offer'


def parse_selected_marketplace(html, url, profile, technical_observation=None):
    if not isinstance(profile, dict) or not isinstance(profile.get('specs'), dict):
        return None, 'invalid_catalog_pdp'
    result, reason = _selected_marketplace_commerce(html, url, technical_observation)
    if result is None:
        return None, reason
    if not catalog_offer_matches(result, profile.get('name'), profile['specs']):
        return None, 'catalog_variant_mismatch'
    return result, reason


def independent_radio_preflight(html, url, profile, technical_observation, read_witness):
    """Check one 4G identity using fresh independent retail proof, without preparing an offer."""
    from automatic_memory_variants import variant_selected_evidence
    from teknosa_public_technical import classification_values

    result, reason = _selected_marketplace_commerce(html, url, technical_observation)
    if result is None:
        return {'status': reason, 'writes': False}
    specs = profile['specs']
    title = result['source_title']
    matcher = TRPriceScraper()
    ram, storage, ambiguous = matcher._unambiguous_listing_capacities(title)
    if (specs.get('network_generation') != '4g' or ambiguous or ram is None or storage is None
            or set(matcher._identity_words(title)) & {'4g', '5g', 'lte'}
            or ram != matcher._spec_int(specs.get('ram_gb'))
            or storage != matcher._spec_int(specs.get('storage_gb'))
            or not matcher.is_strict_match(profile['name'], title, specs)
            or not matcher.is_strict_match(profile['name'], urlparse(url).path.replace('-', ' '), specs)):
        return {'status': 'independent_radio_scope_mismatch', 'writes': False}
    main = BeautifulSoup(html, 'html.parser').select_one('#pdp-main')
    cart = main.select_one('button#addToCartButton')
    fields = classification_values(technical_observation, main, cart)
    if fields is None:
        return {'status': 'selected_technical_classification_unavailable', 'writes': False}
    if (fields.get('5G Desteği') not in (None, 'Hayır')
            or any(key in fields and matcher._spec_int(fields[key]) != value
                   for key, value in (('Bellek Kapasitesi (RAM)', ram), ('Dahili Hafıza', storage)))
            or ('Model Kodu' in fields and
                not matcher.is_strict_match(profile['name'], fields['Model Kodu'], specs)
                and not matcher.is_strict_match(
                    cart.get('data-product-brand', '') + ' ' + fields['Model Kodu'],
                    title, {}))):
        return {'status': 'selected_technical_classification_conflict', 'writes': False}

    for witness in read_witness():
        if product_host(witness.get('url')) not in {'www.hepsiburada.com', 'www.trendyol.com', 'www.n11.com'}:
            continue
        try:
            observed_at = datetime.fromisoformat(witness['observed_at'])
            if not 0 <= (datetime.now(timezone.utc) - observed_at).total_seconds() <= 60:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        proof = variant_selected_evidence(witness, profile['name'], specs)
        if not proof or proof.get('gtin13') != result['gtin13']:
            continue
        return {'status': 'independent_radio_and_selected_commerce_verified', 'writes': False,
                'source_url': url, 'witness_url': witness['url'], 'gtin13': result['gtin13'],
                'sku': result['sku'], 'ram_gb': ram, 'storage_gb': storage, 'network_generation': '4g'}
    return {'status': 'independent_selected_gtin_unavailable', 'writes': False}


def catalog_offer_matches(offer, name, specs):
    return (isinstance(offer, dict) and offer.get('platform') == 'Teknosa'
            and offer.get('selected_teknosa_variant_verified') is True
            and offer.get('merchant') == offer.get('selected_teknosa_seller')
            and offer.get('merchant_id') == offer.get('selected_teknosa_shop_id')
            and product_host(offer.get('url')) == 'www.teknosa.com'
            and urlparse(offer.get('url', '')).path.endswith('-p-' + str(offer.get('sku')))
            and (not urlparse(offer['url']).query or
                 urlparse(offer['url']).query == 'shopId=' + str(offer.get('merchant_id')))
            and _gtin13(offer.get('gtin13')) is not None
            and offer.get('currency') == 'TRY' and offer.get('condition') == 'new'
            and offer.get('availability') == 'in_stock'
            and TRPriceScraper().has_observed_offer_evidence(name, offer, specs))
