"""Existing-model memory variants discovered by the scraper, never hand lists.

Retailer observations are hints for a separate capacity. Every selected
technical page and retailer listing is fetched again in this invocation.
Only the atomic server endpoint can create a variant; the parent is read-only.
"""
from copy import deepcopy
import base64
import binascii
from datetime import datetime, timezone
import html as html_lib
import json
import re
import unicodedata
from urllib.parse import parse_qsl, quote, urljoin, urlparse

import requests

from bs4 import BeautifulSoup

from daily_discovery import PublicSpecificationScraper, DiscoveryImageStore
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


def canonical_observation_time(raw):
    instant = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    if instant.tzinfo is None:
        raise ValueError('variant_observation_timezone_missing')
    return instant.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def parent_specs(product):
    specs = with_reviewed_radio(product, PriceUpdater.get_expected_specs(product['attributes']))
    named_radio = TRPriceScraper._radio_generation(product['name'])
    if named_radio and specs.get('network_generation') not in (None, named_radio):
        raise ValueError('parent_radio_conflict')
    if named_radio:
        specs['network_generation'] = named_radio
    return specs


def family_key(model):
    return re.sub(r'\s+', ' ', re.sub(r'\b[45]g\b', '', model, flags=re.I)).strip().casefold()


def memory_parent_priority(products, public_status):
    """Route limited variant work to models with no currently priced capacity.

    These public booleans are scheduling hints only. An unpriced 6/128 parent
    is already covered if its independently verified 8/128 sibling has a price.
    Radio remains part of the family; an unknown radio never joins another SKU.
    """
    groups = {}
    for product in products:
        specs = parent_specs(product)
        model = TRPriceScraper._catalog_model_name(product['name'], product['attributes'])
        radio = specs.get('network_generation')
        key = ((family_key(model), radio) if model and radio in ('4g', '5g')
               else ('unresolved', product['slug']))
        status = public_status.get(product['id'])
        priced = (status[1] if isinstance(status, (tuple, list)) and len(status) == 2
                  and status[0] == product['slug'] and type(status[1]) is bool else None)
        groups.setdefault(key, []).append((product['slug'], priced))
    priorities, counts = {}, {0: 0, 1: 0, 2: 0}
    for family, members in groups.items():
        level = (1 if any(priced is True for _, priced in members) else
                 0 if family[0] != 'unresolved' and all(priced is False for _, priced in members) else 2)
        counts[level] += 1
        priorities.update((slug, level) for slug, _ in members)
    return {'source': 'public_current_price_snapshot', 'parent_priorities': priorities,
            'models_without_current_price': counts[0], 'models_with_current_price': counts[1],
            'unknown_models': counts[2], 'prices_are_routing_hints_only': True}


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


def observed_technical_link(raw, base):
    try:
        url = urljoin(base, raw)
        parsed = urlparse(url)
        # This observed price-panel anchor is browser-only navigation; it is
        # never sent in an HTTP document request or treated as price evidence.
        if parsed.fragment == 'open-prices':
            url = parsed._replace(fragment='').geturl()
        return url if technical_url(url) else None
    except (TypeError, ValueError):
        return None


def declared_technical_targets(node, base):
    """DOM-declared public version navigation, including non-anchor cards."""
    found = []
    for element in [node, *node.select('[href], [data-kdecode], [data-href], [data-url], [data-link], [data-slug]')]:
        for key in ('href', 'data-kdecode', 'data-href', 'data-url', 'data-link', 'data-slug'):
            value = element.get(key)
            if not isinstance(value, str):
                continue
            if key == 'data-kdecode':
                # The site's own public navigation stores the exact URL as
                # Base64 before JavaScript hydrates href. Never synthesize a
                # capacity slug or treat the decoded destination as evidence.
                if len(value) > 4096:
                    continue
                try:
                    value = base64.b64decode(value, validate=True).decode('utf-8')
                except (binascii.Error, UnicodeError, ValueError):
                    continue
            if key == 'data-slug' and re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)+', value):
                value = 'https://www.kimovil.com/en/where-to-buy-' + value
            target = observed_technical_link(value, base)
            if target and target not in found:
                found.append(target)
    return found


