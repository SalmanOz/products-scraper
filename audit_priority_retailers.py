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
    # Product facts only: never copy entire scripts, customer/session state,
    # merchant legal contacts, or tracking values into the diagnostic report.
    physical = {'hb_technical': [n.get_text(' ', strip=True)[:2500]
                                for n in soup.select('#techSpecs[data-test-id="KeyFeaturesTable"]')][:1]}
    for script in soup.select('script'):
        body = script.get_text()
        marker = 'window["__envoy__SHARED_PROPS"]'
        if marker not in body:
            continue
        import re
        match = re.search(re.escape(marker) + r'\s*=\s*', body)
        if not match:
            continue
        try:
            state, _ = json.JSONDecoder().raw_decode(body[match.end():])
            product = state.get('product', {})
            physical['trendyol_attributes'] = [
                {'key': item['key']['name'], 'value': item['value'].get('name')}
                for item in product.get('attributes', [])
                if isinstance(item, dict) and isinstance(item.get('key'), dict)
                and isinstance(item.get('value'), dict) and item['key'].get('name') in
                ('RAM Kapasitesi', 'Dahili Hafıza', 'Mobil Bağlantı Hızı')][:8]
        except (ValueError, TypeError, AttributeError, KeyError):
            pass
    return {'heading': [n.get_text(' ', strip=True)[:300] for n in soup.select('h1')][:2],
            'physical_facts': physical,
            'product_schemas': len(products),
            'selected_schemas': selected, 'teknosa_cart': cart, 'hb_selected_events': events,
            'hb_buy_box_events': len(soup.select('[data-hbus]')),
            'hb_redux': len(soup.select('script#reduxStore')),
            'teknosa_carts': len(soup.select('#addToCartButton[data-product-id]')),
            'trendyol_selected_states': sum('__envoy__SHARED_PROPS' in s.get_text()
                                             for s in soup.select('script')),
            'n11_price_nodes': len(soup.select('[itemprop="price"], #newPrice, .newPrice'))}


