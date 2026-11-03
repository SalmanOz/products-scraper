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


def parse_selected_marketplace(html, url, profile):
    if (product_host(url) != 'www.teknosa.com' or urlparse(url).query
            or not isinstance(html, str) or len(html.encode()) > MAX_HTML_BYTES
            or not isinstance(profile, dict) or not isinstance(profile.get('specs'), dict)):
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
    if price is None or _amount(cart.get('data-price-without-discount')) != price:
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
            or _amount(state.get('unit_price')) != price or _amount(state.get('unit_sale_price')) != price):
        return None, 'selected_product_state_mismatch'
    from retailer_offer_refresh import _schema_nodes, _type_is, _is_conditional_price
    schemas = [p for p in _schema_nodes(soup, url) if _type_is(p, 'Product')]
    schema = schemas[0] if len(schemas) == 1 else {}
    offer = schema.get('offers')
    schema_seller = offer.get('seller') if isinstance(offer, dict) else None
    normalize_pair = lambda text: re.sub(r'(?<=\d)/(?=\d)', ' ', str(text or ''))
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
    if not catalog_offer_matches(result, profile.get('name'), profile['specs']):
        return None, 'catalog_variant_mismatch'
    return result, 'selected_teknosa_marketplace_cash_offer'


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
