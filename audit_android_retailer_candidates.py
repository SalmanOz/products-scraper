"""Research-only GET audit. Unknown shops NEVER enter offer ingestion here.

Runs the existing eight-model PDP audit, then probes curated candidate pages
with robots delays and ordinary HTTPS. Candidate schema is diagnostic evidence,
not approved price data. No DB/schema/IndexNow/browser or challenge work.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
import requests

from audit_public_retailer_sources import NoLegacyDiscovery
from recover_reviewed_offers import load_profiles
from retailer_offer_refresh import _schema_nodes, _type_is
from tr_price_scraper import TRPriceScraper
from update_prices import PriceUpdater


BATCH = Path(__file__).parent / 'offer_batches/android-source-candidates-2026-10-25.json'
REPORT = Path('android-source-candidate-report.json')
HOSTS = {'bireysel.turktelekom.com.tr', 'www.caglargsm.com', 'www.firsatkurdu.com',
         'www.kavastech.com.tr', 'www.evkur.com.tr', 'www.ayazgrupiletisim.com.tr',
         'www.dolgunteknoloji.com.tr', 'www.inbox.com.tr', 'www.migros.com.tr', 'www.mi.com'}
BOT = 'TeknoskorPriceBot'


def safe_candidate(url):
    try:
        p = urlparse(url)
        allowed_query = p.hostname == 'www.mi.com' and p.query == 'gid=4223707987'
        return bool(p.scheme == 'https' and p.hostname in HOSTS and p.username is None
                    and p.password is None and p.port is None and not p.fragment
                    and (not p.query or allowed_query) and p.path not in ('', '/')
                    and not re.search(r'[\x00-\x20\\]|%2f|%5c|%3f|%00', url, re.I))
    except (ValueError, TypeError):
        return False


def load_candidates():
    candidates = json.loads(BATCH.read_text())['candidates']
    slugs = {p['slug'] for p in load_profiles()}
    if (not candidates or len(candidates) > 24 or {c['slug'] for c in candidates} != slugs
            or len({c['url'] for c in candidates}) != len(candidates)
            or any(not safe_candidate(c['url']) or not c.get('review_status') for c in candidates)):
        raise ValueError('Invalid Android research-only candidate batch')
    return candidates


def document_evidence(html, url, profile):
    soup = BeautifulSoup(html, 'html.parser')
    headings = [h.get_text(' ', strip=True) for h in soup.select('h1')]
    title = headings[0] if len(headings) == 1 else ''
    products = [n for n in _schema_nodes(soup) if _type_is(n, 'Product')]
    # Observe microdata too; NEVER promote it to JSON-LD or bypass the normal
    # reader's exact offer/memory/stock/warranty and retailer approval checks.
    for element in soup.select('[itemscope][itemtype="https://schema.org/Product"]'):
        name = element.select_one(':scope > [itemprop="name"]')
        offers = element.select('[itemprop="offers"][itemscope]')
        if not name or len(offers) != 1:
            continue
        def field(key):
            values = offers[0].select(f'[itemprop="{key}"]')
            if len(values) != 1:
                return None
            node = values[0]
            return node.get('content') or node.get('href') or node.get_text(' ', strip=True)
        products.append({'@type': 'Product', 'name': name.get_text(' ', strip=True),
                         'offers': {k: field(k) for k in ('url', 'price', 'priceCurrency', 'availability')}})
    extracted = []
    for p in products[:3]:
        offers = p.get('offers')
        offers = offers if isinstance(offers, list) else [offers]
        extracted.append({'name': str(p.get('name', ''))[:500], 'url': p.get('url'),
                          'offers': [{k: o.get(k) for k in ('url', 'price', 'priceCurrency', 'availability', 'itemCondition')}
                                     for o in offers[:2] if isinstance(o, dict)]})
    matched = TRPriceScraper().has_observed_offer_evidence(profile['name'],
              {'source_title': title, 'url': url}, profile) if title else False
    return {'heading': title[:500], 'heading_matches_full_memory_and_radio': matched,
            'product_metadata': extracted,
            'candidate_is_approved_for_ingestion': False}


def probe_host(candidates, profiles, *, fetch=requests.get, sleep=time.sleep, clock=time.monotonic):
    """At most three GETs/source, no redirects, no change of request identity."""
    if not candidates or len(candidates) > 2 or any(not safe_candidate(c['url']) for c in candidates):
        raise ValueError('Invalid candidate request scope')
    origin = 'https://' + urlparse(candidates[0]['url']).netloc
    if any('https://' + urlparse(c['url']).netloc != origin for c in candidates):
        raise ValueError('Candidate host changed')
    rows = [{**c, 'candidate_is_approved_for_ingestion': False} for c in candidates]
    last = None
    def get(url, delay=0):
        nonlocal last
        if last is not None:
            sleep(max(0, delay - (clock() - last)))
        last = clock()
        response = fetch(url, timeout=(5, 15), allow_redirects=False,
                         headers={'User-Agent': BOT + '/1.0 (+https://teknoskor.com)',
                                  'Accept': 'text/plain' if url.endswith('/robots.txt') else 'text/html'})
        if len(response.text.encode('utf-8')) > 5 * 1024 * 1024:
            raise requests.RequestException('Oversized candidate response')
        return response
    try:
        robots = get(origin + '/robots.txt')
        valid = (robots.status_code == 200
                 and not re.search(r'<(?:!doctype\s+html|html|head|body)\b', robots.text, re.I)
                 and re.search(r'^\s*user-agent\s*:', robots.text, re.I | re.M))
        if not valid:
            return [{**r, 'reason': 'robots_policy_unavailable', 'robots_http_status': robots.status_code} for r in rows]
        rules = RobotFileParser(); rules.parse(robots.text.splitlines())
        delay = max(0.5, rules.crawl_delay(BOT) or 0)
        rate = rules.request_rate(BOT)
        if rate:
            delay = max(delay, rate.seconds / max(1, rate.requests))
        if delay > 60:
            return [{**r, 'reason': 'robots_delay_exceeds_audit_budget'} for r in rows]
        blocked = False
        for row in rows:
            url = row['url']
            if blocked:
                row['reason'] = 'host_circuit_open'; continue
            if not rules.can_fetch(BOT, url):
                row['reason'] = 'robots_disallowed'; continue
            try:
                response = get(url, delay)
                row['http_status'] = response.status_code
                row['observed_at'] = datetime.now(timezone.utc).isoformat()
                if response.status_code in (403, 429, 500, 502, 503, 504):
                    blocked = True
                if response.status_code != 200:
                    row['reason'] = 'http_error_or_redirect_not_followed'; continue
                soup = BeautifulSoup(response.text, 'html.parser')
                if re.search(r'captcha|just a moment|access denied|robot check',
                             ' '.join(e.get_text(' ', strip=True) for e in soup.select('title,h1')), re.I):
                    blocked = True; row['reason'] = 'challenge_page'; continue
                row.update(document_evidence(response.text, url, profiles[row['slug']]))
                row['reason'] = 'research_only_not_approved'
            except requests.RequestException:
                blocked = True; row['reason'] = 'transport_error'
    except requests.RequestException:
        return [{**r, 'reason': 'robots_transport_error'} for r in rows]
    return rows


async def audit():
    profiles = load_profiles()
    candidates = load_candidates()
    report = {'purpose': 'research_only_zero_writes', 'new_candidates_approved': 0, 'candidates': []}
    try:
        updater = PriceUpdater(price_scraper=NoLegacyDiscovery())
        try:
            await updater.run_update(dry_run=True, known_pdp_only=True,
                                     product_slugs=[p['slug'] for p in profiles], report_path='daily-price-report.json')
        except RuntimeError:
            if not updater.last_report or 'coverage' not in updater.last_report:
                raise
        report['existing_source_coverage'] = updater.last_report['coverage']
        groups = {}
        for c in candidates:
            groups.setdefault(urlparse(c['url']).netloc, []).append(c)
        by_slug = {p['slug']: p for p in profiles}
        with ThreadPoolExecutor(max_workers=4) as pool:
            for rows in pool.map(lambda group: probe_host(group, by_slug), groups.values()):
                report['candidates'].extend(rows)
        report['all_eight_models_have_two_verified_sources'] = updater.last_report['coverage']['with_two_merchants'] == 8
        print(json.dumps({'existing_source_coverage': report['existing_source_coverage'], 'candidates_probed': len(report['candidates']),
                          'new_candidates_approved': 0}, ensure_ascii=False))
    finally:
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    if not report['all_eight_models_have_two_verified_sources']:
        raise RuntimeError('Eight-model second-store gap is NOT closed; candidate prices were not published')


if __name__ == '__main__':
    asyncio.run(audit())
