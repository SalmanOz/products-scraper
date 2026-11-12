"""Public other-seller rows bound to the already verified selected Teknosa SKU.

Fetch the canonical product document, never a disallowed shop/filter URL. The
retailer supplies each actual destination, listing/form identity, seller link,
visible cash price and delivery promise in that same public document. Global
analytics price fields repeat the winner/own-inventory price across rows; they
are not this merchant's price and are deliberately not quoted.
"""
import re
from collections import Counter
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from priority_retailer_transport import product_host
from retailer_barcode import retailer_gtin13
from teknosa_reviewed_source import _cash_price


def canonical_document_url(url):
    if product_host(url) != 'www.teknosa.com':
        return None
    return urlparse(url)._replace(query='').geturl()


def public_seller_offers(html, document_url, profile, maximum=3):
    if (canonical_document_url(document_url) != document_url or type(maximum) is not int
            or not 1 <= maximum <= 8):
        return []
    from teknosa_reviewed_source import parse_catalog_offer
    base, _ = parse_catalog_offer(html, document_url, profile)
    if not base or not base.get('gtin13'):
        return []
    soup = BeautifulSoup(html, 'html.parser')
    sku = urlparse(document_url).path.rsplit('-p-', 1)[-1]
    from retailer_offer_refresh import _is_conditional_price
    offers = []
    cards = soup.select('.pds.active[data-prod-seller-url]')[:24]
    counts = Counter(urljoin(document_url, c.get('data-prod-seller-url')) for c in cards)
    for card in cards:
        relative = card.get('data-prod-seller-url')
        destination = urljoin(document_url, relative)
        if (canonical_document_url(destination) != document_url or not urlparse(destination).query
                or counts[destination] != 1):
            continue
        carts = card.select('button#addToCartButton')
        links = card.select('.pds-title > a[href]')
        prices = card.select('.pds-prices .pds-price2 .prc-last')
        delivery = card.select('.pds-delivery-detail > span')
        forms = card.select('form.add_to_cart_form')
        if any(len(nodes) != 1 for nodes in (carts, links, prices, delivery, forms)):
            continue
        cart, link, form = carts[0], links[0], forms[0]
        seller = link.get_text(' ', strip=True)
        shop = cart.get('data-product-shop-id')
        listing = re.fullmatch(r'shop_offer_([1-9]\d{2,12})', str(cart.get('data-shop-id') or ''))
        codes = form.select('input[name="productCodePost"]')
        price = _cash_price(prices[0].get_text(' ', strip=True))
        pending = (cart.has_attr('disabled') and cart.get('disabled') in ('', 'disabled')
                   and 'js-enable2-btn' in cart.get('class', []))
        if (not seller or len(seller) > 100 or re.search(r'[\x00-\x1f]', seller)
                or seller.casefold() == 'teknosa' or not re.fullmatch(r'[1-9]\d{2,9}', str(shop or ''))
                or shop == '1001' or urlparse(destination).query != 'shopId=' + shop
                or not listing or 'js-shop_offer_' + listing[1] not in cart.get('class', [])
                or cart.get('data-product-id') != sku
                or cart.get('data-product-name') != base['source_title']
                or retailer_gtin13(cart.get('data-product-ean')) != base['gtin13']
                or cart.get('data-shop-name') != seller or cart.get('data-product-url') != relative
                or not re.fullmatch(r'/magaza/[a-z0-9-]+', str(link.get('href') or ''))
                or cart.get('aria-disabled') == 'true' or cart.has_attr('disabled') and not pending
                or cart.get_text(' ', strip=True) != 'Sepete Ekle'
                or form.get('method', '').lower() != 'post' or form.get('action') != '/sepet/add'
                or len(codes) != 1 or codes[0].get('value') != 'offer_' + listing[1]
                or price is None or not 0 < price <= 1000000 or float(price) < base['price'] * 0.6
                or not re.fullmatch(r'[1-9]\d?(?:\s*-\s*[1-9]\d?)?\s+iş günü içinde kargoda',
                                    delivery[0].get_text(' ', strip=True))
                or _is_conditional_price(card, {}, float(price))):
            continue
        offers.append({'platform': 'Teknosa', 'merchant': seller, 'merchant_id': shop,
            'selected_teknosa_seller': seller, 'selected_teknosa_shop_id': shop,
            'selected_teknosa_variant_verified': True, 'sku': sku, 'listing_id': listing[1],
            'gtin13': base['gtin13'], 'observed_barcode': cart.get('data-product-ean'),
            'source_title': base['source_title'], 'url': destination,
            'seller_url': urljoin(document_url, link['href']),
            'source_document_url': document_url, 'source_offer_scope': 'public_other_seller_row',
            'source_render_state': 'pending_seller_card' if pending else 'enabled_seller_card',
            'price': float(price), 'currency': 'TRY', 'condition': 'new', 'availability': 'in_stock',
            'observed_at': base['observed_at']})
    return sorted(offers, key=lambda o: (o['price'], o['merchant_id']))[:maximum]


def parse_seller_destination(html, destination, profile):
    document_url = canonical_document_url(destination)
    if not document_url or document_url == destination:
        return None, 'invalid_public_seller_destination'
    found = [offer for offer in public_seller_offers(html, document_url, profile, maximum=8)
             if offer['url'] == destination]
    return (found[0], 'observed_public_seller_card') if len(found) == 1 else (
        None, 'public_seller_card_not_proven')
