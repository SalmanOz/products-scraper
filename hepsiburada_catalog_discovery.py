"""Public category/brand/model destinations; never category-card prices.

Follow only observed catalog phone links and the document's public rel=next links.
Report the bounded discovery scope honestly, without guessing pages.
"""
import json
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from priority_retailer_transport import MAX_HTML_BYTES, product_host
from tr_price_scraper import TRPriceScraper

CATEGORY_URL = 'https://www.hepsiburada.com/cep-telefonlari-c-371965'
MAX_CATEGORY_PAGES = 80
DISCOVERY_SCOPE = 'bounded_observed_category_brand_and_model_pages'


def _model_taxonomy(path):
    """Recognize public phone taxonomy paths, without inventing taxonomy IDs."""
    found = re.fullmatch(
        r'/([a-z0-9]+(?:-[a-z0-9]+)*)-(android|iphone-ios)-telefonlar-xc-'
        r'(60005201|60005202)(?:-b[1-9]\d{0,7})?-t[1-9]\d{0,7}', path)
    if not found:
        return None
    model, family, category = found.groups()
    aliases = {a for names in TRPriceScraper.PHONE_BRAND_ALIASES.values() for a in names}
    if ((family == 'android' and category == '60005201' and model.split('-')[0] in aliases)
            or (family == 'iphone-ios' and category == '60005202' and model.startswith('iphone-'))):
        return model, family
    return None


def card_destination(raw_url):
    """Extract only a public canonical PDP, never request or retain ad tracking."""
    if not isinstance(raw_url, str) or len(raw_url) > 16000:
        return None
    try:
        target = urljoin(CATEGORY_URL, raw_url)
        parsed = urlparse(target)
        if (parsed.scheme == 'https' and parsed.netloc == 'adservice.hepsiburada.com'
                and parsed.path == '/event/api/v1/track' and not parsed.fragment):
            redirects = parse_qs(parsed.query, max_num_fields=32).get('redirect', [])
            if len(redirects) != 1:
                return None
            parsed = urlparse(redirects[0])
            target = parsed._replace(query='').geturl()
        if product_host(target) != 'www.hepsiburada.com' or urlparse(target).query:
            return None
        return target
    except ValueError:
        return None


def category_identity(url):
    if not isinstance(url, str) or len(url) > 200:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if (parsed.scheme != 'https' or parsed.netloc != 'www.hepsiburada.com'
            or parsed.fragment
            or (parsed.query and not re.fullmatch(r'sayfa=[1-9]\d{0,2}', parsed.query))):
        return None
    if parsed.path == '/cep-telefonlari-c-371965':
        return ''
    if parsed.path == '/iphone-ios-telefonlar-c-60005202':
        return 'apple'
    if parsed.path == '/android-telefonlar-c-60005201':
        return 'android'
    model = _model_taxonomy(parsed.path)
    if model:
        return model[0]
    family_brand = re.fullmatch(r'/([a-z]+)-(android|iphone-ios)-telefonlar-xc-'
                               r'(60005201|60005202)-b[1-9]\d{0,7}', parsed.path)
    if family_brand:
        brand, family, category = family_brand.groups()
        aliases = {a for names in TRPriceScraper.PHONE_BRAND_ALIASES.values() for a in names}
        return brand if brand in aliases and (
            family == 'android' and category == '60005201' and brand not in {'apple', 'iphone'} or
            family == 'iphone-ios' and category == '60005202' and brand in {'apple', 'iphone'}) else None
    found = re.fullmatch(r'/([a-z]+)-cep-telefonlari-xc-371965-b([1-9]\d{0,7})', parsed.path)
    aliases = {a for names in TRPriceScraper.PHONE_BRAND_ALIASES.values() for a in names}
    return found[1] if found and found[1] in aliases else None


def category_scope(url):
    """Equivalent observed phone category routes, never product identities."""
    identity = category_identity(url)
    if identity is None:
        return None
    path = urlparse(url).path
    model = _model_taxonomy(path)
    if model:
        return ('model', *model)
    if path == '/cep-telefonlari-c-371965':
        return ('all_phones',)
    if path == '/android-telefonlar-c-60005201':
        return ('android_phones',)
    return ('brand', identity)


