"""Discover phone PDP addresses from the retailer's public category pages.

Card amounts are deliberately ignored. Each destination must pass the existing
fresh selected SKU/seller/stock/cash reader before it can become an offer.
"""
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from priority_retailer_transport import MAX_HTML_BYTES, product_host


CATEGORY_URL = 'https://www.trendyol.com/cep-telefonu-x-c103498'
MAX_CATEGORY_PAGES = 100


def category_page_number(url):
    if not isinstance(url, str) or len(url) > 150:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if (parsed.scheme != 'https' or parsed.netloc != 'www.trendyol.com'
            or parsed.path != '/cep-telefonu-x-c103498' or parsed.fragment):
        return None
    if not parsed.query:
        return 1
    match = re.fullmatch(r'pi=([1-9]\d{0,2})', parsed.query)
    return int(match[1]) if match else None


def parse_category_document(html, url, diagnostic=None):
    if isinstance(diagnostic, dict):
        diagnostic['status'] = 'invalid_category_document'
    page = category_page_number(url)
    if page is None or not isinstance(html, str) or len(html.encode()) > MAX_HTML_BYTES:
        return [], None
    soup = BeautifulSoup(html, 'html.parser')
    headings = soup.select('h1.title')
    if len(headings) != 1 or headings[0].get_text(' ', strip=True) != 'Cep Telefonu':
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
        number = category_page_number(target)
        if number is not None:
            page_numbers.add(number)
            if number == page + 1:
                page_urls.add(target)
    next_url = next(iter(page_urls)) if len(page_urls) == 1 else None
    status = ('pagination_unrecognized' if not page_numbers or
              any(number > page for number in page_numbers) and next_url is None else 'parsed')
    if isinstance(diagnostic, dict):
        diagnostic.update(status=status, destinations=len(cards),
                          highest_observed_page=max(page_numbers) if page_numbers else None)
    return cards, next_url
