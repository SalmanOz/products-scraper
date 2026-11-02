"""Two reviewed MediaMarkt marketplace buy boxes, with real seller identity.

The transport shares MediaMarkt's robots policy and bounded direct/browser
budgets. A marketplace price never becomes a MediaMarkt first-party offer.
"""

from datetime import datetime, timezone
import math
import re
import requests

from mediamarkt_marketplace_audit import (catalog_variant_matches,
                                         selected_marketplace_offer)
from mediamarkt_source import BOT, MAX_HTML_BYTES, MediaMarktSource, product_url


PROFILES = {
    'https://www.mediamarkt.com.tr/tr/product/_samsung-galaxy-s25-ultra-5g-12-gb-256-gb-akilli-telefon-titanyum-mavi-166438395.html':
        {'id': 30, 'slug': 'samsung-galaxy-s25-ultra',
         'name': 'Samsung Galaxy S25 Ultra', 'ram_gb': 12, 'storage_gb': 256,
         'network_generation': '5g', 'sku': '166438395',
         'seller_name': 'CepHane Teknoloji', 'seller_id': '11289'},
    'https://www.mediamarkt.com.tr/tr/product/_samsung-galaxy-s24-ultra-5g-12-gb-256-gb-akilli-telefon-mor-166438383.html':
        {'id': 28, 'slug': 'samsung-galaxy-s24-ultra',
         'name': 'Samsung Galaxy S24 Ultra', 'ram_gb': 12, 'storage_gb': 256,
         'network_generation': '5g', 'sku': '166438383',
         'seller_name': 'Başaran Ticaret', 'seller_id': '12140'},
}

# MediaMarkt omits radio generation on this selected SKU, and its generic
# "Mobil Telefon Standardı: No" feature is unusable. The selected GTIN is
# independently listed as S24 FE blue 8/128 5G by Trendyol; Samsung Türkiye's
# blue 8/128 product specification confirms 5G Sub6. This exception cannot
# apply to another SKU, EAN, seller, capacity or catalog radio generation.
# https://www.trendyol.com/samsung/galaxy-s24-fe-8-gb-ram-128-gb-mavi-p-857140583
# https://www.samsung.com/tr/smartphones/galaxy-s/galaxy-s24-fe-blue-128gb-sm-s721blbdtur/
S24FE_URL = ('https://www.mediamarkt.com.tr/tr/product/'
             '_samsung-galaxy-s24-fe-8-gb-128-gb-akilli-telefon-mavi-166774856.html')
S24FE_PROFILES = {S24FE_URL:
    {'id': 220, 'slug': 'samsung-galaxy-s24fe', 'name': 'Samsung Galaxy S24 FE',
     'ram_gb': 8, 'storage_gb': 128, 'network_generation': '5g',
     'sku': '166774856', 'gtin13': '8806095750163',
     'seller_name': 'VintageOnline', 'seller_id': '12953'}}
S24FE_RADIO_EVIDENCE = {
    'method': 'reviewed_exact_gtin_5g',
    'gtin13': '8806095750163',
    'samsung_model_spec':
        'https://www.samsung.com/tr/smartphones/galaxy-s/galaxy-s24-fe-blue-128gb-sm-s721blbdtur/',
    'exact_gtin_retailer':
        'https://www.trendyol.com/samsung/galaxy-s24-fe-8-gb-ram-128-gb-mavi-p-857140583',
}
ALL_PROFILES = {**PROFILES, **S24FE_PROFILES}


def reviewed_marketplace_offer(offer, product_name, expected_specs, product=None):
    """Verify an observation from the exact selected new-seller reader."""
    if not isinstance(offer, dict) or not isinstance(expected_specs, dict):
        return False
    profile = ALL_PROFILES.get(offer.get('url'))
    if not profile or product_name != profile['name']:
        return False
    if product is not None and (product.get('id') != profile['id'] or
                                product.get('slug') != profile['slug'] or
                                product.get('name') != profile['name']):
        return False
    try:
        ram = int(expected_specs['ram_gb'])
        storage = int(expected_specs['storage_gb'])
        price = float(offer['price'])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    if (ram != profile['ram_gb'] or storage != profile['storage_gb'] or
            expected_specs.get('network_generation') != profile['network_generation'] or
            not math.isfinite(price) or price <= 0 or price > 5_000_000 or
            offer.get('merchant') != profile['seller_name'] or
            offer.get('seller_name') != profile['seller_name'] or
            offer.get('seller_id') != profile['seller_id'] or
            offer.get('sku') != profile['sku'] or
            offer.get('platform') != 'MediaMarkt' or
            offer.get('condition') != 'new' or
            offer.get('currency') != 'TRY' or
            offer.get('availability') != 'in_stock' or
            not isinstance(offer.get('offer_id'), str) or
            not re.fullmatch(r'\d{1,15}', offer['offer_id'])):
        return False
    if offer.get('url') == S24FE_URL:
        if (offer.get('gtin13') != profile['gtin13'] or
                offer.get('selected_variant_gtin13') != profile['gtin13'] or
                offer.get('network_generation') not in (None, '5g')):
            return False
        # The exact GTIN has a reviewed model-level 5G identity. Keep source
        # RAM/storage/model matching strict; never infer radio for other URLs.
        return catalog_variant_matches(offer, profile['name'],
                                       {'ram_gb': ram, 'storage_gb': storage})
    return catalog_variant_matches(offer, profile['name'], expected_specs)


class MediaMarktMarketplaceSource:
    def __init__(self, transport=None, now=None):
        self.transport = transport or MediaMarktSource()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def read_offer(self, url):
        profile = ALL_PROFILES.get(url)
        if not profile or not product_url(url):
            return None, 'unreviewed_marketplace_url', None
        transport = self.transport
        transport._load_robots()
        if transport._rules is None:
            return None, 'robots_unavailable', None
        if not transport._rules.can_fetch(BOT, url):
            return None, 'robots_disallowed', None
        try:
            body, reason, status = transport._direct(url, MAX_HTML_BYTES, 'text/html')
            if reason == 'http_403':
                body, final_url, reason = transport._browser_get(url)
                if reason != 'ready':
                    return None, reason, status
            elif reason != 'ready':
                return None, reason, status
            else:
                final_url = url
            if final_url != url:
                return None, 'changed_selected_url', status
            evidence = selected_marketplace_offer(body, final_url)
            if evidence is None:
                return None, 'no_exact_new_marketplace_offer', 200
            candidate = {**evidence, 'merchant': evidence['seller_name'],
                         'observed_at': self._now().isoformat().replace('+00:00', 'Z')}
            specs = {key: profile[key] for key in ('ram_gb', 'storage_gb', 'network_generation')}
            if not reviewed_marketplace_offer(candidate, profile['name'], specs):
                return None, 'seller_or_variant_changed', 200
            return candidate, 'observed_exact_new_marketplace_offer', 200
        except (requests.RequestException, ValueError, TypeError, UnicodeError, OSError) as error:
            return None, type(error).__name__, None

    def report(self):
        return self.transport.report()