def category_redirect_target(original, observed):
    """Only an observed equivalent taxonomy, keeping the immediate page.

    This is a new discovery hint, not permission to reuse the changed body.
    PDPs, sellers, queries and different generations cannot pass. An explicit
    radio suffix may disappear into a broader same-model category; that never
    supplies radio evidence for an individual product/offer.
    """
    scope = category_scope(original)
    destination = category_scope(observed)
    same_scope = scope is not None and destination == scope
    if (not same_scope and scope and destination
            and scope[0] == destination[0] == 'model' and scope[2] == destination[2]):
        # The observed Note 12 Pro route omits its old terminal 5g in the
        # canonical category. A 4g -> 5g category change is still a conflict.
        base = re.sub(r'-(?:4g|5g|lte)$', '', scope[1])
        same_scope = base != scope[1] and base == destination[1]
    if (not same_scope or original == observed
            or (urlparse(original).query or 'sayfa=1') !=
               (urlparse(observed).query or 'sayfa=1')):
        return None
    return observed


def _category_soup(html, url):
    brand = category_identity(url)
    if brand is None or not isinstance(html, str) or len(html.encode()) > MAX_HTML_BYTES:
        return None
    soup = BeautifulSoup(html, 'html.parser')
    headings = soup.select('h1')
    def heading_words(value):
        normalized = TRPriceScraper._normalize_words(value)
        # Observed category H1s separate Fold/Flip from the generation while
        # the taxonomy joins them. This changes routing only, never PDP names.
        return re.sub(r'\b(fold|flip)\s+(\d{1,2})\b', r'\1\2', normalized)
    title = heading_words(headings[0].get_text(' ', strip=True)) if len(headings) == 1 else ''
    if not title or len(title) > 250:
        return None
    path = urlparse(url).path
    # SEO headings rearrange these category words and a year. They cannot
    # supply missing model qualifiers or become positive PDP/SKU evidence.
    category_words = {'cep', 'telefon', 'telefonu', 'telefonlar', 'telefonlari',
                      'android', 'iphone', 'ios', 'modelleri', 'fiyatlari', 've',
                      'ozellikleri', 'indirimleri', 'renkleri'}
    def category_tail(words):
        return bool(words) and len(words) <= 24 and all(
            word in category_words or re.fullmatch(r'20\d{2}', word) for word in words)
    model = _model_taxonomy(path)
    if model:
        name = heading_words(model[0].replace('-', ' '))
        labels = {name}
        parts = name.split()
        brand_words = {a for aliases in TRPriceScraper.PHONE_BRAND_ALIASES.values() for a in aliases}
        if len(parts) > 2 and parts[0] == parts[1] and parts[0] in brand_words:
            labels.add(' '.join(parts[1:]))
        if name.startswith('xiaomi redmi ') or name.startswith('xiaomi poco '):
            labels.add(name.removeprefix('xiaomi '))
        if name.startswith('samsung galaxy '):
            labels.add(name.replace('samsung galaxy ', 'samsung ', 1))
        labels |= {re.sub(r' (?:4g|5g|lte)$', '', label) for label in labels}
        candidate = re.sub(r'^20\d{2} ', '', title)
        return soup if any(candidate == label or candidate.startswith(label + ' ') and
            category_tail(candidate[len(label) + 1:].split()) for label in labels) else None
    words = set(title.split())
    phone_markers = {'telefon', 'telefonu', 'telefonlar', 'telefonlari', 'iphone'}
    if not words & phone_markers:
        return None
    if brand == '':
        return soup if (title == 'cep telefonu fiyatlari ve modelleri' or
                        urlparse(url).query and title == 'cep telefonu') else None
    aliases = next((set(aliases) for key, aliases in TRPriceScraper.PHONE_BRAND_ALIASES.items()
                    if brand == key or brand in aliases), {brand})
    if not words & aliases:
        return None
    allowed = aliases | ({'xiaomi'} if brand in {'redmi', 'poco'} else set())
    remainder = [word for word in title.split() if word not in allowed]
    return soup if category_tail(remainder) else None


