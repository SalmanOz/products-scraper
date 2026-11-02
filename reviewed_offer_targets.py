"""Persistent discovery hints for daily jobs; never stored price observations."""

import json
from pathlib import Path

from retailer_offer_refresh import ALLOWED_HOSTS, _safe_url
from tr_price_scraper import TRPriceScraper
from vatan_reviewed_source import BATCH_PATH as VATAN_BATCH_PATH


URL_BATCH = Path(__file__).parent / 'offer_batches/budget-eight-2026-10-25.json'
MEDIAMARKT_BATCH_PATH = Path(__file__).parent / 'offer_batches/mediamarkt-exact-2026-11-02.json'
APPLE_STORE_BATCH_PATH = Path(__file__).parent / 'offer_batches/apple-store-exact-2026-11-02.json'
TURK_TELEKOM_BATCH_PATH = Path(__file__).parent / 'offer_batches/turk-telekom-exact-2026-11-02.json'
PASAJ_BATCH_PATH = Path(__file__).parent / 'offer_batches/pasaj-exact-2026-11-02.json'
S25_FE_256_BATCH_PATH = Path(__file__).parent / 'offer_batches/s25-fe-256-exact-2026-11-02.json'
S25_FE_256_SLUG = 'samsung-galaxy-s25-fe-256gb'
PROTECTED_TWO_STORE_SLUGS = frozenset({
    'tecno-camon-20-pro', 'tecno-camon-40', 'samsung-galaxy-a27-5g',
    'samsung-galaxy-a37-5g', 'samsung-galaxy-a57-5g',
    'samsung-galaxy-a54-5g', 'poco-x7-pro',
})


def _profiles():
    for path in (URL_BATCH, VATAN_BATCH_PATH, MEDIAMARKT_BATCH_PATH,
                 APPLE_STORE_BATCH_PATH, TURK_TELEKOM_BATCH_PATH, PASAJ_BATCH_PATH,
                 S25_FE_256_BATCH_PATH):
        yield from json.loads(path.read_text())['products']


def _identity_matches(profile, product):
    """Only the newly created, slug-unique S25 FE variant may lack a known ID.

    Existing reviewed profiles keep their pinned catalog ID. The new draft gets
    its ID at publication, so its exact slug/name and positive database ID are
    checked along with RAM, storage and radio before its URLs become hints.
    """
    if profile.get('slug') != product.get('slug') or profile.get('name') != product.get('name'):
        return False
    if profile.get('id') is None:
        product_id = product.get('id')
        return (profile.get('slug') == S25_FE_256_SLUG
                and profile.get('name') == 'Samsung Galaxy S25 FE 256GB'
                and isinstance(product_id, int) and not isinstance(product_id, bool)
                and product_id > 0)
    return profile.get('id') == product.get('id')


def with_reviewed_radio(product, expected_specs):
    """Fill a missing radio generation only for an exact reviewed catalog SKU.

    Some verified 4G models list LTE bands but no explicit negative 5G field.
    The reviewed URL batch records their radio identity. A conflicting catalog
    value always wins and prevents that batch's URLs from being used.
    """
    specs = dict(expected_specs)
    if specs.get('network_generation') is not None:
        return specs
    for profile in _profiles():
        if (_identity_matches(profile, product)
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
    for profile in _profiles():
        if not _identity_matches(profile, product):
            continue
        same_memory = all(TRPriceScraper._spec_int(expected_specs.get(key)) == profile.get(key)
                          for key in ('ram_gb', 'storage_gb'))
        same_radio = expected_specs.get('network_generation') == profile.get('network_generation')
        if same_memory and same_radio:
            seeds.extend(profile['targets'])
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
