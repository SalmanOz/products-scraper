"""Read-only runner probe of three reviewed, exact Vatan PDP URLs.

This audit bypasses sitemap discovery only for its fixed test URLs. It never
submits offers to ingestion or makes these URLs production discovery seeds.
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

from retailer_offer_refresh import (
    RetailerOfferRefresher, _MAX_BODY_BYTES, _challenge_page, _product_offer,
)
from tr_price_scraper import TRPriceScraper
from vatan_sitemap_source import BOT, MAX_ROBOTS_BYTES, ROBOTS_URL, VatanSitemapSource


PROFILES = (
    ('Apple iPhone 17', 'apple-iphone-17', 8, 256,
     'https://www.vatanbilgisayar.com/iphone-17-256-gb-akilli-telefon-beyaz.html'),
    ('Xiaomi 17T', 'xiaomi-17t', 12, 256,
     'https://www.vatanbilgisayar.com/xiaomi-17t-12-256gb-akilli-telefon-koyu-mor.html'),
    ('Samsung Galaxy S26', 'samsung-galaxy-s26', 12, 256,
     'https://www.vatanbilgisayar.com/samsung-galaxy-s26-12-256-gb-akilli-telefon-kobalt-mor.html'),
)
SOLVER_URL = 'http://localhost:8191/v1'
MAX_SOLVER_BYTES = 8 * 1024 * 1024
PURCHASABLE = {'in_stock', 'limited'}
OUTPUT_DIR = Path(os.environ.get('VATAN_AUDIT_OUTPUT_DIR', gettempdir()))
REPORT_PATH = OUTPUT_DIR / 'vatan-pdp-access.json'
SUMMARY_PATH = OUTPUT_DIR / 'vatan-pdp-access.md'


def _robots_rules(fetch):
    source = VatanSitemapSource(fetch=fetch, sleep=lambda _: None)
    raw = source._read(ROBOTS_URL, MAX_ROBOTS_BYTES)
    text = raw.decode('utf-8-sig', errors='strict')
    if (re.search(r'<(?:!doctype|html|head|body)\b', text, re.I)
            or not re.search(r'^\s*user-agent\s*:', text, re.I | re.M)):
        raise ValueError('robots_policy_unavailable')
    rules = RobotFileParser()
    rules.parse(text.splitlines())
    delay = float(rules.crawl_delay(BOT) or 0)
    rate = rules.request_rate(BOT)
    if rate:
        delay = max(delay, rate.seconds / max(1, rate.requests))
    if delay > 30:
        raise ValueError('crawl_delay_exceeds_budget')
    return rules, delay


def _offer_fields(offer):
    return {key: offer[key] for key in ('merchant', 'price', 'currency',
                                       'availability', 'source_title', 'url')}


def _browser_get_offer(name, url, specs, post):
    result = {'status': 'unavailable', 'http_status': None, 'offer': None}
    try:
        response = post(SOLVER_URL, json={
            'cmd': 'request.get', 'url': url, 'maxTimeout': 45000,
        }, timeout=60, stream=True)
        try:
            if response.status_code != 200:
                return {**result, 'status': f'solver_http_{response.status_code}'}
            data = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                data.extend(chunk)
                if len(data) > MAX_SOLVER_BYTES:
                    return {**result, 'status': 'solver_response_too_large'}
        finally:
            response.close()
        payload = json.loads(data)
        if not isinstance(payload, dict) or payload.get('status') != 'ok':
            return {**result, 'status': 'solver_unavailable'}
        solution = payload.get('solution')
        if not isinstance(solution, dict):
            return {**result, 'status': 'invalid_solution'}
        status = solution.get('status')
        result['http_status'] = status if isinstance(status, int) else None
        if status != 200:
            return {**result, 'status': f'site_http_{status}'}
        if solution.get('url') != url:
            return {**result, 'status': 'unexpected_final_url'}
        html = solution.get('response')
        if not isinstance(html, str) or len(html.encode('utf-8')) > _MAX_BODY_BYTES:
            return {**result, 'status': 'invalid_or_oversized_html'}
        soup = BeautifulSoup(html, 'html.parser')
        if _challenge_page(soup):
            return {**result, 'status': 'challenge_page'}
        offer = _product_offer(soup, url)
        matcher = TRPriceScraper()
        if (not offer or not matcher.is_strict_match(name, offer['source_title'], specs)
                or not matcher.has_observed_offer_evidence(name, offer, specs)):
            return {**result, 'status': 'no_exact_product_offer'}
        if offer['availability'] not in PURCHASABLE:
            return {**result, 'status': 'exact_offer_out_of_stock'}
        return {**result, 'status': 'verified', 'offer': _offer_fields(offer)}
    except (requests.RequestException, ValueError, TypeError, UnicodeError):
        return {**result, 'status': 'probe_error'}


def run_audit(robots_fetch=None, reader_factory=None, post=None, sleep=time.sleep):
    robots_fetch = robots_fetch or requests.get
    reader_factory = reader_factory or RetailerOfferRefresher
    post = post or requests.post
    rows = []
    report = {'purpose': 'read_only_exact_pdp_runner_probe',
              'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
              'robots_status': 'unavailable', 'products': rows}
    try:
        rules, delay = _robots_rules(robots_fetch)
        report['robots_status'] = 'ready'
    except (requests.RequestException, ValueError, UnicodeError) as error:
        report['robots_status'] = str(error) if isinstance(error, ValueError) else type(error).__name__
        rules, delay = None, 0
    for index, (name, slug, ram, storage, url) in enumerate(PROFILES):
        row = {'slug': slug, 'name': name, 'url': url,
               'normal_get': {'status': 'not_attempted', 'http_status': None, 'offer': None},
               'browser_get': {'status': 'not_attempted', 'http_status': None, 'offer': None}}
        rows.append(row)
        if rules is None or not rules.can_fetch(BOT, url):
            row['normal_get']['status'] = 'robots_unavailable_or_disallowed'
            continue
        if index and delay:
            sleep(delay)
        specs = {'ram_gb': ram, 'storage_gb': storage, 'network_generation': '5g'}
        try:
            reader = reader_factory()
            offers = reader.refresh_existing_offer_targets(name, [{'source_url': url}], specs)
            attempts = reader.target_report(name, [{'source_url': url}], specs)
            detail = attempts[0] if attempts else {}
            purchasable = next((offer for offer in offers
                                if offer['availability'] in PURCHASABLE), None)
            row['normal_get'] = {
                'status': detail.get('reason', 'not_attempted') if purchasable or not offers
                          else 'exact_offer_out_of_stock',
                'http_status': detail.get('http_status'),
                'offer': _offer_fields(purchasable) if purchasable else None,
            }
        except Exception as error:
            row['normal_get']['status'] = type(error).__name__
            offers = []
        if not offers:
            if delay:
                sleep(delay)
            row['browser_get'] = _browser_get_offer(name, url, specs, post)
    report['verified_normal'] = sum(bool(r['normal_get']['offer']) for r in rows)
    report['verified_browser'] = sum(bool(r['browser_get']['offer']) for r in rows)
    return report


def format_summary(report):
    lines = ['## Vatan exact PDP runner access (read only)',
             f"Robots: {report['robots_status']}; normal GET {report['verified_normal']}/3; browser GET {report['verified_browser']}/3."]
    for row in report['products']:
        direct = row['normal_get']
        browser = row['browser_get']
        lines.append(f"- {row['slug']}: normal {direct['status']} HTTP {direct['http_status']}; "
                     f"browser {browser['status']} HTTP {browser['http_status']}.")
    lines.append('No catalog or price writes. Fixed URLs are audit inputs only.')
    return '\n'.join(lines) + '\n'


def main():
    report = run_audit()
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    SUMMARY_PATH.write_text(format_summary(report))
    print(format_summary(report), end='')
    return 0 if report['verified_normal'] + report['verified_browser'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