def autocomplete_technical_variants(row):
    """Copy opaque capacity destinations actually declared by the provider.

    Its public `vers` field is label=slug entries separated by '#'. No region,
    capacity or model suffix is generated. Labels are routing hints only.
    """
    raw = row.get('vers')
    if not isinstance(raw, str) or len(raw) > 32768:
        return []
    variants = []
    for item in raw.split('#')[:128]:
        if item.count('=') != 1:
            continue
        label, slug = item.split('=')
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)+', slug):
            continue
        amounts = re.findall(r'(?<![\w.,])(\d{1,4})\s*(GB|TB)\b', label, re.I)
        if len(amounts) != 2:
            continue
        ram, storage = [int(n) * (1024 if unit.casefold() == 'tb' else 1) for n, unit in amounts]
        target = 'https://www.kimovil.com/en/where-to-buy-' + slug
        if 1 <= ram <= 64 and 32 <= storage <= 4096 and technical_url(target):
            variants.append({'source_url': target, 'ram_gb': ram, 'storage_gb': storage})
    return variants


def variant_selected_evidence(offer, name, specs):
    if isinstance(offer, dict) and offer.get('platform') == 'Teknosa':
        from priority_retailer_transport import product_host
        from retailer_barcode import retailer_gtin13
        from teknosa_marketplace_source import catalog_offer_matches as matches
        matcher = TRPriceScraper()
        title = offer.get('source_title')
        if not isinstance(title, str):
            return None
        ram, storage, ambiguous = matcher._unambiguous_listing_capacities(title)
        radios = set(matcher._identity_words(title)) & {'4g', '5g', 'lte'}
        if 'lte' in radios:
            radios.discard('lte')
            radios.add('4g')
        if (ambiguous or len(radios) != 1 or ram != specs.get('ram_gb')
                or storage != specs.get('storage_gb') or specs.get('network_generation') not in radios
                or product_host(offer.get('url')) != 'www.teknosa.com' or urlparse(offer['url']).query
                or not retailer_gtin13(offer.get('gtin13'))
                or not all(re.fullmatch(r'[1-9]\d{0,19}', str(offer.get(k) or ''))
                           for k in ('sku', 'merchant_id', 'listing_id'))
                or not urlparse(offer['url']).path.endswith('-p-' + str(offer['sku']))
                or not matcher.has_observed_offer_evidence(name, offer, specs)):
            return None
        if offer.get('selected_teknosa_first_party_verified') is True:
            if (offer.get('merchant') != 'Teknosa' or offer.get('merchant_id') != '1001'
                    or offer.get('listing_id') != offer.get('sku') or offer.get('currency') != 'TRY'
                    or offer.get('availability') != 'in_stock'):
                return None
        elif not matches(offer, name, specs):
            return None
        return {'version': 'selected-retailer-sku-v1', 'platform': 'Teknosa',
                'product_id': str(offer['sku']), 'sku': str(offer['sku']),
                'listing_id': str(offer['listing_id']), 'merchant_id': str(offer['merchant_id']),
                'merchant_name': offer['merchant'], 'gtin13': offer['gtin13'],
                'ram_gb': ram, 'storage_gb': storage, 'network_generation': specs['network_generation']}
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


def technical_model_matches(expected, observed):
    """Radio spelling may differ; a Xiaomi/Redmi model family may not."""
    core = lambda name: re.sub(r'\b[45]g\b', '', name, flags=re.I).strip()
    return (isinstance(expected, str) and isinstance(observed, str)
            and KimovilScraper.is_product_name_match(core(expected), core(observed))
            and TRPriceScraper().is_strict_match(core(expected), core(observed), {}))


def selected_technical_radio(connectivity, source_names, soup):
    radio = PriceUpdater.get_expected_specs({'Connectivity': connectivity}).get('network_generation')
    if radio is not None:
        return radio
    # Observed 4G source pages omit the 5G row entirely. Require BOTH
    # independently selected metadata names to explicitly identify 4G and a
    # positive LTE band table. Absence of 5G or the requested catalog radio
    # alone proves nothing. Operator coverage widgets are not device bands.
    labels = []
    for section in soup.find_all('section', class_=re.compile(r'container-sheet-')):
        heading = section.find(['h2', 'h3', 'h4'])
        if heading and heading.get_text(' ', strip=True).casefold().startswith('connectivity'):
            labels.extend(node.get_text(' ', strip=True) for node in section.select('th.label, td.label, dt'))
    if (len(source_names) == 2
            and all(TRPriceScraper._radio_generation(name) == '4g' for name in source_names)
            and not any(re.fullmatch(r'5G(?:\s*NR)?', str(key), re.I) for key in [*connectivity, *labels])
            and isinstance(connectivity.get('4G LTE'), str)
            and re.search(r'\bB\d{1,3}\s*\(', connectivity['4G LTE'], re.I)):
        return '4g'
    return None


