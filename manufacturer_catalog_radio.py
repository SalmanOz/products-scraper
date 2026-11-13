"""Fresh manufacturer proof for a missing catalog radio; never retailer prices.

The manufacturer model/code and physical RAM/storage must independently match
the existing product. Publication uses a dedicated missing-only atomic route.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import re
import time
from urllib.parse import urlencode, urlparse

from bs4 import BeautifulSoup
import requests

from tr_price_scraper import TRPriceScraper
from update_prices import PriceUpdater
from catalog_recovery_limits import MAX_RECOVERY_CATALOG_PRODUCTS

CONTRACT = 'manufacturer-catalog-radio-v1'
HOST = 'https://my.infinixmobility.com'
MAP_URL = HOST + '/api/V1/xpark-app/product/specmap'
SPEC_PATH = '/api/V1/xpark-app/get-product-spec'
BOT = 'TeknoskorPriceBot'


def _attributes(product):
    raw = product.get('attributes')
    raw = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(raw, dict):
        raise ValueError('invalid_catalog_attributes')
    return raw


def _key(value):
    return re.sub(r'[\s-]', '', value.casefold()) if isinstance(value, str) else ''


def _single_field(section, label):
    values = []
    for row in section.select('.twoFth'):
        labels = row.select(':scope > p.twoTitle')
        if len(labels) != 1 or labels[0].get_text(' ', strip=True).casefold() != label.casefold():
            continue
        cells = row.select(':scope > .threeText')
        if len(cells) != 1:
            raise ValueError('manufacturer_field_ambiguous')
        values.append(cells[0].get_text(' ', strip=True))
    if len(values) != 1:
        raise ValueError('manufacturer_field_missing_or_ambiguous')
    return values[0]


def parse_infinix_radio(product, entry, response, *, observed_at=None):
    attrs = _attributes(product)
    model = TRPriceScraper._catalog_model_name(product['name'], attrs)
    if (not model or not model.casefold().startswith('infinix ')
            or 'network_generation' in attrs
            or PriceUpdater.get_expected_specs(attrs).get('network_generation') is not None
            or TRPriceScraper._radio_generation(model) is not None):
        raise ValueError('catalog_radio_not_missing')
    short = model[len('infinix '):]
    if (not isinstance(entry, dict) or not isinstance(entry.get('name'), str)
            or _key(entry['name']) != _key(short) or not re.fullmatch(r'[1-9]\d{0,8}', str(entry.get('pid')))):
        raise ValueError('manufacturer_model_mapping_mismatch')
    data = response.get('data') if isinstance(response, dict) else None
    if (not isinstance(response, dict) or type(response.get('code')) is not int or response['code'] != 200 or not isinstance(data, dict)
            or type(data.get('spec_status')) is not int or data['spec_status'] != 1
            or any(not isinstance(data.get(k), str) or len(data[k].encode()) > 1024 * 1024
                   for k in ('spec_top', 'spec_content'))):
        raise ValueError('manufacturer_specification_unavailable')
    names = BeautifulSoup(data['spec_top'], 'html.parser').select('.topFth p.name')
    if len(names) != 1 or _key(names[0].get_text(' ', strip=True)) != _key(entry['name']):
        raise ValueError('manufacturer_selected_model_mismatch')
    soup = BeautifulSoup(data['spec_content'], 'html.parser')
    sections = {}
    for section in soup.select('div.size'):
        titles = section.select(':scope > p.title')
        if len(titles) != 1:
            continue
        title = titles[0].get_text(' ', strip=True).casefold()
        if title in {'size and weight', 'network', 'memory'}:
            if title in sections:
                raise ValueError('manufacturer_section_ambiguous')
            sections[title] = section
    if set(sections) != {'size and weight', 'network', 'memory'}:
        raise ValueError('manufacturer_selected_sections_missing')
    code = _single_field(sections['size and weight'], 'MODEL')
    aliases = attrs.get('Technical sheet', {}).get('Aliases')
    aliases = aliases if isinstance(aliases, list) else [aliases]
    aliases = [a.strip().upper() for value in aliases if isinstance(value, str)
               for a in re.split(r'[,;|/\n]', value)]
    if (not re.fullmatch(r'[A-Z0-9-]{3,32}', code) or not re.search('[A-Z]', code)
            or not re.search(r'\d', code) or code not in aliases):
        raise ValueError('manufacturer_model_code_mismatch')
    memory = {}
    for key, label in [('ram_gb', 'RAM'), ('storage_gb', 'ROM')]:
        match = re.fullmatch(r'([1-9]\d{0,3})\s*GB', _single_field(sections['memory'], label), re.I)
        if match is None or type(attrs.get(key)) is not int or int(match[1]) != attrs[key]:
            raise ValueError('manufacturer_physical_memory_mismatch')
        memory[key] = int(match[1])
    raw = _single_field(sections['network'], 'TECHNOLOGY')
    if not re.fullmatch(r'[2345]G(?:\s*/\s*[2345]G){0,3}', raw, re.I):
        raise ValueError('manufacturer_network_not_explicit')
    technologies = [value.strip().lower() for value in raw.split('/')]
    if len(set(technologies)) != len(technologies) or '4g' not in technologies:
        raise ValueError('manufacturer_network_not_explicit')
    slug = re.sub(r'\s+', '-', entry['name'].lower())
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug):
        raise ValueError('manufacturer_model_route_invalid')
    return {'provider': 'infinix_public_specs', 'model_name': 'Infinix ' + entry['name'],
            'model_code': code, **memory, 'network_technologies': technologies,
            'source_url': HOST + '/specs/' + slug,
            'observed_at': observed_at or datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')}


class PublicInfinixRadioReader:
    """Public website JSON GETs, without accounts, redirects, retries or prices."""
    def __init__(self, get=None, sleep=None, *, maximum_documents=6):
        if type(maximum_documents) is not int or not 1 <= maximum_documents <= 6:
            raise ValueError('invalid_manufacturer_document_budget')
        self._session = None
        if get is None:
            self._session = requests.Session()
            self._session.trust_env = False  # Never load .netrc or environment credentials/proxies.
        self.get = get or self._session.get
        self.sleep = sleep or time.sleep
        self.maximum_documents = maximum_documents
        self.documents = 0
        self.blocked = False
        self.mapping = None

    def _get(self, url, maximum):
        if self._session is not None:
            self._session.cookies.clear()
        response = self.get(url, timeout=(10, 25), allow_redirects=False, stream=True,
            headers={'User-Agent': BOT + '/1.0 (+https://teknoskor.com)', 'Accept': 'application/json,text/plain'})
        try:
            if response.status_code != 200 or response.url != url:
                self.blocked = True
                raise ValueError('manufacturer_document_denied_or_changed')
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > maximum:
                    self.blocked = True
                    raise ValueError('manufacturer_document_oversized')
                chunks.append(chunk)
            return b''.join(chunks).decode('utf-8-sig', errors='strict')
        finally:
            response.close()
            if self._session is not None:
                self._session.cookies.clear()

    def _read(self, url):
        parsed = urlparse(url)
        if (parsed.scheme != 'https' or parsed.netloc != 'my.infinixmobility.com' or parsed.fragment
                or not (url == MAP_URL or parsed.path == SPEC_PATH and re.fullmatch(r'id=[1-9]\d{0,8}', parsed.query))):
            raise ValueError('invalid_public_manufacturer_document')
        if self.blocked:
            raise ValueError('manufacturer_source_circuit_open')
        try:
            if self.documents >= self.maximum_documents:
                raise ValueError('manufacturer_document_budget_exhausted')
            self.documents += 1
            self.sleep(0.5)
            return json.loads(self._get(url, 2 * 1024 * 1024))
        except (requests.RequestException, UnicodeError, json.JSONDecodeError):
            self.blocked = True
            raise ValueError('manufacturer_document_unavailable') from None

    def observe(self, product):
        attrs = _attributes(product)
        if ('network_generation' in attrs or PriceUpdater.get_expected_specs(attrs).get('network_generation') is not None
                or TRPriceScraper._radio_generation(product['name']) is not None):
            return None
        model = TRPriceScraper._catalog_model_name(product['name'], attrs)
        if not model or not model.casefold().startswith('infinix '):
            return None
        if self.mapping is None:
            document = self._read(MAP_URL)
            if (not isinstance(document, dict) or type(document.get('code')) is not int or document['code'] != 200
                    or not isinstance(document.get('data'), dict) or len(document['data']) > 2000):
                raise ValueError('manufacturer_model_mapping_unavailable')
            self.mapping = document['data']
        entry = self.mapping.get(_key(model[len('infinix '):]))
        if not isinstance(entry, dict) or not re.fullmatch(r'[1-9]\d{0,8}', str(entry.get('pid'))):
            raise ValueError('manufacturer_model_mapping_missing')
        document = self._read(HOST + SPEC_PATH + '?' + urlencode({'id': entry['pid']}))
        return parse_infinix_radio(product, entry, document)


class CatalogRadioPublicationError(RuntimeError):
    def __init__(self, report):
        self.report = report
        super().__init__('manufacturer_radio_publication_unconfirmed')


def resolve_catalog_radios(client, *, publish=False, reader=None, checkpoint=None):
    reader = reader or PublicInfinixRadioReader()
    before = client.get_published_products()
    if not before or len(before) > MAX_RECOVERY_CATALOG_PRODUCTS:
        raise ValueError('invalid_or_oversized_catalog')
    before = deepcopy(before)
    report = {'contract': CONTRACT, 'purpose': 'automatic_missing_catalog_radio',
              'publish': publish, 'writes': False, 'price_writes': False, 'rows': [], 'resolved': 0}
    published = {}
    for product in before:
        attrs = _attributes(product)
        if ('network_generation' in attrs or PriceUpdater.get_expected_specs(attrs).get('network_generation') is not None
                or TRPriceScraper._radio_generation(product['name']) is not None):
            continue
        row = {'product_id': product['id'], 'slug': product['slug'], 'status': 'source_unavailable', 'writes': False}
        report['rows'].append(row)
        try:
            observation = reader.observe(product)
            if observation is None:
                row['status'] = 'manufacturer_not_supported'
                continue
            row['observation'] = observation
            client.resolve_catalog_radio(product, observation, publish=False)
            row['status'] = 'verified_read_only'
        except Exception as error:
            row['reason'] = str(error) if isinstance(error, ValueError) and re.fullmatch(r'[a-z0-9_]{1,100}', str(error)) else type(error).__name__
            if checkpoint:
                checkpoint(report)
            continue
        if publish:
            row['writes'] = report['writes'] = True
            try:
                row['publication'] = client.resolve_catalog_radio(product, observation, publish=True)
                generation = '5g' if '5g' in observation['network_technologies'] else '4g'
                published[product['id']] = generation
                after = client.get_published_products()
                current = {p['id']: p for p in after}
                if len(current) != len(before) or len(after) != len(before):
                    raise ValueError('catalog_changed_after_radio_resolution')
                for original in before:
                    expected = {**_attributes(original)}
                    if original['id'] in published:
                        expected['network_generation'] = published[original['id']]
                    actual = current.get(original['id'])
                    if (actual is None or any(original[k] != actual.get(k) for k in ('id', 'slug', 'name'))
                            or json.dumps(_attributes(actual), sort_keys=True) != json.dumps(expected, sort_keys=True)):
                        raise ValueError('catalog_identity_or_other_attributes_changed')
                row.update(status='resolved', catalog_readback_verified=True)
                report['resolved'] += 1
            except Exception as error:
                row['status'] = 'publication_unconfirmed'
                row['reason'] = str(error) if isinstance(error, ValueError) and re.fullmatch(r'[a-z0-9_]{1,100}', str(error)) else type(error).__name__
                report['status'] = 'publication_verification_failed'
                if checkpoint:
                    checkpoint(report)
                raise CatalogRadioPublicationError(report) from error
        if checkpoint:
            checkpoint(report)
    report.update(status='completed', manufacturer_documents=reader.documents,
                  source_circuit_open=reader.blocked, catalog_count=len(before))
    if checkpoint:
        checkpoint(report)
    return report
