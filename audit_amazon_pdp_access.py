"""Read-only GitHub runner access check for one exact Amazon Turkey PDP.

No search, cart, offer ingestion, or cached price is used by production.
"""

import json
import os
import re
import time
from pathlib import Path
from tempfile import gettempdir
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup


BOT = 'TeknoskorPriceBot'
HOST = 'www.amazon.com.tr'
ROBOTS_URL = f'https://{HOST}/robots.txt'
PDP_URL = f'https://{HOST}/dp/B0FQFBXXWF'
ASIN = 'B0FQFBXXWF'
SOLVER_URL = 'http://localhost:8191/v1'
MAX_ROBOTS = 128 * 1024
MAX_HTML = 2 * 1024 * 1024
MAX_SOLVER = 5 * 1024 * 1024
OUT = Path(os.environ.get('AMAZON_AUDIT_OUTPUT_DIR', gettempdir()))


def exact_pdp_url(url):
    try:
        parsed = urlparse(url)
        return bool(parsed.scheme == 'https' and parsed.hostname == HOST
                    and parsed.username is None and parsed.password is None
                    and parsed.port in (None, 443) and not parsed.query
                    and not parsed.fragment and re.fullmatch(
                        rf'/(?:[^/]+/)?dp/{ASIN}', parsed.path))
    except (TypeError, ValueError):
        return False


def bounded_response(response, limit):
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


def selected_facts(body):
    soup = BeautifulSoup(body, 'html.parser')
    title = soup.select_one('#productTitle')
    stock = soup.select_one('#availability')
    seller = soup.select_one('#sellerProfileTriggerId')
    price = soup.select_one('#tp_price_block_total_price_ww .a-offscreen')
    offers = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            node = json.loads(script.get_text())
        except (TypeError, ValueError):
            continue
        if isinstance(node, dict) and node.get('@type') == 'Product':
            offer = node.get('offers')
            if isinstance(offer, dict):
                offers.append({key: offer.get(key) for key in
                               ('price', 'priceCurrency', 'availability')})
    return {
        'h1': title.get_text(' ', strip=True)[:240] if title else None,
        'stock_label': stock.get_text(' ', strip=True)[:80] if stock else None,
        'selected_seller': seller.get_text(' ', strip=True)[:120] if seller else None,
        'visible_price': price.get_text(' ', strip=True)[:80] if price else None,
        'add_to_cart': bool(soup.select_one('#add-to-cart-button')),
        'structured_offers': offers[:2],
    }


def run():
    report = {'purpose': 'read_only_amazon_exact_pdp_runner_access',
              'robots_status': 'unavailable', 'normal': None, 'browser': None,
              'normal_selected_facts': None, 'browser_selected_facts': None}
    headers = {'User-Agent': BOT + '/1.0 (+https://teknoskor.com)',
               'Accept-Language': 'tr-TR,tr;q=0.9'}
    try:
        response = requests.get(ROBOTS_URL, timeout=(5, 20), allow_redirects=False,
                                stream=True, headers={**headers, 'Accept': 'text/plain'})
        status = response.status_code
        raw = bounded_response(response, MAX_ROBOTS)
        if status != 200 or raw is None:
            report['robots_status'] = f'http_{status}_or_oversized'
            return report
        policy = raw.decode('utf-8-sig')
        if (re.search(r'<(?:!doctype|html|head|body)\b', policy, re.I)
                or not re.search(r'^\s*user-agent\s*:', policy, re.I | re.M)):
            report['robots_status'] = 'invalid_policy'
            return report
        rules = RobotFileParser()
        rules.parse(policy.splitlines())
        if not rules.can_fetch(BOT, PDP_URL):
            report['robots_status'] = 'pdp_disallowed'
            return report
        delay = float(rules.crawl_delay(BOT) or 0)
        rate = rules.request_rate(BOT)
        if rate:
            delay = max(delay, rate.seconds / max(1, rate.requests))
        if delay > 30:
            report['robots_status'] = 'delay_exceeds_budget'
            return report
        report['robots_status'] = 'ready'
        if delay:
            time.sleep(delay)
        response = requests.get(PDP_URL, timeout=(5, 25), allow_redirects=False,
                                stream=True, headers={**headers, 'Accept': 'text/html'})
        normal = {'status': response.status_code, 'final_url': response.url,
                  'location': urljoin(PDP_URL, response.headers.get('Location', ''))
                  if response.headers.get('Location') else None}
        body = bounded_response(response, MAX_HTML)
        report['normal'] = normal
        if normal['status'] in (301, 302, 303, 307, 308):
            final = normal['location']
            if not exact_pdp_url(final) or not rules.can_fetch(BOT, final):
                normal['reason'] = 'unsafe_or_disallowed_redirect'
                return report
            if delay:
                time.sleep(delay)
            response = requests.get(final, timeout=(5, 25), allow_redirects=False,
                                    stream=True, headers={**headers, 'Accept': 'text/html'})
            normal['redirect_status'] = response.status_code
            normal['final_url'] = response.url
            body = bounded_response(response, MAX_HTML)
        final_status = normal.get('redirect_status', normal['status'])
        if final_status == 200 and body:
            report['normal_selected_facts'] = selected_facts(body)
            facts = report['normal_selected_facts']
            if (facts['visible_price'] and facts['stock_label']
                    and facts['structured_offers']):
                return report
        if final_status not in (200, 403):
            return report
        response = requests.post(SOLVER_URL, json={
            'cmd': 'request.get', 'url': PDP_URL, 'maxTimeout': 45000,
        }, timeout=60, stream=True)
        report['browser'] = {'service_http': response.status_code}
        raw = bounded_response(response, MAX_SOLVER)
        if raw is None or report['browser']['service_http'] != 200:
            return report
        payload = json.loads(raw)
        solution = payload.get('solution') if isinstance(payload, dict) else None
        if not isinstance(solution, dict):
            return report
        final = solution.get('url')
        report['browser'].update(site_http=solution.get('status'), final_url=final)
        if (solution.get('status') != 200 or not exact_pdp_url(final)
                or not rules.can_fetch(BOT, final)):
            return report
        html = solution.get('response')
        if isinstance(html, str) and len(html.encode('utf-8')) <= MAX_HTML:
            report['browser_selected_facts'] = selected_facts(html)
    except (requests.RequestException, ValueError, TypeError, UnicodeError) as error:
        report['error_class'] = type(error).__name__
    return report


if __name__ == '__main__':
    result = run()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'amazon-pdp-access.json').write_text(
        json.dumps(result, indent=2, ensure_ascii=False))
    print(json.dumps(result, ensure_ascii=False))
