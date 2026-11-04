"""Systematic existing-catalog recovery, with fresh proof and readback per offer.

No sampling, catalog creation, price guessing, or deferred stale artifact replay.
One manual invocation exhausts the bounded source queue; writing is opt-in.
"""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
from urllib.parse import urlparse

from audit_priority_retailers import inspect_target
from offer_ingestion_client import IngestionClientError, OfferIngestionClient
from priority_retailer_transport import HOSTS, PriorityRetailerTransport, product_host
from publish_priority_candidates import CandidatePublicationError, _matched_rows, refresh_candidates
from retailer_offer_refresh import RetailerOfferRefresher
from update_prices import PriceUpdater


def observed_url(url):
    host = product_host(url)
    if host == 'www.hepsiburada.com':
        from hepsiburada_catalog_source import catalog_request_url
        return catalog_request_url(url)
    if host == 'www.trendyol.com':
        from trendyol_catalog_source import catalog_request_url
        return catalog_request_url(url)
    if host == 'www.n11.com':
        from n11_catalog_source import catalog_request_url
        return catalog_request_url(url)
    if host == 'www.teknosa.com':
        from teknosa_seller_cards import canonical_document_url
        return canonical_document_url(url)
    return url


def catalog_queue(products, prices):
    """Visit every model before additional colors, excluding ambiguous owners."""
    owners, groups = defaultdict(dict), defaultdict(list)
    for product in products:
        for target in product.get('offer_targets', []):
            url = target.get('source_url') if isinstance(target, dict) else None
            if product_host(url):
                canonical = observed_url(url)
                owners[canonical][product['id']] = product
                if product_host(canonical) == 'www.hepsiburada.com':
                    default = urlparse(canonical)._replace(query='').geturl()
                    owners[default][product['id']] = product
    for url, matches in owners.items():
        if len(matches) == 1:
            product = next(iter(matches.values()))
            groups[(product_host(url), product['id'])].append((url, product))
    rows = []
    for group in groups.values():
        for rank, (url, product) in enumerate(sorted(group, key=lambda row: row[0])):
            rows.append((rank, url, product))
    # The stable ID order does not shift models out of the queue when prices change.
    rows.sort(key=lambda row: (row[0], prices.get(row[2]['id'], ('', True))[1],
                              row[2]['id'], list(HOSTS).index(product_host(row[1])), row[1]))
    return [(url, product) for _, url, product in rows]


