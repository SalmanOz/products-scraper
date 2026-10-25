"""Read known public retailer PDPs without browser/challenge bypasses.

Only one selected Product and its own exact Offer can produce an observation.
Aggregator parent names, lowPrice, related products and remembered prices are
never promoted to retailer evidence.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
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


_MERCHANT_DOMAINS = {
    'n11.com': 'n11', 'pazarama.com': 'Pazarama', 'idefix.com': 'idefix',
    'vatanbilgisayar.com': 'Vatan Bilgisayar', 'hepsiburada.com': 'Hepsiburada',
    'trendyol.com': 'Trendyol', 'amazon.com.tr': 'Amazon TR',
    'turkcell.com.tr': 'Pasaj', 'mediamarkt.com.tr': 'MediaMarkt',
    'teknosa.com': 'Teknosa', 'pttavm.com': 'PttAVM', 'apple.com': 'Apple Store',
    'samsung.com': 'Samsung', 'mi.com': 'Xiaomi',
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
        if any(re.search(r'token|auth|password|secret|signature|session|email|api.?key|access.?key|credential',
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
        first = urljoin(second, first)
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


def _product_offer(soup, final_url):
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
    if product.get('hasVariant'):
        return None
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
    if _is_conditional_price(soup, offer, price):
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
    return {'merchant': ALLOWED_HOSTS[_safe_url(final_url).hostname],
            'price': price, 'currency': 'TRY', 'availability': availability,
            # An Offer may name its canonical/seller URL while the fetched PDP
            # carries tracking or another seller query. Keep the observed
            # Offer's URL, never claim that price for a different URL selection.
            'source_title': title.strip(),
            'url': urljoin(final_url, offer['url']) if offer.get('url') else final_url,
            'observed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}


class RetailerOfferRefresher:
    def __init__(self, session=None, fetch=None):
        self._fetch = fetch or (session.get if session is not None else requests.get)
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
        urls = [t.get('source_url') if isinstance(t, dict) else t for t in targets[:12]]
        for url in dict.fromkeys(u for u in urls if _safe_url(u)):
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
        requested_variant = _variant_query(parsed)
        current = source_url
        candidate = None
        self._record_attempt(source_url, 'no_exact_product_offer')
        for redirects in range(4):
            parsed = _safe_url(current)
            if not parsed:
                break
            with self._lock:
                blocked = parsed.hostname.removeprefix('www.') in self._blocked_hosts
            if blocked:
                self._record_attempt(source_url, 'host_circuit_open')
                break
            try:
                response = self._fetch(current, timeout=(5, 15), allow_redirects=False,
                                       headers={'Accept': 'text/html', 'Accept-Language': 'tr-TR,tr;q=0.9'})
                status = response.status_code
                if status in (403, 429):
                    self._record_attempt(source_url, 'access_denied' if status == 403 else 'rate_limited', status)
                    with self._lock:
                        self._blocked_hosts.add(parsed.hostname.removeprefix('www.'))
                    break
                if status in (500, 502, 503, 504):
                    self._record_attempt(source_url, 'retailer_unavailable', status)
                    self._transport_failure(parsed.hostname)
                    break
                if status in _REDIRECTS:
                    location = response.headers.get('Location') or response.headers.get('location')
                    if redirects == 3 or not isinstance(location, str):
                        self._record_attempt(source_url, 'redirect_limit_or_missing_location', status)
                        break
                    destination = urljoin(current, location)
                    destination_url = _safe_url(destination)
                    if not destination_url or (requested_variant and
                            _variant_query(destination_url) != requested_variant):
                        self._record_attempt(source_url, 'unsafe_or_changed_variant_redirect', status)
                        break
                    current = destination
                    continue
                if status != 200:
                    self._record_attempt(source_url, 'http_error', status)
                    break
                actual_url = getattr(response, 'url', None) or current
                if not _safe_url(actual_url) or not _same_resource(actual_url, current):
                    self._record_attempt(source_url, 'unexpected_final_destination', status)
                    break
                html = response.text
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
        urls = []
        if not isinstance(targets, (list, tuple)):
            return []
        for target in (targets or [])[:12]:
            url = target.get('source_url') if isinstance(target, dict) else target
            if _safe_url(url) and url not in urls:
                urls.append(url)
        if not urls:
            return []
        with ThreadPoolExecutor(max_workers=4) as executor:
            candidates = list(executor.map(self._fetch_target, urls))
        # The cached observation is rechecked for every requested catalog model;
        # a base S26 cache hit cannot establish a Plus profile.
        return [dict(offer) for offer in candidates if offer
                and self._scraper.is_strict_match(product_name, offer['source_title'], expected_specs)
                and self._scraper.has_observed_offer_evidence(product_name, offer, expected_specs)]


def refresh_existing_offer_targets(product_name, targets, expected_specs):
    """Convenience one-shot sync API; reuse the class to retain the run cache."""
    return RetailerOfferRefresher().refresh_existing_offer_targets(product_name, targets, expected_specs)
