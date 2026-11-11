"""Selected Trendyol listing proof from catalog URL hints, without phone profiles."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from hepsiburada_s25_fe_source import _nodes, _normalized
from mediamarkt_marketplace_audit import _gtin13, catalog_variant_matches
from priority_retailer_transport import PriorityRetailerTransport, product_host
from retailer_barcode import retailer_gtin13, trendyol_product_code
from tr_price_scraper import TRPriceScraper


def _amount(value):
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and 0 < result <= 1000000 else None
    except (ValueError, InvalidOperation):
        return None


def _product_id(url):
    if product_host(url) != 'www.trendyol.com':
        return None
    return re.search(r'-p-(\d+)$', urlparse(url).path)[1]


def catalog_request_url(url):
    """Historical seller/campaign hints discover a product, not a current seller.

    Fetch its canonical PDP and publish only the seller proven by the new
    selected listing. Direct seller-scoped parser calls keep their selector.
    """
    if _product_id(url) is None:
        return None
    return urlparse(url)._replace(query='').geturl()


def _selected(rows):
    if not isinstance(rows, list):
        return None
    found = [row for row in rows if isinstance(row, dict) and row.get('isSelected') is True]
    return found[0] if len(found) == 1 else None


def selected_attribute_capacity_gb(value, field):
    """Read a single native physical attribute, without catalog defaults.

    Trendyol uses literal "1 TB" for selected storage. TB is storage only;
    ranges, virtual RAM and missing units cannot establish either capacity.
    """
    if not isinstance(value, str) or field not in {'ram_gb', 'storage_gb'}:
        return None
    match = re.fullmatch(r'([1-9]\d{0,3})\s*GB', value, re.I)
    terabytes = re.fullmatch(r'(1|2|4)\s*TB', value, re.I) if field == 'storage_gb' else None
    if match:
        amount = int(match[1])
    elif terabytes:
        amount = int(terabytes[1]) * 1024
    else:
        return None
    low, high = (1, 64) if field == 'ram_gb' else (32, 4096)
    return amount if low <= amount <= high else None


def _selected_group_root(schema, identity, url, brand):
    """A group's own selected offer must be distinct from every child."""
    canonical = urlparse(url)._replace(query='').geturl()
    offer = schema.get('offers')
    schema_brand = schema.get('brand')
    variants = schema.get('hasVariant')
    if (schema.get('productGroupID') != identity or schema.get('sku') != identity
            or schema.get('@id') != canonical or not isinstance(offer, dict)
            or offer.get('url') != canonical or not isinstance(schema_brand, dict)
            or TRPriceScraper._normalize_words(schema_brand.get('name')) != TRPriceScraper._normalize_words(brand.get('name'))
            or not isinstance(variants, list) or not 1 <= len(variants) <= 100):
        return False
    for variant in variants:
        child_offer = variant.get('offers') if isinstance(variant, dict) else None
        if (not isinstance(variant, dict) or str(variant.get('sku')) == identity
                or _product_id(variant.get('@id')) == identity
                or isinstance(child_offer, dict) and _product_id(child_offer.get('url')) == identity):
            return False
    return True


def _selected_group_schema(schema, identity, url, physical, brand):
    if not _selected_group_root(schema, identity, url, brand):
        return False
    properties = schema.get('additionalProperty')
    if not isinstance(properties, list) or len(properties) > 100:
        return False
    fields = {}
    for prop in properties:
        key = prop.get('name') if isinstance(prop, dict) else None
        if key not in {'RAM Kapasitesi', 'Dahili Hafıza', 'Mobil Bağlantı Hızı'}:
            continue
        if key in fields or prop.get('@type') != 'PropertyValue' or not isinstance(prop.get('unitText'), str):
            return False
        fields[key] = prop['unitText']
    for key, label in (('ram_gb', 'RAM Kapasitesi'), ('storage_gb', 'Dahili Hafıza')):
        actual = selected_attribute_capacity_gb(fields.get(label), key)
        if actual is None and key == 'ram_gb' and fields.get(label) == 'Yok':
            # Native A16 groups omit RAM in their property table but spell
            # BOTH capacities in their own root name. Never inspect a child
            # variant caption or fill the amount from a catalog expectation.
            own_ram, own_storage, ambiguous = TRPriceScraper()._unambiguous_listing_capacities(
                schema.get('name') if isinstance(schema.get('name'), str) else '')
            if not ambiguous and own_storage == physical.get('storage_gb'):
                actual = own_ram
        if actual is None or actual != physical.get(key):
            return False
    radio = fields.get('Mobil Bağlantı Hızı', '').casefold()
    return (radio in {'5g', '4g', '4.5g', '4,5g'}
            and ('5g' if radio == '5g' else '4g') == physical.get('network_generation'))