def selected_technical_document(html, url, model, specs, *, canonical_model_url=None, provider_declared_variant=None):
    if not technical_url(url) or not isinstance(html, str) or len(html.encode()) > 5 * 1024 * 1024:
        return None
    soup = BeautifulSoup(html, 'html.parser')
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    if re.search(r'just a moment|captcha|access denied|verify you are human', title, re.I):
        return None
    names = []
    selected_slug = None
    for key in ('deviceki', 'devicecompare'):
        nodes = soup.select(f'meta[name="{key}"]')
        if len(nodes) > 1:
            return None
        if nodes:
            try:
                value = json.loads(html_lib.unescape(nodes[0].get('content', '')))
                if isinstance(value, dict) and isinstance(value.get('name'), str):
                    names.append(value['name'])
                if key == 'devicecompare' and isinstance(value, dict):
                    selected_slug = value.get('slug')
            except (ValueError, TypeError):
                return None
    # Kimovil may spell radio in metadata while the existing model omits it.
    # The independently read technical connectivity must still prove radio.
    if not names or not all(technical_model_matches(model, name) for name in names):
        return None
    if selected_slug is not None and selected_slug != url.rsplit('/where-to-buy-', 1)[-1]:
        return None
    if any(TRPriceScraper._radio_generation(name) not in (None, specs['network_generation']) for name in names):
        return None
    canonical = soup.select('link[rel="canonical"][href]')
    if len(canonical) != 1:
        return None
    actual_canonical = urljoin(url, canonical[0]['href']).rstrip('/')
    if actual_canonical != url:
        # Observed Kimovil variants share their model canonical. This is
        # acceptable only after the caller separately verifies that exact
        # model page and this page independently selects the requested slug.
        selected = soup.select('.versions-list > .item.active')
        if (actual_canonical != canonical_model_url or not technical_url(actual_canonical)
                or selected_slug != url.rsplit('/where-to-buy-', 1)[-1]
                or (selected and (len(selected) != 1 or declared_technical_targets(selected[0], url) != [url]))
                or (not selected and provider_declared_variant != url)):
            return None
    sections = extract_spec_sections(soup)
    actual = {'ram_gb': exact_capacity(find_section_spec(sections, 'Hardware', 'Memory RAM', 'RAM')),
              'storage_gb': exact_capacity(find_section_spec(sections, 'Hardware', 'Capacity', 'Storage'))}
    if any(actual[key] != specs[key] for key in actual):
        return None
    connectivity = next((value for key, value in sections.items() if 'connect' in key.casefold()), {})
    radio = selected_technical_radio(connectivity, names, soup)
    if radio != specs['network_generation']:
        return None
    return {'model_name': names[0], 'source_url': url, **actual,
            'observed_at': utc_observation_time()}


def rejected_technical_observation(document, url, model, specs, phase):
    """Keep bounded public identity facts, never the document or script state.

    A missing exact proof must stay rejected. These observations distinguish
    an unselected capacity from missing metadata in the anonymous CI renderer.
    They are diagnostics only and are never supplied to publication.
    """
    soup = BeautifulSoup(document, 'html.parser')
    sections = extract_spec_sections(soup)
    connectivity = next((v for k, v in sections.items() if 'connect' in k.casefold()), {})
    metadata = []
    names = []
    for key in ('deviceki', 'devicecompare'):
        nodes = soup.select(f'meta[name="{key}"]')
        item = {'key': key, 'count': len(nodes), 'valid_json': False}
        if len(nodes) == 1:
            try:
                value = json.loads(html_lib.unescape(nodes[0].get('content', '')))
                item['valid_json'] = isinstance(value, dict)
                if isinstance(value, dict):
                    name = value.get('name')
                    if isinstance(name, str) and len(name) <= 120 and not re.search(r'[\x00-\x1f]', name):
                        item['model_name'] = name
                        names.append(name)
                    slug = value.get('slug')
                    if isinstance(slug, str) and re.fullmatch(r'[a-z0-9-]{1,160}', slug):
                        item['selected_slug'] = slug
            except (ValueError, TypeError):
                pass
        metadata.append(item)
    canonicals = soup.select('link[rel="canonical"][href]')
    active = soup.select('.versions-list > .item.active')
    return {'scope': 'rejected_selected_technical_document', 'phase': phase,
        'model': model, 'source_url': url,
        'requested_specs': {k: specs.get(k) for k in ('ram_gb', 'storage_gb', 'network_generation')},
        'observed_specs': {
            'ram_gb': exact_capacity(find_section_spec(sections, 'Hardware', 'Memory RAM', 'RAM')),
            'storage_gb': exact_capacity(find_section_spec(sections, 'Hardware', 'Capacity', 'Storage')),
            'network_generation': selected_technical_radio(connectivity, names, soup)},
        'metadata': metadata, 'canonical_count': len(canonicals),
        'canonical_urls': [target for node in canonicals[:3]
                           if (target := observed_technical_link(node['href'], url))],
        'active_version_count': len(active),
        'active_version_urls': [target for node in active[:3] for target in declared_technical_targets(node, url)][:6]}


