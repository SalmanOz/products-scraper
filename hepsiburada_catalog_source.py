"""Selected HB marketplace SKU proof from catalog hints, with no phone profiles."""

import json
import re
from urllib.parse import parse_qsl, urlencode, urlparse

from bs4 import BeautifulSoup

from hepsiburada_s25_fe_source import _nodes, _normalized
from priority_retailer_transport import PriorityRetailerTransport, product_host
from tr_price_scraper import TRPriceScraper


def _valid_gtin13(value):
    from mediamarkt_marketplace_audit import _gtin13
    return _gtin13(value) is not None


def _identity(url, product_id, sku):
    if product_host(url) != 'www.hepsiburada.com':
        return False
    match = re.search(r'-p(m)?-([A-Z0-9]+)$', urlparse(url).path)
    return bool(match and match[2] == (product_id if match[1] else sku))


def catalog_request_url(url):
    """Keep an explicit seller selector; it must match the fresh selected seller.

    Only normalize query encoding. The query-free SKU is a separate fallback,
    never an offer attributed to the historical or newly discovered seller.
    """
    if product_host(url) != 'www.hepsiburada.com':
        return None
    parsed = urlparse(url)
    return parsed._replace(query=urlencode(parse_qsl(parsed.query))).geturl()


def selected_technical_text(soup, sku, product_id, listing_id, merchant_id):
    tables = soup.select('#techSpecs[data-test-id="KeyFeaturesTable"]')
    if tables:
        # Public iPhone 12 PDP renders two identical copies of this component.
        # Keep every field, including SKU and condition, in the comparison;
        # a conflicting or additional copy cannot supply selected evidence.
        texts = [_normalized(table.get_text(' ', strip=True)) for table in tables]
        return texts[0] if len(texts) <= 2 and len(set(texts)) == 1 else ''
    # The newer table has no stable CSS class/id. Its own public component
    # event must bind the container to this exact selected SKU and listing.
    found = []
    for node in soup.select('[data-hbus]'):
        if _normalized(node.get_text(' ', strip=True)) != 'Ürün özellikleri':
            continue
        try:
            event = json.loads(node['data-hbus']).get('data', {})
        except (ValueError, TypeError, AttributeError):
            continue
        if (isinstance(event, dict) and event.get('component') == 'Error Reporting'
                and event.get('page_type') == 'pdp' and event.get('page_value') == sku
                and event.get('product_id') == product_id and event.get('listing_id') == listing_id
                and event.get('merchant_id') == merchant_id and node.parent is not None):
            found.append(_normalized(node.parent.get_text(' ', strip=True)))
    return found[0] if len(found) == 1 else ''