def _phone_variant(product, title, brand, name, specs):
    matcher = TRPriceScraper()
    expected_words = set(matcher._identity_words(name))
    expected_brands = {key for key, aliases in matcher.PHONE_BRAND_ALIASES.items()
                       if expected_words & aliases}
    if expected_brands & {'redmi', 'poco'}:
        expected_brands.add('xiaomi')
    allowed_brand_names = {alias for key in expected_brands
                           for alias in matcher.PHONE_BRAND_ALIASES[key]}
    if (not isinstance(brand, dict) or not allowed_brand_names
            or matcher._normalize_words(brand.get('name')) not in allowed_brand_names):
        return None, 'selected_brand_mismatch'
    features = {}
    attributes = product.get('attributes')
    if not isinstance(attributes, list):
        return None, 'selected_attributes_missing'
    for row in attributes:
        if not isinstance(row, dict) or not isinstance(row.get('key'), dict) or not isinstance(row.get('value'), dict):
            continue
        key = row['key'].get('name')
        if key in ('RAM Kapasitesi', 'Dahili Hafıza', 'Mobil Bağlantı Hızı'):
            if key in features:
                return None, 'selected_attributes_ambiguous'
            features[key] = row['value'].get('name')
    physical = {}
    heading_ram, heading_storage, ambiguous_heading = matcher._unambiguous_listing_capacities(title)
    for key, label in (('ram_gb', 'RAM Kapasitesi'), ('storage_gb', 'Dahili Hafıza')):
        actual = selected_attribute_capacity_gb(features.get(label), key)
        # The live S26 Ultra template supplies literal "Yok" for RAM despite
        # an explicit physical RAM amount in its selected heading. Unknown is
        # not a contradictory capacity: use only that heading's actual amount,
        # which must still agree with selected state, schema, SKU and catalog.
        # No missing field, arbitrary malformed value, virtual RAM or catalog
        # fallback can establish a capacity here.
        if actual is None and features.get(label) == 'Yok' and not ambiguous_heading:
            actual = heading_ram if key == 'ram_gb' else heading_storage
        if actual is None or actual != matcher._spec_int(specs.get(key)):
            return None, 'selected_memory_mismatch'
        physical[key] = actual
    radio = str(features.get('Mobil Bağlantı Hızı') or '').casefold()
    if radio not in {'5g', '4g', '4.5g', '4,5g'}:
        return None, 'selected_radio_missing'
    physical['network_generation'] = '5g' if radio == '5g' else '4g'
    if not catalog_variant_matches({'source_title': title, **physical}, name, specs):
        return None, 'catalog_variant_mismatch'
    return physical, None


def parse_catalog_listing(html, url, name, specs):
    """Require the retailer's selected physical phone variant and commerce."""
    if not isinstance(specs, dict):
        return None, 'invalid_catalog_pdp'
    return _parse_selected_listing(html, url,
        lambda product, title, brand: _phone_variant(product, title, brand, name, specs),
        _selected_group_schema)


def selected_product_state(soup):
    states = []
    for script in soup.select('script'):
        body = script.get_text()
        matches = list(re.finditer(r'window\["__envoy__SHARED_PROPS"\]\s*=\s*', body))
        for match in matches:
            try:
                state, end = json.JSONDecoder().raw_decode(body[match.end():])
                if body[match.end() + end:].strip() not in ('', ';'):
                    return None, 'selected_state_ambiguous'
                states.append(state)
            except (ValueError, TypeError):
                return None, 'selected_state_invalid'
    if len(states) != 1 or not isinstance(states[0], dict):
        return None, 'selected_state_missing'
    return states[0].get('product'), None


