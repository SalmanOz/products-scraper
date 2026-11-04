"""Discover phone PDP addresses from the retailer's public category pages.

Card amounts are deliberately ignored. Each destination must pass the existing
fresh selected SKU/seller/stock/cash reader before it can become an offer.
"""
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from priority_retailer_transport import MAX_HTML_BYTES, product_host
from tr_price_scraper import TRPriceScraper


CATEGORY_URL = 'https://www.trendyol.com/cep-telefonu-x-c103498'
MAX_CATEGORY_PAGES = 100


def category_identity(url):
    if not isinstance(url, str) or len(url) > 150:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if (parsed.scheme != 'https' or parsed.netloc != 'www.trendyol.com'
            or parsed.fragment):
        return None
    page = 1
    if parsed.query:
        match = re.fullmatch(r'pi=([1-9]\d{0,2})', parsed.query)
        if match is None:
            return None
        page = int(match[1])
    if parsed.path == '/cep-telefonu-x-c103498':
        return {'page': page, 'brand': None, 'path': parsed.path, 'heading': 'Cep Telefonu'}
    match = re.fullmatch(r'/([a-z0-9]+)-(android-cep-telefonu|cep-telefonu)-x-b[1-9]\d{0,8}-c(103498|164461)', parsed.path)
    if (match is None or match[1] not in TRPriceScraper.PHONE_BRAND_ALIASES or
            (match[2], match[3]) not in {('cep-telefonu', '103498'), ('android-cep-telefonu', '164461')}):
        return None
    return {'page': page, 'brand': match[1], 'path': parsed.path,
            'heading': match[1] + (' Android Cep Telefonu' if match[3] == '164461' else ' Cep Telefonu')}


def category_page_number(url):
    identity = category_identity(url)
    return identity['page'] if identity else None


def _brand_keys(product):
    words = set(TRPriceScraper._identity_words(product.get('name', '')))
    return {brand for brand, aliases in TRPriceScraper.PHONE_BRAND_ALIASES.items() if words & aliases}


def discovery_links(html, url, products):
    """Actual public phone-brand anchors only; never construct a brand ID."""
    identity = category_identity(url)
    if (not isinstance(html, str) or len(html.encode()) > MAX_HTML_BYTES or
            identity is None and product_host(url) != 'www.trendyol.com'):
        return []
    soup = BeautifulSoup(html, 'html.parser')
    headings = soup.select('h1.title') if identity else soup.select('h1[data-testid="product-title"]')
    if len(headings) != 1:
        return []
    title = headings[0].get_text(' ', strip=True)
    if identity:
        if TRPriceScraper._normalize_words(title) != TRPriceScraper._normalize_words(identity['heading']):
            return []
    elif not any(TRPriceScraper().is_strict_match(re.sub(r'\b[45]g\b', '',
            TRPriceScraper._catalog_model_name(p.get('name', ''), p.get('attributes', {})) or '', flags=re.I).strip(),
            title, {}) for p in products):
        # A phone-brand category spans both radios. Omitting that word only
        # broadens routing; the selected PDP price guard is unchanged.
        return []
    priorities = {}
    for index, product in enumerate(products):
        for brand in _brand_keys(product):
            priorities.setdefault(brand, index)
    found = {}
    for anchor in soup.select('a[href]'):
        try:
            target = urljoin(url, anchor['href'])
        except ValueError:
            continue
        other = category_identity(target)
        if other and other['brand'] in priorities and other['page'] == 1:
            found.setdefault(other['path'], target)
    return sorted(found.values(), key=lambda target: (priorities[category_identity(target)['brand']], target))


def recovery_seed_targets(targets, prices, maximum=8):
    """Reorder existing refresh PDPs across brands before category discovery."""
    if type(maximum) is not int or not 0 <= maximum <= 8:
        raise ValueError('invalid_trendyol_brand_seed_limit')
    if not maximum:
        return []
    def rank(item):
        _, product = item
        status = prices.get(product['id'])
        current = status[1] if isinstance(status, (list, tuple)) and len(status) == 2 and status[0] == product['slug'] else None
        return 0 if current is False else 1 if current is True else 2
    selected, brands = [], set()
    for url, product in sorted(targets, key=rank):
        keys = _brand_keys(product)
        if keys & {'redmi', 'poco'}:
            keys.discard('xiaomi')  # One seed for the same sub-brand spelling.
        keys = tuple(sorted(keys))
        if product_host(url) != 'www.trendyol.com' or not keys or keys in brands:
            continue
        brands.add(keys)
        selected.append((url, product))
        if len(selected) == maximum:
            break
    return selected


def parse_category_document(html, url, diagnostic=None):
    if isinstance(diagnostic, dict):
        diagnostic['status'] = 'invalid_category_document'
    identity = category_identity(url)
    if identity is None or not isinstance(html, str) or len(html.encode()) > MAX_HTML_BYTES:
        return [], None
    page = identity['page']
    soup = BeautifulSoup(html, 'html.parser')
    headings = soup.select('h1.title')
    if (len(headings) != 1 or TRPriceScraper._normalize_words(headings[0].get_text(' ', strip=True)) !=
            TRPriceScraper._normalize_words(identity['heading'])):
        if isinstance(diagnostic, dict):
            diagnostic['status'] = 'category_identity_missing'
        return [], None
    cards, seen = [], set()
    for anchor in soup.select('a.product-card[href]'):
        titles = anchor.select('h2.title')
        title = titles[0].get_text(' ', strip=True) if len(titles) == 1 else ''
        try:
            raw_url = urljoin(CATEGORY_URL, anchor['href'])
        except ValueError:
            continue
        if (not title or len(title) > 500 or product_host(raw_url) != 'www.trendyol.com'):
            continue
        parsed = urlparse(raw_url)
        target = parsed._replace(query='').geturl()
        if target in seen:
            continue
        seen.add(target)
        active_cart = any(button.get_text(' ', strip=True) == 'Sepete Ekle'
                          and not button.has_attr('disabled') and button.get('aria-disabled') != 'true'
                          for button in anchor.select('button'))
        cards.append({'source_url': target, 'discovery_title': title,
                      'discovery_product_id': parsed.path.rsplit('-p-', 1)[-1],
                      'stock_priority': active_cart})
    page_urls = set()
    page_numbers = set()
    for anchor in soup.select('a[href]'):
        try:
            target = urljoin(CATEGORY_URL, anchor['href'])
        except ValueError:
            continue
        other = category_identity(target)
        number = other['page'] if other and other['path'] == identity['path'] else None
        if number is not None:
            page_numbers.add(number)
            if number == page + 1:
                page_urls.add(target)
    next_url = next(iter(page_urls)) if len(page_urls) == 1 else None
    status = ('pagination_unrecognized' if not page_numbers or
              any(number > page for number in page_numbers) and next_url is None else 'parsed')
    if isinstance(diagnostic, dict):
        diagnostic.update(status=status, destinations=len(cards),
                          category_scope='phone_brand' if identity['brand'] else 'phones',
                          highest_observed_page=max(page_numbers) if page_numbers else None)
    return cards, next_url
