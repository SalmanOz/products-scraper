"""Existing-model memory variants discovered by the scraper, never hand lists.

Retailer observations are hints for a separate capacity. Every selected
technical page and retailer listing is fetched again in this invocation.
Only the atomic server endpoint can create a variant; the parent is read-only.
"""
from copy import deepcopy
import html as html_lib
import json
import re
import unicodedata
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup

from daily_discovery import PublicReader, PublicSpecificationScraper, DiscoveryImageStore
from main import KimovilScraper
from source_ingestion import utc_observation_time
from spec_sections import extract_spec_sections, find_section_spec
from product_images import extract_source_image_urls
from selected_sku_evidence import selected_sku_evidence
from tr_price_scraper import TRPriceScraper
from update_prices import PriceUpdater
from reviewed_offer_targets import with_reviewed_radio

CONTRACT = 'automatic-memory-variant-v1'
MAX_VARIANTS = 12


def parent_specs(product):
    specs = with_reviewed_radio(product, PriceUpdater.get_expected_specs(product['attributes']))
    named_radio = TRPriceScraper._radio_generation(product['name'])
    if named_radio and specs.get('network_generation') not in (None, named_radio):
        raise ValueError('parent_radio_conflict')
    if named_radio:
        specs['network_generation'] = named_radio
    return specs


def variant_identity(parent, ram, storage):
    if (type(ram) is not int or type(storage) is not int or not 1 <= ram <= 64
            or not 32 <= storage <= 4096):
        raise ValueError('invalid_memory_variant')
    model = TRPriceScraper._catalog_model_name(parent['name'], parent['attributes'])
    if not model:
        raise ValueError('invalid_parent_model')
    name = f'{model} {ram}GB {storage}GB'
    folded = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode().lower()
    slug = re.sub(r'[^a-z0-9]+', '-', folded.replace('+', ' plus ')).strip('-')
    if len(name) > 120 or len(slug) > 160:
        raise ValueError('variant_identity_oversized')
    return {'name': name, 'slug': slug}


def technical_url(raw):
    if not isinstance(raw, str) or len(raw) > 2048 or re.search(r'[\x00-\x20\\]', raw):
        return False
    parsed = urlparse(raw)
    return (parsed.scheme == 'https' and parsed.netloc == 'www.kimovil.com'
            and not parsed.query and not parsed.fragment
            and bool(re.fullmatch(r'/en/where-to-buy-[a-z0-9-]+', parsed.path)))