def _scoped_seller_witness(default_html, soup, url, product, merchant, winner, variant, schema):
    """Check the alternate listing against a fresh canonical product document."""
    if not default_html or not parse_qs(urlparse(url).query).get('merchantId'):
        return False
    canonical_url = catalog_request_url(url)
    default_soup = BeautifulSoup(default_html, 'html.parser')
    if any([n.get('href') for n in page.select('link[rel="canonical"]')] != [canonical_url]
           for page in (soup, default_soup)):
        return False
    default, _ = selected_product_state(default_soup)
    if (not isinstance(default, dict) or any(default.get(key) != product.get(key)
            for key in ('id', 'productGroupId', 'name', 'brand', 'category'))):
        return False
    listing = default.get('merchantListing')
    if not isinstance(listing, dict):
        return False
    default_merchant, default_winner = listing.get('merchant'), listing.get('winnerVariant')
    default_variant = _selected(default.get('variants'))
    if (not isinstance(default_merchant, dict) or default_merchant.get('id') == merchant['id']
            or not isinstance(default_winner, dict) or default_variant is None
            or any(variant.get(key) != default_variant.get(key) for key in ('itemNumber', 'barcode', 'price'))
            or any(default_variant.get(key) != default_winner.get(key) for key in ('itemNumber', 'barcode'))):
        return False
    # Product JSON-LD remains the canonical seller's metadata on this template.
    defaults = [node for node in _nodes(default_soup) if node.get('@type') in ('Product', 'ProductGroup')]
    default_offer = schema.get('offers')
    default_price = _amount(default_offer.get('price')) if isinstance(default_offer, dict) else None
    if (len(defaults) != 1 or not isinstance(default_offer, dict) or defaults[0].get('offers') != default_offer
            or default_price is None or not isinstance(default_variant.get('price'), dict)
            or default_price != _amount(default_variant['price'].get('value'))):
        return False
    sellers = listing.get('otherMerchants')
    if not isinstance(sellers, list) or len(sellers) > 100:
        return False
    matched = [row for row in sellers if isinstance(row, dict) and row.get('id') == merchant['id']]
    if (len(matched) != 1 or matched[0].get('name') != merchant['name']
            or not isinstance(matched[0].get('url'), str)
            or urljoin(canonical_url, matched[0]['url']) != url):
        return False
    variants = matched[0].get('variants')
    fields = ('listingId', 'itemNumber', 'barcode', 'price', 'inStock', 'sellable', 'stockStatus', 'maxSaleLimit')
    return (retailer_gtin13(winner.get('barcode')) is not None
            and type(winner.get('maxSaleLimit')) is int and winner['maxSaleLimit'] > 0
            and isinstance(variants, list) and len([
                row for row in variants if isinstance(row, dict)
                and row.get('inStock') is True and row.get('sellable') is True
                and type(row.get('stockStatus')) is int and type(row.get('maxSaleLimit')) is int
                and all(row.get(key) == winner.get(key) for key in fields)
            ]) == 1)