class TechnicalVariantReader:
    """Anonymous public GETs with a per-invocation bound and denial stop."""
    def __init__(self, reader=None, maximum_documents=48):
        self.reader = reader or PublicBrowserTechnicalReader()
        self.maximum_documents = maximum_documents
        self.documents = 0
        self.attempts = 0
        self.base_documents = {}
        self.provider_variants = {}
        self.hinted_roots = set()
        self.observed_at = {}
        self.diagnostics = []

    def read(self, url):
        if self.attempts >= self.maximum_documents:
            raise ValueError('technical_document_budget_exhausted')
        self.attempts += 1
        document = self.reader.read(url)
        self.documents += 1
        self.observed_at[url] = utc_observation_time()
        return document

    def discover_roots(self, model, specs):
        # The existing public autocomplete returns actual model URLs. No
        # region/capacity suffix is synthesized or searched in a URL grid.
        endpoint = ('https://www.kimovil.com/_json/autocomplete_devicemodels_joined.json'
                    '?device_type=0&name=' + quote(re.sub(r'\b[45]g\b', '', model, flags=re.I).strip(), safe=''))
        raw = self.read(endpoint)
        if raw.lstrip().startswith('<'):
            raw = BeautifulSoup(raw, 'html.parser').get_text()
        data = json.loads(raw)
        candidates = []
        variants_by_root = {}
        for row in data.get('results', [])[:30]:
            if not isinstance(row, dict) or row.get('is_rumor'):
                continue
            name = row.get('full_name') or row.get('alias')
            target = row.get('url')
            if (not isinstance(name, str) or not isinstance(target, str)
                    or not technical_model_matches(model, name)
                    or TRPriceScraper._radio_generation(name) not in (None, specs['network_generation'])):
                continue
            target = 'https://www.kimovil.com/en/where-to-buy-' + target
            if technical_url(target):
                candidates.append(target)
                variants_by_root.setdefault(target, []).extend(autocomplete_technical_variants(row))
        self.diagnostics.append({'scope': 'public_technical_model_discovery', 'model': model,
            'result_count': len(data.get('results', [])), 'matching_model_urls': list(dict.fromkeys(candidates))[:6]})
        unique = list(dict.fromkeys(candidates))
        roots = {re.sub(r'-[45]g$', '', url) for url in unique}
        if len(unique) > 1 and len(roots) == 1 and next(iter(roots)) in unique:
            # A bare name may refer to the other radio generation. Prefer
            # the actually returned, explicitly matching radio URL, then
            # still require its newly read technical radio facts.
            qualified = [url for url in unique if url.endswith('-' + specs['network_generation'])]
            candidates = qualified if len(qualified) == 1 else [next(iter(roots))]
            self.diagnostics[-1]['radio_qualified_discovery'] = True
        return candidates, variants_by_root

    def locate(self, parent, specs):
        model = TRPriceScraper._catalog_model_name(parent['name'], parent['attributes'])
        if model not in self.base_documents:
            hints = parent.get('technical_source_urls')
            if isinstance(hints, list) and len(hints) <= 4:
                for hint in dict.fromkeys(url for url in hints if technical_url(url)):
                    document = self.read(hint)
                    # The persisted URL is not proof. Its freshly selected
                    # model, physical memory and radio must match the parent.
                    if selected_technical_document(document, hint, model, parent_specs(parent)):
                        self.base_documents[model] = (hint, document)
                        self.provider_variants[hint] = []
                        self.hinted_roots.add(hint)
                        self.diagnostics.append({'scope': 'fresh_persisted_technical_root',
                            'model': model, 'source_url': hint, 'parent_memory_radio_verified': True,
                            'stored_facts_reused': False})
                        break
        if model not in self.base_documents:
            candidates, variants_by_root = self.discover_roots(model, specs)
            if len(set(candidates)) != 1:
                raise ValueError('technical_model_discovery_ambiguous_or_missing')
            base = candidates[0]
            self.base_documents[model] = (base, self.read(base))
            self.provider_variants[base] = variants_by_root.get(base, [])
        base, html = self.base_documents[model]
        proof = selected_technical_document(html, base, model, specs)
        if proof:
            proof['observed_at'] = self.observed_at[base]
            return html, proof
        self.diagnostics.append(rejected_technical_observation(html, base, model, specs, 'requested_base'))
        soup = BeautifulSoup(html, 'html.parser')
        raw_specs = extract_spec_sections(soup)
        default_specs = {
            'ram_gb': exact_capacity(find_section_spec(raw_specs, 'Hardware', 'Memory RAM', 'RAM')),
            'storage_gb': exact_capacity(find_section_spec(raw_specs, 'Hardware', 'Capacity', 'Storage')),
            'network_generation': specs['network_generation'],
        }
        if (not default_specs['ram_gb'] or not default_specs['storage_gb']
                or not 1 <= default_specs['ram_gb'] <= 64 or not 32 <= default_specs['storage_gb'] <= 4096
                or selected_technical_document(html, base, model, default_specs) is None):
            self.diagnostics.append(rejected_technical_observation(html, base, model, default_specs, 'model_family'))
            raise ValueError('technical_model_family_not_verified')
        if base in self.hinted_roots:
            # Provider version URLs remain bound to this exact fresh root,
            # even when search omits it or returns unrelated models.
            _, variants_by_root = self.discover_roots(model, specs)
            self.provider_variants[base] = variants_by_root.get(base, [])
            self.hinted_roots.remove(base)
        observed = []
        observed_priority = {}
        version_hints = []
        version_nodes = soup.select('.versions-list > .item')
        if not version_nodes:
            version_nodes = soup.select('.versions-list [href], .versions-list [data-kdecode], .versions-list [data-href], .versions-list [data-url], .versions-list [data-slug]')
        for node in version_nodes:
            # Link text and the actually observed path are routing hints only;
            # the selected page must independently prove all specifications.
            label = node.get_text(' ', strip=True)
            targets = declared_technical_targets(node, base)
            if len(version_hints) < 8:
                version_hints.append({'source_urls': targets, 'node': node.name,
                    'attribute_names': list(node.attrs),
                    'capacity_tokens': re.findall(r'(?<!\d)\d{1,4}\s*(?:GB|TB)\b', label, re.I)})
            for url in targets:
                if url == base or url in observed:
                    continue
                ram, storage = specs['ram_gb'], specs['storage_gb']
                path = urlparse(url).path
                if (re.search(rf'(?<!\d){ram}\s*GB\b', label, re.I) and re.search(rf'(?<!\d){storage}\s*GB\b', label, re.I)
                        or re.search(rf'(?:-|global|china){ram}gb(?:-|$)', path)
                        or re.search(rf'(?:-|global|china){storage}gb(?:-|$)', path)):
                    observed.append(url)
                    observed_priority[url] = int(bool(re.search(rf'(?<!\d){ram}\s*GB\b', label, re.I)
                        and re.search(rf'(?<!\d){storage}\s*GB\b', label, re.I)))
        observed.sort(key=lambda url: -observed_priority[url])
        declared = {item['source_url'] for item in self.provider_variants.get(base, [])
                    if item['ram_gb'] == specs['ram_gb'] and item['storage_gb'] == specs['storage_gb']}
        for target in sorted(declared):
            if target != base and target not in observed:
                observed.append(target)
        self.diagnostics.append({'scope': 'observed_technical_capacity_links', 'model': model,
            'source_url': base, 'selected_default_specs': default_specs,
            'requested_specs': specs, 'version_lists': len(soup.select('.versions-list')),
            'provider_declared_capacity_urls': sorted(declared),
            'observed_capacity_urls': observed[:6], 'version_hints': version_hints,
            'version_structure': [
                {'tag': container.name, 'attribute_names': list(container.attrs),
                 'children': [{'tag': node.name, 'attribute_names': list(node.attrs),
                    'attributes': {key: value for key, value in node.attrs.items() if key in {
                        'class', 'href', 'data-url', 'data-href', 'data-link', 'data-slug',
                        'data-ram', 'data-rom', 'data-mk', 'data-id'}},
                    'child_tags': [child.name for child in node.find_all(recursive=False)[:4]]}
                    for node in container.find_all(recursive=False)[:3]]}
                for container in soup.select('.versions-list')[:2]]})
        for url in observed[:3]:
            html = self.read(url)
            proof = selected_technical_document(html, url, model, specs, canonical_model_url=base,
                provider_declared_variant=url if url in declared else None)
            if proof:
                proof['observed_at'] = self.observed_at[url]
                return html, proof
            self.diagnostics.append(rejected_technical_observation(html, url, model, specs, 'requested_capacity'))
        raise ValueError('exact_selected_technical_memory_not_observed')


