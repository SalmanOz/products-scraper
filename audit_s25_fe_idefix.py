"""Read-only probe of one exact idefix S25 FE 8/256 product page.

This is source diagnosis, not a reviewed offer seed. In particular, a basket
discount or a page without an independently identified seller cannot become
a production price observation.
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import gettempdir
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from retailer_offer_refresh import _MAX_BODY_BYTES, _challenge_page, _product_offer, _schema_nodes
from tr_price_scraper import TRPriceScraper


URL = ('https://www.idefix.com/samsung-galaxy-s25-fe-256-gb-8-gb-ram-'
       'buz-mavisi-samsung-turkiye-garantili-p-14503314')
ROBOTS_URL = 'https://www.idefix.com/robots.txt'
BOT = 'TeknoskorPriceBot'
NAME = 'Samsung Galaxy S25 FE 256GB'
SPECS = {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'}
OUT = Path(os.environ.get('S25_FE_IDEFIX_AUDIT_OUTPUT_DIR', gettempdir()))


def _read(fetch, url, max_bytes):
    headers = ({'User-Agent': BOT + '/1.0 (+https://teknoskor.com)',
                'Accept': 'text/plain', 'Accept-Language': 'tr-TR,tr;q=0.9'}
               if url == ROBOTS_URL else
               {'Accept': 'text/html', 'Accept-Language': 'tr-TR,tr;q=0.9'})
    response = fetch(url, timeout=(5, 15), allow_redirects=False,
                     headers=headers)
    if response.url != url or len(response.content) > max_bytes:
        return None, 'unexpected_redirect_or_oversized_response', response.status_code
    if response.status_code != 200:
        return None, f'http_{response.status_code}', response.status_code
    return response.text, 'ready', response.status_code


def _selected_offer(soup):
    products = [node for node in _schema_nodes(soup)
                if isinstance(node, dict) and node.get('@type') == 'Product']
    if len(products) != 1:
        return None
    product = products[0]
    offer = product.get('offers')
    if not isinstance(offer, dict) or offer.get('@type') != 'Offer' or offer.get('url') != URL:
        return None
    return offer


def audit_html(html):
    soup = BeautifulSoup(html, 'html.parser')
    if _challenge_page(soup):
        return {'status': 'challenge_page', 'seller_identity': 'unverified'}
    headings = soup.select('h1')
    title = headings[0].get_text(' ', strip=True) if len(headings) == 1 else ''
    matcher = TRPriceScraper()
    if not title or not matcher.is_strict_match(NAME, title, SPECS):
        return {'status': 'identity_unverified', 'seller_identity': 'unverified'}
    offer = _selected_offer(soup)
    if not offer:
        return {'status': 'selected_offer_unverified', 'seller_identity': 'unverified',
                'title': title}
    visible = soup.get_text(' ', strip=True)
    basket_discount = bool(re.search(r'\bSepette\s+İndirim\b', visible, re.I))
    # Exact observed amounts are diagnostic only. Neither JSON-LD price nor
    # ListPrice is published without an ordinary price and seller proof.
    diagnostic = {'title': title, 'seller_identity': 'unverified',
                  'basket_discount_visible': basket_discount,
                  'schema_offer_price': offer.get('price'),
                  'schema_list_price': (offer.get('priceSpecification') or {}).get('price')
                      if isinstance(offer.get('priceSpecification'), dict) else None}
    parsed = _product_offer(soup, URL)
    if basket_discount:
        return {**diagnostic, 'status': 'basket_discount_no_publishable_cash_offer',
                'generic_reader_rejected': parsed is None}
    if (parsed and matcher.has_observed_offer_evidence(NAME, parsed, SPECS)
            and parsed.get('availability') in {'in_stock', 'limited'}):
        return {**diagnostic, 'status': 'exact_offer_seller_unverified',
                'generic_reader_rejected': False}
    return {**diagnostic, 'status': 'no_exact_purchasable_offer',
            'generic_reader_rejected': parsed is None}


def run_audit(fetch=None, sleep=time.sleep):
    fetch = fetch or requests.get
    report = {'purpose': 'read_only_exact_idefix_s25_fe_8_256_diagnosis',
              'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
              'url': URL, 'status': 'not_attempted', 'http_status': None,
              'robots_status': 'unavailable', 'writes': False}
    try:
        robots_text, status, http = _read(fetch, ROBOTS_URL, 64 * 1024)
        if status != 'ready' or not re.search(r'^\s*User-agent\s*:', robots_text, re.I | re.M):
            report['robots_status'] = status if status != 'ready' else 'invalid_robots_policy'
            return report
        rules = RobotFileParser()
        rules.parse(robots_text.splitlines())
        if not rules.can_fetch(BOT, URL):
            report['robots_status'] = 'disallowed'
            return report
        delay = rules.crawl_delay(BOT)
        if delay and delay > 30:
            report['robots_status'] = 'crawl_delay_exceeds_budget'
            return report
        report['robots_status'] = 'ready'
        if delay:
            sleep(delay)
        html, status, http = _read(fetch, URL, _MAX_BODY_BYTES)
        report['http_status'] = http
        if status != 'ready':
            report['status'] = status
            return report
        report.update(audit_html(html))
        return report
    except (requests.RequestException, ValueError, TypeError, UnicodeError):
        report['status'] = 'probe_error'
        return report


def summary(report):
    return ('## S25 FE 8/256 idefix PDP diagnosis (read only)\n'
            f"Robots: {report['robots_status']}; HTTP: {report['http_status']}; "
            f"status: {report['status']}; seller: {report.get('seller_identity', 'unverified')}.\n"
            'Basket discounts and unverified sellers are not production offer seeds. '
            'No catalog, offer, schema, R2 or IndexNow writes.\n')


def main():
    report = run_audit()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 's25-fe-idefix-reader.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    text = summary(report)
    (OUT / 's25-fe-idefix-reader.md').write_text(text)
    print(text, end='')


if __name__ == '__main__':
    main()
