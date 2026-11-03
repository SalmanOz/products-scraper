"""Order discovery work using public current-price status, never as price evidence."""

from typing import Mapping


def prioritize_unpriced_products(products, public_status: Mapping[int, tuple[str, bool]]):
    """Stable order: exact-ID/slug unpriced, priced, then unknown public rows.

    The public snapshot is only a crawl scheduling hint. Each actual price is
    still read and checked from the retailer's selected product page.
    """
    def bucket(product):
        try:
            identity = public_status.get(int(product['id']))
            if identity is None or identity[0] != product['slug']:
                return 2
            return 1 if identity[1] else 0
        except (KeyError, TypeError, ValueError):
            return 2

    ordered = sorted(products, key=bucket)
    counts = {
        'unpriced_first': sum(bucket(product) == 0 for product in ordered),
        'priced_after': sum(bucket(product) == 1 for product in ordered),
        'unknown_last': sum(bucket(product) == 2 for product in ordered),
    }
    return ordered, counts


def prioritize_discoverable_products(products, public_status, discovered_slugs):
    """Spend source budgets on unpriced models with actual PDP candidates first.

    Then protect currently priced models before doing speculative searches for
    catalog models with no local retailer destination. URLs remain hints only.
    """
    def bucket(product):
        identity = public_status.get(product.get('id'))
        if not identity or identity[0] != product.get('slug'):
            return 3
        if identity[1]:
            return 1
        return 0 if product.get('slug') in discovered_slugs else 2
    return sorted(products, key=bucket)
