"""Public phone-category destinations, never category-card prices or offers."""
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from priority_retailer_transport import MAX_HTML_BYTES, product_host
from retailer_barcode import retailer_gtin13
from tr_price_scraper import TRPriceScraper


CATEGORY_URL = 'https://www.teknosa.com/cep-telefonu-c-100001'
MAX_CATEGORY_PAGES = 40


def category_page_number(url):
    if not isinstance(url, str) or len(url) > 150:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if (parsed.scheme != 'https' or parsed.netloc != 'www.teknosa.com'
            or parsed.path != '/cep-telefonu-c-100001' or parsed.fragment):
        return None
    if not parsed.query:
        return 0
    match = re.fullmatch(r'page=([1-9]\d?)', parsed.query)
    return int(match[1]) if match else None


def parse_category_document(html, url):
    page = category_page_number(url)
    if page is None or not isinstance(html, str) or len(html.encode()) > MAX_HTML_BYTES:
        return [], None
    soup = BeautifulSoup(html, 'html.parser')
    titles = soup.select('h1.plp-title')
    if len(titles) != 1 or titles[0].get_text(' ', strip=True) != 'Cep Telefonu Fiyatları ve Modelleri':
        return [], None
    cards, seen = [], set()
    for node in soup.select('[data-product-url][data-product-name]'):
        headings = node.select('h3.prd-title')
        title = headings[0].get_text(' ', strip=True) if len(headings) == 1 else ''
        try:
            target = urljoin(CATEGORY_URL, node.get('data-product-url', ''))
        except ValueError:
            continue
        sku = node.get('data-product-id')
        gtin = retailer_gtin13(node.get('data-product-ean'))
        if (not title or len(title) > 500 or title != node.get('data-product-name')
                or node.get('data-product-page-type') != 'CATEGORY'
                or product_host(target) != 'www.teknosa.com' or urlparse(target).query
                or not isinstance(sku, str) or target.rsplit('-p-', 1)[-1] != sku
                or gtin is None or target in seen):
            continue
        seen.add(target)
        cards.append({'source_url': target, 'discovery_title': title,
                      'discovery_sku': sku, 'discovery_gtin13': gtin,
                      'stock_priority': node.get('data-product-stock') == 'Y'})
    # Follow the actual visible next-page link, never synthesize a page grid.
    next_urls = set()
    for anchor in soup.select('a[href]'):
        try:
            target = urljoin(CATEGORY_URL, anchor['href'])
        except ValueError:
            continue
        label = anchor.get_text(' ', strip=True)
        if (category_page_number(target) == page + 1
                and re.fullmatch(r'Daha Fazla Ürün Gör \(\d+ / \d+\)', label)):
            next_urls.add(target)
    return cards, next(iter(next_urls)) if len(next_urls) == 1 else None


def match_catalog_destinations(cards, products, *, maximum_per_model=2):
    """Only existing catalog identities can acquire automatically found hints."""
    from update_prices import PriceUpdater
    from reviewed_offer_targets import with_reviewed_radio
    if type(maximum_per_model) is not int or not 1 <= maximum_per_model <= 2:
        raise ValueError('Invalid category destination budget')
    matcher = TRPriceScraper()
    matched, owned = {}, {}
    specs = {p['id']: with_reviewed_radio(p, PriceUpdater.get_expected_specs(p.get('attributes')))
             for p in products}
    for card in sorted(cards, key=lambda c: (not c['stock_priority'], c['source_url'])):
        title = card['discovery_title']
        # Capacity punctuation is observed on the category card itself.
        title = re.sub(r'(?<=\d)GB[/_](?=\d)', ' GB ', title, flags=re.I)
        owners = [p for p in products if matcher.is_strict_match(p['name'], title, specs[p['id']])]
        if len(owners) != 1:
            continue
        product = owners[0]
        url = card['source_url']
        if url in owned:
            continue
        owned[url] = product['id']
        group = matched.setdefault(product['id'], [])
        if len(group) < maximum_per_model:
            group.append(card)
    return matched
