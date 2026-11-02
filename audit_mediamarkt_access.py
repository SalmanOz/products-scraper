"""Read-only, bounded GitHub runner check of MediaMarkt public PDP access.

The report contains response metadata and three selected Product/Offer facts;
it never sends an offer to Teknoskor or changes a retailer session.
"""

import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
import requests


HOST = 'www.mediamarkt.com.tr'
ROOT = f'https://{HOST}'
ROBOTS = ROOT + '/robots.txt'
SITEMAPS = tuple(ROOT + f'/sitemaps/sitemap-productdetailspages-00{i}.xml' for i in (0, 1))
PRODUCTS = {
    'iphone17_first_party': ROOT + '/tr/product/_apple-iphone-17-256-gb-akilli-telefon-beyaz-166347558.html',
    'a27_marketplace': ROOT + '/tr/product/_samsung-galaxy-a27-5g-6-gb-128-gb-akilli-telefon-siyah-181000845.html',
    's23_refurbished': ROOT + '/tr/product/_samsung-galaxy-s23-8-gb-128-gb-akilli-telefon-yesil-180405962.html',
}
BOT = 'TeknoskorPriceBot'
HEADERS = {'User-Agent': BOT + '/1.0 (+https://teknoskor.com)',
           'Accept-Language': 'tr-TR,tr;q=0.9'}
MAX_ROBOTS = 64 * 1024
MAX_SITEMAP = 3 * 1024 * 1024
MAX_HTML = 5 * 1024 * 1024
MAX_SOLVER = 8 * 1024 * 1024
SOLVER_URL = 'http://localhost:8191/v1'
OUT = Path(os.environ.get('MEDIAMARKT_AUDIT_OUTPUT_DIR', gettempdir()))


def safe_url(url):
    if not isinstance(url, str) or len(url) > 500 or re.search(r'[\x00-\x20\\]', url):
        return False
    try:
        parsed = urlparse(url)
        return (parsed.scheme == 'https' and parsed.hostname == HOST
                and parsed.username is None and parsed.password is None
                and parsed.port in (None, 443) and not parsed.query and not parsed.fragment)
    except (TypeError, ValueError):
        return False


def read(url, limit, accept):
    response = requests.get(url, timeout=(5, 20), allow_redirects=False,
                            stream=True, headers={**HEADERS, 'Accept': accept})
    try:
        row = {'url': url, 'http_status': response.status_code,
               'final_url': response.url, 'bytes': 0}
        if response.url != url:
            row['reason'] = 'unexpected_final_url'
            return row, b''
        if response.status_code in (301, 302, 303, 307, 308):
            row['redirect'] = urljoin(url, response.headers.get('Location', ''))
            return row, b''
        if response.status_code != 200:
            return row, b''
        declared = response.headers.get('Content-Length', '')
        if declared.isdigit() and int(declared) > limit:
            row['reason'] = 'oversized'
            return row, b''
        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            body.extend(chunk)
            if len(body) > limit:
                row['reason'] = 'oversized'
                return row, b''
        row['bytes'] = len(body)
        return row, bytes(body)
    finally:
        response.close()


