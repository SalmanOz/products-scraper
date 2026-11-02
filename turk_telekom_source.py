"""Selected Türk Telekom eMağaza phone offers from public, robots-allowed pages.

Stock-filtered category cards are URL hints. Only the selected ``?v=`` PDP,
its enabled credit-card cash option, and its selected variant title establish
an offer. Tariff and installment figures are never interpreted as cash prices.
"""

from datetime import datetime, timezone
import re
import time
from threading import RLock
from urllib.parse import parse_qsl, urljoin, urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
import requests

from tr_price_scraper import TRPriceScraper


HOST = 'emagaza.turktelekom.com.tr'
BASE = 'https://' + HOST
ROBOTS_URL = BASE + '/robots.txt'
CATEGORY_URL = BASE + '/tr/akilli-telefon?stock=1'
BOT = 'TeknoskorPriceBot'
MAX_CATEGORY_PAGES = 3
MAX_CANDIDATES_PER_PRODUCT = 2
MAX_ROBOTS_BYTES = 64 * 1024
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_PDP_REQUESTS = 30
_PRICE = re.compile(r'(\d{1,3}(?:\.\d{3})*|\d+)(?:,(\d{2}))?\s*TL\b', re.I)


def product_url(url):
    """Require an exact, single numeric selected-variant query and phone path."""
    if not isinstance(url, str) or len(url) > 500 or re.search(r'[\x00-\x20\\]', url):
        return False
    try:
        parsed = urlparse(url)
        pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=3)
        return bool(parsed.scheme == 'https' and parsed.hostname == HOST
                    and parsed.username is None and parsed.password is None
                    and parsed.port in (None, 443) and not parsed.fragment
                    and re.fullmatch(r'/tr/products/[a-z0-9-]+/[a-z0-9-]+', parsed.path)
                    and len(pairs) == 1 and pairs[0][0] == 'v'
                    and re.fullmatch(r'[1-9]\d{2,7}', pairs[0][1]))
    except (TypeError, ValueError):
        return False