def inspect_target(transport, url, product, *, probe_teknosa_identity=False):
    host = product_host(url)
    observed_url = url
    if host == 'www.hepsiburada.com':
        from hepsiburada_catalog_source import catalog_request_url
        observed_url = catalog_request_url(url)
    elif host == 'www.trendyol.com':
        from trendyol_catalog_source import catalog_request_url
        observed_url = catalog_request_url(url)
    elif host == 'www.n11.com':
        from n11_catalog_source import catalog_request_url
        observed_url = catalog_request_url(url)
    elif host == 'www.teknosa.com':
        from teknosa_seller_cards import canonical_document_url
        observed_url = canonical_document_url(url)
    html, reason, status = transport.read_document(observed_url)
    row = {'retailer': HOSTS[host], 'slug': product['slug'], 'url': url,
           'observed_url': observed_url, 'reason': reason, 'http_status': status}
    if host == 'www.n11.com' and status in (301, 302, 307, 308):
        diagnostic_reader = getattr(transport, 'redirect_diagnostic', None)
        if callable(diagnostic_reader):
            from priority_retailer_transport import validated_n11_redirect_diagnostic
            diagnostic = validated_n11_redirect_diagnostic(diagnostic_reader(observed_url))
            if diagnostic is not None:
                row['redirect_diagnostic'] = diagnostic
    if html is not None:
        row.update(document_evidence(html, url))
        if host == 'www.n11.com':
            from n11_catalog_source import parse_catalog_listing, discover_product_sellers
            specs = with_reviewed_radio(product, PriceUpdater.get_expected_specs(product.get('attributes')))
            final_url = transport.resolved_url(observed_url)
            from priority_retailer_transport import n11_unscoped_redirect
            if n11_unscoped_redirect(observed_url, final_url):
                row['source_selector_reset'] = {'scope': 'fresh_default_pdp',
                                                'historical_selectors_reused': False}
            offer, why = parse_catalog_listing(html, final_url, product['name'], specs)
            row['resolved_url'] = final_url
            row.update(reader_reason=why, prepared_offer=offer)
            row['discovered_sellers'] = discover_product_sellers(html, final_url)
            if offer is None:
                from observed_memory_variants import inspect_n11_memory_variant
                diagnostic = {}
                other = inspect_n11_memory_variant(html, final_url, product['name'], specs, diagnostic)
                if other is not None:
                    row['other_memory_variant'] = other
                else:
                    row['other_memory_variant_diagnostic'] = diagnostic
        if host == 'www.teknosa.com':
            from teknosa_reviewed_source import parse_catalog_offer
            specs = with_reviewed_radio(product, PriceUpdater.get_expected_specs(product.get('attributes')))
            offer, why = parse_catalog_offer(html, url,
                {'name': product['name'], 'specs': specs})
            technical = None
            if offer is None and why == 'catalog_variant_mismatch':
                from teknosa_public_technical import read_observation
                technical, technical_reason = read_observation(transport, html, observed_url)
                row['teknosa_public_technical_reason'] = technical_reason
                if technical is not None:
                    offer, why = parse_catalog_offer(html, url,
                        {'name': product['name'], 'specs': specs}, technical)
            row.update(reader_reason=why, prepared_offer=offer)
            if offer is not None and url == observed_url:
                from teknosa_seller_cards import public_seller_offers
                row['additional_prepared_offers'] = public_seller_offers(html, observed_url,
                    {'name': product['name'], 'specs': specs}, maximum=3)
            if probe_teknosa_identity and offer is None and why == 'catalog_variant_mismatch':
                from offer_target_selection import select_offer_targets
                from teknosa_marketplace_source import independent_radio_preflight

                def read_witnesses():
                    registered = [target for target in product.get('offer_targets', [])
                                  if isinstance(target, dict) and product_host(target.get('source_url')) in
                                  {'www.hepsiburada.com', 'www.trendyol.com', 'www.n11.com'}]
                    for target in select_offer_targets(registered, maximum=3):
                        witness = inspect_target(transport, target['source_url'], product)
                        if witness.get('http_status') == 200 and isinstance(witness.get('prepared_offer'), dict):
                            yield witness['prepared_offer']

                row['independent_radio_preflight'] = independent_radio_preflight(
                    html, observed_url, {'name': product['name'], 'specs': specs}, technical, read_witnesses)
            if offer is None:
                from observed_memory_variants import inspect_teknosa_memory_variant
                diagnostic = {}
                other = inspect_teknosa_memory_variant(html, url, product['name'], specs, technical,
                                                      diagnostic=diagnostic)
                if other is not None:
                    row['other_memory_variant'] = other
                else:
                    row['other_memory_variant_diagnostic'] = diagnostic
        if host == 'www.hepsiburada.com':
            from hepsiburada_catalog_source import parse_catalog_buy_box
            from hepsiburada_catalog_discovery import discover_product_siblings, discover_product_sellers
            specs = with_reviewed_radio(product, PriceUpdater.get_expected_specs(product.get('attributes')))
            offer, why = parse_catalog_buy_box(html, observed_url, product['name'], specs)
            row.update(reader_reason=why, prepared_offer=offer)
            siblings = discover_product_siblings(html, observed_url, product, include_other_storage=True)
            row['discovered_siblings'] = [hint for hint in siblings
                if hint['discovery_scope'] == 'public_product_variant_link']
            memory_links = [hint for hint in siblings if hint['discovery_scope'] == 'public_product_memory_variant_link']
            if memory_links:
                row['discovered_memory_siblings'] = memory_links
            row['discovered_sellers'] = discover_product_sellers(html, observed_url) if offer is not None else []
            if offer is None:
                from observed_memory_variants import inspect_hepsiburada_memory_variant
                other = inspect_hepsiburada_memory_variant(html, observed_url, product['name'], specs)
                if other is not None:
                    row['other_memory_variant'] = other
        if host == 'www.trendyol.com':
            from trendyol_catalog_source import parse_catalog_listing
            from trendyol_catalog_discovery import discovery_links
            specs = with_reviewed_radio(product, PriceUpdater.get_expected_specs(product.get('attributes')))
            final_url = transport.resolved_url(observed_url)
            row['discovered_categories'] = [{'source_url': target, 'source_document_url': final_url,
                'discovery_scope': 'public_phone_brand_category'} for target in discovery_links(html, final_url, [product])]
            offer, why = parse_catalog_listing(html, final_url, product['name'], specs)
            row['resolved_url'] = final_url
            row.update(reader_reason=why, prepared_offer=offer)
            if offer is None and why in {'selected_memory_mismatch', 'selected_product_mismatch'}:
                from observed_memory_variants import inspect_trendyol_memory_variant
                diagnostic = {}
                other = inspect_trendyol_memory_variant(html, final_url, product['name'], specs, diagnostic)
                if other is not None:
                    row['other_memory_variant'] = other
                else:
                    row['other_memory_variant_diagnostic'] = diagnostic
    return row


def main():
    client = OfferIngestionClient.from_env()
    products, prices = client.get_published_products(), client.get_public_price_priority()
    transport = PriorityRetailerTransport(maximum_per_host=8,
                                         n11_collector=getattr(client, 'n11_document_get', None))
    rows = []
    for host, targets in queue(products, prices, maximum=8, spread=True).items():
        for url, product in targets:
            row = inspect_target(transport, url, product)
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