class PublicBrowserTechnicalReader:
    """Normal rendered GETs in the job's existing anonymous browser service.

    This never attaches to a user profile, logs in, supplies proxy/cookie
    overrides, or accepts a changed URL/challenge/denied document as evidence.
    The first source denial closes the source for the rest of this invocation.
    """
    def __init__(self, post=None):
        self.post = post or requests.post
        self.ready = False
        self.blocked = False
        self.session = 'teknoskor-public-memory-specifications'

    @staticmethod
    def allowed_url(raw):
        if technical_url(raw):
            return True
        try:
            u = urlparse(raw)
            query = parse_qsl(u.query, keep_blank_values=True)
            values = dict(query)
            return (u.scheme == 'https' and u.netloc == 'www.kimovil.com' and not u.fragment
                and u.path == '/_json/autocomplete_devicemodels_joined.json' and len(query) == 2
                and set(values) == {'device_type', 'name'} and values['device_type'] == '0'
                and bool(re.fullmatch(r'[A-Za-z0-9 +().-]{3,120}', values['name'])))
        except (ValueError, TypeError):
            return False

    def read(self, url):
        if self.blocked:
            raise ValueError('technical_source_blocked_for_run')
        if not self.allowed_url(url):
            raise ValueError('unapproved_technical_document_url')
        if not self.ready:
            response = self.post('http://localhost:8191/v1',
                json={'cmd': 'sessions.create', 'session': self.session}, timeout=30)
            response.raise_for_status()
            if response.json().get('status') != 'ok':
                raise ValueError('technical_browser_session_unavailable')
            self.ready = True
        response = self.post('http://localhost:8191/v1',
            json={'cmd': 'request.get', 'url': url, 'session': self.session, 'maxTimeout': 45000,
                  'waitInSeconds': 5 if technical_url(url) else 0}, timeout=60)
        response.raise_for_status()
        body = response.json()
        solution = body.get('solution') if isinstance(body, dict) else None
        if not isinstance(body, dict) or body.get('status') != 'ok' or not isinstance(solution, dict):
            self.blocked = True
            raise ValueError('technical_browser_document_unavailable')
        status = solution.get('status')
        if status in (403, 429):
            self.blocked = True
        if status != 200:
            raise ValueError('technical_source_http_' + str(status))
        document = solution.get('response')
        if (solution.get('url') != url or not isinstance(document, str)
                or len(document.encode()) > 5 * 1024 * 1024):
            raise ValueError('technical_document_changed_or_oversized')
        soup = BeautifulSoup(document, 'html.parser')
        title = soup.title.get_text(' ', strip=True) if soup.title else ''
        if re.search(r'just a moment|captcha|access denied|verify you are human|security check', title, re.I):
            self.blocked = True
            raise ValueError('technical_source_challenge')
        return document


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
    values = {record['attribute_key']: record.get('value') for record in records}
    required = {'ram_gb', 'storage_gb', 'battery_mah', 'screen_size_inch', 'Technical sheet',
        'Design & Materials', 'Performance & Hardware', 'Camera', 'Connectivity', 'Batarya', 'Software',
        'performance_score', 'camera_score', 'battery_score', 'screen_score'}
    if not required.issubset(values):
        raise ValueError('selected_technical_required_fields_missing')
    for key in ('performance_score', 'camera_score', 'battery_score', 'screen_score'):
        if type(values[key]) not in (int, float) or not 0 < values[key] <= 10:
            raise ValueError('selected_technical_components_missing')
    for record in records:
        record['observed_at'] = evidence['observed_at']
    soup = BeautifulSoup(html, 'html.parser')
    meta = soup.select_one('meta[name="devicecompare"]')
    device = json.loads(html_lib.unescape(meta['content'])) if meta else {}
    images = [url for url in extract_source_image_urls(soup, device)
              if urlparse(url).netloc == 'cdn-files.kimovil.com' and urlparse(url).scheme == 'https']
    if not records or not images:
        raise ValueError('selected_technical_sources_or_images_missing')
    return records, evidence, images