def _selected_offer(html, url):
    if not product_url(url):
        return None
    soup = BeautifulSoup(html, 'html.parser')
    if re.search(r'captcha|access denied|just a moment',
                 ' '.join(n.get_text(' ', strip=True) for n in soup.select('title,h1')), re.I):
        return None
    parsed = urlparse(url)
    selected_id = dict(parse_qsl(parsed.query))['v']
    canonical = soup.select('link[rel="canonical"]')
    if len(canonical) != 1 or canonical[0].get('href') != BASE + parsed.path:
        return None
    flags = soup.select('.product-variation-flag[data-product-variation-id]')
    if len(flags) != 1 or flags[0].get('data-product-variation-id') != selected_id:
        return None
    form = soup.select('form.commerce-order-item-add-to-cart-form')
    if len(form) != 1 or urljoin(url, form[0].get('action', '')) != url:
        return None
    if soup.select('.alert-warning') and re.search(
            r'stoklarımızda bulunmamaktadır|stokta yok|tükendi',
            ' '.join(n.get_text(' ', strip=True) for n in soup.select('.alert-warning')), re.I):
        return None
    # The PDP may contain an empty mobile h1, but exactly one populated model
    # heading and one full selected-product name must agree on the model.
    headings = [n.get_text(' ', strip=True) for n in soup.select('h1') if n.get_text(' ', strip=True)]
    names = [n.get_text(' ', strip=True) for n in soup.select('.title-and-product-name p.product-name')]
    if len(headings) != 1 or len(names) != 1 or not names[0].casefold().startswith(headings[0].casefold()):
        return None
    title = names[0]
    if re.search(r'outlet|yenilenmi[sş]|refurbished|te[sş]hir|ikinci\s*el', title, re.I):
        return None
    # The active storage radio and selected order's price list must agree with
    # the full variant title and cash button. This excludes adjacent variants.
    capacities = [n for n in form[0].select('input[type="radio"][checked]')
                  if re.fullmatch(r'\d{1,2}\s*(?:GB)?\s*/\s*\d{2,4}\s*GB', n.get('name', ''), re.I)]
    if len(capacities) != 1 or capacities[0]['name'].replace(' ', '').casefold() not in title.replace(' ', '').casefold():
        return None
    price_list = [n.get('value') for n in form[0].select('input[name="price_list"][checked]')]
    if len(price_list) > 1 or (price_list and not re.fullmatch(r'\d{6,15}', price_list[0] or '')):
        return None
    groups = [g for g in soup.select('.payment-group')
              if g.select_one('.group-title') and g.select_one('.group-title').get_text(' ', strip=True) == 'Peşin Ödeme']
    if len(groups) != 1:
        return None
    rows = [r for r in groups[0].select('.payment-row')
            if r.select_one('.cell-title') and r.select_one('.cell-title').get_text(' ', strip=True) == 'Kredi Kartı ile Peşin']
    if len(rows) != 1:
        return None
    row = rows[0]
    prices = row.select('.cell-price .price')
    buttons = row.select('button.paymentSelectBtn[data-offer-id]')
    if len(prices) != 1 or len(buttons) != 1:
        return None
    button = buttons[0]
    if ((price_list and button.get('data-offer-id') != price_list[0]) or button.has_attr('disabled')
            or button.get('aria-disabled') == 'true' or button.get_text(' ', strip=True) != 'Seç'):
        return None
    price_text = prices[0].get_text(' ', strip=True)
    match = _PRICE.fullmatch(price_text)
    if not match:
        return None
    price = int(match[1].replace('.', '')) + int(match[2] or '0') / 100
    if not 0 < price <= 5_000_000:
        return None
    return {'merchant': 'Türk Telekom eMağaza', 'price': price, 'currency': 'TRY',
            'availability': 'in_stock', 'source_title': title, 'url': url,
            'observed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}


class TurkTelekomSource:
    """One category scan per run and bounded, paced exact PDP observations."""

    def __init__(self, get=None, sleep=time.sleep, monotonic=time.monotonic):
        self._get = get or requests.get
        self._sleep, self._monotonic = sleep, monotonic
        self._lock = RLock()
        self._rules = None
        self._loaded = False
        self._cards = []
        self._status = 'not_loaded'
        self._robots_status = 'not_loaded'
        self._last_request = None
        self._delay = 0.5
        self._pdp_requests = 0
        self._category_requests = 0
        self._blocked = False
        self._matcher = TRPriceScraper()

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
            if response.status_code != 200 or response.url != url:
                return None, f'http_{response.status_code}' if response.url == url else 'unexpected_final_url', response.status_code
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

    def _ensure_robots(self):
        if self._rules is not None:
            return
        text, reason, _ = self._request(ROBOTS_URL, MAX_ROBOTS_BYTES)
        if reason != 'ok' or not re.search(r'^\s*User-agent\s*:', text, re.I | re.M):
            self._robots_status = reason if reason != 'ok' else 'invalid_robots'
            raise ValueError('robots_unavailable')
        rules = RobotFileParser()
        rules.parse(text.splitlines())
        if not rules.can_fetch(BOT, CATEGORY_URL):
            self._robots_status = 'category_disallowed'
            raise ValueError('category_disallowed')
        delay = float(rules.crawl_delay(BOT) or 0)
        rate = rules.request_rate(BOT)
        if rate:
            delay = max(delay, rate.seconds / max(1, rate.requests))
        if delay > 30:
            self._robots_status = 'crawl_delay_exceeds_budget'
            raise ValueError('crawl_delay_exceeds_budget')
        self._delay = max(0.5, delay)
        self._rules = rules
        self._robots_status = 'allowed'

    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            self._ensure_robots()
            seen = set()
            for page in range(MAX_CATEGORY_PAGES):
                url = CATEGORY_URL + (f'&page={page}' if page else '')
                if not self._rules.can_fetch(BOT, url):
                    raise ValueError('category_disallowed')
                body, reason, _ = self._request(url, MAX_HTML_BYTES)
                self._category_requests += 1
                if reason != 'ok':
                    raise ValueError('category_' + reason)
                soup = BeautifulSoup(body, 'html.parser')
                if re.search(r'captcha|access denied|just a moment',
                             ' '.join(n.get_text(' ', strip=True) for n in soup.select('title,h1')), re.I):
                    raise ValueError('category_challenge')
                cards = soup.select('a.dls-device-card[href]')
                if not cards:
                    break
                for card in cards:
                    heading = card.select_one('h3.dls-device-card-heading-text')
                    target = urljoin(url, card.get('href', ''))
                    if heading and product_url(target) and self._rules.can_fetch(BOT, target) and target not in seen:
                        seen.add(target)
                        self._cards.append((target, heading.get_text(' ', strip=True)))
            self._status = 'ready'
        except (requests.RequestException, ValueError, TypeError, UnicodeError) as error:
            self._cards = []
            self._status = str(error) if isinstance(error, ValueError) else type(error).__name__

    def discover(self, product_name, expected_specs):
        if not isinstance(product_name, str) or not isinstance(expected_specs, dict):
            return []
        with self._lock:
            self._load()
            matches = []
            expected_radio = expected_specs.get('network_generation') or self._matcher._radio_generation(product_name)
            for url, title in self._cards:
                observed_radio = self._matcher._radio_generation(title)
                if (expected_radio and observed_radio != expected_radio
                        and re.search(r'\b(?:galaxy a1[4567]|redmi note 1[1-5])\b',
                                      self._matcher._normalize_words(product_name))):
                    continue
                if (self._matcher.is_strict_match(product_name, title, expected_specs)
                        and self._matcher.has_observed_offer_evidence(
                            product_name, {'source_title': title, 'url': url}, expected_specs)):
                    matches.append({'source_url': url})
                    if len(matches) == MAX_CANDIDATES_PER_PRODUCT:
                        break
            return matches

    def read_offer(self, url):
        if not product_url(url):
            return None, 'invalid_turk_telekom_pdp_url', None
        with self._lock:
            try:
                self._ensure_robots()
                if not self._rules.can_fetch(BOT, url):
                    return None, 'robots_disallowed', None
                if self._pdp_requests >= MAX_PDP_REQUESTS:
                    return None, 'pdp_budget_exhausted', None
                self._pdp_requests += 1
                body, reason, status = self._request(url, MAX_HTML_BYTES)
                if reason != 'ok':
                    return None, reason, status
                offer = _selected_offer(body, url)
                return offer, 'observed_exact_cash_offer' if offer else 'no_selected_cash_offer', 200
            except (requests.RequestException, ValueError, TypeError, UnicodeError) as error:
                return None, type(error).__name__, None

    def report(self):
        return {'status': self._status, 'robots_status': self._robots_status,
                'category_requests': self._category_requests,
                'stock_card_urls': len(self._cards), 'pdp_requests': self._pdp_requests,
                'blocked': self._blocked}
