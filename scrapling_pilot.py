"""Read-only A/B pilot on the reviewed eight Android SKUs.

No credentials, ingestion client instance, database, price/score writes,
adaptive matching, proxy, challenge solving, cart or login interaction.
Only approved PDP targets are tested. Both lanes use the production parser,
reviewed-SKU checks and outlier guard; extra = distinct (model, merchant),
not extra URLs. Robots/access failures block both lanes, never trigger bypass.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import socket
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from price_sanity import filter_price_outliers
from recover_reviewed_offers import load_profiles, rejection_reason
from retailer_offer_refresh import _challenge_page, _product_offer, _safe_url, _same_resource


BOT = 'TeknoskorPriceBot'
USER_AGENT = BOT + '/1.0 (+https://teknoskor.com)'
REPORT = Path('scrapling-pilot-report.json')
SUMMARY = Path('scrapling-pilot-summary.md')
MAX_BYTES = 5 * 1024 * 1024
LANES = ('requests', 'scrapling_http', 'scrapling_browser')
PROHIBITED = re.compile(r'captcha|challenge|turnstile|recaptcha|login|logout|signin|checkout|'
                        r'add.?to.?cart|basket|sepet|account|payment|token|password|secret|session|authorization', re.I)


def utcnow():
    return datetime.now(timezone.utc)


@dataclass
class Document:
    url: str
    status: int
    text: str
    observed_at: str
    elapsed_ms: int = 0


@lru_cache(maxsize=128)
def public_host(host):
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(a[4][0]).is_global for a in addresses)
    except (OSError, ValueError):
        return False


def plain_get(url):
    """Bounded public GET. No cookies, .netrc credentials, proxy or redirects."""
    p = urlparse(url)
    if (p.scheme != 'https' or p.username or p.password or p.port not in (None, 443)
            or not public_host(p.hostname)):
        raise ValueError('Unsafe public destination')
    started = time.monotonic()
    with requests.Session() as session:
        session.trust_env = False
        with session.get(url, timeout=(5, 20), allow_redirects=False, stream=True,
                         headers={'User-Agent': USER_AGENT, 'Accept-Language': 'tr-TR,tr;q=0.9'}) as response:
            body = bytearray()
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise ValueError('Oversized response')
            return Document(response.url, response.status_code,
                            body.decode(response.encoding or 'utf-8', errors='replace'),
                            utcnow().isoformat(), round((time.monotonic() - started) * 1000))


class Policy:
    def __init__(self, get=plain_get, sleep=time.sleep, clock=time.monotonic):
        self.get, self.sleep, self.clock = get, sleep, clock
        self.rules, self.denied, self.last, self.counts = {}, {}, {}, Counter()

    def prepare(self, url):
        if not _safe_url(url):
            return 'unapproved_target'
        origin = 'https://' + urlparse(url).netloc
        if origin in self.denied:
            return self.denied[origin]
        if origin not in self.rules:
            try:
                doc = self.get(origin + '/robots.txt')
                if (doc.status != 200 or re.search(r'<(?:!doctype|html|body|head)\b', doc.text, re.I)
                        or not re.search(r'^\s*user-agent\s*:', doc.text, re.I | re.M)):
                    self.denied[origin] = f'robots_unavailable_http_{doc.status}'
                    return self.denied[origin]
                rules = RobotFileParser()
                rules.parse(doc.text.splitlines())
                self.rules[origin] = rules
                self.last[origin] = self.clock()
            except (requests.RequestException, ValueError, OSError):
                self.denied[origin] = 'robots_transport_error'
                return self.denied[origin]
        rules = self.rules[origin]
        if not rules.can_fetch(BOT, url):
            return 'robots_disallowed'
        rate = rules.request_rate(BOT)
        delay = max(1.0, rules.crawl_delay(BOT) or 0, rate.seconds / max(1, rate.requests) if rate else 0)
        if delay > 30:
            return 'crawl_delay_exceeds_budget'
        if self.counts[origin] >= 24:
            return 'host_request_budget_reached'
        self.sleep(max(0, delay - (self.clock() - self.last.get(origin, -1000))))
        self.last[origin] = self.clock()
        self.counts[origin] += 1
        return None

    def observe(self, doc):
        origin = 'https://' + urlparse(doc.url).netloc
        if doc.status in (401, 403, 429):
            self.denied[origin] = f'access_or_rate_limit_http_{doc.status}'
        elif doc.status >= 500:
            self.denied[origin] = f'retailer_unavailable_http_{doc.status}'
        elif _challenge_page(BeautifulSoup(doc.text, 'html.parser')):
            self.denied[origin] = 'challenge_page'


def assess(profile, target, doc, now=None):
    row = {'url': target, 'http_status': doc.status, 'elapsed_ms': doc.elapsed_ms,
           'body_sha256': hashlib.sha256(doc.text.encode()).hexdigest(),
           'observed_at': doc.observed_at}
    if doc.status != 200:
        return {**row, 'reason': f'http_{doc.status}'}
    if not _same_resource(doc.url, target):
        return {**row, 'reason': 'changed_destination_or_variant'}
    if len(doc.text.encode()) > MAX_BYTES:
        return {**row, 'reason': 'oversized_response'}
    soup = BeautifulSoup(doc.text, 'html.parser')
    if _challenge_page(soup):
        return {**row, 'reason': 'challenge_page'}
    offer = _product_offer(soup, doc.url)
    if not offer:
        return {**row, 'reason': 'no_exact_product_offer'}
    # Parsing saved HTML must not manufacture a newer observation timestamp.
    offer['observed_at'] = doc.observed_at
    reason = rejection_reason(profile, offer, now or utcnow())
    return {**row, 'reason': reason or 'eligible', 'offer': offer}


def catalog_profile_matches(profile, product):
    return (product.get('id') == profile['id'] and product.get('slug') == profile['slug']
            and product.get('name') == profile['name']
            and all(type(product.get('attributes', {}).get(k)) in (int, float)
                    and product['attributes'][k] == profile[k] for k in ('ram_gb', 'storage_gb')))


def browser_request_allowed(url, method, kind, target, rules, host_check=public_host):
    """No clicks, POSTs, changed PDPs, private addresses or account/cart APIs."""
    try:
        p, base = urlparse(url), urlparse(target)
        if (method != 'GET' or p.scheme != 'https' or p.username or p.password
                or p.port not in (None, 443) or not p.hostname or not host_check(p.hostname)
                or PROHIBITED.search(p.path + '?' + p.query)):
            return False
        if kind == 'document':
            return url == target and rules.can_fetch(BOT, url)
        if kind in ('script', 'stylesheet'):
            # Public static CDNs may load; no images, media, trackers or frames.
            return p.netloc != base.netloc or rules.can_fetch(BOT, url)
        return (kind in ('xhr', 'fetch') and p.netloc == base.netloc
                and rules.can_fetch(BOT, url))
    except (ValueError, TypeError):
        return False


class ScraplingReader:
    def http(self, url):
        from scrapling.fetchers import Fetcher
        started = time.monotonic()
        response = Fetcher.get(url, follow_redirects=False, timeout=20, retries=1,
                               impersonate=None, stealthy_headers=False,
                               headers={'User-Agent': USER_AGENT, 'Accept-Language': 'tr-TR,tr;q=0.9'},
                               selector_config={'adaptive': False})
        return Document(response.url, response.status, response.body.decode('utf-8', errors='replace'),
                        utcnow().isoformat(), round((time.monotonic() - started) * 1000))

    def browser(self, url, rules):
        from scrapling.fetchers import StealthyFetcher
        started, events = time.monotonic(), Counter()
        def setup(page):
            def intercept(route):
                request = route.request
                events['requests_seen'] += 1
                try:
                    allowed = (events['requests_seen'] <= 100 and browser_request_allowed(
                        request.url, request.method, request.resource_type, url, rules))
                    if allowed:
                        events['get_allowed'] += 1
                        route.continue_()
                    else:
                        events['blocked'] += 1
                        route.abort()
                except Exception:
                    events['guard_errors'] += 1
                    route.abort()
            try:
                page.context.route('**/*', intercept)
                events['guard_installed'] = 1
            except Exception:
                # Scrapling catches setup exceptions: close before it can navigate.
                page.context.close()
                raise
        response = StealthyFetcher.fetch(
            url, headless=True, solve_cloudflare=False, google_search=False,
            useragent=USER_AGENT, locale='tr-TR', timeout=25000, retries=1,
            network_idle=False, load_dom=True, wait=2000, page_setup=setup,
            additional_args={'service_workers': 'block', 'accept_downloads': False},
            selector_config={'adaptive': False})
        if events['guard_installed'] != 1 or events['guard_errors']:
            raise RuntimeError('Browser request guard did not complete')
        return (Document(response.url, response.status, response.body.decode('utf-8', errors='replace'),
                         utcnow().isoformat(), round((time.monotonic() - started) * 1000)), dict(events))


def summarize_product(profile, attempts):
    eligible = {lane: [] for lane in LANES}
    for lane in LANES:
        offers = [r[lane]['offer'] for r in attempts if r.get(lane, {}).get('reason') == 'eligible']
        # The same merchant's colors must not inflate outlier voting or coverage.
        by_merchant = {}
        for offer in offers:
            previous = by_merchant.get(offer['merchant'])
            if previous is None or offer['price'] < previous['price']:
                by_merchant[offer['merchant']] = offer
        eligible[lane] = filter_price_outliers(list(by_merchant.values()))
        for row in attempts:
            result = row.get(lane, {})
            if result.get('reason') == 'eligible' and result['offer'] not in eligible[lane]:
                result['reason'] = ('duplicate_merchant' if by_merchant[result['offer']['merchant']] != result['offer']
                                    else 'price_outlier')
    baseline = {o['merchant'] for o in eligible['requests']}
    # Count only offers also surviving a combined cohort check. Retain denials:
    # a conflicting result for the exact same URL never becomes an increment.
    candidates = {o['merchant']: o for o in eligible['requests']}
    conflicts = []
    for row in attempts:
        before = row.get('requests', {})
        observed = [row[lane]['offer'] for lane in LANES if row.get(lane, {}).get('offer')]
        if len({(o['price'], o['availability'], o['source_title']) for o in observed}) > 1:
            conflicts.append({'url': row['url'], 'reason': 'paired_observation_conflict'})
            for offer in observed:
                candidates.pop(offer['merchant'], None)
            continue
        for lane in LANES[1:]:
            result = row.get(lane, {})
            offer = result.get('offer')
            if result.get('reason') != 'eligible':
                continue
            old = before.get('offer')
            if old and (old['price'] != offer['price'] or old['availability'] != offer['availability']
                        or old['source_title'] != offer['source_title']):
                conflicts.append({'url': row['url'], 'lane': lane, 'reason': 'paired_observation_conflict'})
                continue
            if before.get('reason') not in ('eligible', 'duplicate_merchant', 'no_exact_product_offer'):
                continue
            candidates.setdefault(offer['merchant'], offer)
    combined = filter_price_outliers(list(candidates.values()))
    merchants = {o['merchant'] for o in combined}
    return {'slug': profile['slug'], 'name': profile['name'],
            'variant': {k: profile[k] for k in ('ram_gb', 'storage_gb', 'network_generation')},
            'baseline_merchants': sorted(baseline), 'combined_merchants': sorted(merchants),
            'additional_merchants': sorted(merchants - baseline),
            'baseline_pair': len(baseline) >= 2, 'combined_pair': len(merchants) >= 2,
            'paired_conflicts': conflicts, 'attempts': attempts}


def run(profiles, policy, reader, catalog_get=plain_get):
    report = {'mode': 'read_only_pilot', 'started_at': utcnow().isoformat(),
              'scrapling_version': '0.5', 'submitted_offers': 0, 'database_writes': 0,
              'scope': 'reviewed PDPs only; no discovery or merchant approval changes',
              'products': [], 'operational_errors': []}
    for profile in profiles:
        try:
            catalog_doc = catalog_get(f"https://teknoskor.com/api/products/{profile['id']}")
            if catalog_doc.status != 200 or not catalog_profile_matches(profile, json.loads(catalog_doc.text)):
                raise ValueError('Public catalog identity/variant differs')
        except (requests.RequestException, ValueError, OSError):
            report['operational_errors'].append({'slug': profile['slug'], 'reason': 'catalog_not_verified'})
            report['products'].append({**summarize_product(profile, []), 'catalog_verified': False})
            continue
        attempts = []
        for target in profile['targets']:
            row = {'url': target}
            attempts.append(row)
            for lane in LANES:
                blocked = policy.prepare(target)
                if blocked:
                    row[lane] = {'reason': blocked}
                    continue
                # Denial evidence (wrong SKU, stock, warranty, price) is not a
                # reason to keep trying other readers until one accepts it.
                if lane != 'requests' and row['requests'].get('reason') not in ('eligible', 'no_exact_product_offer'):
                    row[lane] = {'reason': 'baseline_denial_preserved'}
                    continue
                try:
                    if lane == 'requests':
                        doc = policy.get(target)
                    elif lane == 'scrapling_http':
                        doc = reader.http(target)
                    else:
                        origin = 'https://' + urlparse(target).netloc
                        doc, events = reader.browser(target, policy.rules[origin])
                    policy.observe(doc)
                    row[lane] = assess(profile, target, doc)
                    if lane == 'scrapling_browser':
                        row[lane]['request_guard'] = events
                except Exception as error:
                    # Exception messages can contain headers; only class is saved.
                    row[lane] = {'reason': 'reader_error', 'error_type': type(error).__name__}
                    report['operational_errors'].append({'slug': profile['slug'], 'lane': lane,
                                                         'reason': type(error).__name__})
                    if lane == 'requests':
                        policy.denied['https://' + urlparse(target).netloc] = 'baseline_transport_error'
            print(profile['slug'], json.dumps({lane: row[lane]['reason'] for lane in LANES}), flush=True)
        report['products'].append({**summarize_product(profile, attempts), 'catalog_verified': True})
    products = report['products']
    report['summary'] = {
        'models': len(products),
        'catalog_verified_models': sum(p['catalog_verified'] for p in products),
        'baseline_model_merchant_pairs': sum(len(p['baseline_merchants']) for p in products),
        'combined_model_merchant_pairs': sum(len(p['combined_merchants']) for p in products),
        'additional_model_merchant_pairs': sum(len(p['additional_merchants']) for p in products),
        'baseline_models_with_two_merchants': sum(p['baseline_pair'] for p in products),
        'combined_models_with_two_merchants': sum(p['combined_pair'] for p in products),
        'new_models_with_two_merchants': sum(p['combined_pair'] and not p['baseline_pair'] for p in products),
        'reasons_by_lane': {lane: dict(Counter(r[lane]['reason'] for p in products for r in p['attempts'])) for lane in LANES},
        'attempted_by_lane': {lane: sum('http_status' in r[lane] or r[lane]['reason'] == 'reader_error'
                                       for p in products for r in p['attempts']) for lane in LANES},
    }
    if not any(report['summary']['attempted_by_lane'][lane] for lane in LANES[1:]):
        report['comparison_status'] = 'not_evaluated_no_policy_eligible_targets'
    else:
        report['comparison_status'] = 'evaluated_with_reported_coverage_limits'
    report['finished_at'] = utcnow().isoformat()
    report['status'] = 'incomplete' if report['operational_errors'] else 'completed'
    return report


def markdown(report):
    s = report.get('summary', {})
    lines = ['# Scrapling Android pilot — no live writes', '',
             f"Status: {report['status']}. Submitted offers: 0. Database writes: 0.", '',
             'Same reviewed URLs and quality checks in both lanes; no cached prices or timestamps.', '',
             '| Model | Baseline merchants | Combined merchants | Additional merchants |',
             '|---|---|---|---|']
    for p in report['products']:
        lines.append(f"| {p['name']} | {', '.join(p['baseline_merchants']) or '—'} | "
                     f"{', '.join(p['combined_merchants']) or '—'} | {', '.join(p['additional_merchants']) or '—'} |")
    lines += ['', '## Totals', '', '```json', json.dumps(s, ensure_ascii=False, indent=2), '```', '',
              'A completed run is not proof of source improvement. Blocked/untested pages are not verified offers.',
              'Pilot counts are not authorization to ingest. Three repeat runs and evidence review precede adoption.']
    return '\n'.join(lines) + '\n'


def main():
    if any(os.getenv(key) for key in ('SCRAPER_INGESTION_SECRET', 'DB_PASSWORD', 'MYSQL_PASSWORD', 'TEKNOSKOR_API_KEY')):
        raise RuntimeError('Pilot refuses production credentials')
    from importlib.metadata import version
    if version('scrapling') != '0.5':
        raise RuntimeError('Pilot requires pinned Scrapling version')
    logging.getLogger('scrapling').setLevel(logging.ERROR)
    report = run(load_profiles(), Policy(), ScraplingReader())
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    SUMMARY.write_text(markdown(report))
    print(json.dumps(report['summary'], ensure_ascii=False, indent=2))
    return 1 if report['status'] != 'completed' else 0


if __name__ == '__main__':
    raise SystemExit(main())
