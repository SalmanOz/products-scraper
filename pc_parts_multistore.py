"""Verify exact PC component GTINs across independent retailer companies."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from audit_hepsiburada_iphone16pm_readonly import parse_exact_buy_box
from hepsiburada_catalog_source import _identity
from hepsiburada_s25_fe_source import _nodes
from laptop_direct_source import DIRECT_STORES, selected_direct_laptop
from laptop_pilot import _hepsiburada_public_get, verify_seller
from laptop_retailer_sources import WORDS, _legal
from pc_parts import ROOT, parse_part
from priority_retailer_transport import PriorityRetailerTransport, compatible_get, product_host
from retailer_barcode import retailer_gtin13
from trendyol_catalog_source import _parse_selected_listing, _selected_group_root

GAMING = urlsplit(ROOT).netloc
INCEHESAP = 'www.incehesap.com'
HEPSIBURADA = 'www.hepsiburada.com'
TRENDYOL = 'www.trendyol.com'
DIRECT = {GAMING, INCEHESAP}
HOSTS = DIRECT | {HEPSIBURADA, TRENDYOL}
STOP = {'source_circuit_open', 'challenge_page', 'http_403', 'http_429', 'transport_error'}


def offer_host(url):
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.netloc not in HOSTS or parsed.fragment:
        return None
    host = parsed.netloc
    if host not in DIRECT:
        return product_host(url)
    if parsed.query:
        return None
    if host == GAMING:
        return host if re.fullmatch(r'/urun/\d+/[a-z0-9-]+/', parsed.path) else None
    return host if re.fullmatch(r'/[a-z0-9-]+-fiyati-\d+/', parsed.path) else None


def company_key(name):
    # Company suffixes vary across public seller profiles. Do not compare
    # marketplace nicknames when determining independence.
    name = re.sub(r' (?:anonim sirketi|limited sirketi|ltd sti|a s|as)$', '', WORDS(name))
    return hashlib.sha256(name.replace(' ', '').encode()).hexdigest() if name else None


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



def trendyol_pc_offer(html, url):
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
    )


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


class PcRetailer:
    def __init__(self, host, budget=150):
        self.host = host
        self.transport = PriorityRetailerTransport(
            maximum_per_host=budget, browser_fallback=False,
            get=_hepsiburada_public_get if host == HEPSIBURADA else compatible_get,
        )
        self.identities = {}
        self.stop = False
        self.rejected = Counter()

    def read(self, url, part):
        if offer_host(url) != self.host or self.stop:
            return None
        html, reason, _ = self.transport._read_document(url, self.host)
        if not html:
            self.rejected[reason] += 1
            self.stop = reason in STOP or reason.startswith('robots_')
            return None
        final = self.transport.resolved_url(url)
        try:
            raw, reason = self.parse(html, final, part)
        except (KeyError, ValueError, TypeError, AttributeError):
            raw, reason = None, 'invalid_source_data'
        if raw is None:
            self.rejected[reason] += 1
            return None
        if (raw['gtin13'] != part['gtin'] or WORDS(raw.get('brand')) != WORDS(part['brand'])
                or raw.get('price_kind') == 'basket'):
            self.rejected['gtin_brand_or_cash_mismatch'] += 1
            return None
        if not verify_seller(raw, None, self.transport, self.identities):
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

    def parse(self, html, url, part):
        if self.host == GAMING:
            actual = parse_part(html, url, part['kind'])
            if actual['mpn'] != part['mpn'] or actual['specs'] != part['specs']:
                return None, 'manufacturer_specifications_changed'
            offer = actual['offer']
            _, _, _, legal_path, _ = DIRECT_STORES[GAMING]
            return {'gtin13': actual['gtin'], 'brand': actual['brand'], 'price': offer['price'],
                    'platform': offer['platform'], 'merchant': offer['seller'], 'merchant_id': GAMING,
                    'observed_at': offer['checkedAt'], 'seller_url': ROOT + legal_path}, 'verified'
        if self.host == TRENDYOL:
            return trendyol_pc_offer(html, url)
        if self.host == HEPSIBURADA:
            return hepsiburada_pc_offer(html, url)
        offer, reason = selected_direct_laptop(html, url, read_facts=lambda _title, brand: {'brand': brand})
        if offer:
            offer['brand'] = offer['facts']['brand']
        return offer, reason


def collect_targets(targets):
    by_host = {}
    for url, part in targets:
        if host := offer_host(url):
            by_host.setdefault(host, []).append((url, part))

    def collect(host, rows):
        reader = PcRetailer(host, budget=max(150, len(rows) * 2))
        results = []
        for url, part in rows:
            if reader.stop:
                break
            if offer := reader.read(url, part):
                results.append(offer)
        print(json.dumps({'host': host, 'verified': len(results), 'rejected': dict(reader.rejected)},
                         ensure_ascii=False), flush=True)
        return results

    with ThreadPoolExecutor(max_workers=min(len(by_host) or 1, 6)) as pool:
        jobs = [pool.submit(collect, host, rows) for host, rows in by_host.items()]
        return [offer for job in jobs for offer in job.result()]


def refresh(snapshot, output):
    targets = []
    for part in snapshot['parts']:
        saved = part.get('offerTargets') or [offer['url'] for offer in part.get('offers', [part['offer']])]
        targets.extend((url, part) for url in dict.fromkeys([part['sourceUrl'], *saved]))
    targets = list({url: (url, part) for url, part in targets}.values())
    verified = collect_targets(targets)
    now = datetime.now(timezone.utc)
    # A failed reader does not create a new observation or advance a timestamp.
    # Unconfirmed prices are omitted until the source can verify them again.
    if not verified:
        raise SystemExit('No component prices verified')
    for part in snapshot['parts']:
        offers = independent_offers(row for row in verified if row['gtin'] == part['gtin'])
        part['offers'] = offers
        if offers:
            part['offer'] = offers[0]
    snapshot['checkedAt'] = now.isoformat()
    output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'parts': len(snapshot['parts']), 'verified_offers': len(verified),
                      'with_two_sellers': sum(len(part['offers']) >= 2 for part in snapshot['parts'])}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    refresh(json.loads(args.refresh.read_text()), args.output)


if __name__ == '__main__':
    main()
