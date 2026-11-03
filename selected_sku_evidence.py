"""Serialize only independently verified public selected-SKU facts.

Never receives a catalog profile: RAM/radio/storage cannot be filled from the
expected phone. The raw heading is retained unchanged alongside this evidence.
"""
import re
from urllib.parse import parse_qsl, urlparse


def selected_sku_evidence(offer):
    # Expand other retailers only when their readers expose the same observed
    # physical attributes and selected identity bindings, with their own tests.
    if not isinstance(offer, dict) or offer.get('platform') != 'Trendyol':
        return None
    if (offer.get('selected_trendyol_variant_verified') is not True
            or offer.get('merchant') != offer.get('selected_trendyol_seller')
            or offer.get('merchant_id') != offer.get('selected_trendyol_merchant_id')
            or offer.get('currency') != 'TRY' or offer.get('condition') != 'new'
            or offer.get('availability') != 'in_stock'):
        return None
    name, merchant_id = offer.get('merchant'), offer.get('merchant_id')
    if (not isinstance(name, str) or not name.strip() or len(name) > 100
            or re.search(r'[\x00-\x1f]', name) or type(merchant_id) is not int or merchant_id <= 0):
        return None
    product, sku = str(offer.get('product_id') or ''), str(offer.get('item_number') or '')
    listing, gtin = offer.get('listing_id'), offer.get('gtin13')
    if (not re.fullmatch(r'[1-9]\d{0,19}', product) or type(offer.get('item_number')) is not int
            or not re.fullmatch(r'[1-9]\d{0,19}', sku)
            or not isinstance(listing, str) or not re.fullmatch(r'[a-f0-9]{32}', listing)
            or not isinstance(gtin, str) or not re.fullmatch(r'\d{13}', gtin)):
        return None
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
        if (url.scheme != 'https' or url.netloc != 'www.trendyol.com' or url.fragment
                or url.params or not re.fullmatch(r'/[a-z0-9-]+/[a-z0-9-]+-p-' + product, url.path)
                or len(query) != len({key for key, _ in query})):
            return None
        for key, value in query:
            if (key == 'merchantId' and value == str(merchant_id)
                    or key == 'v' and value == 'tek-ebat'
                    or key == 'boutiqueId' and re.fullmatch(r'[1-9]\d{0,11}', value)):
                continue
            return None
    except (TypeError, ValueError):
        return None
    return {'version': 'selected-retailer-sku-v1', 'platform': 'Trendyol',
            'product_id': product, 'sku': sku, 'merchant_id': str(merchant_id),
            'merchant_name': name, 'gtin13': gtin, 'ram_gb': ram, 'storage_gb': storage,
            'network_generation': radio, 'listing_id': listing}