def parse_catalog_buy_box(html, url, name, specs):
    from audit_hepsiburada_iphone16pm_readonly import parse_exact_buy_box
    if (product_host(url) != 'www.hepsiburada.com' or not isinstance(html, str)
            or len(html.encode('utf-8')) > 5 * 1024 * 1024):
        return None, 'invalid_catalog_pdp'
    if not all(TRPriceScraper._spec_int(specs.get(k)) for k in ('ram_gb', 'storage_gb')):
        return None, 'catalog_memory_missing'
    soup = BeautifulSoup(html, 'html.parser')
    scripts = soup.select('script#reduxStore')
    if len(scripts) != 1:
        return None, 'redux_missing'
    try:
        root = json.loads(scripts[0].get_text())
        product = root['productState']['product']
        product_id, sku = product['productId'], product['sku']
        merchant = product['merchant']
        seller, merchant_id, postfix = merchant['name'], merchant['id'], merchant['urlPostfix']
        listing_id = product['listingId']
    except (TypeError, ValueError, KeyError):
        return None, 'selected_redux_identity_missing'
    uuid = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
    if (not isinstance(product_id, str) or not re.fullmatch(r'(?:HBC[A-Z0-9]{5,30}|HB[0-9][A-Z0-9]{4,29})', product_id)
            or not isinstance(sku, str) or not re.fullmatch(r'(?:HBCV[A-Z0-9]{5,30}|HBV[0-9][A-Z0-9]{4,29})', sku)
            or not _identity(url, product_id, sku)
            or not isinstance(seller, str) or not 1 <= len(seller.strip()) <= 100
            or re.search(r'[\x00-\x1f]', seller)
            or not isinstance(merchant_id, str) or not re.fullmatch(uuid, merchant_id)
            or not isinstance(listing_id, str) or not re.fullmatch(uuid, listing_id)
            or not isinstance(postfix, str) or not re.fullmatch(r'[a-z0-9-]{1,100}', postfix)):
        return None, 'selected_redux_identity_mismatch'
    variants = product.get('variants')
    if not isinstance(variants, list):
        return None, 'selected_variant_missing'
    selected = [v for v in variants if isinstance(v, dict)
                and v.get('isDefault') is True and v.get('sku') == sku]
    if len(selected) != 1:
        return None, 'selected_variant_missing'
    properties = selected[0].get('properties')
    if not isinstance(properties, list):
        return None, 'selected_properties_missing'
    colors = [p.get('valueObject', {}).get('actualValue') for p in properties
              if isinstance(p, dict) and p.get('name') == 'Renk' and isinstance(p.get('valueObject'), dict)]
    barcodes = selected[0].get('barcodes')
    gtins = sorted(set(g for g in barcodes if _valid_gtin13(g))) if isinstance(barcodes, list) else []
    if len(colors) != 1 or not isinstance(colors[0], str) or not 0 < len(colors[0]) <= 80 or not 1 <= len(gtins) <= 8:
        return None, 'selected_color_or_gtin_missing'
    schemas = [p for p in _nodes(soup) if p.get('@type') == 'Product' and p.get('sku') == sku]
    if len(schemas) != 1 or not isinstance(schemas[0].get('offers'), dict):
        return None, 'schema_selected_product_missing'
    schema_url = schemas[0]['offers'].get('url')
    matcher = TRPriceScraper()
    if (not _identity(schema_url, product_id, sku)
            or not matcher.is_strict_match(name, str(schemas[0].get('name') or ''), specs)):
        return None, 'schema_catalog_identity_mismatch'
    for key, value in parse_qsl(urlparse(url).query):
        if key == 'magaza' and matcher._normalize_words(value) not in {
                matcher._normalize_words(seller), matcher._normalize_words(postfix)}:
            return None, 'selected_query_seller_mismatch'
    text = selected_technical_text(soup, sku, product_id, listing_id, merchant_id)
    if re.search(r'\bKondisyon\s+(?:(?:[ABC]\s*Grade)|yenilen|ikinci|teşhir|teshir)', text, re.I):
        return None, 'selected_used_condition'
    radios = re.findall(r'\bMobil Bağlantı Hızı\s+(5G|4[.,]5G|4G)\b', text, re.I)
    if len(radios) > 1:
        return None, 'selected_radio_ambiguous'
    radio = ('5g' if radios[0].casefold() == '5g' else '4g') if radios else None
    if (radio is None and 'iphone' not in matcher._identity_words(name)
            and matcher._radio_generation(str(schemas[0].get('name') or '')) is None):
        return None, 'selected_radio_missing'
    expected_radio = specs.get('network_generation') or matcher._radio_generation(name)
    if radio and expected_radio and expected_radio != radio:
        return None, 'selected_radio_mismatch'
    physical = {'ram_gb': matcher._spec_int(specs['ram_gb']),
                'storage_gb': matcher._spec_int(specs['storage_gb']), 'network_generation': radio}
    scope = {'URL': url, 'SCHEMA_URL': schema_url, 'CATALOG': {'name': name},
             'SPECS': physical, 'PRODUCT_ID': product_id, 'SKU': sku,
             'COLOR': colors[0], 'GTIN13': gtins[0] if len(gtins) == 1 else None,
             'GTIN13S': gtins, 'CATALOG_SPECS': dict(specs), 'SELLER': seller,
             'SELLER_URL': 'https://www.hepsiburada.com/magaza/' + postfix,
             'MERCHANT_ID': merchant_id, 'MERCHANT_POSTFIX': postfix,
             'LISTING_ID': listing_id, 'generic_catalog': True}
    offer, reason = parse_exact_buy_box(html, url, scope=scope)
    if offer is not None:
        offer.update(platform='Hepsiburada', selected_hb_variant_verified=True,
                     selected_hb_seller=seller, selected_hb_merchant_id=merchant_id,
                     seller_url=scope['SELLER_URL'], **physical)
    return offer, reason