def browser_read(url, rules):
    """One local browser GET for a robots-allowed PDP after ordinary GET 403."""
    row = {'transport': 'browser_get', 'url': url, 'service_http': None,
           'site_http': None, 'final_url': None, 'bytes': 0}
    if not safe_url(url) or not rules.can_fetch(BOT, url):
        row['reason'] = 'invalid_or_disallowed'
        return row, b''
    response = requests.post(SOLVER_URL, json={
        'cmd': 'request.get', 'url': url, 'maxTimeout': 45000,
    }, timeout=60, stream=True)
    try:
        row['service_http'] = response.status_code
        data = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            data.extend(chunk)
            if len(data) > MAX_SOLVER:
                row['reason'] = 'solver_response_too_large'
                return row, b''
        if response.status_code != 200:
            row['reason'] = 'solver_http_error'
            return row, b''
        payload = json.loads(data)
        solution = payload.get('solution') if isinstance(payload, dict) else None
        if not isinstance(payload, dict) or payload.get('status') != 'ok' or not isinstance(solution, dict):
            row['reason'] = 'solver_unavailable'
            return row, b''
        row['site_http'] = solution.get('status')
        row['final_url'] = solution.get('url')
        final = row['final_url']
        if not safe_url(final) or not rules.can_fetch(BOT, final):
            row['reason'] = 'unsafe_or_disallowed_redirect'
            return row, b''
        if row['site_http'] != 200:
            row['reason'] = 'site_http_error'
            return row, b''
        body = solution.get('response')
        if not isinstance(body, str):
            row['reason'] = 'invalid_html'
            return row, b''
        content = body.encode('utf-8')
        row['bytes'] = len(content)
        if len(content) > MAX_HTML:
            row['reason'] = 'html_too_large'
            return row, b''
        return row, content
    finally:
        response.close()


def selected_offer(body, final_url):
    soup = BeautifulSoup(body, 'html.parser')
    heading = soup.select_one('h1')
    title = heading.get_text(' ', strip=True) if heading else None
    canonical = soup.select_one('link[rel="canonical"]')
    canonical_url = canonical.get('href') if canonical else None
    found = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            node = json.loads(script.get_text())
        except (TypeError, ValueError):
            continue
        if isinstance(node, dict) and node.get('@type') == 'BuyAction':
            product = node.get('object')
            if isinstance(product, dict) and product.get('@type') in ('Product', 'ProductGroup'):
                offers = product.get('offers')
                offers = offers if isinstance(offers, list) else [offers]
                if len(offers) == 1 and isinstance(offers[0], dict):
                    offer = offers[0]
                    found.append({'product_type': product.get('@type'),
                                  'product_url_match': product.get('url') == final_url,
                                  'offer_url_match': offer.get('url') == final_url,
                                  'name_matches_h1': product.get('name') == title,
                                  'sku': product.get('sku'),
                                  'price': offer.get('price'),
                                  'currency': offer.get('priceCurrency'),
                                  'availability': offer.get('availability'),
                                  'condition': offer.get('itemCondition'),
                                  'seller': offer.get('seller')})
    buttons = [b for b in soup.select('button[data-test*="cofr-add-to-basket-button"]')
               if b.get_text(' ', strip=True) == 'Sepete Ekle']
    return {'h1': title, 'canonical_matches_final': canonical_url == final_url,
            'selected_offer_count': len(found), 'selected_offer': found[0] if len(found) == 1 else None,
            'active_basket_buttons': sum(b.get('aria-disabled') == 'false' and not b.has_attr('disabled') for b in buttons)}