def parse_category_document(html, url, diagnostic=None):
    soup = _category_soup(html, url)
    if isinstance(diagnostic, dict):
        diagnostic.update(status='parsed' if soup else 'category_identity_missing', scope=DISCOVERY_SCOPE)
        if soup is None and isinstance(html, str) and len(html.encode()) <= MAX_HTML_BYTES:
            diagnostic['observed_headings'] = [node.get_text(' ', strip=True)[:300]
                for node in BeautifulSoup(html, 'html.parser').select('h1')[:2]]
    if soup is None:
        return [], None
    cards, seen = [], set()
    for heading in soup.select('h2[data-test-id^="title-"]'):
        links = heading.select('a[href]')
        if len(links) != 1:
            continue
        link = links[0]
        title = link.get_text(' ', strip=True)
        target = card_destination(link['href'])
        # Only the actual linked canonical PDP can enter discovery. Tracker
        # events and identifiers are neither requested nor persisted.
        if (not title or len(title) > 500 or link.get('title') != title
                or target is None or target in seen):
            continue
        seen.add(target)
        cards.append({'source_url': target, 'discovery_title': title,
                      'stock_priority': False})
    next_urls = set()
    current = urlparse(url)
    page = int(current.query.split('=')[1]) if current.query else 1
    continuation = soup.select('link[rel~="next"][href], a[rel~="next"][href]')
    for link in continuation:
        target = urljoin(CATEGORY_URL, link['href'])
        parsed = urlparse(target)
        if (category_scope(target) is not None and category_scope(target) == category_scope(url)
                and parsed.query == f'sayfa={page + 1}'):
            next_urls.add(target)
    if continuation and len(next_urls) != 1 and isinstance(diagnostic, dict):
        diagnostic['status'] = 'continuation_unrecognized'
    return cards, next(iter(next_urls)) if len(next_urls) == 1 else None


def discovery_links(html, url, products):
    soup = _category_soup(html, url)
    if soup is None:
        return []
    words = set(TRPriceScraper._normalize_words(' '.join(p['name'] for p in products)).split())
    brands = {key for key, aliases in TRPriceScraper.PHONE_BRAND_ALIASES.items() if words & aliases}
    allowed = {alias for key in brands for alias in TRPriceScraper.PHONE_BRAND_ALIASES[key]}
    found, model_links = set(), {}
    matcher = TRPriceScraper()
    for link in soup.select('a[href]'):
        target = urljoin(CATEGORY_URL, link['href'])
        identity = category_identity(target)
        if identity is None or urlparse(target).query:
            continue
        model = _model_taxonomy(urlparse(target).path)
        if model:
            # Taxonomy text is only a discovery hint. The existing strict PDP
            # parser must prove memory, radio, selected seller and price again.
            name = model[0].replace('-', ' ')
            for rank, product in enumerate(products):
                if matcher.is_strict_match(product['name'], name, {}):
                    model_links[target] = rank
                    break
        elif identity in allowed or identity == 'android' and brands - {'apple'}:
            found.add(target)
    return sorted(model_links, key=lambda u: (model_links[u], u)) + sorted(found)


def discover_product_siblings(html, url, product):
    """Read explicit in-stock color links, never sibling prices or offer proof.

    Even a currently unavailable selected color can expose other public SKU
    destinations. Each destination must undergo a separate fresh full PDP read.
    No URL is synthesized from an identifier and no other capacity is adopted.
    """
    from hepsiburada_catalog_source import _identity
    from reviewed_offer_targets import with_reviewed_radio
    from update_prices import PriceUpdater
    if (product_host(url) != 'www.hepsiburada.com' or not isinstance(html, str)
            or len(html.encode()) > MAX_HTML_BYTES):
        return []
    soup = BeautifulSoup(html, 'html.parser')
    scripts = soup.select('script#reduxStore')
    if len(scripts) != 1:
        return []
    try:
        state = json.loads(scripts[0].get_text())['productState']['product']
        if not isinstance(state, dict) or not _identity(url, state.get('productId'), state.get('sku')):
            return []
        variants = state.get('variants')
        if not isinstance(variants, list) or len(variants) > 128:
            return []
        specs = with_reviewed_radio(product, PriceUpdater.get_expected_specs(product.get('attributes')))
        storage = TRPriceScraper._spec_int(specs.get('storage_gb'))
        if not storage:
            return []
    except (KeyError, ValueError, TypeError):
        return []
    matcher, found = TRPriceScraper(), {}
    for variant in variants:
        if (not isinstance(variant, dict) or variant.get('isDefault') is not False
                or variant.get('isInStock') is not True or variant.get('isPreOrder') is not False):
            continue
        target, title, sku = variant.get('url'), variant.get('name'), variant.get('sku')
        if (card_destination(target) != target or not isinstance(sku, str)
                or not isinstance(target, str) or not urlparse(target).path.endswith('-p-' + sku)
                or sku == state.get('sku') or not isinstance(title, str) or not 1 <= len(title) <= 500
                or re.search(r'yenilenmi[sş]|refurbished|ikinci\s*el', title, re.I)
                or not matcher.is_strict_match(product['name'], title, specs)):
            continue
        properties = variant.get('properties')
        capacities = [p['valueObject'].get('actualValue') for p in properties
                      if isinstance(p, dict) and p.get('name') == 'Kapasite'
                      and isinstance(p.get('valueObject'), dict)] if isinstance(properties, list) else []
        if capacities != [f'{storage} GB']:
            continue
        found[target] = {'source_url': target, 'discovery_title': title,
                         'discovery_sku': sku, 'stock_priority': True,
                         'discovery_scope': 'public_product_variant_link'}
    return list(found.values())


