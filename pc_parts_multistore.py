"""Verify exact PC component GTINs across independent retailer companies."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from audit_hepsiburada_iphone16pm_readonly import parse_exact_buy_box
from hepsiburada_catalog_discovery import discover_product_sellers
from hepsiburada_catalog_source import _identity
from hepsiburada_s25_fe_source import _nodes
from laptop_direct_source import DIRECT_STORES, selected_direct_laptop
from laptop_pilot import _hepsiburada_public_get, verify_seller
from laptop_retailer_sources import WORDS, _legal
from n11_catalog_source import _parse_selected_listing as n11_selected_listing
from pc_parts import ROOT, parse_part
from priority_retailer_transport import PriorityRetailerTransport, compatible_get, product_host
from retailer_barcode import retailer_gtin13
from teknosa_marketplace_source import _selected_marketplace_commerce
from trendyol_catalog_source import _parse_selected_listing, _product_id, _selected_group_root, selected_product_state

GAMING = urlsplit(ROOT).netloc
INCEHESAP = 'www.incehesap.com'
HEPSIBURADA = 'www.hepsiburada.com'
TRENDYOL = 'www.trendyol.com'
N11 = 'www.n11.com'
TEKNOSA = 'www.teknosa.com'
DIRECT_PATHS = {
    GAMING: r'/urun/\d+/[a-z0-9-]+/',
    INCEHESAP: r'/[a-z0-9-]+-fiyati-\d+/',
    'pitrap.com': r'/urun/[a-z0-9-]+',
    'www.netsiparis.com': r'/[a-z0-9-]+',
    'www.uzmanlarpc.com': r'/urun/[a-z0-9-]+',
    'www.lowell.com.tr': r'/[a-z0-9-]+-\d+',
    'www.teknostim.com': r'/urun/[a-z0-9-]+',
    'www.sinerji.gen.tr': r'/[a-z0-9-]+-p-\d+',
}
HOSTS = DIRECT_PATHS.keys() | {HEPSIBURADA, TRENDYOL, N11, TEKNOSA}
STOP = {'source_circuit_open', 'challenge_page', 'http_403', 'http_429', 'transport_error'}
TEMPORARY_READ_FAILURES = STOP | {
    'source_cooldown', 'robots_http_403', 'robots_http_429', 'robots_transport_error',
    'http_500', 'http_502', 'http_503', 'http_504', 'pdp_budget_exhausted',
}
OFFER_MAX_AGE = timedelta(hours=72)


def offer_host(url):
    if not isinstance(url, str) or len(url) > 1000 or re.search(r'[\x00-\x20\\]', url):
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme != 'https' or parsed.netloc not in HOSTS or parsed.fragment:
        return None
    host = parsed.netloc
    if host not in DIRECT_PATHS:
        return product_host(url)
    return host if not parsed.query and re.fullmatch(DIRECT_PATHS[host], parsed.path) else None


def company_key(name):
    # Company suffixes vary across public seller profiles. Do not compare
    # marketplace nicknames when determining independence.
    name = re.sub(r' (?:anonim sirketi|limited sirketi|ltd sti|a s|as)$', '', WORDS(name))
    if name == 'beta plus teknoloji hizmetleri sanayi ticaret':
        # The public contact pages identify the same premises at Malazgirt Cad. 32/A.
        name = 'beta oyun teknoloji'
    return hashlib.sha256(name.replace(' ', '').encode()).hexdigest() if name else None


def processor_packaging(title):
    title = WORDS(title)
    for packaging, pattern in (
        ('mpk', r'\bmpk\b'), ('box', r'\b(?:box|kutulu)\b'),
        ('tray', r'\b(?:tray|kutusuz)\b'),
    ):
        if re.search(pattern, title):
            return packaging
    return None


def independent_offers(offers):
    """Keep the cheapest fresh listing per distinct documented company."""
    groups = []
    for offer in sorted(offers, key=lambda row: (row['price'], row['platform'], row['url'])):
        companies = {offer['sellerKey']}
        taxes = {offer['taxKey']} if offer.get('taxKey') else set()
        matches = [group for group in groups if group[0] & companies or group[1] & taxes]
        if matches:
            for group in matches:
                companies.update(group[0])
                taxes.update(group[1])
                groups.remove(group)
            offer = min((group[2] for group in matches), key=lambda row: row['price'])
        groups.append((companies, taxes, offer))
    return sorted((group[2] for group in groups), key=lambda row: (row['price'], row['platform'], row['url']))



def _trendyol_pc_public_get(url, **kwargs):
    # The selected merchant route uses the storefront's standard browser response.
    if _product_id(url) and 'merchantId' in parse_qs(urlsplit(url).query):
        kwargs['headers'] = {'Accept': kwargs['headers']['Accept']}
    return compatible_get(url, **kwargs)


def trendyol_pc_offer(html, url, *, default_html=None):
    def variant(product, title, brand):
        category = product.get('category')
        hierarchy = category.get('hierarchy', '') if isinstance(category, dict) else ''
        if 'bilesenler' not in WORDS(hierarchy):
            return None, 'not_selected_component_category'
        listing = product.get('merchantListing')
        seller = listing.get('merchant') if isinstance(listing, dict) else None
        return {'brand': brand.get('name'), **_legal(seller, 'officialName')}, None

    return _parse_selected_listing(
        html, url, variant,
        lambda schema, identity, source, _physical, brand: _selected_group_root(schema, identity, source, brand),
        default_html=default_html,
    )


def trendyol_pc_seller_targets(html, url, gtin):
    """Discover public alternate seller links, with no price carried over."""
    identity = _product_id(url)
    if not identity:
        return []
    product, _ = selected_product_state(BeautifulSoup(html, 'html.parser'))
    if not isinstance(product, dict) or str(product.get('id')) != identity:
        return []
    listing = product.get('merchantListing')
    others = listing.get('otherMerchants') if isinstance(listing, dict) else None
    if not isinstance(others, list) or len(others) > 100:
        return []
    targets = []
    for other in others:
        if not isinstance(other, dict) or type(other.get('id')) is not int:
            continue
        variants = other.get('variants')
        if not isinstance(variants, list) or not any(
                isinstance(row, dict) and row.get('sellable') is True and row.get('inStock') is True
                and retailer_gtin13(row.get('barcode')) == gtin for row in variants):
            continue
        href = other.get('url')
        if not isinstance(href, str) or len(href) > 1000:
            continue
        try:
            target = urljoin(url, href)
            if (_product_id(target) == identity
                    and parse_qs(urlsplit(target).query).get('merchantId') == [str(other['id'])]):
                targets.append(target)
        except ValueError:
            continue
    return list(dict.fromkeys(targets))


def n11_pc_offer(html, url):
    def selected(product):
        category = product.get('category')
        path = category.get('seoUrl', '') if isinstance(category, dict) else ''
        title = WORDS(product.get('title'))
        return (lambda value: path.startswith('/bilgisayar/bilgisayar-bilesenleri/')
                and WORDS(value.removesuffix(' Fiyatları ve Özellikleri')) == title
                and not re.search(r'\b(?:yenilenmis|ikinci el|teshir)\b', title)), {
                    'brand': product.get('productBrand'), **_legal(product.get('seller'), 'companyName')}
    return n11_selected_listing(html, url, '', {}, verify_laptop=selected)


def teknosa_pc_offer(html, url):
    def selected(cart, title, product):
        brand = product.get('brand')
        brand = brand.get('name') if isinstance(brand, dict) else brand
        if WORDS(brand) != WORDS(cart.get('data-product-brand')) or re.search(
                r'\b(?:yenilenmis|ikinci el|teshir)\b', WORDS(title)):
            return None
        return {'brand': brand}
    return _selected_marketplace_commerce(html, url, verify_product=selected)


def hepsiburada_pc_offer(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    scripts = soup.select('script#reduxStore')
    try:
        product = json.loads(scripts[0].get_text())['productState']['product'] if len(scripts) == 1 else {}
        sku, product_id, listing = (product[key] for key in ('sku', 'productId', 'listingId'))
        merchant = product['merchant']
        seller, seller_id, slug = (merchant[key] for key in ('name', 'id', 'urlPostfix'))
        categories = product['categories']
        variants = product['variants']
    except (TypeError, KeyError, ValueError, AttributeError):
        return None, 'selected_component_state_missing'
    if (not _identity(url, product_id, sku) or product.get('isBundle') is not False
            or not re.fullmatch(r'[a-z0-9-]{1,100}', str(slug))
            or not isinstance(categories, list) or not any(
                isinstance(row, dict) and row.get('categoryId') == '8' for row in categories)):
        return None, 'selected_component_mismatch'
    gtin = retailer_gtin13(product.get('barcode'))
    if not gtin:
        return None, 'selected_gtin_missing'
    if product.get('hasVariants') is False and variants == [] and not product.get('combinedVariants'):
        prices = product.get('prices')
        if not isinstance(prices, list) or len(prices) != 1 or not isinstance(prices[0], dict):
            return None, 'selected_cash_missing'
        variant = {'price': prices[0].get('value')}
    else:
        selected = [row for row in variants if isinstance(row, dict) and row.get('isDefault') is True]
        if (len(selected) != 1 or selected[0].get('sku') != sku
                or retailer_gtin13(selected[0].get('barcode')) != gtin
                or selected[0].get('isInStock') is not True):
            return None, 'selected_variant_unproven'
        variant = selected[0]
    schema = [row for row in _nodes(soup) if row.get('@type') == 'Product' and row.get('sku') == sku]
    if len(schema) != 1 or retailer_gtin13(schema[0].get('gtin')) != gtin:
        return None, 'schema_gtin_mismatch'
    schema_offer = schema[0].get('offers')
    schema_url = schema_offer.get('url') if isinstance(schema_offer, dict) else None
    if not _identity(schema_url, product_id, sku):
        return None, 'schema_variant_url_mismatch'
    requested = parse_qs(urlsplit(url).query).get('magaza')
    if requested and WORDS(requested[0]) not in {WORDS(seller), WORDS(slug)}:
        return None, 'selected_seller_changed'
    seller_url = 'https://www.hepsiburada.com/magaza/' + slug
    scope = dict(URL=url, SCHEMA_URL=schema_url, PRODUCT_ID=product_id, SKU=sku, LISTING_ID=listing,
                 MERCHANT_ID=seller_id, MERCHANT_POSTFIX=slug, SELLER=seller, SELLER_URL=seller_url,
                 GTIN13=gtin, generic_catalog=True)
    offer, reason = parse_exact_buy_box(
        html, url, scope=scope,
        verify_laptop=lambda _soup, _product, title: None if re.search(
            r'ikinci\s*el|yenilenmi[sş]|refurbished', title, re.I) else variant,
    )
    if offer:
        offer.update(platform='Hepsiburada', seller_url=seller_url, brand=product.get('brand'))
    return offer, reason


def hepsiburada_pc_seller_targets(html, url, gtin):
    """Discover seller selections from public PDP inventory, including group URLs."""
    if offer_host(url) != HEPSIBURADA or urlsplit(url).query:
        return []
    scripts = BeautifulSoup(html, 'html.parser').select('script#reduxStore')
    if len(scripts) != 1:
        return []
    try:
        product = json.loads(scripts[0].get_text())['productState']['product']
        current = product['merchant']['id']
        listings = product['listings']
        if (not _identity(url, product['productId'], product['sku'])
                or retailer_gtin13(product['barcode']) != gtin
                or product.get('isBundle') is not False
                or not isinstance(listings, list) or len(listings) > 50):
            return []
    except (KeyError, TypeError, ValueError, AttributeError):
        return []

    targets = []
    for listing in listings:
        if not isinstance(listing, dict) or listing.get('isSalable') is not True or listing.get('isBundle') is not False:
            continue
        name, merchant = listing.get('merchantName'), listing.get('merchantInfo')
        if (not isinstance(name, str) or not isinstance(merchant, dict)
                or merchant.get('id') != listing.get('merchantId') or merchant.get('id') == current
                or merchant.get('name') != name or not re.fullmatch(r'[a-z0-9-]{1,100}', str(merchant.get('urlPostfix')))
                or not all(re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', str(listing.get(key)))
                           for key in ('merchantId', 'listingId'))):
            continue
        target = url + '?' + urlencode({'magaza': name})
        if offer_host(target) == HEPSIBURADA:
            targets.append(target)
    return list(dict.fromkeys(targets))


class PcRetailer:
    def __init__(self, host, budget=150, *, document_reader=None):
        self.host = host
        get = document_reader or {HEPSIBURADA: _hepsiburada_public_get,
                                  TRENDYOL: _trendyol_pc_public_get}.get(host, compatible_get)
        self.transport = PriorityRetailerTransport(
            maximum_per_host=budget, browser_fallback=False, get=get,
        )
        self.identities = {}
        self.stop = False
        self.unavailable = set()
        self.rejected = Counter()
        self.seller_targets = {}
        self.trendyol_default_document = (None, None)

    def read(self, url, part, *, discover_sellers=False, alternatives=()):
        if offer_host(url) != self.host or self.stop:
            return None
        html, reason, _ = self.transport._read_document(url, self.host)
        if not html:
            self.rejected[reason] += 1
            if reason in TEMPORARY_READ_FAILURES:
                self.unavailable.add(url)
            self.stop = reason in STOP or reason.startswith('robots_')
            return None
        final = self.transport.resolved_url(url)
        try:
            raw, reason = self.parse(html, final, part)
        except (KeyError, ValueError, TypeError, AttributeError):
            raw, reason = None, 'invalid_source_data'
        if raw and alternatives and raw['gtin13'] != part['gtin']:
            part = next((candidate for candidate in alternatives if candidate['gtin'] == raw['gtin13']), part)
        if discover_sellers and not urlsplit(final).query:
            if self.host == TRENDYOL:
                self.seller_targets.update((target, part) for target in trendyol_pc_seller_targets(
                    html, final, part['gtin']))
            elif self.host == HEPSIBURADA:
                self.seller_targets.update((target, part) for target in hepsiburada_pc_seller_targets(
                    html, final, part['gtin']))
                if raw and raw['gtin13'] == part['gtin']:
                    self.seller_targets.update((row['source_url'], part) for row in discover_product_sellers(html, final))
        if raw is None:
            self.rejected[reason] += 1
            return None
        if (raw['gtin13'] != part['gtin'] or WORDS(raw.get('brand')) != WORDS(part['brand'])
                or raw.get('price_kind') == 'basket'):
            self.rejected['gtin_brand_or_cash_mismatch'] += 1
            return None
        if self.host != GAMING and part['kind'] == 'cpu':
            expected = processor_packaging(part['name'])
            if expected and processor_packaging(raw.get('source_title')) != expected:
                self.rejected['processor_packaging_mismatch'] += 1
                return None
        if not self.verify_seller(raw):
            self.rejected['seller_company_unverified'] += 1
            return None
        key = company_key(raw.get('_legal_name'))
        if not key:
            self.rejected['seller_company_unverified'] += 1
            return None
        row = {'gtin': raw['gtin13'], 'sellerKey': key, 'price': raw['price'],
               'currency': 'TRY', 'platform': raw['platform'], 'seller': raw['merchant'],
               'stock': 'in_stock', 'checkedAt': raw['observed_at'], 'url': url}
        if raw.get('_tax_key'):
            row['taxKey'] = hashlib.sha256(raw['_tax_key'].encode()).hexdigest()
        return row

    def verify_seller(self, raw):
        return verify_seller(raw, None, self.transport, self.identities)

    def parse(self, html, url, part):
        if self.host == GAMING:
            actual = parse_part(html, url, part['kind'])
            if part.get('sourceUrl') == url and (
                    actual['mpn'] != part['mpn'] or actual['specs'] != part['specs']):
                return None, 'manufacturer_specifications_changed'
            offer = actual['offer']
            _, _, _, legal_path, _ = DIRECT_STORES[GAMING]
            return {'gtin13': actual['gtin'], 'brand': actual['brand'], 'price': offer['price'],
                    'platform': offer['platform'], 'merchant': offer['seller'], 'merchant_id': GAMING,
                    'observed_at': offer['checkedAt'], 'seller_url': ROOT + legal_path}, 'verified'
        if self.host == TRENDYOL:
            row, reason = trendyol_pc_offer(html, url)
            route = urlsplit(url)
            if (reason in {'selected_variant_mismatch', 'selected_price_mismatch', 'schema_selected_offer_mismatch'}
                    and 'merchantId' in parse_qs(route.query)):
                canonical = route._replace(query='').geturl()
                if self.trendyol_default_document[0] != canonical:
                    default, _, _ = self.transport._read_document(canonical, self.host, fresh=True)
                    if self.transport.resolved_url(canonical) != canonical:
                        default = None
                    self.trendyol_default_document = (canonical, default)
                if self.trendyol_default_document[1]:
                    return trendyol_pc_offer(html, url, default_html=self.trendyol_default_document[1])
            return row, reason
        if self.host == HEPSIBURADA:
            return hepsiburada_pc_offer(html, url)
        if self.host == N11:
            return n11_pc_offer(html, url)
        if self.host == TEKNOSA:
            return teknosa_pc_offer(html, url)
        offer, reason = selected_direct_laptop(html, url, read_facts=lambda _title, brand: {'brand': brand})
        if offer:
            offer['brand'] = offer['facts']['brand']
        return offer, reason


def collect_targets(targets, *, discover_sellers=False, alternatives_by_url=None, unavailable=None, document_readers=None):
    alternatives_by_url = alternatives_by_url or {}
    by_host = {}
    for url, part in targets:
        if host := offer_host(url):
            by_host.setdefault(host, []).append((url, part))

    def collect(host, rows):
        reader = PcRetailer(host, budget=min(400, max(150, len(rows) * 2)),
                            document_reader=(document_readers or {}).get(host))
        results = []
        for index, (url, part) in enumerate(rows):
            if offer := reader.read(url, part, discover_sellers=discover_sellers, alternatives=alternatives_by_url.get(url, ())):
                results.append(offer)
            if reader.stop:
                if url in reader.unavailable:
                    reader.unavailable.update(target for target, _ in rows[index + 1:])
                break
        visited = {url for url, _ in rows}
        for url, part in reader.seller_targets.items():
            if reader.stop:
                break
            if url not in visited and (offer := reader.read(url, part)):
                results.append(offer)
        print(json.dumps({'host': host, 'verified': len(results), 'rejected': dict(reader.rejected)},
                         ensure_ascii=False), flush=True)
        return results, reader.unavailable

    with ThreadPoolExecutor(max_workers=min(len(by_host) or 1, 6)) as pool:
        jobs = [pool.submit(collect, host, rows) for host, rows in by_host.items()]
        offers = []
        for job in jobs:
            verified, unread = job.result()
            offers.extend(verified)
            if unavailable is not None:
                unavailable.update(unread)
        return offers


def retained_observations(part, unavailable, now):
    previous = []
    for offer in part.get('offers', []):
        if (offer['url'] not in unavailable or offer.get('gtin') != part['gtin']
                or offer.get('stock') != 'in_stock' or not offer.get('sellerKey')):
            continue
        try:
            timestamp = datetime.fromisoformat(offer['checkedAt'])
            if timedelta(0) <= now - timestamp <= OFFER_MAX_AGE:
                previous.append(offer)
        except (ValueError, TypeError, KeyError):
            continue
    return previous


def refresh(snapshot, output, *, discovery=None):
    targets = []
    for part in snapshot['parts']:
        saved = part.get('offerTargets') or [offer['url'] for offer in part.get('offers', [part['offer']])]
        targets.extend((url, part) for url in dict.fromkeys([part['sourceUrl'], *saved]))
    targets = {url: part for url, part in targets}
    if discovery is not None:
        parts = {part['gtin']: part for part in snapshot['parts']}
        for gtin, urls in discovery:
            if gtin in parts:
                for url in urls:
                    if offer_host(url):
                        targets.setdefault(url, parts[gtin])
    last_check = {offer['url']: offer['checkedAt'] for part in snapshot['parts'] for offer in part.get('offers', [])}
    ordered = sorted(targets.items(), key=lambda row: (last_check.get(row[0], ''), row[0]))
    unavailable = set()
    verified = collect_targets(ordered, discover_sellers=discovery is not None, unavailable=unavailable)
    now = datetime.now(timezone.utc)
    if not verified:
        raise SystemExit('No component prices verified')
    by_gtin = {}
    for row in verified:
        by_gtin.setdefault(row['gtin'], []).append(row)
    retained_count = 0
    for part in snapshot['parts']:
        observations = by_gtin.get(part['gtin'], [])
        retained = retained_observations(part, unavailable, now)
        retained_count += len(retained)
        if discovery is not None:
            existing = part.get('offerTargets') or [part['sourceUrl'], *(row['url'] for row in part.get('offers', []))]
            part['offerTargets'] = list(dict.fromkeys([*existing, *(row['url'] for row in observations)]))
        offers = independent_offers([*observations, *retained])
        part['offers'] = offers
        if offers:
            part['offer'] = offers[0]
    snapshot['checkedAt'] = now.isoformat()
    output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'parts': len(snapshot['parts']), 'verified_offers': len(verified),
                      'retained_unavailable_offers': retained_count,
                      'with_two_sellers': sum(len(part['offers']) >= 2 for part in snapshot['parts'])}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--discover-from', type=Path, help='JSON pairs of catalog GTIN and candidate product URLs')
    args = parser.parse_args()
    refresh(json.loads(args.refresh.read_text()), args.output,
            discovery=json.loads(args.discover_from.read_text()) if args.discover_from else None)


if __name__ == '__main__':
    main()
