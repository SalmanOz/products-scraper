"""Read known public retailer PDPs without browser/challenge bypasses.

Only one selected Product and its own exact Offer can produce an observation.
Aggregator parent names, lowPrice, related products and remembered prices are
never promoted to retailer evidence.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import unescape
import ipaddress
import json
import math
import re
import unicodedata
from threading import Lock
from urllib.parse import parse_qsl, urljoin, urlparse

from bs4 import BeautifulSoup
import requests

from tr_price_scraper import TRPriceScraper
from public_retailer_sources import PublicRetailerSources, SOURCES, public_source
from pazarama_category_source import PazaramaCategorySource, pazarama_product_url
from vatan_reviewed_source import VatanReviewedSource
from mediamarkt_source import MediaMarktSource, product_url as mediamarkt_product_url
from apple_store_source import AppleStoreSource, reviewed_slug as apple_store_reviewed_slug


_MERCHANT_DOMAINS = {
    'n11.com': 'n11', 'pazarama.com': 'Pazarama', 'idefix.com': 'idefix',
    'vatanbilgisayar.com': 'Vatan Bilgisayar', 'hepsiburada.com': 'Hepsiburada',
    'trendyol.com': 'Trendyol', 'amazon.com.tr': 'Amazon TR',
    'turkcell.com.tr': 'Pasaj', 'mediamarkt.com.tr': 'MediaMarkt',
    'teknosa.com': 'Teknosa', 'pttavm.com': 'PttAVM', 'apple.com': 'Apple Store',
    'samsung.com': 'Samsung', 'mi.com': 'Xiaomi',
    **SOURCES,
}
ALLOWED_HOSTS = {
    host: merchant for apex, merchant in _MERCHANT_DOMAINS.items()
    for host in (apex, f'www.{apex}')
}
_AVAILABILITY = {
    'InStock': 'in_stock', 'LimitedAvailability': 'limited',
    'PreOrder': 'preorder', 'PreSale': 'preorder',
    'OutOfStock': 'out_of_stock', 'SoldOut': 'out_of_stock',
    'Discontinued': 'out_of_stock',
}
_REDIRECTS = {301, 302, 303, 307, 308}
_MAX_BODY_BYTES = 5 * 1024 * 1024
_VARIANT_KEYS = {
    'sku', 'skuid', 'selectedsku', 'selectedskuid', 'variant', 'variantid',
    'selectedvariant', 'selectedvariantid', 'variation', 'variationid',
    'selectedvariation', 'selectedvariationid', 'productvariant', 'productvariantid',
    'storage', 'storagegb', 'internalstorage', 'dahilihafiza', 'memory',
    'memorysize', 'memorygb', 'ram', 'ramgb', 'capacity', 'capacitygb',
}


def _variant_query(parsed):
    selected = {}
    for key, value in parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=50):
        key = re.sub(r'[^a-z0-9]', '', key.casefold())
        if key in _VARIANT_KEYS:
            value = value.strip().casefold()
            if not value or (key in selected and selected[key] != value):
                raise ValueError('Ambiguous selected variant')
            selected[key] = value
    return selected


def _safe_url(url):
    if not isinstance(url, str) or not url or len(url) > 1000:
        return None
    if re.search(r'[\x00-\x20\\]', url):
        return None
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        if (parsed.scheme != 'https' or not host or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)
                or host not in ALLOWED_HOSTS):
            return None
        query = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=50)
        if host.removeprefix('www.') in SOURCES and not public_source(url):
            return None
        if host.removeprefix('www.') == 'pazarama.com' and not pazarama_product_url(url):
            return None
        if any(re.search(r'token|auth|password|secret|signature|session|email|api.?key|access.?key|credential|add.?to.?cart|wc.?ajax|nonce|pao.?key|pao.?edit',
                         key, re.I) for key, _ in query):
            return None
        _variant_query(parsed)
        try:
            ipaddress.ip_address(host)
            return None
        except ValueError:
            pass
        return parsed
    except (TypeError, ValueError):
        return None


def _type_is(node, expected):
    types = node.get('@type', [])
    if not isinstance(types, list):
        types = [types]
    return any(isinstance(value, str) and value.rstrip('/').rsplit('/', 1)[-1] == expected
               for value in types)


def _same_resource(first, second):
    if isinstance(first, str) and isinstance(second, str):
        try:
            first = urljoin(second, first)
            # JSON-LD @id fragments identify the entity within the fetched document;
            # they are not navigable SKU selectors and are never fetched.
            first = urlparse(first)._replace(fragment='').geturl()
            second = urlparse(second)._replace(fragment='').geturl()
        except ValueError:
            return False
    a, b = _safe_url(first), _safe_url(second)
    return bool(a and b and ALLOWED_HOSTS[a.hostname] == ALLOWED_HOSTS[b.hostname]
                and a.path.rstrip('/') == b.path.rstrip('/')
                and _variant_query(a) == _variant_query(b))


def _schema_nodes(soup):
    nodes = []
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            pending = [json.loads(script.get_text())]
        except (TypeError, ValueError):
            continue
        # Traverse document lists/@graph only, not arbitrary recommendation or
        # hasVariant branches whose price may belong to another phone.
        while pending and len(nodes) < 2000:
            node = pending.pop()
            if isinstance(node, list):
                pending.extend(node[:2000])
            elif isinstance(node, dict):
                nodes.append(node)
                graph = node.get('@graph')
                if isinstance(graph, (dict, list)):
                    pending.append(graph)
    return nodes


def _challenge_page(soup):
    heading = ' '.join(element.get_text(' ', strip=True)
                       for element in soup.select('title, h1'))
    if re.search(r'captcha|robot check|just a moment|attention required|'
                 r'you have been blocked|access denied', heading, re.I):
        return True
    if soup.select('form[action*="validateCaptcha"], #captchacharacters, #challenge-form'):
        return True
    visible = soup.get_text(' ', strip=True)
    return bool(re.search(r'al[ıi]şverişe devam etmek için|'
                          r'continue shopping.*click the button', visible, re.I))


def _is_conditional_price(soup, offer, price):
    if any(key in offer for key in ('validForMemberTier', 'eligibleCustomerType', 'eligibleTransactionVolume')):
        return True
    def fold(value):
        return ''.join(c for c in unicodedata.normalize('NFKD', value)
                       if not unicodedata.combining(c)).lower().replace('ı', 'i')
    label = fold(str(offer.get('name', '')) + ' ' + str(offer.get('description', '')))
    condition = r'(?:sepette|sepete\s+ozel|kuponla|uyelere\s+ozel|premium[\s\x27’]*a?\s+ozel)'
    if re.search(condition, label):
        return True
    # Visible conditional prices are denial evidence even if JSON-LD silently
    # loses that condition. Never derive the unconditional price by subtracting
    # a discount, choosing a struck price or taking another product's number.
    visible = fold(soup.get_text(' ', strip=True))
    amount = r'([0-9]{1,3}(?:\.[0-9]{3})+(?:,[0-9]{1,2})?|[0-9]{4,7}(?:,[0-9]{1,2})?)'
    for match in re.finditer(condition + r'\s*:?\s*' + amount + r'\s*(?:tl|₺)', visible):
        if float(match[1].replace('.', '').replace(',', '.')) == price:
            return True
    return False


def _vatan_selected_variant(soup, product, offer, final_url, price, availability):
    """Accept only Vatan's separately priced, selected PDP beside other variants.

    Its Product.hasVariant lists other colors/capacities even though the Product
    itself has one Offer for the fetched SKU. The generic reader still rejects
    every other hasVariant shape and never reads prices from variant branches.
    """
    parsed = _safe_url(final_url)
    condition = offer.get('itemCondition')
    if (not parsed or parsed.hostname.removeprefix('www.') != 'vatanbilgisayar.com'
            or parsed.query or availability != 'in_stock'
            or not isinstance(condition, str)
            or condition.rstrip('/').rsplit('/', 1)[-1] != 'NewCondition'
            or offer.get('url') != final_url):
        return False
    seller = offer.get('seller')
    seller_url = _safe_url(seller.get('url')) if isinstance(seller, dict) else None
    if (not isinstance(seller, dict) or seller.get('name') != 'Vatan Bilgisayar'
            or not seller_url or seller_url.hostname.removeprefix('www.') != 'vatanbilgisayar.com'):
        return False
    sku = str(product.get('sku', ''))
    variants = product.get('hasVariant')
    def other_variant(variant):
        if not isinstance(variant, dict) or not isinstance(variant.get('url'), str):
            return False
        target = _safe_url(variant['url'])
        return bool(target and target.hostname.removeprefix('www.') == 'vatanbilgisayar.com'
                    and not target.query and not _same_resource(variant['url'], final_url)
                    and str(variant.get('sku', '')) != sku)
    if (not re.fullmatch(r'\d{5,7}', sku) or not isinstance(variants, list)
            or not variants or not all(other_variant(variant) for variant in variants)):
        return False
    heading = soup.select('.product-detail h1.product-list__product-name')
    code = soup.select('.product-detail .product-id')
    cart = soup.select('.product-detail button#add-to-cart-button.detail-cart-button.basketBTN')
    if (len(heading) != 1 or len(code) != 1 or len(cart) != 1
            or ' '.join(unescape(product.get('name', '')).split()) != heading[0].get_text(' ', strip=True)
            or not re.search(r'/\s*' + re.escape(sku) + r'\b', code[0].get_text(' ', strip=True))
            or cart[0].has_attr('disabled') or 'disabled' in cart[0].get('class', [])
            or str(cart[0].get('aria-disabled', '')).casefold() == 'true'
            or not re.search(r'\bSepete Ekle\b', cart[0].get_text(' ', strip=True), re.I)):
        return False
    web_price = soup.select('.product-detail .web-ozel-prod')
    visible_prices = (soup.select('.product-detail .web-ozel-prod .web-ozel-fiyat')
                      if web_price else soup.select('.product-detail .product-detail-price'))
    if len(visible_prices) != 1:
        return False
    visible = visible_prices[0].get_text(' ', strip=True)
    match = re.fullmatch(r'(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?)\s*(?:TL|₺)', visible, re.I)
    return bool(match and float(match[1].replace('.', '').replace(',', '.')) == price)


def _pazarama_visible_cash_price(soup, product, offer, url, schema_price, availability):
    """Read only the selected PDP's ordinary price, never its Sepette Offer.price.

    Pazarama's JSON-LD Offer.price is the automatic cart-discount amount when
    its selected product panel displays ``Sepette``. The separate, unstruck
    main price is the visible ordinary price before that cart promotion.
    """
    if (not pazarama_product_url(url) or availability != 'in_stock'
            or not isinstance(product.get('url'), str)
            or not _same_resource(product['url'], url)):
        return None
    if (any(key in offer for key in ('validForMemberTier', 'eligibleCustomerType',
                                    'eligibleTransactionVolume', 'priceSpecification'))
            or re.search(r'kupon|üyelere|uyelere|üyelik|uyelik|premium',
                         str(offer.get('name', '')) + ' ' + str(offer.get('description', '')), re.I)):
        return None
    headings = soup.select('.product-detail h1')
    if len(headings) != 1:
        return None
    heading = ' '.join(headings[0].get_text(' ', strip=True).split())
    title = ' '.join(unescape(str(product.get('name', ''))).split())
    if not heading or heading != title:
        return None
    brand = product.get('brand')
    brand_name = brand.get('name') if isinstance(brand, dict) else brand
    if not isinstance(brand_name, str) or not brand_name.strip():
        return None
    brand_key = TRPriceScraper._normalize_words(brand_name)
    title_words = set(TRPriceScraper._normalize_words(title).split())
    brand_markers = {'xiaomi': {'xiaomi', 'redmi', 'poco'},
                     'samsung': {'samsung', 'galaxy'},
                     'apple': {'apple', 'iphone'}}.get(brand_key, {brand_key})
    if not title_words.intersection(brand_markers):
        return None
    if str(product.get('itemCondition', '')).rstrip('/').rsplit('/', 1)[-1] != 'NewCondition':
        return None
    if (offer.get('itemCondition') is not None and
            str(offer['itemCondition']).rstrip('/').rsplit('/', 1)[-1] != 'NewCondition'):
        return None
    seller = offer.get('seller')
    if not isinstance(seller, str) or not seller.strip() or len(seller) > 120:
        return None
    detail = soup.select_one('.product-detail')
    if detail is None:
        return None
    seller_elements = [node for node in detail.select('p')
                       if len(node.select('span')) == 2
                       and node.select('span')[0].get_text(' ', strip=True).rstrip(':').strip() == 'Satıcı']
    if (len(seller_elements) != 1
            or TRPriceScraper._normalize_words(seller_elements[0].select('span')[1].get_text(' ', strip=True))
            != TRPriceScraper._normalize_words(seller)):
        return None
    carts = detail.select('button[data-testid="base-addbasket-button"]')
    if (not carts or any(button.has_attr('disabled')
                         or str(button.get('aria-disabled', '')).lower() == 'true'
                         or button.get_text(' ', strip=True) != 'Sepete Ekle'
                         for button in carts)):
        return None
    # The selected product area excludes site navigation and recommendation
    # cards. Other discount mechanisms are not verified by this source.
    visible = detail.get_text(' ', strip=True)
    if re.search(r'kupon|üyelere|uyelere|üyelik|uyelik|premium|plus\s+fiyat', visible, re.I):
        return None
    main_prices = [node for node in detail.select('p')
                   if 'text-4xl' in node.get('class', [])
                   and not node.find_parent(attrs={'data-testid': 'product-card-discount'})]
    if len(main_prices) != 1:
        return None
    main_price = main_prices[0]
    if (any(name in {'del', 's'} for name in [main_price.name, *(parent.name for parent in main_price.parents)])
            or any('line-through' in parent.get('class', [])
                   for parent in [main_price, *main_price.parents] if hasattr(parent, 'get'))):
        return None
    amount = re.fullmatch(r'(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*TL',
                          main_price.get_text(' ', strip=True), re.I)
    if not amount:
        return None
    cash_price = float(amount[1].replace('.', '').replace(',', '.'))
    if not math.isfinite(cash_price) or not 0 < cash_price <= 5_000_000:
        return None
    discounts = detail.select('[data-testid="product-card-discount"]')
    if discounts:
        if len(discounts) != 1 or discounts[0].parent is not main_price.parent:
            return None
        discount = re.fullmatch(r'Sepette\s+(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*TL',
                                discounts[0].get_text(' ', strip=True), re.I)
        if (not discount or float(discount[1].replace('.', '').replace(',', '.')) != schema_price
                or cash_price <= schema_price):
            return None
    elif re.search(r'\bSepette\b', visible, re.I) or cash_price != schema_price:
        return None
    return cash_price


def _product_offer(soup, final_url):
    # MediaMarkt publishes marketplace and refurbished BuyAction offers under
    # its own host. Only the dedicated selected first-party reader may accept
    # those pages; the generic Product parser must never label a third-party
    # seller as MediaMarkt.
    if (_safe_url(final_url) and
            _safe_url(final_url).hostname.removeprefix('www.') == 'mediamarkt.com.tr'):
        return None
    nodes = _schema_nodes(soup)
    products = [node for node in nodes if _type_is(node, 'Product')]
    by_id = {node['@id']: node for node in nodes
             if isinstance(node.get('@id'), str)}

    def resolve(node):
        if isinstance(node, dict) and set(node) == {'@id'}:
            return by_id.get(node['@id'], {})
        return node

    def own_offer(product):
        offers = product.get('offers')
        offers = offers if isinstance(offers, list) else [offers]
        if len(offers) != 1:
            return None
        offer = resolve(offers[0])
        if not isinstance(offer, dict) or not _type_is(offer, 'Offer'):
            return None
        if _type_is(offer, 'AggregateOffer') or 'lowPrice' in offer or 'highPrice' in offer:
            return None
        # Troy publishes an http Offer URL on its HTTPS PDP. Normalize document
        # metadata only, with an exact HTTPS canonical; NEVER request HTTP or
        # relax URL validation for any other host, path, port or variant.
        if public_source(final_url) == 'troyestore.com' and isinstance(offer.get('url'), str):
            raw = offer['url']
            canonical = soup.select_one('link[rel="canonical"]')
            if raw.startswith('http://') and canonical and canonical.get('href') == final_url:
                secured = 'https://' + raw[len('http://'):]
                if public_source(secured) and urlparse(secured).netloc == urlparse(final_url).netloc and _same_resource(secured, final_url):
                    offer = {**offer, 'url': secured}
        return offer

    # An explicit URL links the Product/Offer to the fetched PDP. If there are
    # several Products but no such link, guessing the main one is unsafe.
    selected = []
    for product in products:
        if any(isinstance(product.get(key), str) and
               not _same_resource(urljoin(final_url, product[key]), final_url)
               for key in ('url', '@id') if product.get(key)):
            continue
        offer = own_offer(product)
        identities = [product.get('url'), product.get('@id')]
        if offer:
            identities.append(offer.get('url'))
        if any(_same_resource(url, final_url) for url in identities):
            selected.append(product)
    if not selected and len(products) == 1:
        product = products[0]
        explicit = [product.get('url'), product.get('@id')]
        if any(isinstance(url, str) and not _same_resource(urljoin(final_url, url), final_url)
               for url in explicit if url):
            return None
        selected = products
    if len(selected) != 1:
        return None
    product = selected[0]
    offer = own_offer(product)
    title = product.get('name')
    if (not offer or not isinstance(title, str) or not title.strip()
            or len(title.encode('utf-16-le', errors='surrogatepass')) > 2000):
        return None
    if offer.get('url') is not None and not _same_resource(offer['url'], final_url):
        return None
    item_reference = offer.get('itemOffered')
    if (isinstance(item_reference, dict) and item_reference.get('@id') is not None
            and item_reference['@id'] != product.get('@id')):
        return None
    item = resolve(item_reference)
    if isinstance(item, dict):
        if item.get('name') is not None and item['name'] != title:
            return None
        if item.get('@id') is not None and item['@id'] != product.get('@id'):
            return None
    if offer.get('priceCurrency') != 'TRY':
        return None
    raw_price = offer.get('price')
    if isinstance(raw_price, bool) or not re.fullmatch(r'\d+(?:\.\d{1,2})?', str(raw_price)):
        return None
    price = float(raw_price)
    if not math.isfinite(price) or not 0 < price <= 5_000_000:
        return None
    is_pazarama = pazarama_product_url(final_url)
    if not is_pazarama and _is_conditional_price(soup, offer, price):
        return None
    availability = offer.get('availability')
    if not isinstance(availability, str):
        return None
    availability = _AVAILABILITY.get(availability.rstrip('/').rsplit('/', 1)[-1])
    if availability is None:
        return None
    # Never read priceSpecification or installment figures as a cash price.
    if any(key in offer for key in ('billingDuration', 'billingIncrement', 'loanTerm')):
        return None
    specifications = offer.get('priceSpecification', [])
    specifications = specifications if isinstance(specifications, list) else [specifications]
    if any(isinstance(spec, dict) and any(key in spec for key in (
            'billingDuration', 'billingIncrement', 'loanTerm', 'billingStart'))
            for spec in specifications):
        return None
    if re.search(r'\b(?:taksit|installment|monthly|aylık|aylik)\b', str(offer.get('name', '')), re.I):
        return None
    if product.get('hasVariant') and not _vatan_selected_variant(
            soup, product, offer, final_url, price, availability):
        return None
    if is_pazarama:
        price = _pazarama_visible_cash_price(soup, product, offer, final_url, price, availability)
        if price is None:
            return None
    if public_source(final_url) and not _public_retailer_product(soup, product, offer, final_url, price, availability):
        return None
    return {'merchant': ALLOWED_HOSTS[_safe_url(final_url).hostname],
            'price': price, 'currency': 'TRY', 'availability': availability,
            # An Offer may name its canonical/seller URL while the fetched PDP
            # carries tracking or another seller query. Keep the observed
            # Offer's URL, never claim that price for a different URL selection.
            'source_title': unescape(title.strip()) if product.get('hasVariant') else title.strip(),
            'url': urljoin(final_url, offer['url']) if offer.get('url') else final_url,
            'observed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}


def _public_retailer_product(soup, product, offer, url, price, availability):
    """New sources are consumer, new, Turkish Apple SKUs only; fail on drift."""
    host = public_source(url)
    heading = soup.select_one('h1')
    title = heading.get_text(' ', strip=True) if heading else ''
    if not title or not TRPriceScraper().is_strict_match(title, product['name'], {}):
        return False
    if not re.search(r'\biphone\b', title, re.I):
        return False
    if re.search(r'teshir|teşhir|outlet|yenilen|ikinci|ithalatçı|ithalatci|yurt dışı', title + ' ' + url, re.I):
        return False
    if str(offer.get('itemCondition', '')).rstrip('/').rsplit('/', 1)[-1] != 'NewCondition':
        return False
    brand = product.get('brand', {})
    if (brand.get('name') if isinstance(brand, dict) else brand) != 'Apple':
        return False
    # TU/A is Apple's Turkey sales SKU. Do not infer warranty for an imported
    # or unidentifiable SKU merely from the retailer's name.
    sku_proof = str(product.get('sku', '')) + ' ' + title + ' ' + url
    if not re.search(r'[a-z0-9]{4,}tu(?:/a\b|-a\b|a(?:_|\b))', sku_proof, re.I):
        return False
    seller = offer.get('seller', {})
    expected_seller = 'Pozitif Teknoloji' if host == 'pt.com.tr' else 'Troyestore'
    if not isinstance(seller, dict) or seller.get('name') != expected_seller:
        return False
    selector = '.summary .pt-price-col > .product-page-price' if host == 'pt.com.tr' else '.product-details-area .p-info .p-price .one-price'
    prices = soup.select(selector)
    if len(prices) != 1:
        return False
    text = prices[0].get_text(' ', strip=True)
    amounts = re.findall(r'\d{1,3}(?:\.\d{3})+(?:,\d{2})|\d+(?:,\d{2})', text)
    if len(amounts) != 1 or float(amounts[0].replace('.', '').replace(',', '.')) != price:
        return False
    if host == 'pt.com.tr':
        if 'Tüm Kartlar Tek Çekim' not in soup.get_text(' ', strip=True):
            return False
        # Main SKU stock, not accessory stock or the related-product carousel.
        stock = soup.select_one('.summary > .stock')
        if not stock:
            return False
        stock_classes = stock.get('class', [])
        expected = 'in_stock' if 'in-stock' in stock_classes else 'out_of_stock' if 'out-of-stock' in stock_classes else None
        if availability != expected:
            return False
    elif availability in {'in_stock', 'limited'}:
        button = soup.select_one('button[data-js="btn-add-basket"]')
        if not button or button.has_attr('disabled') or 'btn-disabled' in button.get('class', []):
            return False
    return True


class RetailerOfferRefresher:
    MAX_TARGETS_PER_REFRESH = 12

    def __init__(self, session=None, fetch=None, vatan_source=None,
                 mediamarkt_source=None, apple_store_source=None):
        self._fetch = fetch or (session.get if session is not None else requests.get)
        self.public_sources = PublicRetailerSources(fetch=self._fetch)
        self.pazarama_source = PazaramaCategorySource(fetch=self._fetch)
        self.vatan_reviewed_source = vatan_source or VatanReviewedSource(get=self._fetch)
        self.mediamarkt_source = mediamarkt_source or MediaMarktSource(get=self._fetch)
        self.apple_store_source = apple_store_source or AppleStoreSource(get=self._fetch)
        self._scraper = TRPriceScraper()
        self._cache = {}
        self._blocked_hosts = set()
        self._transport_failures = {}
        self._diagnostics = {}
        self._lock = Lock()

    def _record_attempt(self, source_url, reason, http_status=None):
        # URL safety is checked before this method; never log credentials or
        # response bodies, and never turn a failed attempt into checked_at.
        with self._lock:
            self._diagnostics[source_url] = {
                'reason': reason, 'http_status': http_status,
            }

    def target_report(self, product_name, targets, expected_specs):
        rows = []
        if not isinstance(targets, (list, tuple)):
            return rows
        urls = [t.get('source_url') if isinstance(t, dict) else t for t in targets]
        unique = list(dict.fromkeys(u for u in urls if _safe_url(u)))
        # A later merchant may have been attempted before earlier extra colors.
        # Keep those observations visible even when the report is capped.
        with self._lock:
            attempted = [u for u in unique if self._diagnostics.get(u, {}).get('reason')
                         not in (None, 'target_budget_skipped')]
        attempted_urls = set(attempted)
        visible = (attempted + [u for u in unique if u not in attempted_urls])[:24]
        for url in visible:
            with self._lock:
                detail = dict(self._diagnostics.get(url, {'reason': 'not_attempted', 'http_status': None}))
                offer = self._cache.get(url)
            if offer and not self._scraper.has_observed_offer_evidence(product_name, offer, expected_specs):
                detail['reason'] = 'identity_or_variant_not_verified'
            rows.append({'source_url': url, 'merchant': ALLOWED_HOSTS[_safe_url(url).hostname], **detail})
        return rows

    def _transport_failure(self, host):
        host = host.removeprefix('www.')
        with self._lock:
            failures = self._transport_failures.get(host, 0) + 1
            self._transport_failures[host] = failures
            if failures >= 2:
                self._blocked_hosts.add(host)

    def _fetch_target(self, source_url):
        parsed = _safe_url(source_url)
        if not parsed:
            return None
        with self._lock:
            if source_url in self._cache:
                return self._cache[source_url]
        if parsed.hostname.removeprefix('www.') == 'mediamarkt.com.tr':
            if mediamarkt_product_url(source_url):
                candidate, reason, status = self.mediamarkt_source.read_offer(source_url)
            else:
                candidate, reason, status = None, 'invalid_mediamarkt_pdp_url', None
            self._record_attempt(source_url, reason, status)
            with self._lock:
                self._cache[source_url] = candidate
            return candidate
        if parsed.hostname.removeprefix('www.') == 'apple.com':
            if apple_store_reviewed_slug(source_url):
                candidate, reason, status = self.apple_store_source.read_offer(source_url)
            else:
                candidate, reason, status = None, 'unreviewed_apple_pdp', None
            self._record_attempt(source_url, reason, status)
            with self._lock:
                self._cache[source_url] = candidate
            return candidate
        requested_variant = _variant_query(parsed)
        current = source_url
        candidate = None
        self._record_attempt(source_url, 'no_exact_product_offer')
        for redirects in range(4):
            parsed = _safe_url(current)
            if not parsed:
                break
            reviewed_vatan = self.vatan_reviewed_source.is_reviewed(current)
            if reviewed_vatan:
                allowed, reason = self.vatan_reviewed_source.prepare(current)
                if not allowed:
                    self._record_attempt(source_url, reason)
                    break
            with self._lock:
                blocked = parsed.hostname.removeprefix('www.') in self._blocked_hosts
            # Generic discovery URLs can trip the retailer-wide circuit after
            # a 403. Reviewed Vatan PDPs have their own exact-URL robots gate
            # and bounded browser fallback, so keep those six independent.
            if blocked and not reviewed_vatan:
                self._record_attempt(source_url, 'host_circuit_open')
                break
            try:
                fetch = (self.public_sources.fetch if public_source(current) else
                         self.pazarama_source.fetch if pazarama_product_url(current) else self._fetch)
                response = fetch(current, timeout=(5, 15), allow_redirects=False,
                                       headers={'Accept': 'text/html', 'Accept-Language': 'tr-TR,tr;q=0.9'})
                status = response.status_code
                browser_html = None
                if status == 403 and reviewed_vatan:
                    browser_html, reason = self.vatan_reviewed_source.browser_get(current)
                    if browser_html is None:
                        self._record_attempt(source_url, reason, status)
                    else:
                        status = 200
                if status in (403, 429):
                    if not (status == 403 and reviewed_vatan):
                        self._record_attempt(source_url, 'access_denied' if status == 403 else 'rate_limited', status)
                    with self._lock:
                        self._blocked_hosts.add(parsed.hostname.removeprefix('www.'))
                    break
                if status in (500, 502, 503, 504):
                    self._record_attempt(source_url, 'retailer_unavailable', status)
                    self._transport_failure(parsed.hostname)
                    break
                if status in _REDIRECTS:
                    if reviewed_vatan:
                        self._record_attempt(source_url, 'reviewed_url_redirect', status)
                        break
                    location = response.headers.get('Location') or response.headers.get('location')
                    if redirects == 3 or not isinstance(location, str):
                        self._record_attempt(source_url, 'redirect_limit_or_missing_location', status)
                        break
                    destination = urljoin(current, location)
                    destination_url = _safe_url(destination)
                    if not destination_url or (requested_variant and
                            _variant_query(destination_url) != requested_variant) or (public_source(source_url) and
                            public_source(destination) != public_source(source_url)) or (
                            pazarama_product_url(source_url) and not pazarama_product_url(destination)):
                        self._record_attempt(source_url, 'unsafe_or_changed_variant_redirect', status)
                        break
                    current = destination
                    continue
                if status != 200:
                    self._record_attempt(source_url, 'http_error', status)
                    break
                actual_url = current if browser_html is not None else getattr(response, 'url', None) or current
                if not _safe_url(actual_url) or not _same_resource(actual_url, current):
                    self._record_attempt(source_url, 'unexpected_final_destination', status)
                    break
                html = browser_html if browser_html is not None else response.text
                if not isinstance(html, str) or len(html.encode('utf-8')) > _MAX_BODY_BYTES:
                    self._record_attempt(source_url, 'invalid_or_oversized_response', status)
                    break
                soup = BeautifulSoup(html, 'html.parser')
                if _challenge_page(soup):
                    self._record_attempt(source_url, 'challenge_page', status)
                    with self._lock:
                        self._blocked_hosts.add(parsed.hostname.removeprefix('www.'))
                    break
                with self._lock:
                    self._transport_failures[parsed.hostname.removeprefix('www.')] = 0
                candidate = _product_offer(soup, actual_url)
                self._record_attempt(source_url, 'observed_exact_offer' if candidate else 'no_exact_product_offer', status)
                break
            except requests.RequestException:
                self._record_attempt(source_url, 'transport_error')
                self._transport_failure(parsed.hostname)
                break
            except (TypeError, ValueError):
                self._record_attempt(source_url, 'invalid_response')
                break
        with self._lock:
            self._cache[source_url] = candidate
        return candidate

    def refresh_existing_offer_targets(self, product_name, targets, expected_specs):
        if not isinstance(targets, (list, tuple)):
            return []
        by_merchant = {}
        seen = set()
        for target in targets:
            url = target.get('source_url') if isinstance(target, dict) else target
            parsed = _safe_url(url)
            if parsed and url not in seen:
                seen.add(url)
                by_merchant.setdefault(ALLOWED_HOSTS[parsed.hostname], []).append(url)
        # Search results often contain many colors from one retailer before the
        # next retailer. Spend the bounded PDP budget across distinct merchants
        # first, then try additional colors from each merchant in source order.
        urls = []
        for index in range(self.MAX_TARGETS_PER_REFRESH):
            for bucket in by_merchant.values():
                if index < len(bucket):
                    urls.append(bucket[index])
                    if len(urls) == self.MAX_TARGETS_PER_REFRESH:
                        break
            if len(urls) == self.MAX_TARGETS_PER_REFRESH:
                break
        if not urls:
            return []
        selected = set(urls)
        with self._lock:
            for bucket in by_merchant.values():
                for url in bucket:
                    if url not in selected and url not in self._diagnostics:
                        self._diagnostics[url] = {'reason': 'target_budget_skipped', 'http_status': None}
        selected_by_merchant = {}
        for url in urls:
            merchant = ALLOWED_HOSTS[_safe_url(url).hostname]
            selected_by_merchant.setdefault(merchant, []).append(url)

        def fetch_merchant(merchant_urls):
            # A 403/429 from the first PDP must open the circuit before another
            # request to that retailer starts. Other retailers remain parallel.
            return [(url, self._fetch_target(url)) for url in merchant_urls]

        with ThreadPoolExecutor(max_workers=4) as executor:
            fetched = dict(item for group in executor.map(
                fetch_merchant, selected_by_merchant.values()) for item in group)
        candidates = [fetched[url] for url in urls]
        # The cached observation is rechecked for every requested catalog model;
        # a base S26 cache hit cannot establish a Plus profile.
        return [dict(offer) for offer in candidates if offer
                and self._scraper.is_strict_match(product_name, offer['source_title'], expected_specs)
                and self._scraper.has_observed_offer_evidence(product_name, offer, expected_specs)]


def refresh_existing_offer_targets(product_name, targets, expected_specs):
    """Convenience one-shot sync API; reuse the class to retain the run cache."""
    return RetailerOfferRefresher().refresh_existing_offer_targets(product_name, targets, expected_specs)
