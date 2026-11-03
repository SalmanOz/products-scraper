"""Public category/brand first-page destinations; never category-card prices.

The visible category has no next-page link. Follow only its observed phone-brand
links and report this limited discovery scope honestly, without guessing pages.
"""
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from priority_retailer_transport import MAX_HTML_BYTES, product_host
from tr_price_scraper import TRPriceScraper

CATEGORY_URL = 'https://www.hepsiburada.com/cep-telefonlari-c-371965'
MAX_CATEGORY_PAGES = 20
DISCOVERY_SCOPE = 'observed_category_and_brand_first_pages'


def category_identity(url):
    if not isinstance(url, str) or len(url) > 200:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if (parsed.scheme != 'https' or parsed.netloc != 'www.hepsiburada.com'
            or parsed.query or parsed.fragment):
        return None
    if parsed.path == '/cep-telefonlari-c-371965':
        return ''
    found = re.fullmatch(r'/([a-z]+)-cep-telefonlari-xc-371965-b([1-9]\d{0,7})', parsed.path)
    aliases = {a for names in TRPriceScraper.PHONE_BRAND_ALIASES.values() for a in names}
    return found[1] if found and found[1] in aliases else None


def _category_soup(html, url):
    brand = category_identity(url)
    if brand is None or not isinstance(html, str) or len(html.encode()) > MAX_HTML_BYTES:
        return None
    soup = BeautifulSoup(html, 'html.parser')
    headings = soup.select('h1')
    title = TRPriceScraper._normalize_words(headings[0].get_text(' ', strip=True)) if len(headings) == 1 else ''
    expected = ('cep telefonu fiyatlari ve modelleri' if brand == '' else brand + ' telefonlar')
    return soup if title == expected else None


def parse_category_document(html, url, diagnostic=None):
    soup = _category_soup(html, url)
    if isinstance(diagnostic, dict):
        diagnostic.update(status='parsed' if soup else 'category_identity_missing', scope=DISCOVERY_SCOPE)
    if soup is None:
        return [], None
    cards, seen = [], set()
    for heading in soup.select('h2[data-test-id^="title-"]'):
        links = heading.select('a[href]')
        if len(links) != 1:
            continue
        link = links[0]
        title = link.get_text(' ', strip=True)
        target = urljoin(CATEGORY_URL, link['href'])
        # Sponsored tracking/redirect URLs, merchant selectors and unrelated
        # links are deliberately not persisted or requested.
        if (not title or len(title) > 500 or link.get('title') != title
                or product_host(target) != 'www.hepsiburada.com'
                or urlparse(target).query or target in seen):
            continue
        seen.add(target)
        cards.append({'source_url': target, 'discovery_title': title,
                      'stock_priority': False})
    return cards, None


def discovery_links(html, url, products):
    soup = _category_soup(html, url)
    if soup is None:
        return []
    words = set(TRPriceScraper._normalize_words(' '.join(p['name'] for p in products)).split())
    brands = {key for key, aliases in TRPriceScraper.PHONE_BRAND_ALIASES.items() if words & aliases}
    allowed = {alias for key in brands for alias in TRPriceScraper.PHONE_BRAND_ALIASES[key]}
    found = set()
    for link in soup.select('a[href]'):
        target = urljoin(CATEGORY_URL, link['href'])
        if category_identity(target) in allowed:
            found.add(target)
    return sorted(found)
