"""Read reviewed Turkcell Pasaj cash PDPs from their public HTML.

The contracted Pasaj Limit price, bundle cards, marketplace sellers, and an
AggregateOffer's lowPrice are not cash observations. A selected cash radio,
its seller and visible price must agree with one nested JSON-LD Offer.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re
import time
from urllib.parse import urlparse
from threading import RLock
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
import requests


IPHONE17_256_URL = ('https://www.turkcell.com.tr/pasaj/cep-telefonu/'
                    'ios-telefonlar/apple-iphone-17/iphone-17-256-gb')
IPHONE16_128_URL = ('https://www.turkcell.com.tr/pasaj/cep-telefonu/'
                    'ios-telefonlar/apple-iphone-16/iphone-16-128-gb')
A26_6_128_URL = ('https://www.turkcell.com.tr/pasaj/cep-telefonu/'
                 'android-telefonlar/samsung-galaxy-a26-5g-6-gb-ram-128-gb-hafiza-akilli-telefon')
FOLD8_ULTRA_12_256_URL = ('https://www.turkcell.com.tr/pasaj/cep-telefonu/'
                          'android-telefonlar/samsung-galaxy-z-fold8-ultra-12gb-256gb')
REVIEWED = {
    IPHONE17_256_URL: {'title': 'iPhone 17 256 GB', 'brand': 'Apple',
                       'ram': '8 GB', 'storage': '256 GB', 'sku': '1175749'},
    IPHONE16_128_URL: {'title': 'iPhone 16 128 GB', 'brand': 'Apple',
                       'ram': '8 GB', 'storage': '128 GB'},
    A26_6_128_URL: {'title': 'Samsung Galaxy A26 5G 6 GB Ram 128 GB',
                    'brand': 'Samsung', 'ram': '6 GB', 'storage': '128 GB',
                    'network': '5G', 'selected_color': True},
    FOLD8_ULTRA_12_256_URL: {'title': 'Samsung Galaxy Z Fold8 Ultra 12GB 256GB',
                             'brand': 'Samsung', 'ram': '12 GB', 'storage': '256 GB',
                             'network': '5G', 'selected_color': True},
}
ROBOTS_URL = 'https://www.turkcell.com.tr/robots.txt'
BOT = 'TeknoskorPriceBot'
MAX_ROBOTS_BYTES = 64 * 1024
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_PDP_REQUESTS = 6
SELLER = 'Turkcell Satış A.Ş.'
_CATALOG_PATH = re.compile(r'/pasaj/cep-telefonu/(?:android-telefonlar|ios-telefonlar|'
                           r'yapay-zeka-telefonlar)/(?:[a-z0-9-]+/){0,2}[a-z0-9-]+')
_USED = re.compile(r'outlet|yenilenmi[sş]|refurbish|ikinci[- ]el|te[sş]hir|kullan[iı]lm[iı][sş]', re.I)


def catalog_product_url(url):
    if not isinstance(url, str) or len(url) > 500 or re.search(r'[\x00-\x20\\%]', url):
        return False
    try:
        parsed = urlparse(url)
        return bool(parsed.scheme == 'https' and parsed.netloc == 'www.turkcell.com.tr'
                    and not parsed.query and not parsed.fragment and _CATALOG_PATH.fullmatch(parsed.path)
                    and not _USED.search(parsed.path))
    except ValueError:
        return False


def product_url(url):
    """Only accept exact PDPs whose variants and RAM evidence were reviewed."""
    return isinstance(url, str) and url in REVIEWED


def _money(value):
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() and Decimal('0') < amount <= Decimal('5000000') else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _cash_text(value):
    match = re.fullmatch(r'\s*(\d{1,3}(?:\.\d{3})*|\d+)(?:,(\d{2}))?\s*', value)
    if not match:
        return None
    return _money(match[1].replace('.', '') + '.' + (match[2] or '00'))


def _feature(soup, name):
    rows = []
    for row in soup.select('.m-product-detail-features__wrap'):
        title = row.select_one('.m-product-detail-features__title')
        value = row.select_one('.m-product-detail-features__text')
        if title and value and title.get_text(' ', strip=True).casefold() == name.casefold():
            rows.append(value.get_text(' ', strip=True))
    return rows[0] if len(rows) == 1 else None


def _iphone16_ram_in_exact_faq(soup):
    """Its RAM is stated in the SKU-named FAQ, not the short feature table."""
    questions = []
    for node in soup.select('#service-faq .m-accordion__container'):
        heading = node.select_one('.m-accordion__head [itemprop="name"]')
        if heading and heading.get_text(' ', strip=True) == 'iPhone 16 128 GB Özellikleri Nelerdir?':
            questions.append(node)
    if len(questions) != 1:
        return False
    answer = questions[0].select_one('.m-accordion__body[itemprop="acceptedAnswer"]')
    if not answer:
        return False
    memory = [li.get_text(' ', strip=True) for li in answer.select('li')
              if re.search(r'\b(?:RAM|bellek)\b', li.get_text(' ', strip=True), re.I)]
    return memory == ['8 GB LPDDR5 RAM ile donatılmıştır.']


def _selected_offer(html, url, catalog_profile=None):
    generic = not product_url(url)
    if generic and (not isinstance(catalog_profile, dict) or not catalog_product_url(url)):
        return None
    soup = BeautifulSoup(html, 'html.parser')
    if re.search(r'captcha|access denied|just a moment',
                 ' '.join(n.get_text(' ', strip=True) for n in soup.select('title,h1')), re.I):
        return None
    canonical = soup.select('link[rel="canonical"]')
    headings = [n.get_text(' ', strip=True) for n in soup.select('h1') if n.get_text(' ', strip=True)]
    if generic:
        from tr_price_scraper import TRPriceScraper
        matcher = TRPriceScraper()
        if (len(headings) != 1 or _USED.search(headings[0]) or
                not matcher.has_observed_offer_evidence(catalog_profile['name'],
                    {'source_title': headings[0], 'url': url}, catalog_profile['specs'])):
            return None
        reviewed = {'title': headings[0], 'brand': catalog_profile['brand'],
                    'ram': f"{catalog_profile['specs']['ram_gb']} GB",
                    'storage': f"{catalog_profile['specs']['storage_gb']} GB",
                    'selected_color': True}
        ram_values = [_feature(soup, label) for label in ('Ram (bellek) Kapasitesi', 'Bellek Ram')]
        ram_values = [value for value in ram_values if value is not None]
        ram_feature = ram_values[0] if len(set(ram_values)) == 1 else None
        radio_feature = _feature(soup, 'Hücresel Plan')
        observed_radio = matcher._radio_generation(radio_feature or '')
        expected_radio = catalog_profile['specs'].get('network_generation')
        if observed_radio and expected_radio and observed_radio != expected_radio:
            return None
    else:
        reviewed = REVIEWED[url]
        ram_feature = _feature(soup, 'Ram (bellek) Kapasitesi')
    if (len(canonical) != 1 or canonical[0].get('href') != url or
            headings != [reviewed['title']] or
            _feature(soup, 'Dahili Hafıza') != reviewed['storage'] or
            (url == IPHONE16_128_URL and (
                not _iphone16_ram_in_exact_faq(soup) or
                _feature(soup, 'Ram (bellek) Kapasitesi') not in (None, reviewed['ram']))) or
            (url != IPHONE16_128_URL and
             ram_feature != reviewed['ram']) or
            ('network' in reviewed and
             _feature(soup, 'Hücresel Plan') != reviewed['network'])):
        return None
    products = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and data.get('@type') == 'Product':
            products.append(data)
    if len(products) != 1:
        return None
    product = products[0]
    brand_matches = (product.get('brand') == {'@type': 'Brand', 'name': reviewed['brand']})
    if generic:
        brand = product.get('brand')
        brand_matches = (isinstance(brand, dict) and brand.get('@type') == 'Brand'
                         and str(brand.get('name', '')).casefold() == reviewed['brand'].casefold())
    if (product.get('name') != headings[0] or product.get('url') != url or
            not re.fullmatch(r'[1-9]\d{3,10}', str(product.get('sku', ''))) or
            ('sku' in reviewed and str(product.get('sku')) != reviewed['sku']) or
            not brand_matches or (generic and product.get('itemCondition') not in
                (None, 'https://schema.org/NewCondition', 'http://schema.org/NewCondition'))):
        return None
    aggregate = product.get('offers')
    if not isinstance(aggregate, dict) or aggregate.get('@type') != 'AggregateOffer':
        return None
    offers = aggregate.get('offers')
    if (not isinstance(offers, list) or len(offers) != 1 or
            aggregate.get('offerCount') != 1 or
            aggregate.get('priceCurrency') != 'TRY' or
            aggregate.get('availability') != 'https://schema.org/InStock'):
        return None
    offer = offers[0]
    if (not isinstance(offer, dict) or offer.get('@type') != 'Offer' or
            offer.get('priceCurrency') != 'TRY' or
            (generic and offer.get('itemCondition') not in
                (None, 'https://schema.org/NewCondition', 'http://schema.org/NewCondition')) or
            offer.get('offeredBy') != {'@type': 'Organization', 'name': SELLER}):
        return None
    price = _money(offer.get('price'))
    if (price is None or price != _money(aggregate.get('lowPrice')) or
            ((not generic or aggregate.get('highPrice') is not None)
             and price != _money(aggregate.get('highPrice')))):
        return None
    # Scope all visible evidence to the selected cash option in the same
    # purchase form as the enabled single-product basket button.
    cash = soup.select('form.m-form .price-radio-cash')
    if len(cash) != 1:
        return None
    cash = cash[0]
    form = cash.find_parent('form')
    selected = cash.select('input.product-basket-type[type="radio"][data-type="cash"][checked]')
    seller = cash.select('.m-card-offer__seller-container .a-price-radio-b__title')
    amounts = cash.select('.a-price-radio-b__price-box .a-price-val')
    currencies = cash.select('.a-price-radio-b__price-box .a-price__currency')
    buttons = form.select('button.device-available.add-to-basket-non-login') if form else []
    if (len(selected) != 1 or selected[0].get('name') != 'price' or
            len(seller) != 1 or seller[0].get_text(' ', strip=True) != SELLER or
            len(amounts) != 1 or len(currencies) != 1 or
            currencies[0].get_text(' ', strip=True) != 'TL' or
            _cash_text(amounts[0].get_text(' ', strip=True)) != price or
            len(buttons) != 1 or buttons[0].has_attr('disabled') or
            buttons[0].get('aria-disabled') == 'true' or
            buttons[0].get('style', '').replace(' ', '').casefold().find('display:none') >= 0 or
            buttons[0].get_text(' ', strip=True) != 'Sepete Ekle' or
            buttons[0].get('id') != 'p-' + str(product['sku'])):
        return None
    if reviewed.get('selected_color'):
        colors = form.select('select.m-select-color option[selected]') if form else []
        if (len(colors) != 1 or colors[0].get('data-stock-available') != 'true' or
                colors[0].get('data-at-basket') != 'false' or
                colors[0].get('data-seller-name') != SELLER or
                _cash_text(colors[0].get('data-price', '')) != price or
                not re.fullmatch(re.escape(str(product['sku'])) + r'-\d+-\d+',
                                 colors[0].get('data-device-product-uid', ''))):
            return None
    return {'merchant': 'Pasaj', 'price': float(price), 'currency': 'TRY',
            'availability': 'in_stock', 'source_title': headings[0], 'url': url,
            'observed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}


class PasajSource:
    """Bounded read-only requests with fail-closed robots and redirect handling."""

    def __init__(self, get=None, sleep=time.sleep, monotonic=time.monotonic):
        self._get = get or requests.get
        self._sleep, self._monotonic = sleep, monotonic
        self._lock = RLock()
        self._rules = None
        self._last_request = None
        self._delay = 0.5
        self._pdp_requests = 0
        self._blocked = False
        self._robots_status = 'not_loaded'
        self._catalog_profiles = {}
        self._pdp_limit = MAX_PDP_REQUESTS

    def configure_catalog_budget(self, product_count):
        if type(product_count) is not int or product_count < 1:
            raise ValueError('Catalog product count must be a positive integer')
        if self._pdp_requests or self._rules is not None:
            raise RuntimeError('Configure the catalog budget before source requests')
        self._pdp_limit = max(MAX_PDP_REQUESTS, min(60, product_count))

    def register_catalog_targets(self, product_name, targets, expected_specs):
        from vatan_sitemap_source import VatanSitemapSource
        from tr_price_scraper import TRPriceScraper
        matcher = TRPriceScraper()
        specs = {key: matcher._spec_int(expected_specs.get(key)) for key in ('ram_gb', 'storage_gb')}
        specs['network_generation'] = expected_specs.get('network_generation') or matcher._radio_generation(product_name)
        if not specs['ram_gb'] or not specs['storage_gb']:
            return
        brands = {'apple': 'Apple', 'samsung': 'Samsung', 'xiaomi': 'Xiaomi', 'poco': 'POCO',
                  'honor': 'Honor', 'oppo': 'OPPO', 'vivo': 'Vivo', 'realme': 'realme',
                  'tecno': 'Tecno', 'infinix': 'Infinix'}
        brand = brands.get(product_name.split()[0].casefold()) if product_name else None
        if not brand:
            return
        with self._lock:
            for row in targets:
                url = row.get('source_url') if isinstance(row, dict) else None
                if (not product_url(url) and catalog_product_url(url)
                        and VatanSitemapSource._variant_matches(urlparse(url).path, product_name, specs)):
                    self._catalog_profiles[url] = {'name': product_name, 'brand': brand, 'specs': specs}

    def is_catalog_target(self, url):
        return url in self._catalog_profiles

    def parse_offer(self, html, url):
        """Use the same parser for a live GET and a GET-only coverage audit."""
        return _selected_offer(html, url, self._catalog_profiles.get(url))

    def _request(self, url, limit):
        if self._blocked or (self._rules and not self._rules.can_fetch(BOT, url)):
            raise ValueError('blocked_or_disallowed')
        if self._last_request is not None:
            self._sleep(max(0, self._delay - (self._monotonic() - self._last_request)))
        self._last_request = self._monotonic()
        response = self._get(url, timeout=(5, 15), allow_redirects=False, stream=True,
                             headers={'User-Agent': BOT + '/1.0 (+https://teknoskor.com)',
                                      'Accept': 'text/plain' if url == ROBOTS_URL else 'text/html',
                                      'Accept-Language': 'tr-TR,tr;q=0.9'})
        try:
            if response.status_code in (403, 429):
                self._blocked = True
            if response.url != url:
                return None, 'unexpected_final_url', response.status_code
            if response.status_code != 200:
                return None, 'http_' + str(response.status_code), response.status_code
            mime = response.headers.get('Content-Type', '').lower()
            if (url == ROBOTS_URL and not ('text/plain' in mime or 'text/html' in mime) or
                    url != ROBOTS_URL and 'text/html' not in mime):
                return None, 'unexpected_content_type', 200
            size = str(response.headers.get('Content-Length') or '')
            if size.isdigit() and int(size) > limit:
                return None, 'oversized_response', 200
            body = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                body.extend(chunk)
                if len(body) > limit:
                    return None, 'oversized_response', 200
            return body.decode('utf-8-sig', errors='strict'), 'ok', 200
        finally:
            response.close()

    def _ensure_robots(self, url):
        if self._rules is None:
            body, reason, _ = self._request(ROBOTS_URL, MAX_ROBOTS_BYTES)
            if reason != 'ok' or not re.search(r'^\s*User-agent\s*:', body, re.I | re.M):
                self._robots_status = reason if reason != 'ok' else 'invalid_robots'
                raise ValueError('robots_unavailable')
            rules = RobotFileParser()
            rules.parse(body.splitlines())
            delay = float(rules.crawl_delay(BOT) or 0)
            rate = rules.request_rate(BOT)
            if rate:
                delay = max(delay, rate.seconds / max(1, rate.requests))
            if delay > 30:
                self._robots_status = 'crawl_delay_exceeds_budget'
                raise ValueError('crawl_delay_exceeds_budget')
            self._delay = max(0.5, delay)
            self._rules = rules
        if not self._rules.can_fetch(BOT, url):
            self._robots_status = 'pdp_disallowed'
            raise ValueError('robots_disallowed')
        self._robots_status = 'allowed'

    def read_offer(self, url):
        if not product_url(url) and not self.is_catalog_target(url):
            return None, 'unreviewed_pasaj_pdp', None
        with self._lock:
            try:
                self._ensure_robots(url)
                if self._pdp_requests >= self._pdp_limit:
                    return None, 'pdp_budget_exhausted', None
                self._pdp_requests += 1
                body, reason, status = self._request(url, MAX_HTML_BYTES)
                if reason != 'ok':
                    return None, reason, status
                offer = self.parse_offer(body, url)
                return offer, 'observed_exact_cash_offer' if offer else 'no_selected_cash_offer', 200
            except ValueError as error:
                return None, str(error), None
            except (requests.RequestException, TypeError, UnicodeError) as error:
                return None, type(error).__name__, None

    def report(self):
        return {'robots_status': self._robots_status, 'pdp_requests': self._pdp_requests,
                'blocked': self._blocked, 'reviewed_pdp_count': len(REVIEWED),
                'catalog_discovered_urls': len(self._catalog_profiles), 'pdp_request_limit': self._pdp_limit}