def variant_selected_evidence(offer, name, specs):
    ordinary = selected_sku_evidence(offer)
    if ordinary is not None:
        if offer['platform'] == 'Trendyol':
            from trendyol_catalog_source import catalog_offer_matches as matches
        else:
            from n11_catalog_source import catalog_offer_matches as matches
        return ordinary if matches(offer, name, specs) else None
    from hepsiburada_catalog_source import catalog_offer_matches
    if (not isinstance(offer, dict) or not catalog_offer_matches(offer, name, specs)
            or offer.get('platform') != 'Hepsiburada' or not offer.get('gtin13')
            or offer.get('network_generation') not in {'4g', '5g'} or urlparse(offer['url']).query):
        return None
    return {'version': 'selected-retailer-sku-v1', 'platform': 'Hepsiburada',
            'product_id': offer['product_id'], 'sku': offer['sku'], 'listing_id': offer['listing_id'],
            'merchant_id': offer['merchant_id'], 'merchant_name': offer['merchant'], 'gtin13': offer['gtin13'],
            **{key: offer[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}}


def exact_capacity(raw):
    """A single source cell, not the first amount in a multi-variant table."""
    if not isinstance(raw, str) or re.search(r'[/+;]|\b(?:or|virtual|extended)\b', raw, re.I):
        return None
    amounts = re.findall(r'(?<![\w.,])([1-9]\d{0,3})\s*(GB|TB)\b', raw, re.I)
    if len(amounts) != 1 or not re.match(r'^\s*[1-9]\d{0,3}\s*(?:GB|TB)\b', raw, re.I):
        return None
    return int(amounts[0][0]) * (1024 if amounts[0][1].casefold() == 'tb' else 1)


def selected_technical_document(html, url, model, specs):
    if not technical_url(url) or not isinstance(html, str) or len(html.encode()) > 5 * 1024 * 1024:
        return None
    soup = BeautifulSoup(html, 'html.parser')
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    if re.search(r'just a moment|captcha|access denied|verify you are human', title, re.I):
        return None
    names = []
    for key in ('deviceki', 'devicecompare'):
        nodes = soup.select(f'meta[name="{key}"]')
        if len(nodes) > 1:
            return None
        if nodes:
            try:
                value = json.loads(html_lib.unescape(nodes[0].get('content', '')))
                if isinstance(value, dict) and isinstance(value.get('name'), str):
                    names.append(value['name'])
            except (ValueError, TypeError):
                return None
    # Kimovil may spell radio in metadata while the existing model omits it.
    # The independently read technical connectivity must still prove radio.
    core = lambda name: re.sub(r'\b[45]g\b', '', name, flags=re.I).strip()
    if not names or not all(KimovilScraper.is_product_name_match(core(model), core(name)) for name in names):
        return None
    if any(TRPriceScraper._radio_generation(name) not in (None, specs['network_generation']) for name in names):
        return None
    canonical = soup.select('link[rel="canonical"][href]')
    if len(canonical) != 1 or urljoin(url, canonical[0]['href']).rstrip('/') != url:
        return None
    sections = extract_spec_sections(soup)
    actual = {'ram_gb': exact_capacity(find_section_spec(sections, 'Hardware', 'Memory RAM', 'RAM')),
              'storage_gb': exact_capacity(find_section_spec(sections, 'Hardware', 'Capacity', 'Storage'))}
    if any(actual[key] != specs[key] for key in actual):
        return None
    connectivity = next((value for key, value in sections.items() if 'connect' in key.casefold()), {})
    radio = PriceUpdater.get_expected_specs({'Connectivity': connectivity}).get('network_generation')
    if radio != specs['network_generation']:
        return None
    return {'model_name': names[0], 'source_url': url, **actual,
            'observed_at': utc_observation_time()}


class TechnicalVariantReader:
    """Anonymous public GETs with a per-invocation bound and denial stop."""
    def __init__(self, reader=None, maximum_documents=48):
        self.reader = reader or PublicReader()
        self.maximum_documents = maximum_documents
        self.documents = 0
        self.base_documents = {}

    def read(self, url):
        if self.documents >= self.maximum_documents:
            raise ValueError('technical_document_budget_exhausted')
        self.documents += 1
        return self.reader.read(url)

    def locate(self, parent, specs):
        model = TRPriceScraper._catalog_model_name(parent['name'], parent['attributes'])
        if model not in self.base_documents:
            # The existing public autocomplete returns actual model URLs. No
            # region/capacity suffix is synthesized or searched in a URL grid.
            endpoint = ('https://www.kimovil.com/_json/autocomplete_devicemodels_joined.json'
                        '?device_type=0&name=' + quote(model, safe=''))
            raw = self.read(endpoint)
            if raw.lstrip().startswith('<'):
                raw = BeautifulSoup(raw, 'html.parser').get_text()
            data = json.loads(raw)
            candidates = []
            core = lambda text: re.sub(r'\b[45]g\b', '', text, flags=re.I).strip()
            for row in data.get('results', [])[:30]:
                if not isinstance(row, dict) or row.get('is_rumor'):
                    continue
                name = row.get('full_name') or row.get('alias')
                target = row.get('url')
                if (not isinstance(name, str) or not isinstance(target, str)
                        or not KimovilScraper.is_product_name_match(core(model), core(name))):
                    continue
                target = 'https://www.kimovil.com/en/where-to-buy-' + target
                if technical_url(target):
                    candidates.append(target)
            if len(set(candidates)) != 1:
                raise ValueError('technical_model_discovery_ambiguous_or_missing')
            base = candidates[0]
            self.base_documents[model] = (base, self.read(base))
        base, html = self.base_documents[model]
        proof = selected_technical_document(html, base, model, specs)
        if proof:
            return html, proof
        soup = BeautifulSoup(html, 'html.parser')
        observed = []
        for anchor in soup.select('a[href]'):
            url = urljoin(base, anchor['href'])
            # Link text and the actually observed path are routing hints only;
            # the selected page must independently prove all specifications.
            if url != base and technical_url(url) and url not in observed:
                label = anchor.get_text(' ', strip=True)
                ram, storage = specs['ram_gb'], specs['storage_gb']
                path = urlparse(url).path
                if (re.search(rf'(?<!\d){ram}\s*GB\b', label, re.I) and re.search(rf'(?<!\d){storage}\s*GB\b', label, re.I)
                        or re.search(rf'(?:-|global|china){ram}gb-{storage}gb$', path)):
                    observed.append(url)
        for url in observed[:3]:
            html = self.read(url)
            proof = selected_technical_document(html, url, model, specs)
            if proof:
                return html, proof
        raise ValueError('exact_selected_technical_memory_not_observed')


class CachedSpecificationScraper(PublicSpecificationScraper):
    def __init__(self, html, proof):
        class NoImages:
            enabled = False
        super().__init__(reader=None, image_store=NoImages())
        self.html, self.proof = html, proof

    def get_via_flaresolverr(self, url):
        return self.html if url == self.proof['source_url'] else None


def technical_records(parent, specs, identity, reader):
    html, evidence = reader.locate(parent, specs)
    records = []
    scraper = CachedSpecificationScraper(html, evidence)
    if not scraper.scrape_product_details(evidence['source_url'], product_slug=identity['slug'],
            expected_name=evidence['model_name'], expected_memory=specs, existing_attributes={},
            existing_images=[], seed_physical_attributes=True, record_sink=records):
        raise ValueError('selected_technical_parser_failed')
    evidence['observed_at'] = utc_observation_time()
    soup = BeautifulSoup(html, 'html.parser')
    meta = soup.select_one('meta[name="devicecompare"]')
    device = json.loads(html_lib.unescape(meta['content'])) if meta else {}
    images = [url for url in extract_source_image_urls(soup, device)
              if urlparse(url).netloc == 'cdn-files.kimovil.com' and urlparse(url).scheme == 'https']
    if not records or not images:
        raise ValueError('selected_technical_sources_or_images_missing')
    return records, evidence, images


def observed_variant_candidates(result, products):
    by_slug = {p['slug']: p for p in products}
    matcher = TRPriceScraper()
    known = set()
    for product in products:
        model = matcher._catalog_model_name(product['name'], product['attributes'])
        specs = parent_specs(product)
        known.add((model, specs.get('ram_gb'), specs.get('storage_gb'), specs.get('network_generation')))
    candidates = {}
    for row in result['rows']:
        preview, parent = row.get('other_memory_variant'), by_slug.get(row.get('slug'))
        if not isinstance(preview, dict) or preview.get('scope') != 'other_memory_variant_preview_only' or not parent:
            continue
        specs, offer = preview.get('observed_specs'), preview.get('offer')
        if (not isinstance(specs, dict) or not isinstance(offer, dict)
                or variant_selected_evidence(offer, matcher._catalog_model_name(parent['name'], parent['attributes']), specs) is None):
            continue
        model = matcher._catalog_model_name(parent['name'], parent['attributes'])
        key = (model, specs.get('ram_gb'), specs.get('storage_gb'), specs.get('network_generation'))
        if key in known:
            continue
        bucket = candidates.setdefault(key, {'parent': deepcopy(parent), 'specs': deepcopy(specs), 'targets': []})
        if offer['url'] not in bucket['targets']:
            bucket['targets'].append(offer['url'])
    return list(candidates.values())


def memory_variant_destinations(cards, products):
    """Observed card capacities route to a fresh PDP, never establish a SKU."""
    matcher = TRPriceScraper()
    families, known = {}, set()
    for p in products:
        specs = parent_specs(p)
        model = matcher._catalog_model_name(p['name'], p['attributes'])
        if not model or specs.get('network_generation') not in ('4g', '5g'):
            continue
        family = (model, specs['network_generation'])
        previous = families.get(family)
        if previous is None or p['id'] < previous['id']:
            families[family] = p
        known.add((*family, specs.get('ram_gb'), specs.get('storage_gb')))
    found = {}
    for card in sorted(cards, key=lambda c: (not c.get('stock_priority', False), c['source_url'])):
        title = card.get('discovery_title', '')
        title = re.sub(r'(?<=\d)GB[/_](?=\d)', ' GB ', title, flags=re.I)
        title = re.sub(r'(?<![\w.,])([1-9]|1\d|2[0-4])\s*G\s*\+\s*(64|128|256|512|1024)\s*G(?!\w)',
                       r'RAM \1 GB; ROM \2 GB', title, flags=re.I)
        ram, storage, ambiguous = matcher._unambiguous_listing_capacities(title)
        if ambiguous or not ram or not storage or not 1 <= ram <= 64 or not 32 <= storage <= 4096:
            continue
        owners = []
        for (model, radio), parent in families.items():
            actual = {'ram_gb': ram, 'storage_gb': storage, 'network_generation': radio}
            if ((model, radio, ram, storage) not in known and matcher.is_strict_match(model, title, actual)
                    and matcher.is_strict_match(model, urlparse(card['source_url']).path.replace('-', ' '), actual)):
                owners.append((model, radio, parent))
        if len(owners) != 1:
            continue
        model, radio, parent = owners[0]
        key = (model, radio, ram, storage)
        found.setdefault(key, (parent, card))
    return list(found.values())


def memory_target_preflight(products, transport, *, maximum=48):
    """Find capacity hints in the current catalog, then GET the actual PDPs.

    No manually selected models/URLs or persisted price observations are input.
    This diagnostic does not consume category continuation state or publish.
    """
    from audit_priority_retailers import inspect_target
    from priority_retailer_transport import product_host
    hints, seen = [], set()
    matcher = TRPriceScraper()
    for parent in products:
        specs = parent_specs(parent)
        if specs.get('network_generation') not in {'4g', '5g'}:
            continue
        model = matcher._catalog_model_name(parent['name'], parent['attributes'])
        for target in parent.get('offer_targets', []):
            url = target.get('source_url') if isinstance(target, dict) else None
            if not product_host(url) or url in seen:
                continue
            text = urlparse(url).path.replace('-', ' ')
            ram, storage, ambiguous = matcher._unambiguous_listing_capacities(text)
            if ambiguous or not ram or not storage or (ram, storage) == (specs['ram_gb'], specs['storage_gb']):
                continue
            actual = {'ram_gb': ram, 'storage_gb': storage, 'network_generation': specs['network_generation']}
            if not matcher.is_strict_match(model, text, actual):
                continue
            seen.add(url)
            hints.append((parent, url))
    rows = [inspect_target(transport, url, parent) for parent, url in hints[:maximum]]
    return {'purpose': 'readonly_automatic_memory_variant_preflight', 'catalog_count': len(products),
            'publish': False, 'writes': False, 'accepted': 0, 'status': 'completed', 'queued': min(len(hints), maximum),
            'observed_capacity_hints': len(hints), 'rows': rows, 'sources': transport.report(),
            'models_without_priority_hints': []}


def prepare_memory_variants(result, products, *, transport, technical_reader=None,
                            publish=False, source_client=None, image_store=None, checkpoint=None):
    from audit_priority_retailers import inspect_target
    from priority_retailer_transport import HOSTS, product_host
    technical_reader = technical_reader or TechnicalVariantReader()
    candidates = observed_variant_candidates(result, products)
    report = {'contract': CONTRACT, 'purpose': 'automatic_existing_model_memory_variants',
              'publish': publish, 'writes': False, 'observed_candidates': len(candidates),
              'attempt_limit': MAX_VARIANTS, 'rows': [], 'created': 0, 'existing': 0}
    for candidate in candidates[:MAX_VARIANTS]:
        parent, specs = candidate['parent'], candidate['specs']
        identity = variant_identity(parent, specs['ram_gb'], specs['storage_gb'])
        row = {'parent_slug': parent['slug'], 'variant': {**identity, **specs}, 'status': 'pending', 'writes': False}
        report['rows'].append(row)
        try:
            records, evidence, image_urls = technical_records(parent, specs, identity, technical_reader)
            expected_parent = parent_specs(parent)
            profile = {**identity, 'attributes': {'ram_gb': specs['ram_gb'], 'storage_gb': specs['storage_gb'],
                       'Connectivity': {'5G': specs['network_generation'] == '5g'}}}
            offers = []
            for url in candidate['targets'][:4]:
                source_state = result.get('sources', {}).get(HOSTS.get(product_host(url)), {})
                if source_state.get('blocked') is True:
                    continue  # A new capacity never resets this run's source circuit.
                fresh = inspect_target(transport, url, profile)
                offer = fresh.get('prepared_offer')
                selected = variant_selected_evidence(offer, identity['name'], specs)
                if not selected or not TRPriceScraper().has_observed_offer_evidence(identity['name'], offer, specs):
                    continue
                offers.append({'merchant_name': offer['merchant'], 'price': offer['price'], 'currency': 'TRY',
                    'availability': 'in_stock', 'source_url': offer['url'], 'source_title': offer['source_title'],
                    'source_variant_evidence': selected, 'observed_at': offer['observed_at'], 'checked_at': offer['observed_at']})
            if not offers:
                raise ValueError('fresh_selected_variant_offer_missing')
            row.update(status='technical_and_live_offer_verified', technical_evidence=evidence,
                       source_keys=sorted(r['attribute_key'] for r in records),
                       offers=offers, observed_source_image_count=len(image_urls))
            if publish:
                if source_client is None:
                    raise ValueError('atomic_variant_client_required')
                image_store = image_store or DiscoveryImageStore(required=True)
                row['image_upload_attempted'] = True
                row['writes'] = report['writes'] = True
                images = [image_store.upload(url, identity['slug'], index) for index, url in enumerate(image_urls[:3])]
                payload = {'contract': CONTRACT, 'mode': 'publish',
                    'parent': {key: parent[key] for key in ('id', 'slug', 'name')},
                    'variant': {**identity, **specs}, 'technical_evidence': evidence,
                    'sources': records, 'images': images, 'offers': offers}
                payload['parent'].update({key: expected_parent[key] for key in ('ram_gb', 'storage_gb', 'network_generation')})
                # Image uploads are writes too; never claim no writes after an
                # unsuccessful later server transaction.
                response = source_client.session.request('POST', source_client.base_url + '/api/ingestion/memory-variants',
                    headers={'Authorization': 'Bearer ' + source_client.secret, 'Accept': 'application/json'},
                    json=payload, timeout=(10, 180))
                body = source_client._json_body(response)
                if (body.get('committed') is not True or body.get('contract') != CONTRACT
                        or body.get('parent_unchanged') is not True or body.get('outcome') not in ('created', 'existing')):
                    row['server_issues'] = body.get('issues', [])[:15]
                    raise ValueError('atomic_variant_publication_rejected')
                row.update(status=body['outcome'], publication={key: body.get(key) for key in
                    ('product_id', 'slug', 'parent_product_id', 'parent_unchanged', 'scores_recalculated')})
                report[body['outcome']] += 1
        except Exception as error:
            reason = str(error) if isinstance(error, ValueError) and re.fullmatch(r'[a-z_]{1,100}', str(error)) else type(error).__name__
            row.update(status='rejected', reason=reason)
        if checkpoint:
            checkpoint(report)
    report.update(technical_documents=technical_reader.documents, sources=transport.report())
    return report


def verify_variant_catalog_readback(before, after, report):
    """Do not count a response as a visible new variant or change the baseline."""
    def identity(product):
        specs = parent_specs(product)
        return tuple(product.get(k) for k in ('id', 'slug', 'name')) + tuple(
            specs.get(k) for k in ('ram_gb', 'storage_gb', 'network_generation'))
    original = {p['id']: identity(p) for p in before}
    current = {p['id']: identity(p) for p in after}
    if len(current) != len(after) or any(current.get(key) != value for key, value in original.items()):
        raise ValueError('original_catalog_identity_changed_after_variants')
    expected = {}
    for row in report['rows']:
        if row['status'] != 'created':
            continue
        published = row.get('publication') or {}
        variant = row['variant']
        product_id = published.get('product_id')
        if type(product_id) is not int or product_id in original or product_id in expected:
            raise ValueError('variant_publication_identity_conflict')
        expected[product_id] = (product_id, variant['slug'], variant['name'], variant['ram_gb'],
                                variant['storage_gb'], variant['network_generation'])
    if set(current) - set(original) != set(expected) or any(current.get(key) != value for key, value in expected.items()):
        raise ValueError('published_variant_catalog_readback_incomplete')
    return {'parent_identities_unchanged': len(original), 'new_variants_verified': len(expected), 'catalog_after': len(current)}
