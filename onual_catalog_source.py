"""Extract scoped shop destinations from public OnuAl comparison pages."""

import re
from urllib.parse import parse_qsl, urlsplit

from bs4 import BeautifulSoup

from hepsiburada_catalog_discovery import public_sku_search_urls
from laptop_pilot import _hepsiburada_public_get
from priority_retailer_transport import (PriorityRetailerTransport, catalog_product_alias, compatible_get)


HOST = 'onual.com'
BASE = 'https://' + HOST
PRODUCT = re.compile(r'/urun/([a-z0-9-]{3,220})-p-([1-9]\d{0,11})')
CATEGORY_WORDS = {
    'phone': re.compile(r'\b(?:cep telefonu|akilli telefon|apple iphone|iphone \d|samsung galaxy [aszmf]\d{1,3}|'
                        r'redmi note|poco [xmf]\d{1,2}|xiaomi \d{1,2}|oppo reno|vivo [vxy]\d|honor magic)\b'),
    'tws': re.compile(r'\b(?:tws|tam kablosuz|airpods|freebuds|galaxy buds|aerofit|wf 1000xm\d)\b'),
    'laptop': re.compile(r'\b(?:notebook|laptop|dizustu bilgisayar|tasinabilir bilgisayar|macbook)\b'),
    'monitor': re.compile(r'\b(?:monitor|monitoru)\b'),
    'cpu': re.compile(r'\bislemci\b'),
    'motherboard': re.compile(r'\banakart\b'),
    'ram': re.compile(r'\b(?:ram|bellek)\b.*\bddr[345]\b|\bddr[345]\b.*\b(?:ram|bellek)\b'),
    'gpu': re.compile(r'\bekran karti\b'),
    'ssd': re.compile(r'\bssd\b'),
    'case': re.compile(r'\b(?:bilgisayar kasasi|pc kasasi|gaming kasa)\b'),
    'psu': re.compile(r'\b(?:guc kaynagi|power supply|psu)\b'),
    'cooler': re.compile(r'\b(?:islemci sogutucu|cpu cooler|sivi sogutma)\b'),
}
ACCESSORY = re.compile(r'\b(?:yedek|kilif|ekran koruyucu|koruyucu cam|adaptor(?:u)?|canta(?:si)?|kablolu kulaklik|'
                       r'monitor (?:kolu|askisi)|kablo(?:su)?|ssd kutusu)\b')


def category(title):
    if ACCESSORY.search(title):
        return None
    # Device names may list their integrated RAM, SSD or graphics card.
    for kind in ('phone', 'tws', 'laptop', 'monitor'):
        if CATEGORY_WORDS[kind].search(title):
            return None if kind == 'tws' and re.search(r'\bhoparlor\b', title) else kind
    if re.search(r'\bfreedos\b', title):
        return None
    kinds = [kind for kind, words in CATEGORY_WORDS.items() if kind not in ('phone', 'tws', 'laptop', 'monitor')
             and words.search(title)]
    if 'cooler' in kinds and 'cpu' in kinds:
        kinds.remove('cpu')
    return kinds[0] if len(kinds) == 1 else None


def public_links(html, url):
    if not url.startswith(BASE) or not PRODUCT.fullmatch(url.removeprefix(BASE)):
        return []
    soup = BeautifulSoup(html, 'html.parser')
    if [node.get('href') for node in soup.select('link[rel=canonical]')] != [url]:
        return []
    links = []
    for node in soup.select('a.product-detail-btn[target=_blank][rel~=noopener][href], '
                            'a.other-store-item[target=_blank][rel~=noopener][href]'):
        href = node['href']
        if len(href) > 1000:
            continue
        try:
            route = urlsplit(href)
            if route.netloc == 'zxro.com':
                if route.scheme != 'https' or route.path != '/u/' or route.fragment:
                    continue
                query = parse_qsl(route.query, keep_blank_values=True, max_num_fields=2)
                if len(query) != 2 or set(dict(query)) != {'redirect', 'url'} or dict(query)['redirect'] != '1':
                    continue
                href = dict(query)['url']
            links.append(href)
        except ValueError:
            continue
    return list(dict.fromkeys(links))


def selected_hb_search_sku(url):
    match = re.fullmatch(r'https://www\.hepsiburada\.com/ara\?q=(HBCV[A-Z0-9]{5,30})', url)
    return match[1] if match else None


def product_link_reader(maximum):
    """Resolve only documented retailer alias URLs, under per-store budgets."""
    aliases = PriorityRetailerTransport(maximum_per_host=maximum, browser_fallback=False,
                                        get=compatible_get, unavailable_robots_hosts=('www.teknosa.com',))
    hb = PriorityRetailerTransport(maximum_per_host=maximum, browser_fallback=False, get=_hepsiburada_public_get)
    resolved = {}

    def resolve(url):
        if url not in resolved:
            if catalog_product_alias(url):
                target = aliases.discover_canonical_product_url(url)
                resolved[url] = [target] if target else []
            elif sku := selected_hb_search_sku(url):
                html, _, _ = hb.read_hepsiburada_search(sku)
                resolved[url] = public_sku_search_urls(html, sku)
            else:
                resolved[url] = [url]
        return resolved[url]

    return resolve
