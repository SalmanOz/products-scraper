"""Discover phone PDP addresses from the retailer's public category pages.

Card amounts are deliberately ignored. Each destination must pass the existing
fresh selected SKU/seller/stock/cash reader before it can become an offer.
"""
import json
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from priority_retailer_transport import MAX_HTML_BYTES, product_host
from tr_price_scraper import TRPriceScraper


CATEGORY_URL = 'https://www.trendyol.com/cep-telefonu-x-c103498'
MAX_CATEGORY_PAGES = 100
MAX_MODEL_BRANCHES = 12


def category_identity(url):
    if not isinstance(url, str) or len(url) > 150:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if (parsed.scheme != 'https' or parsed.netloc != 'www.trendyol.com'
            or parsed.fragment or parsed.params):
        return None
    page = 1
    if parsed.query:
        match = re.fullmatch(r'pi=([1-9]\d{0,2})', parsed.query)
        if match is None:
            return None
        page = int(match[1])
    if parsed.path == '/cep-telefonu-x-c103498':
        return {'page': page, 'brand': None, 'path': parsed.path, 'heading': 'Cep Telefonu'}
    model = re.fullmatch(r'/([a-z0-9]+(?:-[a-z0-9]+){1,12})-cep-telefonu-x-c103498-a292-v([1-9]\d{0,8})',
                         parsed.path)
    if model:
        label = model[1].replace('-', ' ')
        return {'page': page, 'brand': None, 'path': parsed.path, 'heading': label + ' Cep Telefonu',
                'model': label, 'model_id': model[2]}
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


def _model_key(label):
    words = TRPriceScraper._identity_words(label)
    if words[-2:] == ['cep', 'telefonu']:
        words = words[:-2]
    if words[:2] in (['apple', 'iphone'], ['samsung', 'galaxy'], ['xiaomi', 'redmi'], ['xiaomi', 'poco']):
        words = words[1:]
    # The retailer's model filter can include both radios. The selected PDP
    # must still establish the actual radio and capacity.
    return tuple(word for word in words if word not in ('4g', '5g', 'lte'))


def discovery_links(html, url, products):
    """Follow observed phone model and brand anchors matching the catalog."""
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
    priorities, model_priorities = {}, {}
    for index, product in enumerate(products):
        for brand in _brand_keys(product):
            priorities.setdefault(brand, index)
        model = TRPriceScraper._catalog_model_name(product.get('name', ''), product.get('attributes', {}))
        if model:
            model_priorities.setdefault(_model_key(model), index)
    found = {}
    for anchor in soup.select('a[href]'):
        try:
            target = urljoin(url, anchor['href'])
        except ValueError:
            continue
        other = category_identity(target)
        if not other or other['page'] != 1:
            continue
        if identity and other.get('model'):
            key = _model_key(other['model'])
            if key in model_priorities and _model_key(anchor.get_text(' ', strip=True)) == key:
                found.setdefault(target, (0, model_priorities[key], target))
        elif other['brand'] in priorities:
            found.setdefault(target, (1, priorities[other['brand']], target))
    return sorted(found, key=found.__getitem__)


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


