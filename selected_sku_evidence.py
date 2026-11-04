"""Serialize only independently verified public selected-SKU facts.

Never receives a catalog profile: RAM/radio/storage cannot be filled from the
expected phone. The raw heading is retained unchanged alongside this evidence.
"""
import re
from urllib.parse import parse_qsl, urlparse


def selected_capacity_title(title, selected):
    """Disambiguate an observed '+' pair only with independent selected facts.

    This is parsing text, never the stored source title. Virtual RAM, choices,
    attached RAM labels and conflicting additional declarations remain unsafe.
    """
    if (not isinstance(title, str) or not selected or type(selected.get('ram_gb')) is not int or type(selected.get('storage_gb')) is not int
            or re.search(r'\b(?:virtual|sanal|extended)\s*ram\b|\bram\s*(?:virtual|sanal|extended)\b', title, re.I)):
        return title
    # Observed Xiaomi PDPs use 12G+256G and omit the first unit in 12+256G.
    # Interpret only one complete pair agreeing with independently selected
    # attributes; a lone 4G/5G remains connectivity, never capacity evidence.
    shorthand = re.compile(r'(?<![\w.,])([1-9]|1\d|2[0-4])\s*G?\s*\+\s*'
                           r'(64|128|256|512|1024)\s*G(?!\w)', re.I)
    matches = list(shorthand.finditer(title))
    if len(matches) == 1:
        match = matches[0]
        before, after = title[max(0, match.start()-24):match.start()], title[match.end():match.end()+24]
        rest = title[:match.start()] + ' ' * len(match[0]) + title[match.end():]
        extra_g = [m for m in re.finditer(r'(?<![\w.,])(\d+)\s*G(?!\w)', rest, re.I)
                   if m[1] not in {'4', '5'} or re.search(r'\b(?:ram|rom)\s*:?\s*$', rest[:m.start()], re.I)
                   or re.match(r'\s*(?:ram|rom)\b', rest[m.end():], re.I)]
        if (int(match[1]) == selected['ram_gb'] and int(match[2]) == selected['storage_gb']
                and not extra_g
                and not re.search(r'\b(?:iphone|ram|rom|depolama|haf[ıi]za)\s*:?\s*$', before, re.I)
                and not re.match(r'\s*ram\b', after, re.I)):
            title = title[:match.start()] + f'RAM {match[1]} GB; ROM {match[2]} GB' + title[match.end():]
    pair = re.compile(r'(?<![\w.,])(?P<ram>[1-9]|1\d|2[0-4])\s*(?P<ram_unit>GB)?\s*\+\s*'
                      r'(?P<storage>64|128|256|512|1024)\s*(?P<storage_unit>GB)?(?!\w)', re.I)
    def replace(match):
        before, after = title[max(0, match.start()-24):match.start()], title[match.end():match.end()+24]
        if (not (match['ram_unit'] or match['storage_unit'])
                or int(match['ram']) != selected['ram_gb'] or int(match['storage']) != selected['storage_gb']
                or re.search(r'\b(?:iphone|ram|rom|depolama|haf[ıi]za)\s*:?\s*$', before, re.I)
                or re.match(r'\s*ram\b', after, re.I)):
            return match[0]
        return f'{match["ram"]} GB RAM {match["storage"]} GB ROM'
    return pair.sub(replace, title)


def _teknosa_selected_sku_evidence(offer):
    from priority_retailer_transport import product_host
    from retailer_barcode import retailer_gtin13
    if (offer.get('selected_teknosa_variant_verified') is not True
            or offer.get('selected_teknosa_physical_specs_verified') is not True
            or offer.get('merchant') != offer.get('selected_teknosa_seller')
            or offer.get('merchant_id') != offer.get('selected_teknosa_shop_id')
            or offer.get('currency') != 'TRY' or offer.get('condition') != 'new'
            or offer.get('availability') != 'in_stock'):
        return None
    name, merchant = offer.get('merchant'), offer.get('merchant_id')
    sku, listing, gtin = offer.get('sku'), offer.get('listing_id'), offer.get('gtin13')
    ram, storage, radio = (offer.get(k) for k in ('ram_gb', 'storage_gb', 'network_generation'))
    url = offer.get('url')
    if (not isinstance(name, str) or not name.strip() or len(name) > 100
            or re.search(r'[\x00-\x1f]', name) or name.casefold() == 'teknosa'
            or not isinstance(merchant, str) or not re.fullmatch(r'[1-9]\d{2,9}', merchant) or merchant == '1001'
            or not isinstance(sku, str) or not re.fullmatch(r'[1-9]\d{8,13}', sku)
            or offer.get('product_id') != sku
            or not isinstance(listing, str) or not re.fullmatch(r'[1-9]\d{2,12}', listing)
            or not isinstance(gtin, str) or not re.fullmatch(r'\d{13}', gtin) or retailer_gtin13(gtin) != gtin
            or type(ram) is not int or not 1 <= ram <= 64
            or type(storage) is not int or not 32 <= storage <= 4096 or radio != '5g'
            or product_host(url) != 'www.teknosa.com' or urlparse(url).query
            or not urlparse(url).path.endswith('-p-' + sku)):
        return None
    return {'version': 'selected-retailer-sku-v1', 'platform': 'Teknosa',
            'product_id': sku, 'sku': sku, 'listing_id': listing,
            'merchant_id': merchant, 'merchant_name': name, 'gtin13': gtin,
            'ram_gb': ram, 'storage_gb': storage, 'network_generation': radio}


