"""Bounded daily discovery. No placeholders, quotas, rumors or security bypasses.

Only the atomic server endpoint publishes. Search/aggregator rows are discovery
hints; a newly fetched, exact retailer Product/Offer is mandatory.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import unicodedata
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from main import KimovilScraper
from source_ingestion import SourceIngestionClient, SourceIngestionError
from tr_price_scraper import TRPriceScraper
from retailer_offer_refresh import RetailerOfferRefresher, _safe_url
from update_prices import PriceUpdater
from product_images import R2ProductImageStore
from manufacturer_specs import vivo_spec_links, parse_vivo_specifications, fill_missing_records, missing_critical_facts, vivo_product_images

CONTRACT = 'catalog-discovery-v1'
MAX_ATTEMPTS = 8
MAX_PUBLICATIONS = 2
MAX_BODY = 5 * 1024 * 1024
LISTINGS = {
    'apple': 'https://www.apple.com/tr/iphone/',
    'samsung': 'https://www.samsung.com/tr/smartphones/',
    'xiaomi': 'https://www.mi.com/tr/mobile/',
    'vivo': 'https://www.vivo.com/tr/products',
}
USER_AGENT = 'TeknoSkorCatalogDiscovery/1.0 (+https://teknoskor.com/about)'


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def load_previous_state(path):
    """Rotation is advisory; a missing/corrupt cache must not stop discovery."""
    try:
        if not path.is_file() or path.stat().st_size > 100_000:
            return {}
        raw = json.loads(path.read_text(encoding='utf-8'))
        attempted = raw.get('attempted') if isinstance(raw, dict) else None
        if not isinstance(attempted, dict) or len(attempted) > 500:
            return {}
        valid = {}
        for slug, timestamp in attempted.items():
            if not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug) or len(slug) > 160 or not isinstance(timestamp, str):
                continue
            try:
                date = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                if date.tzinfo is None or (date - datetime.now(timezone.utc)).total_seconds() > 300:
                    continue
                valid[slug] = date.astimezone(timezone.utc).isoformat()
            except ValueError:
                continue
        return {'attempted': valid}
    except (OSError, ValueError):
        return {}


def write_json_checkpoint(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def slugify(name):
    value = unicodedata.normalize('NFKD', name.replace('+', ' plus ')).encode('ascii', 'ignore').decode().lower()
    return re.sub('[^a-z0-9]+', '-', value).strip('-')


def safe_product_url(brand, raw):
    try:
        url = urlparse(raw)
        hosts = {'apple': ('apple.com', 'www.apple.com'),
                 'samsung': ('samsung.com', 'www.samsung.com'),
                 'xiaomi': ('mi.com', 'www.mi.com'), 'poco': ('mi.com', 'www.mi.com'),
                 'vivo': ('vivo.com', 'www.vivo.com')}
        if (url.scheme != 'https' or url.hostname not in hosts.get(brand, ()) or
                url.port is not None or url.username is not None or url.password is not None or
                url.query or url.fragment or re.search(r'[\x00-\x20\\]', raw)):
            return False
        patterns = {
            'apple': r'/tr/iphone-[a-z0-9-]+/(?:specs/)?',
            'samsung': r'/tr/smartphones/galaxy-[a-z0-9-]+/(?:[a-z0-9-]+/)?',
            'xiaomi': r'/tr/product/(?:xiaomi|redmi|poco)-[a-z0-9-]+/(?:specs/)?',
            'poco': r'/tr/product/poco-[a-z0-9-]+/(?:specs/)?',
            'vivo': r'/tr/products/[a-z0-9-]+(?:/param)?/?',
        }
        return bool(re.fullmatch(patterns[brand], url.path))
    except (ValueError, TypeError, KeyError):
        return False


class PublicReader:
    def __init__(self, session=None):
        # Never share the authenticated ingestion session with a source host.
        self.session = session or requests.Session()
        self.blocked = set()
        self.last_errors = {}

    def read(self, url):
        host = urlparse(url).hostname
        if host in self.blocked:
            raise ValueError('source_blocked_for_run')
        with self.session.get(url, timeout=(10, 25), allow_redirects=False, stream=True,
                              headers={'User-Agent': USER_AGENT, 'Accept': 'text/html,application/json'}) as response:
            if response.status_code in (403, 429):
                self.blocked.add(host)
            if response.status_code != 200:
                self.last_errors[host] = f'source_http_{response.status_code}'
                raise ValueError(f'source_http_{response.status_code}')
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > MAX_BODY:
                    raise ValueError('source_too_large')
                chunks.append(chunk)
            text = b''.join(chunks).decode(response.encoding or 'utf-8', errors='replace')
        if re.search(r'just a moment|verify you are human|<title>[^<]*captcha', text[:20000], re.I):
            self.blocked.add(host)
            self.last_errors[host] = 'source_challenge'
            raise ValueError('source_challenge')
        return text


def candidate_name(brand, text):
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'\s+(?:yeni|new)$', '', text, flags=re.I)
    text = re.sub(r'^(?:Keşfet|Discover)\s+', '', text, flags=re.I)
    if not 3 <= len(text) <= 100 or re.search(r'\b(?:GB|TB|RAM|watch|buds|headphone|tablet|aksesuar|karşılaştır|satın|buy|compare)\b', text, re.I):
        return None
    if brand == 'apple' and re.fullmatch(r'iPhone [A-Za-z0-9+ -]+', text, re.I):
        text = f'Apple {text}'
    elif brand == 'samsung' and re.fullmatch(r'(?:Samsung )?Galaxy [A-Za-z0-9+ -]+', text, re.I):
        text = text if text.lower().startswith('samsung ') else f'Samsung {text}'
    elif brand == 'xiaomi' and re.fullmatch(r'(?:Xiaomi|Redmi|POCO) [A-Za-z0-9+ -]+', text, re.I):
        text = f'Xiaomi {text}' if text.lower().startswith('redmi ') else text
    elif brand == 'vivo' and re.fullmatch(r'(?:vivo )?[VXY][0-9][A-Za-z0-9+ -]*', text, re.I):
        text = text if text.lower().startswith('vivo ') else f'vivo {text}'
    else:
        return None
    if not re.search(r'\d', text) and text not in ('Apple iPhone Air', 'Apple iPhone Duo'):
        return None
    return text


def discover_candidates(brand, html, listing_url):
    soup = BeautifulSoup(html, 'html.parser')
    found = {}
    for anchor in soup.select('a[href]'):
        url = urljoin(listing_url, anchor['href'])
        # Fragments/query/locale redirects are not product proof.
        if not safe_product_url(brand, url):
            continue
        names = [anchor.get_text(' ', strip=True), anchor.get('aria-label', '')]
        names += [image.get('alt', '') for image in anchor.select('img[alt]')]
        for label in names:
            name = candidate_name(brand, label)
            if not name:
                continue
            actual_brand = 'poco' if name.lower().startswith('poco ') else brand
            slug = slugify(name)
            found.setdefault(slug, {'name': name, 'slug': slug, 'brandSlug': actual_brand,
                                    'evidenceUrl': url, 'segment': 'new_release' if re.search(r'\b(?:yeni|new)\b', label, re.I) else 'popular_missing'})
            break
    return list(found.values())[:100]


def product_names(html, page_url=None):
    soup = BeautifulSoup(html, 'html.parser')
    names = [tag.get_text(' ', strip=True) for tag in soup.select('h1')]
    canonical = soup.find('link', rel='canonical')
    title_proof = bool(page_url and vivo_spec_links(html, page_url))
    if page_url and canonical and canonical.get('href'):
        actual = urlparse(urljoin(page_url, canonical['href']))
        expected = urlparse(page_url)
        path = lambda url: re.sub(r'/(?:buy|specs|param)/?$', '', url.path).rstrip('/')
        if (actual.scheme == 'https' and actual.hostname == expected.hostname and
                not actual.username and not actual.password and not actual.port and not actual.query and
                path(actual) == path(expected)):
            title_proof = True
        else:
            title_proof = False
    if title_proof:
        titles = [soup.title.get_text(' ', strip=True)] if soup.title else []
        og = soup.find('meta', attrs={'property': 'og:title'})
        if og and og.get('content'):
            titles.append(og['content'])
        names += [re.split(r'\s+[–—-]\s+|\s*\|\s*', title)[0].strip() for title in titles]

    def visit(value, depth=0):
        if depth > 12:
            return
        if isinstance(value, list):
            for part in value:
                visit(part, depth + 1)
        elif isinstance(value, dict):
            types = value.get('@type', [])
            types = types if isinstance(types, list) else [types]
            if 'Product' in types and isinstance(value.get('name'), str):
                names.append(value['name'])
            visit(value.get('@graph'), depth + 1)
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            visit(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError):
            pass
    return names


def model_key(name):
    words = slugify(name).split('-')
    return ''.join(word for word in words if word not in {'apple', 'samsung', 'xiaomi', 'vivo', 'poco', 'galaxy'})


def may_duplicate(first, second):
    if model_key(first) == model_key(second):
        return True
    radio = lambda value: re.search(r'\b[45]g\b', value, re.I)
    return (not radio(first) or not radio(second)) and model_key(re.sub(r'\b[45]g\b', '', first, flags=re.I)) == model_key(re.sub(r'\b[45]g\b', '', second, flags=re.I))


class PublicSpecificationScraper(KimovilScraper):
    """Reuse only the existing parser; no challenge-solving transport in this job."""
    def __init__(self, reader, **kwargs):
        super().__init__(**kwargs)
        self.reader = reader

    def get_via_flaresolverr(self, url):
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.hostname != 'www.kimovil.com' or parsed.username or parsed.password or parsed.port:
            return None
        try:
            return self.reader.read(url)
        except (ValueError, requests.RequestException) as error:
            logging.warning('Public specification request unavailable: %s (%s)', parsed.hostname, type(error).__name__)
            return None


class DiscoveryImageStore(R2ProductImageStore):
    def upload(self, source_url, product_slug, index):
        parsed = urlparse(source_url)
        if (parsed.scheme != 'https' or parsed.hostname not in {'cdn-files.kimovil.com', 'de-gdpr-exstatic-vivofs.vivo.com'} or
                parsed.port or parsed.username or parsed.password):
            raise ValueError('Unapproved public product image source')
        return super().upload(source_url, product_slug, index)


def staged_attributes(records):
    # Existing parser emits top-level claims. Nested/prototype paths are not accepted locally.
    return {record['attribute_key']: record['value'] for record in records
            if not record['attribute_key'].startswith('/') and '.' not in record['attribute_key']}


def direct_offers(candidate, attributes, price_scraper, refresher):
    expected = PriceUpdater.get_expected_specs(attributes)
    hints = asyncio.run(price_scraper.get_best_prices(candidate['name'], expected))
    targets = [{'source_url': hint.get('url')} for hint in hints if _safe_url(hint.get('url'))]
    rows = refresher.refresh_existing_offer_targets(candidate['name'], targets, expected)
    # PDP, not card/aggregator evidence; no unknown stock or preorder publication.
    by_merchant = {}
    for row in rows:
        if row.get('availability') != 'in_stock' or not row.get('observed_at'):
            continue
        merchant = row['merchant']
        if merchant not in by_merchant or row['price'] < by_merchant[merchant]['price']:
            by_merchant[merchant] = row
    return [{'merchant_name': row['merchant'], 'price': float(row['price']), 'currency': 'TRY',
             'availability': 'in_stock', 'source_url': row['url'], 'source_title': row['source_title'],
             'observed_at': row['observed_at'], 'checked_at': row['observed_at']}
            for row in list(by_merchant.values())[:12]]


def submit(client, payload):
    response = client._request_with_retry('POST', f'{client.base_url}/api/ingestion/catalog-discovery',
                                          json=payload, authenticated=True)
    return client._json_body(response)


def run_discovery(*, client, reader, specs, price_scraper, refresher, publish=False,
                  previous=None, report=None, checkpoint=None):
    report = report if report is not None else {}
    report.update({'contract': CONTRACT, 'started_at': utc_now(), 'publish_enabled': publish,
                   'sources': [], 'candidates': [], 'created': 0})
    capability = client._json_body(client._request_with_retry('GET',
        f'{client.base_url}/api/ingestion/catalog-discovery', authenticated=True))
    if capability.get('contract') != CONTRACT:
        raise RuntimeError('Live discovery contract is not ready; no publication attempted')
    catalog = client.fetch_catalog()
    identities = {model_key(product.name) for product in catalog}
    existing_names = [product.name for product in catalog]
    slugs = {product.slug for product in catalog}
    candidates = {}
    for brand, url in LISTINGS.items():
        try:
            found = discover_candidates(brand, reader.read(url), url)
            report['sources'].append({'brand': brand, 'status': 'ok' if found else 'no_phone_links', 'discovered': len(found)})
            for item in found:
                if (item['slug'] not in slugs and model_key(item['name']) not in identities and
                        not any(may_duplicate(item['name'], name) for name in existing_names)):
                    candidates.setdefault(item['slug'], item)
        except Exception as error:
            # Exception messages may contain request diagnostics; reports keep only type/reason.
            report['sources'].append({'brand': brand, 'status': 'unavailable', 'reason': type(error).__name__})
    previous = previous or {}
    attempted = dict(previous.get('attempted', {}))
    ordered = sorted(candidates.values(), key=lambda item: (attempted.get(item['slug'], ''), item['slug']))
    report['new_candidates'] = len(ordered)
    report['attempted'] = {key: value for key, value in attempted.items() if key in candidates}
    if checkpoint:
        checkpoint(report)
    for candidate in ordered[:MAX_ATTEMPTS]:
        if report['created'] >= MAX_PUBLICATIONS:
            break
        slug = candidate['slug']
        attempted[slug] = utc_now()
        report['attempted'][slug] = attempted[slug]
        result = {'slug': slug, 'name': candidate['name'], 'evidence_url': candidate['evidenceUrl']}
        report['candidates'].append(result)
        try:
            overview_html = reader.read(candidate['evidenceUrl'])
            names = product_names(overview_html, candidate['evidenceUrl'])
            if not any(model_key(name) == model_key(candidate['name']) for name in names):
                result.update(status='held', reasons=['manufacturer_identity_not_confirmed'], observed_names=names[:8]); continue
            records, images = [], []
            source_url = specs.search_product_on_kimovil(candidate['name'])
            parsed_specs = source_url and specs.scrape_product_details(source_url, product_slug=slug,
                    expected_name=candidate['name'], seed_physical_attributes=True,
                    existing_attributes={}, existing_images=[], record_sink=records, image_sink=images)
            result['spec_database_status'] = 'parsed' if parsed_specs else 'unavailable'
            conflicts = []
            official_images = []
            for official_url in vivo_spec_links(overview_html, candidate['evidenceUrl'])[:1]:
                try:
                    official_html = reader.read(official_url)
                    official = parse_vivo_specifications(official_html, official_url, candidate, utc_now())
                    records, conflicts = fill_missing_records(records, official)
                    official_images = vivo_product_images(official_html)
                    result['manufacturer_spec_url'] = official_url
                except (ValueError, requests.RequestException) as error:
                    result['manufacturer_spec_error'] = type(error).__name__
            # Retain useful partial facts in the job artifact, not public pages.
            result['collected_sources'] = records
            result['official_image_candidates'] = official_images
            result['missing_fields'] = missing_critical_facts(records)
            if conflicts:
                result.update(status='held', reasons=['manufacturer_spec_conflict'], conflicting_fields=conflicts); continue
            if result['missing_fields']:
                result.update(status='held', reasons=['missing_verified_specifications']); continue
            if not images and official_images:
                uploaded = []
                for index, image_url in enumerate(official_images):
                    try:
                        uploaded.append(specs.image_store.upload(image_url, slug, index))
                    except Exception as error:
                        result['image_error'] = type(error).__name__
                if uploaded:
                    images.append({'product_slug': slug, 'urls': uploaded})
            if not images:
                result.update(status='held', reasons=['first_party_product_image_unavailable']); continue
            attributes = staged_attributes(records)
            offers = direct_offers(candidate, attributes, price_scraper, refresher)
            if not offers:
                result.update(status='held', reasons=['no_current_in_stock_exact_sku_pdp_offer']); continue
            payload = {'mode': 'validate', 'candidate': candidate, 'sources': records,
                       'images': images[0]['urls'] if images else [], 'offers': offers}
            validation = submit(client, payload)
            if validation.get('eligible') is not True:
                result.update(status='held', reasons=validation.get('issues', ['server_quality_rejected'])); continue
            if not publish:
                result.update(status='ready_not_published', merchants=len(offers)); continue
            payload['mode'] = 'publish'
            response = submit(client, payload)
            if response.get('committed') is not True or response.get('slug') != slug:
                raise RuntimeError('Publication was not confirmed')
            result.update(status=response.get('outcome'), merchants=len(offers))
            if response.get('outcome') == 'created':
                report['created'] += 1
        except SourceIngestionError as error:
            body = error.response_body
            if body.get('committed') is True:
                result.update(status='committed_needs_attention', reasons=['post_commit_scoring_failed'])
                if body.get('outcome') == 'created':
                    report['created'] += 1
            else:
                result.update(status='held', reasons=body.get('issues', [f'server_http_{error.status_code}']))
                if body.get('missing_fields'):
                    result['missing_fields'] = body['missing_fields']
        except Exception as error:
            result.update(status='held', reasons=[type(error).__name__])
        finally:
            if checkpoint:
                checkpoint(report)
    report['finished_at'] = utc_now()
    if isinstance(getattr(reader, 'last_errors', None), dict):
        report['source_errors'] = reader.last_errors
    report['attempted'] = {key: value for key, value in attempted.items() if key in candidates}
    if not any(item['status'] == 'ok' for item in report['sources']):
        raise RuntimeError('All manufacturer discovery sources unavailable; see report')
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--report', default='daily-discovery-report.json')
    parser.add_argument('--state', default='.discovery-state/state.json')
    args = parser.parse_args()
    report = {}
    code = 0
    def checkpoint(value):
        path = Path(args.report)
        write_json_checkpoint(path, value)
        if value.get('attempted') is not None:
            state_path = Path(args.state)
            write_json_checkpoint(state_path, {'attempted': value['attempted']})
    try:
        client = SourceIngestionClient.from_env()
        reader = PublicReader()
        specs = PublicSpecificationScraper(reader, ingestion_client=client, require_images=False,
            image_store=DiscoveryImageStore(required=True))
        state_path = Path(args.state)
        previous = load_previous_state(state_path)
        run_discovery(client=client, reader=reader, specs=specs, price_scraper=TRPriceScraper(),
                      refresher=RetailerOfferRefresher(), publish=args.publish, previous=previous, report=report,
                      checkpoint=checkpoint)
    except Exception as error:
        report['fatal'] = type(error).__name__
        code = 1
    checkpoint(report)
    print(json.dumps({key: report[key] for key in ('created', 'new_candidates', 'fatal') if key in report}))
    return code


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
