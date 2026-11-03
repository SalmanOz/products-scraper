"""Selected Trendyol listing proof from catalog URL hints, without phone profiles."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from hepsiburada_s25_fe_source import _nodes, _normalized
from mediamarkt_marketplace_audit import _gtin13, catalog_variant_matches
from priority_retailer_transport import PriorityRetailerTransport, product_host
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


def parse_catalog_listing(html, url, name, specs):
    """Require schema, selected state, two variant lists and visible cash/cart agreement."""
    identity = _product_id(url)
    if (identity is None or not isinstance(html, str) or len(html.encode()) > 5 * 1024 * 1024
            or not isinstance(specs, dict)):
        return None, 'invalid_catalog_pdp'
    soup = BeautifulSoup(html, 'html.parser')
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
    product = states[0].get('product')
    if not isinstance(product, dict) or str(product.get('id')) != identity or product.get('inStock') is not True:
        return None, 'selected_product_mismatch'
    headings = soup.select('h1[data-testid="product-title"]')
    title = _normalized(headings[0].get_text(' ', strip=True)) if len(headings) == 1 else ''
    brand = product.get('brand')
    state_title = _normalized(str(brand.get('name') or '') + ' ' + str(product.get('name') or '')) if isinstance(brand, dict) else ''
    matcher = TRPriceScraper()
    if (not title or matcher._normalize_words(title) != matcher._normalize_words(state_title)
            or not matcher.is_strict_match(name, title, specs)
            or re.search(r'yenilenmi[sş]|refurbished|ikinci\s*el', title, re.I)):
        return None, 'catalog_title_mismatch'
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
        match = re.fullmatch(r'(\d{1,4})\s*GB', str(features.get(label) or ''), re.I)
        actual = int(match[1]) if match else None
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
            or _gtin13(winner.get('barcode')) is None
            or winner.get('sellable') is not True or winner.get('inStock') is not True
            or type(winner.get('stockStatus')) is not int or winner['stockStatus'] != 1):
        return None, 'selected_merchant_or_stock_mismatch'
    variant, merchant_variant = _selected(product.get('variants')), _selected(listing.get('variants'))
    if (variant is None or merchant_variant is None
            or any(v.get('itemNumber') != winner['itemNumber'] or v.get('inStock') is not True
                   for v in (variant, merchant_variant))
            or variant.get('barcode') != winner['barcode']):
        return None, 'selected_variant_mismatch'
    price_data = winner.get('price')
    if not isinstance(price_data, dict) or price_data.get('currency') != 'TRY':
        return None, 'selected_price_missing'
    selling = price_data.get('sellingPrice')
    price = _amount(selling.get('value')) if isinstance(selling, dict) else None
    if price is None or not isinstance(variant.get('price'), dict) or _amount(variant['price'].get('value')) != price:
        return None, 'selected_price_mismatch'
    # A coupon/member price cannot silently become the unconditional price.
    for key in ('discountedPrice', 'couponApplicablePrice', 'tyPlusCouponApplicablePrice',
                'discountedPriceAfterNoLimitPromotions'):
        value = price_data.get(key)
        if value is not None and (not isinstance(value, dict) or _amount(value.get('value')) != price):
            return None, 'conditional_price'
    schemas = [node for node in _nodes(soup) if node.get('@type') == 'Product']
    schema = schemas[0] if len(schemas) == 1 else {}
    offer = schema.get('offers')
    if (str(schema.get('sku')) != identity or _product_id(schema.get('@id')) != identity
            or matcher._normalize_words(str(schema.get('name') or '')) != matcher._normalize_words(title)
            or not isinstance(offer, dict) or offer.get('@type') != 'Offer'
            or _product_id(offer.get('url')) != identity
            or offer.get('priceCurrency') != 'TRY' or _amount(offer.get('price')) != price
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
    if not match or _amount(match[1].replace('.', '').replace(',', '.')) != price:
        return None, 'visible_cash_price_mismatch'
    from retailer_offer_refresh import _is_conditional_price
    if _is_conditional_price(soup, offer, float(price)):
        return None, 'conditional_price'
    carts = soup.select('button[data-testid="buy-now-button"]')
    if (len(carts) != 1 or carts[0].has_attr('disabled') or carts[0].get('aria-disabled') == 'true'
            or _normalized(carts[0].get_text(' ', strip=True)) != 'Şimdi Al'):
        return None, 'selected_cart_unavailable'
    return {'platform': 'Trendyol', 'merchant': seller, 'merchant_id': seller_id,
            'selected_trendyol_seller': seller, 'selected_trendyol_merchant_id': seller_id,
            'product_id': identity, 'listing_id': winner['listingId'], 'item_number': winner['itemNumber'],
            'gtin13': winner['barcode'], 'selected_trendyol_variant_verified': True,
            'price': float(price), 'currency': 'TRY', 'condition': 'new', 'availability': 'in_stock',
            'url': url, 'source_title': title, **physical,
            'observed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}, 'selected_new_cash_offer_observed'


def catalog_offer_matches(offer, name, specs):
    return (isinstance(offer, dict) and offer.get('platform') == 'Trendyol'
            and offer.get('selected_trendyol_variant_verified') is True
            and offer.get('merchant') == offer.get('selected_trendyol_seller')
            and offer.get('merchant_id') == offer.get('selected_trendyol_merchant_id')
            and type(offer.get('merchant_id')) is int and offer['merchant_id'] > 0
            and offer.get('availability') == 'in_stock' and offer.get('condition') == 'new'
            and offer.get('currency') == 'TRY' and _product_id(offer.get('url')) == offer.get('product_id')
            and _gtin13(offer.get('gtin13')) is not None and catalog_variant_matches(offer, name, specs))


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