def selected_sku_evidence(offer):
    if isinstance(offer, dict) and offer.get('platform') == 'Teknosa':
        return _teknosa_selected_sku_evidence(offer)
    if not isinstance(offer, dict) or offer.get('platform') not in ('Trendyol', 'n11'):
        return None
    prefix = 'trendyol' if offer['platform'] == 'Trendyol' else 'n11'
    if (offer.get('selected_' + prefix + '_variant_verified') is not True
            or offer.get('merchant') != offer.get('selected_' + prefix + '_seller')
            or offer.get('merchant_id') != offer.get('selected_' + prefix + '_merchant_id')
            or offer.get('currency') != 'TRY' or offer.get('condition') != 'new'
            or offer.get('availability') != 'in_stock'):
        return None
    name, merchant_id = offer.get('merchant'), offer.get('merchant_id')
    if (not isinstance(name, str) or not name.strip() or len(name) > 100
            or re.search(r'[\x00-\x1f]', name) or type(merchant_id) is not int or merchant_id <= 0):
        return None
    product, sku = str(offer.get('product_id') or ''), str(offer.get('item_number' if prefix == 'trendyol' else 'sku') or '')
    listing, gtin = offer.get('listing_id'), offer.get('gtin13')
    from retailer_barcode import trendyol_product_code
    retailer_code = (trendyol_product_code(offer.get('observed_barcode'))
                     if prefix == 'trendyol' and gtin is None
                     and offer.get('barcode_kind') == 'retailer_product_code' else None)
    if (not re.fullmatch(r'[1-9]\d{0,19}', product) or (prefix == 'trendyol' and type(offer.get('item_number')) is not int)
            or not re.fullmatch(r'[1-9]\d{0,19}', sku)
            or (prefix == 'trendyol' and (not isinstance(listing, str) or not re.fullmatch(r'[a-f0-9]{32}', listing)))
            or (retailer_code is None and (not isinstance(gtin, str) or not re.fullmatch(r'\d{13}', gtin)))):
        return None
    if retailer_code is None:
        checksum = sum(int(digit) * (3 if index % 2 else 1) for index, digit in enumerate(gtin[:12]))
        if (10 - checksum % 10) % 10 != int(gtin[-1]):
            return None
    ram, storage, radio = (offer.get(k) for k in ('ram_gb', 'storage_gb', 'network_generation'))
    if (type(ram) is not int or not 1 <= ram <= 64 or type(storage) is not int
            or not 32 <= storage <= 4096 or radio not in ('4g', '5g')):
        return None
    try:
        raw_url = offer.get('url')
        if not isinstance(raw_url, str) or re.search(r'[\x00-\x20\\]', raw_url):
            return None
        url = urlparse(raw_url)
        query = parse_qsl(url.query, keep_blank_values=True)
        if (url.scheme != 'https' or url.fragment or url.params
                or len(query) != len({key for key, _ in query})):
            return None
        if prefix == 'n11':
            from priority_retailer_transport import n11_product_selectors
            selectors = n11_product_selectors(raw_url)
            group = str(offer.get('group_id') or '')
            if (selectors is None or not re.fullmatch(r'[1-9]\d{0,19}', group)
                    or not re.fullmatch(r'/urun/[a-z0-9-]+-' + group, url.path)):
                return None
            expected_selectors = {'magaza': offer.get('selected_n11_seller_slug'),
                'renk': offer.get('selected_n11_color_slug'), 'dahili-hafiza': f'{storage}-gb'}
            if any(expected_selectors.get(key) != value for key, value in selectors.items()):
                return None
        elif (url.netloc != 'www.trendyol.com' or not re.fullmatch(
                r'/[a-z0-9-]+/[a-z0-9-]+-p-' + product, url.path)):
            return None
        for key, value in query:
            if prefix == 'n11':
                continue  # Already bound to the independently selected fields.
            if (key == 'merchantId' and value == str(merchant_id)
                    or key == 'v' and value == 'tek-ebat'
                    or key == 'boutiqueId' and re.fullmatch(r'[1-9]\d{0,11}', value)):
                continue
            return None
    except (TypeError, ValueError):
        return None
    proof = {'version': 'selected-retailer-sku-v1', 'platform': offer['platform'],
            'product_id': product, 'sku': sku, 'merchant_id': str(merchant_id),
            'merchant_name': name, 'gtin13': gtin, 'ram_gb': ram, 'storage_gb': storage,
            'network_generation': radio}
    if retailer_code is not None:
        proof.pop('gtin13')
        proof.update(version='selected-retailer-sku-v2', retailer_barcode=retailer_code)
    if prefix == 'trendyol':
        proof['listing_id'] = listing
    else:
        proof['group_id'] = group
        for key, field in (('merchant_slug', 'selected_n11_seller_slug'), ('color_slug', 'selected_n11_color_slug')):
            value = offer.get(field)
            if value is not None:
                if not isinstance(value, str) or len(value) > 100 or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', value):
                    return None
                proof[key] = value
    if offer.get('price_kind') == 'basket':
        listed, payable = offer.get('listed_price'), offer.get('price')
        if (prefix not in ('n11', 'trendyol') or type(listed) not in (int, float) or type(payable) not in (int, float)
                or not 0 < payable < listed <= 1000000
                or any(abs(p * 100 - round(p * 100)) > 0.00001 for p in (listed, payable))):
            return None
        proof.update(price_kind='basket', listed_price=listed, payable_price=payable)
        if prefix == 'trendyol':
            from trendyol_basket_price import valid_basket_promotion
            if not valid_basket_promotion(offer.get('basket_promotion'), listed, payable):
                return None
            proof['basket_promotion'] = dict(offer['basket_promotion'])
        elif offer.get('basket_promotion') is not None:
            return None
    elif offer.get('basket_promotion') is not None:
        return None
    return proof
