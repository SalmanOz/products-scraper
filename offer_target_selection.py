"""Bounded refresh planning across checkout platforms and actual sellers."""
from retailer_offer_refresh import ALLOWED_HOSTS, _safe_url

PRIORITY_RETAILERS = {'Hepsiburada': 0, 'Trendyol': 1, 'n11': 2, 'Teknosa': 3}


def select_offer_targets(targets, maximum=12):
    unique = {}
    for target in targets:
        row = dict(target) if isinstance(target, dict) else {'source_url': target}
        url = row.get('source_url')
        parsed = _safe_url(url)
        if not parsed:
            continue
        row['merchant_name'] = ALLOWED_HOSTS[parsed.hostname]
        if url not in unique:
            unique[url] = row
        elif row.get('seller_name'):
            unique[url]['seller_name'] = row['seller_name']
    platforms, sellers, variants = [], [], []
    seen_platforms, seen_sellers = set(), set()
    for row in unique.values():
        platform = row['merchant_name']
        seller = str(row.get('seller_name') or platform).strip().casefold()
        identity = (platform, seller)
        if platform not in seen_platforms:
            platforms.append(row)
            seen_platforms.add(platform)
        elif identity not in seen_sellers:
            sellers.append(row)
        else:
            variants.append(row)
        seen_sellers.add(identity)
    platforms.sort(key=lambda row: PRIORITY_RETAILERS.get(row['merchant_name'], 4))
    return (platforms + sellers + variants)[:maximum]