def _public_state_pagination(soup, url, identity, card_count):
    """Project the selected category's observed pagination into HTML reads.

    Some current brand pages have no pagination anchors. Their public search
    state names the next page in an API link. Never request that API or reuse
    its product amounts: bind both links to this exact category/index and
    read only the ordinary public HTML page through the existing transport.
    """
    states = []
    for script in soup.select('script:not([src])'):
        body = script.get_text()
        match = re.match(r'^\s*window\["__single-search-result__PROPS"\]\s*=\s*', body)
        if match is None:
            continue
        try:
            state, end = json.JSONDecoder().raw_decode(body[match.end():])
            if body[match.end() + end:].strip() not in ('', ';'):
                return False, None
            states.append(state)
        except (ValueError, TypeError):
            return False, None
    state = states[0] if len(states) == 1 and isinstance(states[0], dict) else {}
    data = state.get('data')
    page = identity['page']
    if (not isinstance(data, dict) or type(data.get('pageIndex')) is not int
            or data['pageIndex'] != page or type(data.get('total')) is not int
            or not card_count <= data['total'] <= 100000 or not card_count
            or not isinstance(data.get('title'), str)
            or TRPriceScraper._normalize_words(data['title']) != TRPriceScraper._normalize_words(identity['heading'])
            or not isinstance(data.get('_links'), dict)):
        return False, None
    fixed = {'promotionSearch': 'false', 'stickyShellNavigation': 'true', 'channelId': '1',
             'subPathStrategy': 'no-subpath', 'isTyPlusIncluded': 'false',
             'pageSize': '36', 'pathModel': identity['path'].lstrip('/')}
    def matches(raw, index):
        if not isinstance(raw, str) or len(raw) > 1000 or re.search(r'[\x00-\x20\\]', raw):
            return False
        try:
            parsed = urlparse(raw)
            pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=9)
            params = dict(pairs)
            return (parsed.scheme == 'https' and parsed.netloc == 'apigw.trendyol.com'
                    and parsed.path == '/discovery-sfint-search-service/api/search/products/'
                    and not parsed.fragment and not parsed.params
                    and len(pairs) == len(params) == 9
                    and params.pop('isDynamicRenderingAgent', '') in ('true', 'false')
                    and params == {**fixed, 'pi': str(index)})
        except ValueError:
            return False
    links = data['_links']
    if 'next' not in links or not matches(links.get('current'), page):
        return False, None
    if links.get('next') is None:
        return (data['total'] <= page * 36 and data['total'] > (page - 1) * 36), None
    if data['total'] <= page * 36 or not matches(links['next'], page + 1):
        return False, None
    return True, urlparse(url)._replace(query=urlencode({'pi': page + 1})).geturl()


def parse_category_document(html, url, diagnostic=None):
    if isinstance(diagnostic, dict):
        diagnostic['status'] = 'invalid_category_document'
    identity = category_identity(url)
    if identity is None or not isinstance(html, str) or len(html.encode()) > MAX_HTML_BYTES:
        return [], None
    page = identity['page']
    soup = BeautifulSoup(html, 'html.parser')
    headings = soup.select('h1.title')
    if isinstance(diagnostic, dict):
        diagnostic['observed_headings'] = [h.get_text(' ', strip=True)[:180] for h in headings[:2]]
    if (len(headings) != 1 or TRPriceScraper._normalize_words(headings[0].get_text(' ', strip=True)) !=
            TRPriceScraper._normalize_words(identity['heading'])):
        if isinstance(diagnostic, dict):
            diagnostic['status'] = 'category_identity_missing'
        return [], None
    if identity.get('model'):
        selected = soup.select('input[type="checkbox"][id^="checkbox-web-aggregations-checkbox-292-"][checked], '
                               'input[type="checkbox"][name^="web-aggregations-checkbox-292-"][checked]')
        model_id = identity['model_id']
        label = ''
        if len(selected) == 1 and selected[0].get('value') == model_id:
            control = selected[0]
            name = f'web-aggregations-checkbox-292-{model_id}'
            if control.get('id') == f'checkbox-{name}-{model_id}':
                label = control.get('aria-label', '')
            elif control.get('name') == name:
                wrapper = control.find_parent('label', attrs={'for': name})
                button = wrapper.find('button', attrs={'id': name, 'role': 'checkbox', 'aria-checked': 'true'}) if wrapper else None
                if button and button.get('value') == model_id:
                    label = button.get('aria-label', '')
        if _model_key(label) != _model_key(identity['model']):
            if isinstance(diagnostic, dict):
                diagnostic['status'] = 'category_model_filter_missing'
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
    state_valid, state_next = _public_state_pagination(soup, url, identity, len(cards))
    pagination_source = 'public_category_links'
    if state_valid:
        if next_url is not None and next_url != state_next:
            status, next_url = 'pagination_conflict', None
        elif next_url is None:
            status, next_url = 'parsed', state_next
            pagination_source = 'public_category_state_html_projection'
    if isinstance(diagnostic, dict):
        diagnostic.update(status=status, destinations=len(cards),
                          pagination_source=pagination_source,
                          category_scope=('phone_model' if identity.get('model') else
                                          'phone_brand' if identity['brand'] else 'phones'),
                          highest_observed_page=max(page_numbers) if page_numbers else None)
    return cards, next_url