def _parse_selected_listing(html, url, read_variant, check_group, *, default_html=None):
    """Cross-check shared selected listing, SKU, merchant, cart and price."""
    identity = _product_id(url)
    if identity is None or not isinstance(html, str) or len(html.encode()) > 5 * 1024 * 1024:
        return None, 'invalid_catalog_pdp'
    soup = BeautifulSoup(html, 'html.parser')
    product, reason = selected_product_state(soup)
    if reason:
        return None, reason
    if not isinstance(product, dict) or str(product.get('id')) != identity or product.get('inStock') is not True:
        return None, 'selected_product_mismatch'
    headings = soup.select('h1[data-testid="product-title"]')
    title = _normalized(headings[0].get_text(' ', strip=True)) if len(headings) == 1 else ''
    brand = product.get('brand')
    state_title = _normalized(str(brand.get('name') or '') + ' ' + str(product.get('name') or '')) if isinstance(brand, dict) else ''
    matcher = TRPriceScraper()
    if (not title or matcher._normalize_words(title) != matcher._normalize_words(state_title)
            or re.search(r'yenilenmi[sş]|refurbished|ikinci\s*el', title, re.I)):
        return None, 'catalog_title_mismatch'
    physical, reason = read_variant(product, title, brand)
    if physical is None:
        return None, reason
    listing = product.get('merchantListing')
    merchant = listing.get('merchant') if isinstance(listing, dict) else None
    winner = listing.get('winnerVariant') if isinstance(listing, dict) else None
    if not isinstance(merchant, dict) or not isinstance(winner, dict):
        return None, 'selected_merchant_missing'
    seller, seller_id = merchant.get('name'), merchant.get('id')
    query = parse_qs(urlparse(url).query)
    if (not isinstance(seller, str) or not 1 <= len(seller.strip()) <= 100
            or re.search(r'[\x00-\x1f]', seller) or type(seller_id) is not int or seller_id <= 0
            or query.get('merchantId', [str(seller_id)]) != [str(seller_id)]
            or not isinstance(winner.get('listingId'), str)
            or not re.fullmatch(r'[a-f0-9]{32}', winner['listingId'])
            or type(winner.get('itemNumber')) is not int or winner['itemNumber'] <= 0
            or (retailer_gtin13(winner.get('barcode')) is None
                and trendyol_product_code(winner.get('barcode')) is None)
            or winner.get('sellable') is not True or winner.get('inStock') is not True
            or type(winner.get('stockStatus')) is not int or winner['stockStatus'] != 1):
        return None, 'selected_merchant_or_stock_mismatch'
    variant, merchant_variant = _selected(product.get('variants')), _selected(listing.get('variants'))
    if (variant is None or merchant_variant is None
            or any(v.get('itemNumber') != winner['itemNumber'] or v.get('inStock') is not True
                   for v in (variant, merchant_variant))
            or merchant_variant.get('barcode') not in (None, winner['barcode'])):
        return None, 'selected_variant_mismatch'
    schemas = [node for node in _nodes(soup) if node.get('@type') in ('Product', 'ProductGroup')]
    schema = schemas[0] if len(schemas) == 1 else {}
    scoped = _scoped_seller_witness(default_html, soup, url, product, merchant, winner, variant, schema)
    if variant.get('barcode') != winner['barcode'] and not scoped:
        return None, 'selected_variant_mismatch'
    price_data = winner.get('price')
    if not isinstance(price_data, dict) or price_data.get('currency') != 'TRY':
        return None, 'selected_price_missing'
    selling = price_data.get('sellingPrice')
    price = _amount(selling.get('value')) if isinstance(selling, dict) else None
    basket = None
    if soup.select('.campaign-price-content'):
        from trendyol_basket_price import public_basket_price
        basket = public_basket_price(soup, listing, winner)
        if basket is None:
            return None, 'public_basket_promotion_mismatch'
        price = _amount(basket['price'])
    if price is None or not scoped and (not isinstance(variant.get('price'), dict)
                                        or _amount(variant['price'].get('value')) != price):
        return None, 'selected_price_mismatch'
    # A coupon/member price cannot silently become the unconditional price.
    for key in ('discountedPrice', 'couponApplicablePrice', 'tyPlusCouponApplicablePrice',
                'discountedPriceAfterNoLimitPromotions'):
        value = price_data.get(key)
        if value is not None and (not isinstance(value, dict) or _amount(value.get('value')) != price):
            return None, 'conditional_price'
    if schema.get('@type') == 'ProductGroup' and not check_group(schema, identity, url, physical, brand):
        return None, 'schema_selected_group_mismatch'
    offer = schema.get('offers')
    if (str(schema.get('sku')) != identity or _product_id(schema.get('@id')) != identity
            or matcher._normalize_words(str(schema.get('name') or '')) != matcher._normalize_words(title)
            or not isinstance(offer, dict) or offer.get('@type') != 'Offer'
            or _product_id(offer.get('url')) != identity
            or offer.get('priceCurrency') != 'TRY' or not scoped and _amount(offer.get('price')) != price
            or offer.get('itemCondition') != 'https://schema.org/NewCondition'
            or offer.get('availability') != 'https://schema.org/InStock'):
        return None, 'schema_selected_offer_mismatch'
    prices = soup.select('[data-testid="normal-price"] .discounted')
    for block in soup.select('button.price.lowest-price[data-testid="lowest-price"]'):
        labels = block.select('.lowest-price-label')
        if (len(labels) == 1 and re.fullmatch(r'Son [1-9]\d? Günün En Düşük Fiyatı!',
                _normalized(labels[0].get_text(' ', strip=True)))):
            prices.extend(block.select('span.discounted'))
    visible = _normalized(prices[0].get_text(' ', strip=True)) if len(prices) == 1 else ''
    match = re.fullmatch(r'(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?)\s*(?:TL|₺)', visible)
    if basket is None and (not match or _amount(match[1].replace('.', '').replace(',', '.')) != price):
        return None, 'visible_cash_price_mismatch'
    from retailer_offer_refresh import _is_conditional_price
    if (any(key in offer for key in ('validForMemberTier', 'eligibleCustomerType', 'eligibleTransactionVolume'))
            or basket is not None and any(offer.get(key) not in (None, '') for key in ('name', 'description'))
            or basket is None and _is_conditional_price(soup, offer, float(price))):
        return None, 'conditional_price'
    carts = soup.select('button[data-testid="buy-now-button"]')
    if (len(carts) != 1 or carts[0].has_attr('disabled') or carts[0].get('aria-disabled') == 'true'
            or _normalized(carts[0].get_text(' ', strip=True)) != 'Şimdi Al'):
        return None, 'selected_cart_unavailable'
    result = {'platform': 'Trendyol', 'merchant': seller, 'merchant_id': seller_id,
            'selected_trendyol_seller': seller, 'selected_trendyol_merchant_id': seller_id,
            'product_id': identity, 'listing_id': winner['listingId'], 'item_number': winner['itemNumber'],
            'gtin13': retailer_gtin13(winner['barcode']), 'observed_barcode': winner['barcode'],
            'selected_trendyol_variant_verified': True,
            'price': float(price), 'currency': 'TRY', 'condition': 'new', 'availability': 'in_stock',
            'url': url, 'source_title': title, **physical,
            'observed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}
    if result['gtin13'] is None:
        # Keep a retailer product code distinct from GTIN. All selected product,
        # physical variant, listing, merchant, schema, stock and cash checks above
        # remain mandatory, including the independently selected barcode match.
        result['barcode_kind'] = 'retailer_product_code'
    if basket is not None:
        result.update(price_kind='basket', listed_price=basket['listed_price'],
                      basket_promotion=basket['basket_promotion'])
    return result, 'selected_trendyol_public_basket_offer' if basket is not None else 'selected_new_cash_offer_observed'


def catalog_offer_matches(offer, name, specs):
    return (isinstance(offer, dict) and offer.get('platform') == 'Trendyol'
            and offer.get('selected_trendyol_variant_verified') is True
            and offer.get('merchant') == offer.get('selected_trendyol_seller')
            and offer.get('merchant_id') == offer.get('selected_trendyol_merchant_id')
            and type(offer.get('merchant_id')) is int and offer['merchant_id'] > 0
            and offer.get('availability') == 'in_stock' and offer.get('condition') == 'new'
            and offer.get('currency') == 'TRY' and _product_id(offer.get('url')) == offer.get('product_id')
            and (_gtin13(offer.get('gtin13')) is not None
                 or offer.get('gtin13') is None and offer.get('barcode_kind') == 'retailer_product_code'
                 and trendyol_product_code(offer.get('observed_barcode')) is not None)
            and catalog_variant_matches(offer, name, specs))


class TrendyolCatalogSource:
    def __init__(self, get=None, post=None, transport=None):
        self.transport = transport or PriorityRetailerTransport(get=get, post=post, maximum_per_host=6)
        self._profiles, self._ambiguous = {}, set()

    def register_catalog_targets(self, name, targets, specs):
        matcher = TRPriceScraper()
        if not all(matcher._spec_int(specs.get(k)) for k in ('ram_gb', 'storage_gb')):
            return
        for row in targets:
            url = row.get('source_url') if isinstance(row, dict) else None
            if (_product_id(url) is None or not matcher.is_strict_match(
                    name, urlparse(url).path.replace('-', ' '), specs)):
                continue
            profile = {'name': name, 'specs': dict(specs)}
            if url in self._profiles and self._profiles[url] != profile:
                self._ambiguous.add(url)
            self._profiles[url] = profile

    def read_offer(self, url):
        profile = self._profiles.get(url)
        if profile is None or url in self._ambiguous:
            return None, 'unmatched_catalog_pdp', None
        observed_url = catalog_request_url(url)
        html, reason, status = self.transport.read_document(observed_url)
        if html is None:
            return None, reason, status
        final_url = self.transport.resolved_url(observed_url) if hasattr(self.transport, 'resolved_url') else observed_url
        offer, reason = parse_catalog_listing(html, final_url, profile['name'], profile['specs'])
        return offer, reason, status

    def configure_catalog_budget(self, count):
        self.transport.configure_catalog_budget(count)

    def report(self):
        return self.transport.report()
