"""Read retailer inventories as discovery hints for exact live verification."""
from collections import Counter, deque
from contextlib import closing
import gzip
from io import BytesIO
import re
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET
from zlib import error as GzipError

from curl_cffi.requests import RequestsError
from requests import RequestException

from catalog_queue import SourceDeferred
from monitor_scale_discovery import match_cards
from priority_retailer_transport import BOT
from retailer_barcode import retailer_gtin13
from trendyol_basket_price import amount


MAX_XML = 32 * 1024 * 1024
MAX_SITEMAPS = 16
GOOGLE = '{http://base.google.com/ns/1.0}'
SITEMAP = '{http://www.sitemaps.org/schemas/sitemap/0.9}'
FEED_FORMATS = ('google', 'akakce')
SITEMAP_ROOTS = {
    'www.vatanbilgisayar.com': ('https://www.vatanbilgisayar.com/sitemap.axd',),
    'www.mediamarkt.com.tr': (
        'https://www.mediamarkt.com.tr/sitemaps/sitemap-productdetailspages-000.xml',
        'https://www.mediamarkt.com.tr/sitemaps/sitemap-productdetailspages-001.xml',
    ),
    'www.teknosa.com': ('https://www.teknosa.com/siteharitasi.xml',),
}


class InventoryError(ValueError):
    pass


def xml_root(raw):
    if len(raw) > MAX_XML:
        raise InventoryError('inventory_too_large')
    try:
        if raw.startswith(b'\x1f\x8b'):
            with gzip.GzipFile(fileobj=BytesIO(raw)) as source:
                raw = source.read(MAX_XML + 1)
            if len(raw) > MAX_XML:
                raise InventoryError('inventory_too_large')
        # Parse decoded UTF-8 so an alternate encoding cannot hide a DTD.
        document = raw.decode('utf-8-sig')
        if '\x00' in document or re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', document, re.I):
            raise InventoryError('inventory_unsafe_xml')
        return ET.fromstring(document)
    except (UnicodeError, ET.ParseError, OSError, EOFError, GzipError) as error:
        raise InventoryError('inventory_invalid_xml') from error


def store_url(url, host):
    if not isinstance(url, str) or len(url) > 1000 or re.search(r'[\x00-\x20\\]', url):
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme == 'https' and parsed.netloc == host and not parsed.fragment and bool(parsed.path)


class StoreInventory:
    def __init__(self, host, get):
        self.host, self.get = host, get
        self.requests = 0

    def _read(self, url, accept, limit):
        self.requests += 1
        with closing(self.get(url, timeout=(5, 20), stream=True, headers={
            'User-Agent': BOT + '/1.0 (+https://teknoskor.com)', 'Accept': accept,
        })) as response:
            if response.status_code != 200:
                raise InventoryError(f'inventory_http_{response.status_code}')
            if response.url != url:
                raise InventoryError('inventory_unexpected_final_url')
            length = str(response.headers.get('Content-Length') or '')
            if length.isascii() and length.isdecimal() and int(length) > limit:
                raise InventoryError('inventory_too_large')
            data = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if len(data) + len(chunk) > limit:
                    raise InventoryError('inventory_too_large')
                data.extend(chunk)
            return data

    def xml(self, url):
        if not store_url(url, self.host):
            raise InventoryError('inventory_url_disallowed')
        return xml_root(self._read(url, 'application/xml,text/xml,application/gzip', MAX_XML))


def feed_commerce(item, feed_format):
    def read(tag):
        fields = item.findall(tag)
        return (fields[0].text or '').strip() if len(fields) == 1 and not len(fields[0]) else ''

    if feed_format == 'google':
        retail = re.fullmatch(r'([0-9]{1,7}(?:\.[0-9]{1,2})?) TRY', read(GOOGLE + 'price'))
        sale = re.fullmatch(r'([0-9]{1,7}(?:\.[0-9]{1,2})?) TRY', read(GOOGLE + 'sale_price'))
        price, sale_price = amount(retail[1]) if retail else None, amount(sale[1]) if sale else None
        # A future or expiring sale requires an independent date policy. Keep
        # such rows eligible for discovery and mark the price as unauditable.
        if item.find(GOOGLE + 'sale_price_effective_date') is not None or (
                item.find(GOOGLE + 'sale_price') is not None and not sale_price):
            price = None
        elif sale_price:
            price = sale_price if price and sale_price < price else None
        availability = read(GOOGLE + 'availability').replace(' ', '_')
    else:
        retail = re.fullmatch(r'([0-9]{1,7}(?:\.[0-9]{1,2})?)', read('price'))
        price = amount(retail[1]) if retail else None
        quantity = read('quantity')
        availability = ('in_stock' if int(quantity) else 'out_of_stock') if re.fullmatch(r'[0-9]{1,9}', quantity) else ''
    return (price, availability if availability in ('in_stock', 'out_of_stock') else None)


