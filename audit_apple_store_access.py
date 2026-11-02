"""Read-only exact-variant Apple Turkey store access audit on a GitHub runner."""

import json
import os
import re
import time
from pathlib import Path
from tempfile import gettempdir
from urllib.parse import unquote, urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
import requests


BOT = 'TeknoskorPriceBot'
ROBOTS_URL = 'https://www.apple.com/robots.txt'
PDP_URLS = {
    'apple-iphone-18-pro-max': ('https://www.apple.com/tr/shop/buy-iphone/iphone-18-pro/'
                                '6.9-in%C3%A7-ekran-256gb-siyah'),
    'apple-iphone-18-pro': ('https://www.apple.com/tr/shop/buy-iphone/iphone-18-pro/'
                            '6.3-in%C3%A7-ekran-256gb-siyah'),
    'apple-iphone-17e': ('https://www.apple.com/tr/shop/buy-iphone/iphone-17e/'
                         '6.1-in%C3%A7-ekran-256gb-siyah'),
    'apple-iphone-17': ('https://www.apple.com/tr/shop/buy-iphone/iphone-17/'
                        '6.3-in%C3%A7-ekran-256gb-siyah'),
}
SOLVER_URL = 'http://localhost:8191/v1'
MAX_ROBOTS = 64 * 1024
MAX_HTML = 1024 * 1024
MAX_SOLVER = 3 * 1024 * 1024
OUT = Path(os.environ.get('APPLE_STORE_AUDIT_OUTPUT_DIR', gettempdir()))


def exact_url(url, expected):
    if not isinstance(url, str) or re.search(r'[\x00-\x20\\]', url):
        return False
    try:
        parsed = urlparse(url)
        target = urlparse(expected)
        return (parsed.scheme == target.scheme and parsed.hostname == target.hostname
                and parsed.username is None and parsed.password is None
                and parsed.port in (None, 443) and not parsed.query and not parsed.fragment
                and unquote(parsed.path) == unquote(target.path))
    except (TypeError, ValueError):
        return False


def bounded(response, limit):
    try:
        size = response.headers.get('Content-Length', '')
        if size.isdigit() and int(size) > limit:
            return None
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            body.extend(chunk)
            if len(body) > limit:
                return None
        return bytes(body)
    finally:
        response.close()


def facts(body, expected):
    soup = BeautifulSoup(body, 'html.parser')
    products = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.get_text())
        except (TypeError, ValueError):
            continue
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, dict) and item.get('@type') == 'Product':
                offer = item.get('offers')
                if (exact_url(item.get('url'), expected) and isinstance(offer, list)
                        and len(offer) == 1 and isinstance(offer[0], dict)):
                    products.append({
                        'name': item.get('name'), 'url_matches_selected': True,
                        'sku': offer[0].get('sku'), 'price': offer[0].get('price'),
                        'currency': offer[0].get('priceCurrency'),
                    })
    buttons = [button for button in soup.select('button')
               if 'Çantaya Ekleyin' in button.get_text(' ', strip=True)]
    selected = [radio for radio in soup.select('input[type="radio"]')
                if radio.has_attr('checked')]
    title = soup.title.get_text(' ', strip=True) if soup.title else None
    selected_product = products[0] if len(products) == 1 else None
    price = selected_product.get('price') if selected_product else None
    try:
        visible_amount = f'{float(price):,.2f}'.replace(',', '_').replace('.', ',').replace('_', '.') + ' TL'
    except (TypeError, ValueError):
        visible_amount = None
    return {'page_title': title, 'selected_product_count': len(products),
            'selected_product': selected_product,
            'active_bag_button': any(not button.has_attr('disabled') for button in buttons),
            'selected_radio_count': len(selected),
            'visible_price_mentions': soup.get_text(' ', strip=True).count(visible_amount)
            if visible_amount else 0}


def run():
    result = {'purpose': 'read_only_apple_store_four_selected_iphone_runner_access',
              'robots_status': 'unavailable', 'products': {}}
    headers = {'User-Agent': BOT + '/1.0 (+https://teknoskor.com)',
               'Accept-Language': 'tr-TR,tr;q=0.9'}
    try:
        response = requests.get(ROBOTS_URL, timeout=(5, 20), stream=True,
                                allow_redirects=False, headers={**headers, 'Accept': 'text/plain'})
        status = response.status_code
        raw = bounded(response, MAX_ROBOTS)
        if status != 200 or raw is None:
            result['robots_status'] = f'http_{status}_or_oversized'
            return result
        text = raw.decode('utf-8-sig')
        if (re.search(r'<(?:!doctype|html|head|body)\b', text, re.I)
                or not re.search(r'^\s*user-agent\s*:', text, re.I | re.M)):
            result['robots_status'] = 'invalid_policy'
            return result
        rules = RobotFileParser()
        rules.parse(text.splitlines())
        delay = float(rules.crawl_delay(BOT) or 0)
        if delay > 30 or not all(rules.can_fetch(BOT, url) for url in PDP_URLS.values()):
            result['robots_status'] = 'pdp_disallowed_or_delay_exceeds_budget'
            return result
        result['robots_status'] = 'ready'
        for slug, url in PDP_URLS.items():
            row = {'url': url, 'normal': None, 'browser': None,
                   'normal_facts': None, 'browser_facts': None}
            result['products'][slug] = row
            response = requests.get(url, timeout=(5, 25), stream=True,
                                    allow_redirects=False,
                                    headers={**headers, 'Accept': 'text/html'})
            row['normal'] = {'status': response.status_code, 'final_url': response.url}
            status = response.status_code
            html = bounded(response, MAX_HTML)
            if status == 200 and exact_url(row['normal']['final_url'], url) and html:
                row['normal_facts'] = facts(html, url)
            if delay:
                time.sleep(delay)
            response = requests.post(SOLVER_URL, json={
                'cmd': 'request.get', 'url': url, 'maxTimeout': 45000,
            }, timeout=60, stream=True)
            row['browser'] = {'service_http': response.status_code}
            status = response.status_code
            raw = bounded(response, MAX_SOLVER)
            if status == 200 and raw is not None:
                payload = json.loads(raw)
                solution = payload.get('solution') if isinstance(payload, dict) else None
                if isinstance(solution, dict):
                    row['browser'].update(site_http=solution.get('status'),
                                          final_url=solution.get('url'))
                    if solution.get('status') == 200 and exact_url(solution.get('url'), url):
                        html = solution.get('response')
                        if isinstance(html, str) and len(html.encode('utf-8')) <= MAX_HTML:
                            row['browser_facts'] = facts(html, url)
            if delay:
                time.sleep(delay)
    except (requests.RequestException, ValueError, TypeError, UnicodeError) as error:
        result['error_class'] = type(error).__name__
    return result


if __name__ == '__main__':
    report = run()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'apple-store-access.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False))
