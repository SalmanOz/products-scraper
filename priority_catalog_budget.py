"""Size existing-target refresh budgets from actual public PDP destinations.

One model can have several colors or marketplace resources. Counting models
alone drops verified targets after automatic discovery has increased coverage.
"""
from urllib.parse import urlparse

from priority_retailer_transport import HOSTS, product_host


def priority_catalog_source_budgets(products):
    destinations = {host: set() for host in HOSTS}
    for product in products:
        for target in product.get('offer_targets') or []:
            url = target.get('source_url') if isinstance(target, dict) else None
            host = product_host(url)
            if host is not None:
                # Selected seller/color hints discover the current canonical
                # document. They do not each need another identical GET.
                destinations[host].add(urlparse(url)._replace(query='').geturl())
    budgets = {}
    for host, urls in destinations.items():
        # Reserve one additional discovered PDP per model. Trendyol may also
        # perform the already-validated same-product canonical redirect.
        count = len(urls) + len(products)
        if host == 'www.trendyol.com':
            count *= 2
        budgets[host] = max(1, min(400, count))
    return budgets