def catalog_offer_matches(offer, name, specs):
    matcher = TRPriceScraper()
    return (isinstance(offer, dict) and offer.get('selected_hb_variant_verified') is True
            and offer.get('platform') == 'Hepsiburada'
            and offer.get('merchant') == offer.get('selected_hb_seller')
            and offer.get('merchant_id') == offer.get('selected_hb_merchant_id')
            and offer.get('availability') == 'in_stock' and offer.get('condition') == 'new'
            and offer.get('currency') == 'TRY'
            and isinstance(offer.get('merchant'), str) and 0 < len(offer['merchant']) <= 100
            and _identity(offer.get('url'), offer.get('product_id'), offer.get('sku'))
            and isinstance(offer.get('observed_gtins'), list) and 1 <= len(offer['observed_gtins']) <= 8
            and all(_valid_gtin13(g) for g in offer['observed_gtins'])
            and len(set(offer['observed_gtins'])) == len(offer['observed_gtins'])
            and offer.get('gtin13') == (offer['observed_gtins'][0] if len(offer['observed_gtins']) == 1 else None)
            and all(offer.get(k) == matcher._spec_int(specs.get(k)) for k in ('ram_gb', 'storage_gb'))
            and (not offer.get('network_generation') or not specs.get('network_generation')
                 or offer['network_generation'] == specs['network_generation'])
            and matcher.has_observed_offer_evidence(name, offer, specs))


class HepsiburadaCatalogSource:
    def __init__(self, get=None, post=None, transport=None):
        self.transport = transport or PriorityRetailerTransport(get=get, post=post, maximum_per_host=6)
        self._profiles = {}
        self._ambiguous = set()

    def register_catalog_targets(self, name, targets, specs):
        matcher = TRPriceScraper()
        if not all(matcher._spec_int(specs.get(k)) for k in ('ram_gb', 'storage_gb')):
            return
        for row in targets:
            url = row.get('source_url') if isinstance(row, dict) else None
            if (product_host(url) != 'www.hepsiburada.com'
                    or not matcher.is_strict_match(name, urlparse(url).path.replace('-', ' '), specs)):
                continue
            profile = {'name': name, 'specs': dict(specs)}
            if url in self._profiles and self._profiles[url] != profile:
                self._ambiguous.add(url)
            self._profiles[url] = profile

    def read_offer(self, url):
        profile = self._profiles.get(url)
        if profile is None or url in self._ambiguous:
            return None, 'unmatched_catalog_pdp', None
        observed_url = catalog_request_url(url)
        html, reason, status = self.transport.read_document(observed_url)
        if html is None:
            return None, reason, status
        offer, reason = parse_catalog_buy_box(html, observed_url, profile['name'], profile['specs'])
        if (offer is None and urlparse(observed_url).query and reason in {
                'selected_query_seller_mismatch', 'selected_redux_identity_missing'}):
            # Preserve default-buy-box recovery for retired historical sellers.
            # The fallback gets its own fresh document and query-free source;
            # exact candidate publication will reject a changed source identity.
            fallback = urlparse(observed_url)._replace(query='').geturl()
            html, transport_reason, status = self.transport.read_document(fallback)
            if html is None:
                return None, transport_reason, status
            offer, reason = parse_catalog_buy_box(html, fallback, profile['name'], profile['specs'])
        return offer, reason, status

    def configure_catalog_budget(self, count):
        self.transport.configure_catalog_budget(count)

    def report(self):
        return self.transport.report()