def feed_routes(root, feed_format, host, identities, normalize, *, commerce=None):
    """Join on a catalog GTIN and discard price, stock and seller fields."""
    if feed_format == 'google' and root.tag == 'rss' and len(root.findall('channel')) == 1:
        items, barcode, link = root.iterfind('channel/item'), GOOGLE + 'gtin', 'link'
    elif feed_format == 'akakce' and root.tag == 'products':
        items, barcode, link = root.iterfind('product'), 'barcode', 'url'
    else:
        raise InventoryError('inventory_format_mismatch')
    counts, claims, routes, ambiguous = Counter(), {}, {}, set()
    for item in items:
        counts['items'] += 1
        links = item.findall(link)
        url = (links[0].text or '').strip() if len(links) == 1 and not len(links[0]) else ''
        if not store_url(url, host):
            counts['invalid_store_url'] += 1
            continue
        barcodes = item.findall(barcode)
        gtin = retailer_gtin13((barcodes[0].text or '').strip()) if len(barcodes) == 1 and not len(barcodes[0]) else None
        if url in claims and claims[url] != gtin:
            ambiguous.add(url)
        claims[url] = gtin
        if gtin is None:
            counts['invalid_barcode'] += 1
        elif gtin not in identities:
            counts['outside_catalog'] += 1
        elif url not in routes:
            try:
                routes[url] = normalize({**identities[gtin], 'url': url})
            except ValueError:
                counts['unsupported_product_url'] += 1
        if commerce is not None and url in routes and gtin in identities:
            values = feed_commerce(item, feed_format)
            if url in commerce and commerce[url] != (routes[url][2], values):
                commerce[url] = None
            else:
                commerce[url] = (routes[url][2], values)
    if commerce is not None:
        for url in ambiguous:
            commerce.pop(url, None)
    counts['ambiguous_product_urls'] = len(ambiguous)
    result = [route for url, route in routes.items() if url not in ambiguous]
    counts['candidate_gtins'] = len({route[2]['gtin'] for route in result})
    return result, dict(counts)


def compare_feed_offer(claim, offer):
    price, availability = claim
    if price is None or availability is None:
        return 'feed_commerce_unproven'
    if offer.get('price_kind') == 'basket':
        return 'live_unit_price_unproven'
    if offer['availability'] != availability:
        return 'feed_stock_differs'
    if amount(str(offer['price'])) != price:
        return 'feed_price_differs'
    return 'matched_live_price_and_stock'


def sitemap_hints(inventory, models, max_documents, *, roots=()):
    """Inspect known indexes first, then their product sitemap pages."""
    roots = tuple(dict.fromkeys(roots)) or SITEMAP_ROOTS.get(inventory.host, ())
    if not roots:
        raise InventoryError('sitemap_url_required')
    pending = deque(roots)
    seen, hints, counts = set(), {}, Counter()
    while pending and counts['documents'] < max_documents:
        url = pending.popleft()
        if url in seen or not store_url(url, inventory.host):
            continue
        seen.add(url)
        path = urlsplit(url).path
        if (inventory.host == 'www.gaming.gen.tr' and path.startswith('/wp-sitemap-posts-')
                and not path.startswith('/wp-sitemap-posts-product-')):
            continue
        if re.search(r'(?:image|categor|brand|campaign|content|marketpage|service|special|blog|author|taxonomies)',
                     path, re.I):
            continue
        counts['documents'] += 1
        try:
            root = inventory.xml(url)
        except SourceDeferred as error:
            counts[error.reason] += 1
            break
        except (RequestsError, RequestException):
            counts['transport_error'] += 1
            break
        except InventoryError as error:
            counts[str(error)] += 1
            if str(error) in {'inventory_http_403', 'inventory_http_429', 'inventory_http_500',
                              'inventory_http_502', 'inventory_http_503', 'inventory_http_504'}:
                break
            continue
        if root.tag in {SITEMAP + 'sitemapindex', 'sitemapindex'}:
            ns = SITEMAP if root.tag.startswith('{') else ''
            pending.extend(loc.text.strip() for loc in root.iterfind(f'{ns}sitemap/{ns}loc') if loc.text)
        elif root.tag in {SITEMAP + 'urlset', 'urlset'}:
            ns = SITEMAP if root.tag.startswith('{') else ''
            links = (loc.text.strip() for loc in root.iterfind(f'{ns}url/{ns}loc') if loc.text)
            cards = ((link, '') for link in links if store_url(link, inventory.host))
            for row in match_cards(models, cards):
                hints[row['url']] = row
        else:
            counts['inventory_format_mismatch'] += 1
    counts['candidate_gtins'] = len({row['gtin'] for row in hints.values()})
    return list(hints.values()), dict(counts)