def existing_selected_memory_sibling(parent, preview, products):
    """Route a freshly proved other capacity to exactly one existing SKU.

    This never creates a variant or changes the original SKU. Publication
    still re-fetches the listing against the sibling's current full profile.
    """
    if (not isinstance(preview, dict) or preview.get('scope') != 'other_memory_variant_preview_only'
            or preview.get('writes') is not False):
        return None
    specs, offer = preview.get('observed_specs'), preview.get('offer')
    if not isinstance(specs, dict) or not isinstance(offer, dict):
        return None
    model = TRPriceScraper._catalog_model_name(parent['name'], parent['attributes'])
    try:
        radio = parent_specs(parent).get('network_generation')
    except ValueError:
        return None
    if (not model or radio not in ('4g', '5g') or specs.get('network_generation') != radio
            or variant_selected_evidence(offer, model, specs) is None):
        return None
    matches = []
    for product in products:
        if product['id'] == parent['id']:
            continue
        other_model = TRPriceScraper._catalog_model_name(product['name'], product['attributes'])
        if not other_model or family_key(other_model) != family_key(model):
            continue
        try:
            actual = parent_specs(product)
        except ValueError:
            continue
        if all(actual.get(k) == specs.get(k) for k in ('ram_gb', 'storage_gb', 'network_generation')):
            matches.append(product)
    return matches[0] if len(matches) == 1 else None