def run():
    report = {'purpose': 'read_only_mediamarkt_runner_access',
              'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
              'robots': {}, 'sitemaps': [], 'products': {}}
    try:
        robots_row, body = read(ROBOTS, MAX_ROBOTS, 'text/plain')
        report['robots'] = robots_row
        policy = body.decode('utf-8-sig', errors='strict')
        if (robots_row['http_status'] != 200
                or re.search(r'<(?:!doctype|html|head|body)\b', policy, re.I)
                or not re.search(r'^\s*user-agent\s*:', policy, re.I | re.M)):
            report['reason'] = 'robots_policy_unavailable'
            return report
        rules = RobotFileParser()
        rules.parse(policy.splitlines())
        report['robots']['valid_policy'] = True
        advertised = set(re.findall(r'^\s*Sitemap:\s*(\S+)\s*$', policy, re.I | re.M))
        for url in SITEMAPS:
            if url not in advertised or not rules.can_fetch(BOT, url):
                report['sitemaps'].append({'url': url, 'reason': 'not_advertised_or_disallowed'})
                continue
            row, content = read(url, MAX_SITEMAP, 'application/xml,text/xml')
            if content:
                try:
                    if b'<!DOCTYPE' in content.upper() or b'<!ENTITY' in content.upper():
                        raise ET.ParseError('unsafe_xml')
                    root = ET.fromstring(content)
                    row['url_count'] = sum(child.tag.endswith('}url') for child in root)
                except ET.ParseError:
                    row['reason'] = 'invalid_xml'
            report['sitemaps'].append(row)
        for name, url in PRODUCTS.items():
            if not safe_url(url) or not rules.can_fetch(BOT, url):
                report['products'][name] = {'reason': 'invalid_or_disallowed'}
                continue
            row, content = read(url, MAX_HTML, 'text/html')
            if row['http_status'] == 403 and os.environ.get('MEDIAMARKT_AUDIT_BROWSER_GET') == '1':
                try:
                    browser_row, content = browser_read(url, rules)
                except (requests.RequestException, ValueError, TypeError):
                    browser_row, content = {'transport': 'browser_get', 'reason': 'transport_error'}, b''
                row['browser_response'] = browser_row
                if browser_row.get('site_http') == 200 and content:
                    row['selected'] = selected_offer(content, browser_row['final_url'])
            if row.get('redirect'):
                destination = row['redirect']
                row['redirect_allowed'] = safe_url(destination) and rules.can_fetch(BOT, destination)
                if row['redirect_allowed']:
                    final, content = read(destination, MAX_HTML, 'text/html')
                    row['redirect_response'] = final
                    if final['http_status'] == 200 and content:
                        row['selected'] = selected_offer(content, destination)
            elif row['http_status'] == 200 and content:
                row['selected'] = selected_offer(content, url)
            report['products'][name] = row
    except (requests.RequestException, UnicodeError, ValueError) as error:
        report['reason'] = type(error).__name__
    return report


def main():
    report = run()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'mediamarkt-access.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    selected = report['products'].get('iphone17_first_party', {}).get('selected', {})
    offer = selected.get('selected_offer') or {}
    ready = (report['robots'].get('valid_policy')
             and len(report['sitemaps']) == 2
             and all(0 < row.get('url_count', 0) <= 20_000 for row in report['sitemaps'])
             and offer.get('seller') is None
             and offer.get('product_url_match') and offer.get('offer_url_match')
             and offer.get('condition', '').endswith('/NewCondition')
             and offer.get('availability', '').endswith('/InStock')
             and selected.get('active_basket_buttons', 0) > 0)
    summary = ('MediaMarkt runner access: ' + ('READY' if ready else 'NOT READY')
               + f"; robots={report['robots'].get('http_status')}"
               + f"; sitemap URLs={[x.get('url_count') for x in report['sitemaps']]}"
               + f"; first-party iPhone17 price={offer.get('price')}.\n")
    detail_lines = []
    for name, row in report['products'].items():
        final = row.get('browser_response') or row.get('redirect_response') or row
        chosen = row.get('selected') or {}
        item = chosen.get('selected_offer') or {}
        detail_lines.append(
            f"- {name}: initial HTTP {row.get('http_status')}, final HTTP {final.get('http_status')}, "
            f"browser service HTTP {final.get('service_http')}, browser site HTTP {final.get('site_http')}, "
            f"browser reason {final.get('reason')}, "
            f"redirect allowed {row.get('redirect_allowed')}, selected {chosen.get('selected_offer_count')}, "
            f"cash {item.get('price')}, seller {str(item.get('seller'))[:60]}, "
            f"condition {str(item.get('condition'))[-30:]}, active basket {chosen.get('active_basket_buttons')}."
        )
    (OUT / 'mediamarkt-access.md').write_text(
        '## Read-only MediaMarkt access check\n\n' + summary + '\n'.join(detail_lines) + '\n')
    print(summary, end='')
    return 0 if ready else 1


if __name__ == '__main__':
    raise SystemExit(main())