def sweep(*, client, publish=False, transport=None, reader=None, checkpoint=None,
          discover_n11=False, discover_teknosa=False, discover_trendyol=False,
          discover_hepsiburada=False, current_listing_inventory=None, coverage_first=False,
          category_audit_only=False):
    if category_audit_only and (publish or discover_n11):
        raise ValueError('Category routing audits cannot publish or fetch product search results')
    products, prices = client.get_published_products(), client.get_public_price_priority()
    if (not products or len(products) > 200 or len({p['id'] for p in products}) != len(products)
            or len({p['slug'] for p in products}) != len(products)):
        raise ValueError('invalid_or_oversized_catalog')
    collector = getattr(client, 'n11_document_get', None)
    listing_scoped = callable(getattr(type(client), 'ingest_listing_offers', None))
    transport = transport or PriorityRetailerTransport(maximum_per_host=350, n11_collector=collector)
    reader = reader or RetailerOfferRefresher(n11_document_get=collector)
    if publish:
        for source in (reader.hepsiburada_catalog_source, reader.trendyol_catalog_source,
                       reader.teknosa_catalog_source, reader.n11_catalog_source):
            source.configure_catalog_budget(350)
    targets = catalog_queue(products, prices)
    result = {'purpose': 'systematic_priority_catalog_recovery', 'catalog_count': len(products),
              'publish': publish, 'writes': False, 'status': 'running', 'queued': len(targets),
              'rows': [], 'accepted': 0, 'discovery': [], 'models_without_priority_hints': sorted(
                  {p['slug'] for p in products} - {p['slug'] for _, p in targets})}
    # The catalog's 12 refresh hints are not a complete list of current listing
    # resources. Do not let those omitted, already verified colors consume the
    # small discovery allowance again. This inventory is only an exclusion set:
    # it never supplies a price, evidence, or permission to write an offer.
    inventory_urls = set()
    covered_models = set()
    if current_listing_inventory is not None:
        try:
            inventory = _matched_rows(current_listing_inventory)
            if set(inventory) != {p['slug'] for p in products}:
                raise ValueError('inventory_catalog_changed')
            inventory_urls = {observed_url(row['source_url']) for rows in inventory.values()
                              for row in rows if isinstance(row, dict) and product_host(row.get('source_url'))}
            result['discovery_inventory'] = {'status': 'current', 'priority_listing_destinations': len(inventory_urls)}
            if coverage_first:
                by_slug = {p['slug']: p for p in products}
                for row in current_listing_inventory['productCoverage']:
                    product = by_slug[row['slug']]
                    count = row.get('matchedMerchants')
                    if (type(count) is int and 2 <= count <= len(inventory[row['slug']])
                            and prices.get(product['id']) == (product['slug'], True)):
                        covered_models.add(product['slug'])
        except ValueError:
            result['discovery_inventory'] = {'status': 'unavailable', 'priority_listing_destinations': 0}
    # Recovery iterations need new prices and second independent sellers.
    # A fresh authenticated whole-catalog audit can retain already-covered
    # models without renewing any retailer observation or changing the 80%
    # catalog-wide quality gate. Ordinary full refresh remains the default.
    refresh_targets = [(url, product) for url, product in targets if product['slug'] not in covered_models]
    result['queued'] = len(refresh_targets)
    if category_audit_only:
        result.update(purpose='readonly_category_routing_audit', category_candidates=[], queued=0)
    if coverage_first:
        result['coverage_priority'] = {
            'status': 'current_server_audit' if result.get('discovery_inventory', {}).get('status') == 'current' else 'full_refresh_fallback',
            'retained_current_multi_merchant_models': sorted(covered_models),
            'models_needing_recovery': sorted(p['slug'] for p in products if p['slug'] not in covered_models),
            'retailer_observation_timestamps_renewed': False,
        }
    publication_failed = False
    processed = set()
    refresh_urls = {url for url, _ in refresh_targets}
    sibling_counts = Counter()
    seller_counts = Counter()
    def process_target(url, product, audited_row=None):
        nonlocal publication_failed
        if (product['slug'], url) in processed:
            return
        processed.add((product['slug'], url))
        if category_audit_only:
            result['category_candidates'].append({'slug': product['slug'], 'source_url': url})
            return  # A destination hint is never a fetched/verified/publishable quote.
        row = audited_row if audited_row is not None else inspect_target(transport, url, product)
        extra_offers = row.pop('additional_prepared_offers', [])
        row['expected_specs'] = product.get('attributes') and {
            key: value for key, value in PriceUpdater.get_expected_specs(
                product['attributes']).items() if key in ('ram_gb', 'storage_gb')}
        result['rows'].append(row)
        if row.get('prepared_offer') is not None and publish and not publication_failed:
            if product['slug'] == 'samsung-galaxy-a37-5g' and not listing_scoped:
                row['publication'] = {'status': 'protected_paired_workflow_required'}
            else:
                audit = {'purpose': 'automatic_priority_retailer_diagnosis', 'writes': False,
                         'catalog_count': len(products), 'rows': [row]}
                # Re-read immediately, hydrate current complete seller targets, atomically
                # ingest only this observation, and verify the commercial readback.
                try:
                    refreshed = refresh_candidates(audit, client=client, reader=reader)
                except CandidatePublicationError as error:
                    # Stop ALL further price writes, but finish the independent
                    # GET-only diagnosis so the rest of the catalog is not lost.
                    refreshed = error.result
                    publication_failed = True
                except IngestionClientError:
                    # No mutation has started: the fresh catalog/proof read
                    # was unavailable even after bounded retries. Preserve
                    # the rest of the independent source diagnosis.
                    refreshed = {'status': 'publication_preflight_unavailable', 'writes': False}
                    publication_failed = True
                row['publication'] = refreshed
                result['accepted'] += refreshed.get('ingestion', {}).get('accepted', 0)
                result['writes'] |= refreshed['writes']
        result['sources'] = transport.report()
        if checkpoint:
            checkpoint(result)
        seller_candidates = (row.get('discovered_sellers', [])
            if product_host(row.get('observed_url')) == 'www.hepsiburada.com'
            and not urlparse(row['observed_url']).query and row.get('prepared_offer') is not None else [])
        for candidate in seller_candidates:
            destination = candidate.get('source_url') if isinstance(candidate, dict) else None
            if (seller_counts[product['slug']] >= 2 or product_host(destination) != 'www.hepsiburada.com'
                    or candidate.get('discovery_scope') != 'public_other_seller_selector'
                    or candidate.get('source_document_url') != row.get('observed_url')
                    or destination in refresh_urls or observed_url(destination) in inventory_urls
                    or (product['slug'], destination) in processed):
                continue
            seller_counts[product['slug']] += 1
            result['queued'] += 1
            process_target(destination, product)
        # Expand only original refresh seeds after category discovery. This
        # prevents recursively expanding colors or counting future queued
        # category/refresh targets twice. A sibling is a new GET, never a quote
        # copied from another variant's state or a new catalog product.
        if url in refresh_urls and product_host(url) == 'www.hepsiburada.com':
            for candidate in row.get('discovered_siblings', []):
                destination = candidate.get('source_url') if isinstance(candidate, dict) else None
                if (sibling_counts[product['slug']] >= 2 or product_host(destination) != 'www.hepsiburada.com'
                        or candidate.get('discovery_scope') != 'public_product_variant_link'
                        or destination in refresh_urls or observed_url(destination) in inventory_urls
                        or (product['slug'], destination) in processed):
                    continue
                sibling_counts[product['slug']] += 1
                result['queued'] += 1
                process_target(destination, product)
        for offer in extra_offers:
            destination = offer.get('url')
            if ((product['slug'], destination) in processed or product_host(destination) != 'www.teknosa.com'
                    or offer.get('source_offer_scope') != 'public_other_seller_row'
                    or offer.get('source_document_url') != row.get('observed_url')):
                continue
            result['queued'] += 1
            process_target(destination, product, audited_row={
                'slug': product['slug'], 'retailer': 'Teknosa', 'url': destination,
                'observed_url': offer['source_document_url'], 'reason': 'document_read', 'http_status': 200,
                'document_reused': True, 'reader_reason': 'observed_public_seller_card', 'prepared_offer': offer})
    deferred_categories = []
    def discover_category(retailer, report_key, module, read_category, resume=None):
        from teknosa_catalog_discovery import match_catalog_destinations
        if resume is None:
            cards, category_url, category_seen, pending_branches = [], module.CATEGORY_URL, set(), []
            discovery = result[report_key] = {'retailer': retailer, 'pages': [],
                                            'new_targets': [], 'pagination_complete': False}
        else:
            cards, category_url, category_seen, pending_branches = resume
            discovery = result[report_key]
            discovery['resume_passes'] = 1
        # One shared budget across both passes, including failed attempts.
        for _ in range(module.MAX_CATEGORY_PAGES - len(discovery['pages'])):
            if not category_url or category_url in category_seen:
                break
            category_seen.add(category_url)
            html, reason, status = read_category(category_url)
            discovery['pages'].append({'url': category_url, 'reason': reason, 'http_status': status})
            if html is None:
                transient = reason in {'http_500', 'http_502', 'http_503', 'http_504',
                                       'browser_timeout', 'source_cooldown'}
                if reason == 'source_cooldown':
                    # No request was sent. Retain this exact observed page for
                    # the later pass; do not guess a continuation around it.
                    category_seen.remove(category_url)
                    next_category_url = category_url
                else:
                    discovery.setdefault('incomplete_pages', []).append(category_url)
                    next_category_url = pending_branches.pop(0) if pending_branches else None
                source_blocked = transport.report().get(retailer, {}).get('blocked') is True
                if (not source_blocked and next_category_url and reason in {
                        'browser_changed_url', 'unexpected_final_url', 'http_404', 'http_410', 'already_read'}):
                    # An obsolete/duplicate observed branch does not make the
                    # other queued categories unavailable. Do not retry it or
                    # accept its changed URL; visit the next observed branch.
                    category_url = next_category_url
                    continue
                if (resume is None and transient and not source_blocked and next_category_url
                        and len(discovery['pages']) < module.MAX_CATEGORY_PAGES
                        and callable(getattr(module, 'discovery_links', None))):
                    deferred_categories.append((retailer, report_key, module, read_category,
                                                (cards, next_category_url, category_seen, pending_branches)))
                    discovery['deferred_observed_pages'] = 1 + len(pending_branches)
                    category_url = next_category_url
                break
            parser = {}
            found, next_category_url = module.parse_category_document(html, category_url, diagnostic=parser)
            discovery['pages'][-1]['parser'] = parser
            cards.extend(found)
            if parser.get('status') != 'parsed':
                if callable(getattr(module, 'discovery_links', None)):
                    discovery.setdefault('incomplete_pages', []).append(category_url)
                    if pending_branches:
                        category_url = pending_branches.pop(0)
                        continue
                break
            if callable(getattr(module, 'discovery_links', None)):
                discovery_products = sorted(
                    (p for p in products if p['slug'] not in covered_models),
                    key=lambda p: (prices.get(p['id'], ('', True))[1], p['id']))
                pending_branches.extend(link for link in module.discovery_links(html, category_url, discovery_products)
                                        if link not in category_seen and link not in pending_branches)
                if next_category_url and next_category_url not in category_seen and next_category_url not in pending_branches:
                    pending_branches.append(next_category_url)
                next_category_url = pending_branches.pop(0) if pending_branches else None
            category_url = next_category_url
            if checkpoint:
                checkpoint(result)
        discovery['pagination_complete'] = category_url is None
        if getattr(module, 'DISCOVERY_SCOPE', None):
            discovery.update(scope=module.DISCOVERY_SCOPE,
                             branch_queue_complete=category_url is None and not discovery.get('incomplete_pages'),
                             pagination_complete=False)
        known = ({observed_url(url) for url, _ in targets}
                 | {observed_url(url) for _, url in processed} | inventory_urls)
        discovery['destinations_seen'] = len(cards)
        matched = match_catalog_destinations([c for c in cards if c['source_url'] not in known], products)
        already_discovered = Counter(row['slug'] for row in discovery['new_targets'])
        for product in sorted(products, key=lambda p: (prices.get(p['id'], ('', True))[1], p['id'])):
            if product['slug'] in covered_models:
                continue
            for candidate in matched.get(product['id'], [])[:max(0, 2 - already_discovered[product['slug']])]:
                discovery['new_targets'].append({'slug': product['slug'], **candidate})
                result['queued'] += 1
                process_target(candidate['source_url'], product)
                result['models_without_priority_hints'] = [slug for slug in
                    result['models_without_priority_hints'] if slug != product['slug']]
    try:
        if discover_hepsiburada:
            import hepsiburada_catalog_discovery
            discover_category('Hepsiburada', 'hepsiburada_category_discovery', hepsiburada_catalog_discovery,
                              transport.read_hepsiburada_category)
        if discover_teknosa:
            import teknosa_catalog_discovery
            discover_category('Teknosa', 'category_discovery', teknosa_catalog_discovery,
                              transport.read_teknosa_category)
        if discover_trendyol:
            import trendyol_catalog_discovery
            discover_category('Trendyol', 'trendyol_category_discovery', trendyol_catalog_discovery,
                              transport.read_trendyol_category)
        for url, product in ([] if category_audit_only else refresh_targets):
            process_target(url, product)
        if discover_n11:
            from n11_catalog_source import discover_catalog_targets
            from reviewed_offer_targets import with_reviewed_radio
            from tr_price_scraper import TRPriceScraper
            inspected = {row['observed_url'] for row in result['rows']} | inventory_urls
            for product in sorted(products, key=lambda p: (prices.get(p['id'], ('', True))[1], p['id'])):
                if product['slug'] in covered_models:
                    continue
                specs = with_reviewed_radio(product, PriceUpdater.get_expected_specs(product.get('attributes')))
                storage = specs.get('storage_gb')
                model = TRPriceScraper._catalog_model_name(product['name'], specs)
                if not model or not storage:
                    continue
                query = f'{model} {storage} GB'
                html, reason, status = transport.read_n11_search(query)
                diagnostic = {'slug': product['slug'], 'retailer': 'n11', 'query': query,
                              'reason': reason, 'http_status': status, 'new_targets': []}
                result['discovery'].append(diagnostic)
                if html is not None:
                    candidates = discover_catalog_targets(html, product['name'], specs,
                                                          excluded_urls=inspected)
                    for candidate in candidates:
                        url = candidate['source_url']
                        if url in inspected:
                            continue
                        inspected.add(url)
                        diagnostic['new_targets'].append(candidate)
                        result['queued'] += 1
                        process_target(url, product)
                    if diagnostic['new_targets']:
                        result['models_without_priority_hints'] = [slug for slug in
                            result['models_without_priority_hints'] if slug != product['slug']]
                if checkpoint:
                    checkpoint(result)
        # Other retailers and the ordinary seed queue supply useful independent
        # work while the source's existing cooldown runs. Resume only observed
        # pending pages once; retain the same transport, robots, circuit and
        # total page/PDP limits. Explicit denials are never retried here.
        for retailer, report_key, module, read_category, continuation in deferred_categories:
            if transport.report().get(retailer, {}).get('blocked') is True:
                result[report_key]['resume_status'] = 'source_blocked'
                continue
            discover_category(retailer, report_key, module, read_category, continuation)
        result['status'] = ('category_audit_completed' if category_audit_only else
                            'publication_failed' if publication_failed else 'completed')
    except Exception as error:
        result.update(status='failed', error_type=type(error).__name__)
        if checkpoint:
            checkpoint(result)
        raise
    finally:
        result['sources'] = transport.report()
    if checkpoint:
        checkpoint(result)
    return result


