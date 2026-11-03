"""Automatically inspect catalog URL hints on HB, Trendyol, n11 and Teknosa; GET only."""

import json
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from offer_ingestion_client import OfferIngestionClient
from priority_retailer_transport import HOSTS, PriorityRetailerTransport, product_host
from retailer_offer_refresh import _safe_url, _schema_nodes, _type_is
from reviewed_offer_targets import with_reviewed_radio
from update_prices import PriceUpdater


def queue(products, prices, maximum=2, spread=False):
    if type(maximum) is not int or not 1 <= maximum <= 8:
        raise ValueError('Invalid read-only sample budget')
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
        # Broader diagnostics sample both ends of the queue rather than
        # repeatedly inspecting only the newest two unavailable models.
        if spread and len(unique) > maximum and maximum > 1:
            result[host] = [unique[round(i * (len(unique) - 1) / (maximum - 1))]
                            for i in range(maximum)]
        else:
            result[host] = unique[:maximum]
    return result


def document_evidence(html, url):
    soup = BeautifulSoup(html, 'html.parser')
    products = [p for p in _schema_nodes(soup, url) if _type_is(p, 'Product')]
    selected = []
    for product in products[:2]:
        offer = product.get('offers')
        seller = offer.get('seller') if isinstance(offer, dict) else None
        selected.append({**{key: product.get(key) for key in ('name', 'sku', 'gtin13')},
                         'price': offer.get('price') if isinstance(offer, dict) else None,
                         'schema_seller': seller.get('name') if isinstance(seller, dict) else seller})
    carts = soup.select('#addToCartButton[data-product-id]')
    cart = {key: carts[0].get(key) for key in ('data-product-id', 'data-product-ean',
            'data-shop-name', 'data-product-shop-id', 'data-product-stock',
            'data-product-stock-status', 'data-product-price')} if len(carts) == 1 else None
    events = []
    for node in soup.select('[data-hbus]'):
        try:
            data = json.loads(node['data-hbus']).get('data', {})
        except (TypeError, ValueError, AttributeError):
            continue
        if isinstance(data, dict) and data.get('placement') == 'buybox' and data.get('component') in ('variant', 'Key Features'):
            events.append({key: data.get(key) for key in ('component', 'merchant_name',
                           'merchant_id', 'product_id', 'page_value', 'price')})
    return {'heading': [n.get_text(' ', strip=True)[:300] for n in soup.select('h1')][:2],
            'product_schemas': len(products),
            'selected_schemas': selected, 'teknosa_cart': cart, 'hb_selected_events': events,
            'hb_buy_box_events': len(soup.select('[data-hbus]')),
            'hb_redux': len(soup.select('script#reduxStore')),
            'teknosa_carts': len(soup.select('#addToCartButton[data-product-id]')),
            'trendyol_selected_states': sum('__envoy__SHARED_PROPS' in s.get_text()
                                             for s in soup.select('script')),
            'n11_price_nodes': len(soup.select('[itemprop="price"], #newPrice, .newPrice'))}


def main():
    client = OfferIngestionClient.from_env()
    products, prices = client.get_published_products(), client.get_public_price_priority()
    transport = PriorityRetailerTransport(maximum_per_host=8)
    rows = []
    for host, targets in queue(products, prices, maximum=8, spread=True).items():
        for url, product in targets:
            observed_url = url
            if host == 'www.hepsiburada.com':
                from hepsiburada_catalog_source import catalog_request_url
                observed_url = catalog_request_url(url)
            elif host == 'www.trendyol.com':
                from trendyol_catalog_source import catalog_request_url
                observed_url = catalog_request_url(url)
            html, reason, status = transport.read_document(observed_url)
            row = {'retailer': HOSTS[host], 'slug': product['slug'], 'url': url,
                   'observed_url': observed_url, 'reason': reason, 'http_status': status}
            if html is not None:
                row.update(document_evidence(html, url))
                if host == 'www.teknosa.com':
                    from teknosa_reviewed_source import parse_exact_first_party_offer
                    specs = with_reviewed_radio(product, PriceUpdater.get_expected_specs(product.get('attributes')))
                    offer, why = parse_exact_first_party_offer(html, url,
                        {'name': product['name'], 'specs': specs})
                    row.update(reader_reason=why, prepared_offer=offer)
                if host == 'www.hepsiburada.com':
                    from hepsiburada_catalog_source import parse_catalog_buy_box
                    specs = with_reviewed_radio(product, PriceUpdater.get_expected_specs(product.get('attributes')))
                    offer, why = parse_catalog_buy_box(html, observed_url, product['name'], specs)
                    row.update(reader_reason=why, prepared_offer=offer)
                if host == 'www.trendyol.com':
                    from trendyol_catalog_source import parse_catalog_listing
                    specs = with_reviewed_radio(product, PriceUpdater.get_expected_specs(product.get('attributes')))
                    final_url = transport.resolved_url(observed_url)
                    offer, why = parse_catalog_listing(html, final_url, product['name'], specs)
                    row['resolved_url'] = final_url
                    row.update(reader_reason=why, prepared_offer=offer)
            rows.append(row)
    report = {'purpose': 'automatic_priority_retailer_diagnosis', 'writes': False,
              'catalog_count': len(products), 'rows': rows, 'sources': transport.report()}
    inventory = {host: [] for host in HOSTS}
    for product in products:
        for target in product.get('offer_targets', []):
            url = target.get('source_url') if isinstance(target, dict) else None
            parsed = _safe_url(url)
            if parsed and parsed.hostname in inventory and len(inventory[parsed.hostname]) < 3:
                inventory[parsed.hostname].append({'slug': product['slug'], 'url': url})
    report['hint_inventory_sample'] = inventory
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
            lines.append('  - Selected metadata: ' + json.dumps(row['selected_schemas'], ensure_ascii=False))
            if row.get('teknosa_cart'):
                lines.append('  - Selected cart: ' + json.dumps(row['teknosa_cart'], ensure_ascii=False))
            if row.get('hb_selected_events'):
                lines.append('  - Selected buy box: ' + json.dumps(row['hb_selected_events'], ensure_ascii=False))
            if row.get('reader_reason'):
                lines.append('  - Catalog reader: ' + row['reader_reason'] + '; ' +
                             json.dumps(row.get('prepared_offer'), ensure_ascii=False))
    lines += ['', 'Safe catalog hint sample: ' + json.dumps(inventory, ensure_ascii=False)]
    lines += ['', json.dumps(report['sources'], ensure_ascii=False),
              '', 'No catalog, price, score, or IndexNow writes. Documents are not offers.']
    Path('priority-retailer-audit.md').write_text('\n'.join(lines))
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
