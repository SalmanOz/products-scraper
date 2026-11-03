"""Automatically inspect catalog URL hints on HB, Trendyol, n11 and Teknosa; GET only."""

import json
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from offer_ingestion_client import OfferIngestionClient
from priority_retailer_transport import HOSTS, PriorityRetailerTransport, product_host
from retailer_offer_refresh import _schema_nodes, _type_is


def queue(products, prices):
    groups = {host: {} for host in HOSTS}
    for product in products:
        for target in product.get('offer_targets', []):
            url = target.get('source_url') if isinstance(target, dict) else None
            host = product_host(url)
            if host:
                groups[host].setdefault(url, {})[product['id']] = product
    result = {}
    for host, targets in groups.items():
        unique = [(url, next(iter(matches.values()))) for url, matches in targets.items()
                  if len(matches) == 1]
        unique.sort(key=lambda row: (prices.get(row[1]['id'], ('', True))[1], -row[1]['id'], row[0]))
        result[host] = unique[:2]
    return result


def document_evidence(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    products = [p for p in _schema_nodes(soup, url) if _type_is(p, 'Product')]
    return {'heading': [n.get_text(' ', strip=True)[:300] for n in soup.select('h1')][:2],
            'product_schemas': len(products),
            'selected_schemas': [{key: p.get(key) for key in ('name', 'sku', 'gtin13')}
                                 for p in products[:2]],
            'hb_buy_box_events': len(soup.select('[data-hbus]')),
            'hb_redux': len(soup.select('script#reduxStore')),
            'teknosa_carts': len(soup.select('#addToCartButton[data-product-id]')),
            'trendyol_selected_states': sum('__envoy__SHARED_PROPS' in s.get_text()
                                             for s in soup.select('script')),
            'n11_price_nodes': len(soup.select('[itemprop="price"], #newPrice, .newPrice'))}


def main():
    client = OfferIngestionClient.from_env()
    products, prices = client.get_published_products(), client.get_public_price_priority()
    transport = PriorityRetailerTransport()
    rows = []
    for host, targets in queue(products, prices).items():
        for url, product in targets:
            html, reason, status = transport.read_document(url)
            row = {'retailer': HOSTS[host], 'slug': product['slug'], 'url': url,
                   'reason': reason, 'http_status': status}
            if html is not None:
                row.update(document_evidence(html, url))
            rows.append(row)
    report = {'purpose': 'automatic_priority_retailer_diagnosis', 'writes': False,
              'catalog_count': len(products), 'rows': rows, 'sources': transport.report()}
    Path('priority-retailer-audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    lines = ['## Priority retailer catalog audit (GET only)',
             f'Catalog: {len(products)}; selected PDP hints: {len(rows)}; writes: false.',
             '| Retailer | Catalog model | HTTP | Result | Selected Product schemas |',
             '|---|---|---:|---|---:|']
    for row in rows:
        lines.append(f"| {row['retailer']} | {row['slug']} | {row['http_status']} | "
                     f"{row['reason']} | {row.get('product_schemas', 0)} |")
    lines.append('')
    for row in rows:
        if row.get('heading'):
            lines.append(f"- {row['slug']}: " + '; '.join(row['heading']))
    lines += ['', json.dumps(report['sources'], ensure_ascii=False),
              '', 'No catalog, price, score, or IndexNow writes. Documents are not offers.']
    Path('priority-retailer-audit.md').write_text('\n'.join(lines))
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