def category_page_diagnostics(result):
    """Public category routing/parser facts only; no source bodies or row prices."""
    events = []
    for key in ('category_discovery', 'trendyol_category_discovery', 'hepsiburada_category_discovery'):
        discovery = result.get(key) or {}
        for index, page in enumerate(discovery.get('pages', []), 1):
            parser = page.get('parser') or {}
            events.append({'category_page': index, 'retailer': discovery.get('retailer'),
                'url': page.get('url'), 'reason': page.get('reason'), 'http_status': page.get('http_status'),
                'parser_status': parser.get('status'), 'observed_headings': parser.get('observed_headings', [])})
    return events


def save_report(result):
    Path('priority-catalog-sweep.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    counts = Counter(row.get('reader_reason', row.get('reason')) for row in result['rows'])
    lines = ['## Systematic priority retailer recovery',
             f"Status: {result['status']}; catalog: {result['catalog_count']}; "
             f"visited: {len(result['rows'])}/{result['queued']}; accepted: {result['accepted']}.",
             '', '| Model | Retailer | HTTP | Parser | Publication |', '|---|---|---:|---|---|']
    if result.get('purpose') == 'readonly_category_routing_audit':
        lines.insert(2, 'Read-only category routing audit: retailer product pages were not fetched; no prices were written. This does not pass the catalog price coverage gate.')
    for row in result['rows']:
        lines.append(f"| {row['slug']} | {row['retailer']} | {row['http_status']} | "
                     f"{row.get('reader_reason', row['reason'])} | "
                     f"{row.get('publication', {}).get('status', '')} |")
    lines += ['', 'Reasons: ' + json.dumps(counts, ensure_ascii=False), '',
              'Sources: ' + json.dumps(result.get('sources'), ensure_ascii=False), '',
              'No-hint models: ' + ', '.join(result['models_without_priority_hints'])]
    if result.get('discovery_inventory'):
        lines += ['', 'Discovery inventory: ' + json.dumps(result['discovery_inventory'], ensure_ascii=False)]
    if result.get('coverage_priority'):
        priority = result['coverage_priority']
        lines += ['', 'Coverage priority: ' + json.dumps({
            'status': priority['status'],
            'retained_current_multi_merchant_models': len(priority['retained_current_multi_merchant_models']),
            'models_needing_recovery': len(priority['models_needing_recovery']),
            'retailer_observation_timestamps_renewed': False}, ensure_ascii=False)]
    if result.get('discovery'):
        lines += ['', 'Automatic n11 discovery: ' + json.dumps({
            'catalog_queries': len(result['discovery']),
            'search_documents_read': sum(row['reason'] == 'document_read' for row in result['discovery']),
            'new_targets': sum(len(row['new_targets']) for row in result['discovery'])}, ensure_ascii=False)]
    for key in ('category_discovery', 'trendyol_category_discovery', 'hepsiburada_category_discovery'):
        if not result.get(key):
            continue
        discovery = result[key]
        lines += ['', f"Automatic {discovery['retailer']} category discovery: " + json.dumps({
            'pages': len(discovery['pages']), 'pagination_complete': discovery['pagination_complete'],
            'scope': discovery.get('scope'), 'branch_queue_complete': discovery.get('branch_queue_complete'),
            'deferred_observed_pages': discovery.get('deferred_observed_pages', 0),
            'resume_passes': discovery.get('resume_passes', 0),
            'destinations_seen': discovery.get('destinations_seen'),
            'new_targets': len(discovery['new_targets'])}, ensure_ascii=False)]
    if result.get('public_coverage'):
        lines += ['', 'Public coverage: ' + json.dumps(result['public_coverage'], ensure_ascii=False)]
    for event in category_page_diagnostics(result):
        if event['reason'] != 'document_read' or event['parser_status'] != 'parsed':
            lines += ['', 'Incomplete category page: ' + json.dumps(event, ensure_ascii=False)]
    for row in result['rows']:
        other = row.get('other_memory_variant')
        if isinstance(other, dict) and other.get('scope') == 'other_memory_variant_preview_only':
            offer = other['offer']
            lines += ['', 'Other memory variant (preview only; not an offer on the original SKU): ' + json.dumps({
                'catalog_slug': row['slug'], 'catalog_memory': other['catalog_memory'],
                'observed_specs': other['observed_specs'], 'seller': offer['merchant'],
                'source_url': offer['url'], 'price': offer['price'], 'writes': False}, ensure_ascii=False)]
    Path('priority-catalog-sweep.md').write_text('\n'.join(lines))


def coverage_readback(result, current):
    """An exhausted queue is operational completion, not sufficient coverage."""
    if not isinstance(current, dict) or len(current) != result['catalog_count']:
        return {'passed': False, 'reason': 'public_catalog_readback_changed_or_incomplete'}
    priced = sum(row[1] is True for row in current.values())
    return {'catalog': len(current), 'priced': priced, 'ratio': priced / len(current),
            'minimum_ratio': 0.8, 'passed': priced / len(current) >= 0.8,
            'documents_read': sum(row.get('reason') == 'document_read' and not row.get('document_reused')
                                  for row in result['rows']),
            'source_cooldown_skips': sum(row.get('reason') == 'source_cooldown' for row in result['rows']),
            'source_circuit_skips': sum(row.get('reason') == 'source_circuit_open' for row in result['rows'])}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--n11-preflight', action='store_true')
    parser.add_argument('--coverage-first', action='store_true')
    parser.add_argument('--hepsiburada-category-audit', action='store_true')
    args = parser.parse_args()
    if args.hepsiburada_category_audit and (args.publish or args.n11_preflight):
        raise ValueError('The category routing audit is read-only')
    if args.n11_preflight:
        if args.publish or args.coverage_first:
            raise ValueError('The collector preflight is read-only')
        client = OfferIngestionClient.from_env()
        products = client.get_published_products()
        candidate = next((row for row in catalog_queue(products, {})
                          if product_host(row[0]) == 'www.n11.com'), None)
        if candidate is None:
            raise ValueError('No catalog n11 destination available')
        transport = PriorityRetailerTransport(maximum_per_host=2,
                                             n11_collector=client.n11_document_get)
        row = inspect_target(transport, candidate[0], candidate[1])
        result = {'purpose': 'n11_collector_readonly_preflight', 'catalog_count': len(products),
                  'status': 'completed' if row['reason'] == 'document_read' else 'failed',
                  'publish': False, 'writes': False, 'accepted': 0, 'queued': 1, 'rows': [row],
                  'models_without_priority_hints': [], 'sources': transport.report()}
        save_report(result)
        print(Path('priority-catalog-sweep.md').read_text())
        if result['status'] != 'completed':
            raise SystemExit('Dedicated source collector preflight failed; no writes')
        return
    visited = 0
    category_events_seen = set()
    def checkpoint(result):
        nonlocal visited
        save_report(result)
        for event in category_page_diagnostics(result):
            identity = (event['retailer'], event['category_page'])
            if identity not in category_events_seen:
                category_events_seen.add(identity)
                print(json.dumps(event, ensure_ascii=False), flush=True)
        if len(result['rows']) > visited:
            visited = len(result['rows'])
            row = result['rows'][-1]
            print(json.dumps({'visited': visited, 'queued': result['queued'],
                'accepted': result['accepted'], 'slug': row['slug'], 'retailer': row['retailer'],
                'url': row['url'], 'observed_url': row['observed_url'], 'resolved_url': row.get('resolved_url'),
                'reason': row.get('reader_reason', row['reason']), 'http_status': row['http_status'],
                'expected_specs': row.get('expected_specs'), 'heading': row.get('heading'),
                'physical_facts': row.get('physical_facts'), 'publication': row.get('publication')},
                ensure_ascii=False), flush=True)
    client = OfferIngestionClient.from_env()
    try:
        listing_inventory = client.get_offer_identity_readiness()
    except IngestionClientError:
        # A missing optimization cannot suppress the ordinary fresh reads.
        listing_inventory = {}
    result = sweep(client=client, publish=args.publish, checkpoint=checkpoint,
                   discover_n11=not args.hepsiburada_category_audit,
                   discover_teknosa=not args.hepsiburada_category_audit,
                   discover_trendyol=not args.hepsiburada_category_audit,
                   discover_hepsiburada=True, current_listing_inventory=listing_inventory,
                   coverage_first=args.coverage_first or args.hepsiburada_category_audit,
                   category_audit_only=args.hepsiburada_category_audit)
    if args.hepsiburada_category_audit:
        save_report(result)
        print(Path('priority-catalog-sweep.md').read_text())
        return
    result['public_coverage'] = coverage_readback(result, client.get_public_price_priority())
    save_report(result)
    print(Path('priority-catalog-sweep.md').read_text())
    if result['status'] != 'completed':
        raise SystemExit('Publication verification failed; remaining queue inspected without writes')
    if not result['public_coverage']['passed']:
        raise SystemExit('Public catalog coverage gate failed; queue completion alone is insufficient')


if __name__ == '__main__':
    main()