def discover_product_sellers(html, url):
    """Discover the public 'Ürüne git' seller selection, never its row price.

    The observed UI selects ?magaza=<visible merchant name> on the same SKU.
    Require its Other Sellers event, rendered shop link and selected product's
    listing inventory to agree before issuing a separate full fresh PDP read.
    """
    if (product_host(url) != 'www.hepsiburada.com' or urlparse(url).query
            or not isinstance(html, str) or len(html.encode()) > MAX_HTML_BYTES):
        return []
    soup = BeautifulSoup(html, 'html.parser')
    scripts = soup.select('script#reduxStore')
    if len(scripts) != 1:
        return []
    uuid = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
    try:
        selected = json.loads(scripts[0].get_text())['productState']['product']
        sku, product_id = selected['sku'], selected['productId']
        current, listing_id, listings = selected['merchant'], selected['listingId'], selected['listings']
        if (not isinstance(sku, str) or not urlparse(url).path.endswith('-p-' + sku)
                or not isinstance(product_id, str) or not isinstance(current, dict)
                or not all(isinstance(v, str) and uuid.fullmatch(v) for v in (current.get('id'), listing_id))
                or not isinstance(listings, list) or len(listings) > 50):
            return []
    except (TypeError, ValueError, KeyError):
        return []
    panels = []
    for node in soup.select('[data-hbus]'):
        try:
            data = json.loads(node['data-hbus']).get('data')
        except (ValueError, TypeError, AttributeError):
            continue
        if isinstance(data, dict) and data.get('component') == 'Other Sellers':
            panels.append((node, data))
    if len(panels) != 1:
        return []
    panel, data = panels[0]
    others = data.get('other_seller')
    if (data.get('placement') != 'Other Sellers' or data.get('page_type') != 'pdp'
            or data.get('page_value') != sku or data.get('product_id') != product_id
            or data.get('listing_id') != listing_id or data.get('merchant_id') != current['id']
            or data.get('merchant_name') != current.get('name')
            or not isinstance(others, list) or len(others) > 20):
        return []
    found = {}
    for other in others:
        if not isinstance(other, dict):
            continue
        name, merchant_id = other.get('merchant_name'), other.get('merchant_id')
        if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 100
                or re.search(r'[\x00-\x1f]', name) or not isinstance(merchant_id, str)
                or not uuid.fullmatch(merchant_id) or merchant_id == current['id']):
            continue
        matches = [row for row in listings if isinstance(row, dict)
                   and row.get('merchantId') == merchant_id and row.get('merchantName') == name]
        if (len(matches) != 1 or matches[0].get('isSalable') is not True
                or matches[0].get('isBundle') is not False
                or not isinstance(matches[0].get('listingId'), str)
                or not uuid.fullmatch(matches[0]['listingId'])):
            continue
        links = [a for a in panel.select('a[href]') if a.get_text(' ', strip=True) == name]
        if len(links) != 1:
            continue
        shop = urlparse(urljoin(CATEGORY_URL, links[0]['href']))
        if (shop.scheme != 'https' or shop.netloc != 'www.hepsiburada.com' or shop.query or shop.fragment
                or not re.fullmatch(r'/magaza/[a-z0-9-]{1,100}', shop.path)):
            continue
        destination = urlparse(url)._replace(query=urlencode({'magaza': name})).geturl()
        if product_host(destination) != 'www.hepsiburada.com':
            continue
        found[destination] = {'source_url': destination, 'discovery_seller': name,
            'discovery_merchant_id': merchant_id, 'discovery_listing_id': matches[0]['listingId'],
            'source_document_url': url, 'discovery_scope': 'public_other_seller_selector'}
    return list(found.values())
