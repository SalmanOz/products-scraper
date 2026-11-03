"""Selected n11 SKU/shop cash proof from public server-rendered product JSON.

Never persist the surrounding window.model: it also contains visitor/account
fields. Only the public product, SKU definitions and commercial metadata matter.
"""
from datetime import datetime, timezone
import json
import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from mediamarkt_marketplace_audit import _gtin13
from priority_retailer_transport import PriorityRetailerTransport, product_host
from teknosa_reviewed_source import _amount
from tr_price_scraper import TRPriceScraper


def _cash(value):
    match = re.fullmatch(r'(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?)\s*TL',
                         ' '.join(str(value or '').split()))
    return _amount(match[1].replace('.', '').replace(',', '.')) if match else None


def catalog_request_url(url):
    return urlparse(url)._replace(query='').geturl() if product_host(url) == 'www.n11.com' else None


def parse_catalog_listing(html, url, name, specs):
    canonical = catalog_request_url(url)
    if canonical is None or not isinstance(html, str) or len(html.encode()) > 5 * 1024 * 1024:
        return None, 'invalid_catalog_pdp'
    soup = BeautifulSoup(html, 'html.parser')
    states = []
    for script in soup.select('script:not([src])'):
        body = script.get_text()
        matches = list(re.finditer(r'\bwindow\.model\s*=\s*(?=\{)', body))
        for match in matches:
            try:
                state, end = json.JSONDecoder().raw_decode(body[match.end():])
                if body[match.end() + end:].strip() not in ('', ';'):
                    return None, 'selected_state_ambiguous'
                states.append(state)
            except (ValueError, TypeError):
                return None, 'selected_state_invalid'
    state = states[0] if len(states) == 1 and isinstance(states[0], dict) else {}
    product, meta = state.get('product'), state.get('productMeta')
    if not isinstance(product, dict) or not isinstance(meta, dict):
        return None, 'selected_product_state_missing'
    group = canonical.rsplit('-', 1)[1]
    if (str(product.get('groupId')) != group or str(meta.get('groupId')) != group
            or product.get('productUrlWithoutSellerShop') != canonical
            or product.get('availableToBuy') is not True or product.get('outOfStock') is not False
            or type(product.get('stock')) is not int or product['stock'] <= 0
            or any(product.get(key) is not False for key in
                   ('secondHand', 'preSale', 'isPreSale', 'privateProduct', 'isPrivateProduct',
                    'sellerHasMinimumCartAmount', 'mobileDiscountExist', 'hasInstantDiscount'))):
        return None, 'selected_product_or_unconditional_stock_mismatch'
    main = soup.select('.pdpMainInfo')
    heading = main[0].select('h1.title') if len(main) == 1 else []
    title = ' '.join(heading[0].get_text(' ', strip=True).split()) if len(heading) == 1 else ''
    matcher = TRPriceScraper()
    if (title != product.get('title') or not matcher.is_strict_match(name, title, specs)
            or not matcher.has_observed_offer_evidence(name, {'source_title': title, 'url': url}, specs)):
        return None, 'catalog_title_variant_mismatch'
    seller = product.get('seller')
    if not isinstance(seller, dict):
        return None, 'selected_seller_missing'
    seller_name, seller_id = seller.get('nickName'), seller.get('id')
    links = soup.select('.sidebarSellerArea-top-name')
    if (not isinstance(seller_name, str) or not 1 <= len(seller_name) <= 100
            or re.search(r'[\x00-\x1f]', seller_name) or type(seller_id) is not int or seller_id <= 0
            or len(links) != 1 or links[0].get('title') != seller_name
            or links[0].get_text(' ', strip=True) != seller_name
            or not re.fullmatch(r'/magaza/[a-z0-9-]+', str(links[0].get('href') or ''))
            or str(meta.get('sellerId')) != str(seller_id)):
        return None, 'selected_seller_identity_mismatch'
    seller_slug = links[0]['href'].split('/')[-1]
    query = parse_qs(urlparse(url).query)
    if query.get('magaza', [seller_slug]) != [seller_slug]:
        return None, 'selected_query_seller_mismatch'
    try:
        skus = json.loads(state['skus']) if isinstance(state.get('skus'), str) else state.get('skus')
        definitions = json.loads(state['skuDefinitions']) if isinstance(state.get('skuDefinitions'), str) else state.get('skuDefinitions')
    except (ValueError, TypeError):
        return None, 'selected_skus_invalid'
    if not isinstance(skus, list) or not isinstance(definitions, list):
        return None, 'selected_skus_missing'
    selected = [s for s in skus if isinstance(s, dict) and s.get('productId') == product.get('id')
                and str(s.get('id')) == str(product.get('defaultSkuId'))]
    sku = selected[0] if len(selected) == 1 else {}
    gtin = _gtin13(sku.get('gtin'))
    if (gtin is None or product.get('defaultGtin') != gtin or sku.get('outOfStock') is not False
            or type(sku.get('stock')) is not int or sku['stock'] <= 0
            or sku.get('currentStock') != sku['stock'] or sku['stock'] != product['stock']
            or sku.get('storeName') != seller_name or str(meta.get('productId')) != str(product.get('id'))
            or str(meta.get('skuId')) != str(sku.get('id')) or meta.get('stock') != sku['stock']):
        return None, 'selected_sku_stock_or_gtin_mismatch'
    attributes = sku.get('skuAttributes')
    if not isinstance(attributes, list):
        return None, 'selected_sku_attributes_missing'
    values = {}
    for attribute in attributes:
        definition = attribute.get('skuDefinition') if isinstance(attribute, dict) else None
        if not isinstance(definition, dict) or definition.get('name') in values:
            return None, 'selected_sku_attributes_ambiguous'
        if not any(d.get('id') == definition.get('id') and d.get('name') == definition.get('name')
                   for d in definitions if isinstance(d, dict)):
            return None, 'selected_sku_definition_mismatch'
        values[definition.get('name')] = attribute.get('name')
    if (values.get('Dahili Hafıza') != str(matcher._spec_int(specs.get('storage_gb'))) + ' GB'
            or not isinstance(values.get('Renk'), str) or len(values['Renk']) > 60):
        return None, 'selected_sku_memory_missing'
    if query.get('renk', [matcher._normalize_words(values['Renk']).replace(' ', '-')]) != [
            matcher._normalize_words(values['Renk']).replace(' ', '-')]:
        return None, 'selected_query_color_mismatch'
    if query.get('dahili-hafiza', [values['Dahili Hafıza'].lower().replace(' ', '-')]) != [
            values['Dahili Hafıza'].lower().replace(' ', '-')]:
        return None, 'selected_query_memory_mismatch'
    choices = main[0].select('button.sku-selection .sku-attributes-text')
    if (len(choices) != 1 or choices[0].get_text(' ', strip=True) !=
            ', '.join(str(a.get('name') or '') for a in attributes)):
        return None, 'visible_selected_variant_mismatch'
    price = _amount(product.get('displayPriceFloat'))
    original = _amount(product.get('priceFloat'))
    if (price is None or _cash(product.get('displayPrice')) != price
            or _amount(product.get('displayPriceNumber')) != price
            or _cash(sku.get('displayPrice')) != price or _amount(sku.get('displayPriceNumber')) != price
            or _cash(meta.get('discountedPrice')) != price
            or original is None or _cash(product.get('price')) != original
            or _cash(sku.get('price')) != original or _amount(meta.get('price')) != original
            or product.get('hasDiscount') not in (True, False)
            or (product['hasDiscount'] is False and original != price)
            or original < price):
        return None, 'conditional_or_inconsistent_cash_price'
    from retailer_offer_refresh import _schema_nodes, _type_is, _is_conditional_price
    schemas = [p for p in _schema_nodes(soup, url) if _type_is(p, 'Product')]
    schema = schemas[0] if len(schemas) == 1 else {}
    offer = schema.get('offers')
    if (str(schema.get('sku')) != str(sku.get('id'))
            or not matcher.is_strict_match(name, str(schema.get('name') or ''), specs)
            or not isinstance(offer, dict) or offer.get('@type') != 'Offer'
            or offer.get('url') != canonical or offer.get('priceCurrency') != 'TRY'
            or _amount(offer.get('price')) != price
            or offer.get('availability') != 'https://schema.org/InStock'):
        return None, 'schema_selected_cash_offer_mismatch'
    visible = main[0].select('.price-wrapper .newPrice ins')
    carts = main[0].select('button#addToBasket')
    if (len(visible) != 1 or _cash(visible[0].get_text(' ', strip=True)) != price
            or len(carts) != 1 or carts[0].has_attr('disabled')
            or carts[0].get('aria-disabled') == 'true' or carts[0].get_text(' ', strip=True) != 'Sepete Ekle'
            or _is_conditional_price(main[0], offer, float(price))):
        return None, 'visible_cash_or_selected_cart_mismatch'
    return {'platform': 'n11', 'merchant': seller_name, 'merchant_id': seller_id,
            'selected_n11_seller': seller_name, 'selected_n11_merchant_id': seller_id,
            'selected_n11_variant_verified': True, 'sku': str(sku['id']),
            'product_id': str(product['id']), 'group_id': group, 'gtin13': gtin,
            'source_title': title, 'url': canonical, 'price': float(price), 'currency': 'TRY',
            'condition': 'new', 'availability': 'in_stock',
            'observed_at': datetime.now(timezone.utc).isoformat()}, 'selected_n11_cash_offer'


