"""Persistent discovery hints for daily jobs; never stored price observations."""

import json
from pathlib import Path

from retailer_offer_refresh import ALLOWED_HOSTS, _safe_url
from tr_price_scraper import TRPriceScraper


URL_BATCH = Path(__file__).parent / 'offer_batches/budget-eight-2026-10-25.json'
PROTECTED_TWO_STORE_SLUGS = frozenset({
    'tecno-camon-20-pro', 'tecno-camon-40', 'samsung-galaxy-a27-5g',
    'samsung-galaxy-a37-5g', 'samsung-galaxy-a57-5g',
    'samsung-galaxy-a54-5g', 'poco-x7-pro',
})


def with_reviewed_radio(product, expected_specs):
    """Fill a missing radio generation only for an exact reviewed catalog SKU.

    Some verified 4G models list LTE bands but no explicit negative 5G field.
    The reviewed URL batch records their radio identity. A conflicting catalog
    value always wins and prevents that batch's URLs from being used.
    """
    specs = dict(expected_specs)
    if specs.get('network_generation') is not None:
        return specs
    for profile in json.loads(URL_BATCH.read_text())['products']:
        if (profile.get('id') == product.get('id')
                and profile.get('slug') == product.get('slug')
                and profile.get('name') == product.get('name')
                and all(TRPriceScraper._spec_int(specs.get(key)) == profile.get(key)
                        for key in ('ram_gb', 'storage_gb'))
                and profile.get('network_generation') in {'4g', '5g'}):
            specs['network_generation'] = profile['network_generation']
            break
    return specs


def daily_offer_targets(product, expected_specs):
    """Retain reviewed URLs after offers expire; validate the current SKU again.

    The browser-observations file is deliberately never opened. Product/RAM/
    storage/radio drift disables a seed, rather than changing catalog identity.
    Stored API targets and URL-only reviewed seeds are merged, with one URL per
    merchant first so 12 color URLs cannot crowd out the second store.
    """
    seeds = []
    for profile in json.loads(URL_BATCH.read_text())['products']:
        if (profile.get('id') != product.get('id') or profile.get('slug') != product.get('slug')
                or profile.get('name') != product.get('name')):
            continue
        same_memory = all(TRPriceScraper._spec_int(expected_specs.get(key)) == profile.get(key)
                          for key in ('ram_gb', 'storage_gb'))
        same_radio = expected_specs.get('network_generation') == profile.get('network_generation')
        if same_memory and same_radio:
            seeds = profile['targets']
        break
    stored = [row.get('source_url') for row in product.get('offer_targets', [])
              if isinstance(row, dict)]
    urls = list(dict.fromkeys(u for u in [*seeds, *stored] if _safe_url(u)))
    first, rest, merchants = [], [], set()
    for url in urls:
        merchant = ALLOWED_HOSTS[_safe_url(url).hostname]
        row = {'merchant_name': merchant, 'source_url': url}
        if merchant in merchants:
            rest.append(row)
        else:
            first.append(row)
            merchants.add(merchant)
    return (first + rest)[:12]