def observed_variant_candidates(result, products):
    by_slug = {p['slug']: p for p in products}
    matcher = TRPriceScraper()
    known = set()
    for product in products:
        model = matcher._catalog_model_name(product['name'], product['attributes'])
        if not model:
            continue
        specs = parent_specs(product)
        known.add((family_key(model), specs.get('ram_gb'), specs.get('storage_gb'), specs.get('network_generation')))
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
        if not model:
            continue
        key = (family_key(model), specs.get('ram_gb'), specs.get('storage_gb'), specs.get('network_generation'))
        if key in known:
            continue
        bucket = candidates.setdefault(key, {'parent': deepcopy(parent), 'specs': deepcopy(specs), 'targets': []})
        if offer['url'] not in bucket['targets']:
            bucket['targets'].append(offer['url'])
    priorities = (result.get('memory_variant_priority') or {}).get('parent_priorities')
    if not isinstance(priorities, dict):
        return list(candidates.values())
    tiers = {0: {}, 1: {}, 2: {}}
    for candidate in candidates.values():
        parent = candidate['parent']
        level = priorities.get(parent['slug'], 2)
        if type(level) is not int or level not in tiers:
            level = 2
        model = matcher._catalog_model_name(parent['name'], parent['attributes'])
        family = (family_key(model), candidate['specs']['network_generation'])
        tiers[level].setdefault(family, []).append(candidate)
    ordered = []
    for level in (0, 1, 2):
        families = list(tiers[level].values())
        # Give every missing model a first attempt before another capacity of
        # the same model can consume the remaining technical/publication slots.
        while any(families):
            for family in families:
                if family:
                    ordered.append(family.pop(0))
    return ordered