def catalog_offer_matches(offer, name, specs):
    return (isinstance(offer, dict) and offer.get('platform') == 'n11'
            and offer.get('selected_n11_variant_verified') is True
            and offer.get('merchant') == offer.get('selected_n11_seller')
            and offer.get('merchant_id') == offer.get('selected_n11_merchant_id')
            and type(offer.get('merchant_id')) is int and offer['merchant_id'] > 0
            and catalog_request_url(offer.get('url')) == offer.get('url')
            and offer.get('url', '').endswith('-' + str(offer.get('group_id')))
            and _gtin13(offer.get('gtin13')) is not None
            and offer.get('currency') == 'TRY' and offer.get('condition') == 'new'
            and offer.get('availability') == 'in_stock'
            and TRPriceScraper().has_observed_offer_evidence(name, offer, specs))


class N11CatalogSource:
    def __init__(self, get=None, post=None, transport=None):
        self.transport = transport or PriorityRetailerTransport(get=get, post=post, maximum_per_host=6)
        self._profiles, self._ambiguous = {}, set()

    def configure_catalog_budget(self, count):
        self.transport.configure_catalog_budget(count)

    def register_catalog_targets(self, name, targets, specs):
        matcher = TRPriceScraper()
        for target in targets:
            url = target.get('source_url') if isinstance(target, dict) else None
            if (product_host(url) != 'www.n11.com'
                    or not matcher.is_strict_match(name, urlparse(url).path.replace('-', ' '), specs)):
                continue
            profile = {'name': name, 'specs': dict(specs)}
            if url in self._profiles and self._profiles[url] != profile:
                self._ambiguous.add(url)
            self._profiles[url] = profile

    def read_offer(self, url):
        profile = self._profiles.get(url)
        if profile is None or url in self._ambiguous:
            return None, 'unmatched_catalog_pdp', None
        observed = catalog_request_url(url)
        html, reason, status = self.transport.read_document(observed)
        if html is None:
            return None, reason, status
        offer, reason = parse_catalog_listing(html, observed, profile['name'], profile['specs'])
        return offer, reason, status