def memory_variant_destinations(cards, products):
    """Observed card capacities route to a fresh PDP, never establish a SKU."""
    matcher = TRPriceScraper()
    families, known = {}, set()
    for p in products:
        specs = parent_specs(p)
        model = matcher._catalog_model_name(p['name'], p['attributes'])
        if not model or specs.get('network_generation') not in ('4g', '5g'):
            continue
        family = (family_key(model), specs['network_generation'])
        previous = families.get(family)
        if previous is None or p['id'] < previous['id']:
            families[family] = p
        known.add((*family, specs.get('ram_gb'), specs.get('storage_gb')))
    known_storage = {(model, radio, storage) for model, radio, _, storage in known}
    found = {}
    for card in sorted(cards, key=lambda c: (not c.get('stock_priority', False), c['source_url'])):
        title = card.get('discovery_title', '')
        title = re.sub(r'(?<=\d)GB[/_](?=\d)', ' GB ', title, flags=re.I)
        title = re.sub(r'(?<![\w.,])([1-9]|1\d|2[0-4])\s*G\s*\+\s*(64|128|256|512|1024)\s*G(?!\w)',
                       r'RAM \1 GB; ROM \2 GB', title, flags=re.I)
        ram, storage, ambiguous = matcher._unambiguous_listing_capacities(title)
        if (ambiguous or storage is None or not 32 <= storage <= 4096
                or ram is not None and not 1 <= ram <= 64):
            continue
        owners = []
        for (model, radio), parent in families.items():
            actual = {'ram_gb': ram, 'storage_gb': storage, 'network_generation': radio}
            already_known = ((model, radio, storage) in known_storage if ram is None
                             else (model, radio, ram, storage) in known)
            # Storage-only category labels (especially iPhone) are URL hints,
            # not selected RAM evidence. Read an unseen storage PDP without
            # filling RAM from the parent; its complete selected facts and
            # independent technical source must still establish the new SKU.
            if (not already_known and matcher.is_strict_match(model, title, actual)
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
              'attempt_limit': MAX_VARIANTS, 'rows': [], 'created': 0, 'existing': 0,
              'technical_diagnostics': getattr(technical_reader, 'diagnostics', []),
              'source_deferrals': []}
    reachable = []
    for candidate in candidates:
        targets = [url for url in candidate['targets']
                   if result.get('sources', {}).get(HOSTS.get(product_host(url)), {}).get('blocked') is not True]
        if targets:
            reachable.append({**candidate, 'targets': targets})
        else:
            report['source_deferrals'].append({'parent_slug': candidate['parent']['slug'],
                'variant': {**variant_identity(candidate['parent'], candidate['specs']['ram_gb'], candidate['specs']['storage_gb']),
                            **candidate['specs']},
                'reason': 'all_observed_variant_sources_blocked_for_run',
                'retailers': sorted({HOSTS[product_host(url)] for url in candidate['targets']})})
    report.update(eligible_source_candidates=len(reachable),
                  deferred_source_candidates=len(report['source_deferrals']))
    for candidate in reachable[:MAX_VARIANTS]:
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
                observed_at = canonical_observation_time(offer['observed_at'])
                offers.append({'merchant_name': offer['merchant'], 'price': offer['price'], 'currency': 'TRY',
                    'availability': 'in_stock', 'source_url': offer['url'], 'source_title': offer['source_title'],
                    'source_variant_evidence': selected, 'observed_at': observed_at, 'checked_at': observed_at})
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
                images = []
                for index, source_image in enumerate(image_urls[:3]):
                    try:
                        images.append(image_store.upload(source_image, identity['slug'], index))
                    except Exception:
                        row['source_image_failures'] = row.get('source_image_failures', 0) + 1
                if not images:
                    raise ValueError('verified_variant_image_upload_missing')
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
                        or body.get('parent_unchanged') is not True or body.get('outcome') not in ('created', 'existing')
                        or body.get('parent_product_id') != parent['id'] or type(body.get('product_id')) is not int
                        or body['product_id'] <= 0 or body.get('outcome') == 'created' and body.get('slug') != identity['slug']):
                    row['server_issues'] = body.get('issues', [])[:15]
                    raise ValueError('atomic_variant_publication_rejected')
                row.update(status=body['outcome'], publication={key: body.get(key) for key in
                    ('product_id', 'slug', 'parent_product_id', 'parent_unchanged', 'scores_recalculated')})
                row['score_refresh_pending'] = body.get('scores_recalculated') is not True
                report[body['outcome']] += 1
        except Exception as error:
            reason = str(error) if isinstance(error, ValueError) and re.fullmatch(r'[a-z0-9_]{1,100}', str(error)) else type(error).__name__
            row.update(status='rejected', reason=reason)
        if checkpoint:
            checkpoint(report)
    report.update(technical_documents=technical_reader.documents,
                  technical_request_attempts=getattr(technical_reader, 'attempts', technical_reader.documents),
                  technical_diagnostics=getattr(technical_reader, 'diagnostics', []), sources=transport.report())
    report['score_refresh_pending'] = sum(row.get('score_refresh_pending') is True for row in report['rows'])
    report['verified_candidates'] = sum(row['status'] in {'technical_and_live_offer_verified', 'created', 'existing'}
                                        for row in report['rows'])
    report['attempted_candidates'] = len(report['rows'])
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
