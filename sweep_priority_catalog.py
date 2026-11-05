"""Systematic existing-catalog recovery, with fresh proof and readback per offer.

No sampling, catalog creation, price guessing, or deferred stale artifact replay.
One manual invocation exhausts the bounded source queue; writing is opt-in.
"""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
from urllib.parse import unquote, urlparse

from audit_priority_retailers import inspect_target
from offer_ingestion_client import IngestionClientError, OfferIngestionClient
from priority_retailer_transport import HOSTS, PriorityRetailerTransport, product_host
from publish_priority_candidates import CandidatePublicationError, CandidateObservationExpired, _matched_rows, refresh_candidates
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


def catalog_queue(products, prices, *, diagnostic=None):
    """Visit every model before additional colors, excluding ambiguous owners."""
    from tr_price_scraper import TRPriceScraper
    matcher = TRPriceScraper()
    owners, groups = defaultdict(dict), defaultdict(list)
    for product in products:
        name_words = set(matcher._identity_words(product.get('name') or ''))
        expected_brands = {brand for brand, aliases in matcher.PHONE_BRAND_ALIASES.items()
                           if name_words & set(aliases)}
        allowed_brands = expected_brands | ({'xiaomi'} if expected_brands & {'poco', 'redmi'} else set())
        for target in product.get('offer_targets', []):
            url = target.get('source_url') if isinstance(target, dict) else None
            if product_host(url):
                path_words = set(matcher._identity_words(unquote(urlparse(url).path)))
                observed_brands = {brand for brand, aliases in matcher.PHONE_BRAND_ALIASES.items()
                                   if path_words & set(aliases)}
                # Negative routing evidence only. Unknown/brandless slugs and
                # other capacities still receive a fresh selected PDP read.
                # Removing a definitely wrong owner also frees that same URL
                # for its actual model instead of making ownership ambiguous.
                if expected_brands and observed_brands - allowed_brands:
                    if isinstance(diagnostic, list):
                        diagnostic.append({'slug': product['slug'], 'url': url, 'reason': 'url_brand_conflict'})
                    continue
                canonical = observed_url(url)
                if product_host(canonical) is None:
                    if isinstance(diagnostic, list):
                        diagnostic.append({'slug': product['slug'], 'retailer': HOSTS[product_host(url)],
                                           'reason': 'unroutable_public_pdp_selector'})
                    continue
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


def recovery_hepsiburada_seeds(targets, prices, maximum=12):
    """Give actual PDP gaps a turn before category navigation spends the source.

    Alternate price and second-seller gaps, one existing destination per SKU.
    Prices only order requests; every quote still needs the same fresh reader.
    """
    buckets, seen = defaultdict(list), set()
    for url, product in targets:
        if product_host(url) != 'www.hepsiburada.com' or product['slug'] in seen:
            continue
        seen.add(product['slug'])
        status = prices.get(product['id'])
        priced = (status[1] if isinstance(status, (tuple, list)) and len(status) == 2
                  and status[0] == product['slug'] and type(status[1]) is bool else None)
        buckets[priced].append((url, product))
    ordered = []
    for index in range(max((len(rows) for rows in buckets.values()), default=0)):
        for priced in (False, True, None):
            if index < len(buckets[priced]):
                ordered.append(buckets[priced][index])
                if len(ordered) >= maximum:
                    return ordered
    return ordered


def sweep(*, client, publish=False, transport=None, reader=None, checkpoint=None,
          discover_n11=False, discover_teknosa=False, discover_trendyol=False,
          discover_hepsiburada=False, current_listing_inventory=None, coverage_first=False,
          category_audit_only=False, category_frontier_path=None, memory_discovery=False,
          memory_recovery=None):
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
    hint_exclusions = []
    targets = catalog_queue(products, prices, diagnostic=hint_exclusions)
    result = {'purpose': 'systematic_priority_catalog_recovery', 'catalog_count': len(products),
              'publish': publish, 'writes': False, 'status': 'running', 'queued': len(targets),
              'conflicting_hint_exclusions': [r for r in hint_exclusions if r['reason'] == 'url_brand_conflict'],
              'unroutable_hint_exclusions': [r for r in hint_exclusions if r['reason'] == 'unroutable_public_pdp_selector'],
              'rows': [], 'accepted': 0, 'discovery': [], 'models_without_priority_hints': sorted(
                  {p['slug'] for p in products} - {p['slug'] for _, p in targets})}
    if memory_recovery is not None:
        if not memory_discovery or category_audit_only:
            raise ValueError('incremental_memory_recovery_requires_memory_sweep')
        result['initial_catalog_count'] = len(products)
        memory_recovery.start(products, prices)
    from hepsiburada_category_frontier import load_frontier, save_frontier
    frontier, frontier_status = load_frontier(
        category_frontier_path if discover_hepsiburada and not category_audit_only else None, products)
    if frontier_status != 'disabled':
        result['hepsiburada_category_continuation'] = {
            'status': frontier_status, 'catalog_fingerprint': frontier['catalog_fingerprint'],
            'cycle_started_at': frontier['cycle_started_at'],
            'restored_pending': len(frontier['pending']), 'restored_visited': len(frontier['visited']),
            'prices_or_offer_evidence_restored': False}
    # The catalog's 12 refresh hints are not a complete list of current listing
    # resources. Do not let those omitted, already verified colors consume the
    # small discovery allowance again. This inventory is only an exclusion set:
    # it never supplies a price, evidence, or permission to write an offer.
    inventory_urls = set()
    covered_models = set()
    n11_present_models = set()
    platform_scopes = {
        'Hepsiburada': ('www.hepsiburada.com', 'hepsiburada_store_gap_discovery', discover_hepsiburada),
        'Trendyol': ('www.trendyol.com', 'trendyol_store_gap_discovery', discover_trendyol),
        'Teknosa': ('www.teknosa.com', 'teknosa_store_gap_discovery', discover_teknosa),
    }
    platform_present_models = {retailer: set() for retailer in platform_scopes}
    result['priority_store_coverage_before'] = {'status': 'unavailable'}
    if current_listing_inventory is not None:
        try:
            inventory = _matched_rows(current_listing_inventory)
            if set(inventory) != {p['slug'] for p in products}:
                raise ValueError('inventory_catalog_changed')
            inventory_urls = {observed_url(row['source_url']) for rows in inventory.values()
                              for row in rows if isinstance(row, dict) and product_host(row.get('source_url'))}
            result['discovery_inventory'] = {'status': 'current', 'priority_listing_destinations': len(inventory_urls)}
            result['priority_store_coverage_before'] = priority_store_coverage_readback(current_listing_inventory, products)
            n11_present_models = {slug for slug, rows in inventory.items() if any(
                product_host(row.get('source_url')) == 'www.n11.com' for row in rows if isinstance(row, dict))}
            for retailer, (host, _, _) in platform_scopes.items():
                platform_present_models[retailer] = {slug for slug, rows in inventory.items() if any(
                    product_host(row.get('source_url')) == host for row in rows if isinstance(row, dict))}
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
    n11_gap_models = covered_models - n11_present_models
    platform_gap_models = {retailer: covered_models - platform_present_models[retailer]
                           for retailer in platform_scopes}
    platform_gap_cards = {retailer: {} for retailer in platform_scopes}
    for retailer, (_, report_key, enabled) in platform_scopes.items():
        if enabled:
            result[report_key] = {
                'eligible_models': sorted(platform_gap_models[retailer]), 'admitted_models': [],
                'source_budget_shared': True, 'routing_hint_limit': 2000,
                'prices_or_offer_evidence_reused': False}
    n11_gap_admitted = set()
    if discover_n11:
        result['n11_store_gap_discovery'] = {
            'eligible_models': sorted(n11_gap_models), 'source_budget_shared': True,
            'admitted_models': [], 'same_run_query_reuses': 0,
            'prices_or_offer_evidence_reused': False}

    def admit_n11_gap(slug):
        if slug not in n11_gap_models:
            return False
        if slug not in n11_gap_admitted:
            n11_gap_admitted.add(slug)
            result['n11_store_gap_discovery']['admitted_models'].append(slug)
        return True
    if memory_discovery:
        from automatic_memory_variants import memory_parent_priority
        result['memory_variant_priority'] = memory_parent_priority(products, prices)
    publication_failed = False
    processed = set()
    refresh_urls = {url for url, _ in refresh_targets}
    sibling_counts = Counter()
    seller_counts = Counter()
    memory_counts = Counter()
    memory_retailer_counts = Counter()
    trendyol_category_hints = []
    if discover_trendyol and not category_audit_only:
        result['trendyol_brand_category_discovery'] = {'scope': 'observed_pdp_breadcrumbs_only',
            'initial_model_limit': 8, 'seed_models': [], 'observed_category_urls': trendyol_category_hints,
            'prices_or_offer_evidence_reused': False, 'source_budget_shared': True}
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
        if discover_trendyol and product_host(row.get('observed_url')) == 'www.trendyol.com':
            from trendyol_catalog_discovery import category_identity
            for hint in row.get('discovered_categories', []):
                target = hint.get('source_url') if isinstance(hint, dict) else None
                identity = category_identity(target)
                if (identity and identity['brand'] and identity['page'] == 1
                        and hint.get('discovery_scope') == 'public_phone_brand_category'
                        and hint.get('source_document_url') == row.get('resolved_url', row['observed_url'])
                        and target not in trendyol_category_hints):
                    trendyol_category_hints.append(target)
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
                except CandidateObservationExpired:
                    # A slow preflight may age this observation before any
                    # writer is called. Skip it; subsequent targets require
                    # their own fresh proof and all existing checks.
                    refreshed = {'status': 'expired_before_write', 'writes': False}
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
        if (memory_recovery is not None and not publication_failed
                and row.get('other_memory_variant')):
            memory_recovery.observe(result, row, products, prices)
            publication_failed |= memory_recovery.failed
        result['sources'] = transport.report()
        if checkpoint:
            checkpoint(result)
        if memory_discovery and row.get('prepared_offer') is None and row.get('other_memory_variant'):
            from automatic_memory_variants import existing_selected_memory_sibling
            sibling = existing_selected_memory_sibling(product, row['other_memory_variant'], products)
            if sibling is not None and (sibling['slug'] not in covered_models or
                    discover_n11 and row['retailer'] == 'n11' and admit_n11_gap(sibling['slug'])):
                offer = row['other_memory_variant']['offer']
                destination = offer['url']
                if (sibling['slug'], destination) not in processed:
                    row['routed_to_existing_variant'] = {'id': sibling['id'], 'slug': sibling['slug']}
                    result['queued'] += 1
                    process_target(destination, sibling, audited_row={
                        'slug': sibling['slug'], 'retailer': row['retailer'], 'url': destination,
                        'observed_url': row['observed_url'], 'reason': row['reason'],
                        'http_status': row['http_status'], 'document_reused': True,
                        'reader_reason': 'fresh_selected_existing_memory_sibling',
                        'prepared_offer': offer})
                    result['models_without_priority_hints'] = [slug for slug in
                        result['models_without_priority_hints'] if slug != sibling['slug']]
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
        if product_host(row.get('observed_url')) == 'www.n11.com':
            for candidate in row.get('discovered_sellers', []):
                destination = candidate.get('source_url') if isinstance(candidate, dict) else None
                key = ('n11', product['slug'])
                if (seller_counts[key] >= 3 or product_host(destination) != 'www.n11.com'
                        or candidate.get('discovery_scope') not in ('public_other_seller_selector', 'public_selected_sku_selector')
                        or candidate.get('source_document_url') != row.get('resolved_url', row.get('observed_url'))
                        or destination in refresh_urls or observed_url(destination) in inventory_urls
                        or (product['slug'], destination) in processed):
                    continue
                seller_counts[key] += 1
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
    def process_category_cards(retailer, discovery, cards, *, early=False):
        from teknosa_catalog_discovery import match_catalog_destinations
        known = ({observed_url(url) for url, _ in targets}
                 | {observed_url(url) for _, url in processed} | inventory_urls)
        matched = match_catalog_destinations([c for c in cards if c['source_url'] not in known], products)
        already_discovered = Counter(row['slug'] for row in discovery['new_targets'])
        for product in sorted(products, key=lambda p: (prices.get(p['id'], ('', True))[1], p['id'])):
            if (product['slug'] in covered_models or early and
                    prices.get(product['id']) != (product['slug'], False)):
                continue
            for candidate in matched.get(product['id'], [])[:max(0, 2 - already_discovered[product['slug']])]:
                discovery['new_targets'].append({'slug': product['slug'], **candidate})
                result['queued'] += 1
                process_target(candidate['source_url'], product)
                result['models_without_priority_hints'] = [slug for slug in
                    result['models_without_priority_hints'] if slug != product['slug']]
        if memory_discovery:
            from automatic_memory_variants import memory_variant_destinations
            memory_hints = memory_variant_destinations(cards, products)
            priorities = result['memory_variant_priority']['parent_priorities']
            memory_hints.sort(key=lambda hint: priorities.get(hint[0]['slug'], 2))
            for parent, card in memory_hints:
                url = card['source_url']
                memory_key = (retailer, parent['slug'])
                # Category cards are only routing hints. Share the recovery's
                # bounded early covered-family admission before spending a PDP
                # read; the ordinary final pass keeps the same 2/24 counters.
                if (memory_counts[memory_key] >= 2 or memory_retailer_counts[retailer] >= 24
                        or (parent['slug'], url) in processed):
                    continue
                if early:
                    if memory_counts[memory_key] >= 1:
                        continue
                    admit = getattr(memory_recovery, 'admit_early_memory_hint', None)
                    if not (admit(parent, products, prices) if callable(admit)
                            else priorities.get(parent['slug'], 2) == 0):
                        continue
                memory_counts[memory_key] += 1
                memory_retailer_counts[retailer] += 1
                discovery.setdefault('memory_variant_hints', []).append({'parent_slug': parent['slug'], **card})
                result['queued'] += 1
                process_target(url, parent)

    deferred_categories = []
    def discover_category(retailer, report_key, module, read_category, resume=None):
        if resume is None:
            is_hb = report_key == 'hepsiburada_category_discovery'
            history = frontier['visited'] if is_hb else []
            pending_branches = ([u for u in frontier['pending'] if u != module.CATEGORY_URL] if is_hb else
                list(trendyol_category_hints) if report_key == 'trendyol_category_discovery' else [])
            # Always re-read the public root, then prioritize the actual unread
            # frontier. Previously parsed categories are not price evidence.
            cards, category_url = [], module.CATEGORY_URL
            category_seen = set(history) - {module.CATEGORY_URL}
            discovery = result[report_key] = {'retailer': retailer, 'pages': [],
                                            'new_targets': [], 'pagination_complete': False}
        else:
            cards, category_url, category_seen, pending_branches = resume
            discovery = result[report_key]
            discovery['resume_passes'] = 1
        redirected_categories = set(discovery.get('redirected_categories', {}).values())
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
                source_blocked = transport.report().get(retailer, {}).get('blocked') is True
                redirect_reader = getattr(transport, 'category_redirect_url', None)
                redirect_validator = getattr(module, 'category_redirect_target', None)
                if (not source_blocked and category_url not in redirected_categories
                        and callable(redirect_reader) and callable(redirect_validator)):
                    target = redirect_reader(category_url)
                    if (target and target not in category_seen
                            and redirect_validator(category_url, target) == target):
                        discovery.setdefault('redirected_categories', {})[category_url] = target
                        discovery['pages'][-1]['observed_redirect_url'] = target
                        redirected_categories.add(target)
                        pending_branches = [u for u in pending_branches if u != target]
                        category_url = target
                        continue  # Fresh GET within the same bounded category budget.
                if reason == 'source_cooldown':
                    # No request was sent. Retain this exact observed page for
                    # the later pass; do not guess a continuation around it.
                    category_seen.remove(category_url)
                    next_category_url = category_url
                else:
                    discovery.setdefault('incomplete_pages', []).append(category_url)
                    next_category_url = pending_branches.pop(0) if pending_branches else None
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
            if retailer in platform_gap_models and platform_gap_models[retailer] and not category_audit_only:
                gap_cards = platform_gap_cards[retailer]
                for card in found:
                    destination = card.get('source_url')
                    if (destination not in gap_cards and len(gap_cards) < 2000
                            and product_host(destination) == platform_scopes[retailer][0]):
                        # Only routing data survives until primary price and
                        # second-seller gaps have completed. No card amount,
                        # GTIN, stock claim or selected evidence is retained.
                        gap_cards[destination] = {key: card[key] for key in
                            ('source_url', 'discovery_title', 'stock_priority')}
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
            if (report_key == 'hepsiburada_category_discovery' and not category_audit_only
                    and (coverage_first or memory_discovery)):
                # A later category denial must not starve newly observed PDP
                # gaps. These are routing hints only: process_target applies
                # the ordinary selected-offer proof and fresh publication read.
                discovery['early_gap_reads'] = True
                process_category_cards(retailer, discovery, found, early=True)
            category_url = next_category_url
            if checkpoint:
                checkpoint(result)
        discovery['pagination_complete'] = category_url is None
        pending_urls = list(dict.fromkeys(pending for pending in
            ([category_url] if category_url else []) + pending_branches if pending not in category_seen))
        discovery['remaining_observed_pages'] = len(pending_urls)
        if report_key == 'hepsiburada_category_discovery':
            discovery['pending_observed_urls'] = pending_urls
            discovery['visited_category_urls'] = list(dict.fromkeys(frontier['visited'] + [
                page['url'] for page in discovery['pages']
                if (page.get('parser') or {}).get('status') == 'parsed']))
            if category_frontier_path is not None and not category_audit_only:
                # Preserve the completed category pass before product refresh,
                # other sources or variant publication can interrupt the job.
                # The existing always-save cache step then retains these URL
                # hints even when the remaining sweep is cancelled/times out.
                result['hepsiburada_category_frontier_saved'] = save_frontier(category_frontier_path, result)
        discovery['category_budget_exhausted'] = bool(category_url and len(discovery['pages']) >= module.MAX_CATEGORY_PAGES)
        if getattr(module, 'DISCOVERY_SCOPE', None):
            discovery.update(scope=module.DISCOVERY_SCOPE,
                             branch_queue_complete=category_url is None and not discovery.get('incomplete_pages'),
                             pagination_complete=False)
        discovery['destinations_seen'] = len(cards)
        process_category_cards(retailer, discovery, cards)
    try:
        if discover_hepsiburada and not category_audit_only and (coverage_first or memory_discovery):
            early_targets = recovery_hepsiburada_seeds(refresh_targets, prices)
            early = result['hepsiburada_pdp_priority'] = {
                'scope': 'existing_price_and_seller_gaps_before_categories',
                'initial_model_limit': 12, 'initial_models_processed': 0,
                'source_circuit_reused': True, 'stored_prices_reused': False}
            for url, product in early_targets:
                if transport.report().get('Hepsiburada', {}).get('blocked') is True:
                    break
                process_target(url, product)
                early['initial_models_processed'] += 1
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
            if not category_audit_only:
                seeds = trendyol_catalog_discovery.recovery_seed_targets(refresh_targets, prices)
                for url, product in seeds:
                    if transport.report().get('Trendyol', {}).get('blocked') is True:
                        break
                    process_target(url, product)
                    result['trendyol_brand_category_discovery']['seed_models'].append(product['slug'])
            discover_category('Trendyol', 'trendyol_category_discovery', trendyol_catalog_discovery,
                              transport.read_trendyol_category)
        for url, product in ([] if category_audit_only else refresh_targets):
            process_target(url, product)
        if discover_n11:
            from n11_catalog_source import discover_catalog_targets, discover_catalog_memory_targets, public_search_card_document
            from reviewed_offer_targets import with_reviewed_radio
            from tr_price_scraper import TRPriceScraper
            inspected = {row['observed_url'] for row in result['rows']} | inventory_urls
            search_hints = {}
            priorities = (result.get('memory_variant_priority') or {}).get('parent_priorities', {})
            for product in sorted(products, key=lambda p: (
                    p['slug'] in covered_models,
                    priorities.get(p['slug'], 2) if memory_discovery else 0,
                    prices.get(p['id'], ('', True))[1], p['id'])):
                store_gap = product['slug'] in covered_models
                if store_gap and not admit_n11_gap(product['slug']):
                    continue
                specs = with_reviewed_radio(product, PriceUpdater.get_expected_specs(product.get('attributes')))
                storage = specs.get('storage_gb')
                model = TRPriceScraper._catalog_model_name(product['name'], specs)
                if not model or not storage:
                    continue
                phone_search = getattr(transport, 'read_n11_phone_search', None)
                if callable(phone_search):
                    import re
                    # Radio suffixes are often absent from public n11 titles.
                    # Broaden only this phone-category URL query, preserving
                    # Pro/Plus/Max and every selected-PDP verification below.
                    query_model = re.sub(r'\s+', ' ', re.sub(r'\b(?:4g|5g|lte)\b', '', model, flags=re.I)).strip()
                    query = query_model if memory_discovery else f'{query_model} {storage} GB'
                else:
                    query = model if memory_discovery else f'{model} {storage} GB'
                reused = query in search_hints
                if reused:
                    html, reason, status = search_hints[query], 'same_run_public_search_hints', None
                    result['n11_store_gap_discovery']['same_run_query_reuses'] += 1
                else:
                    html, reason, status = (phone_search(query) if callable(phone_search)
                                            else transport.read_n11_search(query))
                    if html is not None and status == 200:
                        # Only observed public names/URLs survive to another SKU.
                        # All price/SKU/seller/stock evidence requires a new PDP.
                        search_hints[query] = public_search_card_document(html)
                diagnostic = {'slug': product['slug'], 'retailer': 'n11', 'query': query,
                              'reason': reason, 'http_status': status, 'new_targets': []}
                if store_gap:
                    diagnostic['coverage_scope'] = 'missing_n11_platform_offer'
                if reused:
                    diagnostic['routing_hints_reused'] = True
                if callable(phone_search):
                    diagnostic['discovery_scope'] = 'public_phone_category_family_query'
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
                    if memory_discovery:
                        for candidate in discover_catalog_memory_targets(html, product, excluded_urls=inspected):
                            memory_key = ('n11', product['slug'])
                            if memory_counts[memory_key] >= 2 or memory_retailer_counts['n11'] >= 24:
                                break
                            url = candidate['source_url']
                            inspected.add(url)
                            memory_counts[memory_key] += 1
                            memory_retailer_counts['n11'] += 1
                            diagnostic.setdefault('memory_variant_hints', []).append(candidate)
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
        for retailer, (host, report_key, enabled) in platform_scopes.items():
            if not enabled or not platform_gap_models[retailer] or category_audit_only:
                continue
            from teknosa_catalog_discovery import match_catalog_destinations
            matched = match_catalog_destinations(list(platform_gap_cards[retailer].values()), products)
            hints = defaultdict(list)
            for url, product in targets:
                if product_host(url) == host:
                    hints[product['id']].append(url)
            for product in sorted(products, key=lambda p: p['id']):
                if product['slug'] not in platform_gap_models[retailer]:
                    continue
                # Prefer freshly observed category destinations, then existing
                # catalog hints. The normal shared transport and all writer
                # guards remain in force; retained rows never supply quotes.
                destinations = list(dict.fromkeys(
                    [c['source_url'] for c in matched.get(product['id'], [])] + hints[product['id']]))
                destinations = [url for url in destinations if
                    (product['slug'], url) not in processed and observed_url(url) not in inventory_urls][:2]
                if not destinations:
                    continue
                result[report_key]['admitted_models'].append(product['slug'])
                for url in destinations:
                    result['queued'] += 1
                    process_target(url, product)
        result['status'] = ('category_audit_completed' if category_audit_only else
                            'publication_failed' if publication_failed else 'completed')
    except Exception as error:
        result.update(status='failed', error_type=type(error).__name__)
        if checkpoint:
            checkpoint(result)
        raise
    finally:
        result['sources'] = transport.report()
    if memory_recovery is not None:
        memory_recovery.finish(result, products, prices)
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
                **({'observed_redirect_url': page['observed_redirect_url']} if page.get('observed_redirect_url') else {}),
                'parser_status': parser.get('status'), 'observed_headings': parser.get('observed_headings', [])})
    return events


def save_report(result):
    Path('priority-catalog-sweep.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    counts = Counter(row.get('reader_reason', row.get('reason')) for row in result['rows'])
    lines = ['## Systematic priority retailer recovery',
             f"Status: {result['status']}; catalog: {result['catalog_count']}; "
             f"visited: {len(result['rows'])}/{result['queued']}; accepted: {result['accepted']}.",
             '', '| Model | Retailer | HTTP | Parser | Publication |', '|---|---|---:|---|---|']
    if result.get('conflicting_hint_exclusions'):
        lines.insert(2, f"Excluded {len(result['conflicting_hint_exclusions'])} historical URL hints with explicit conflicting brands; catalog coverage denominator is unchanged.")
    if result.get('unroutable_hint_exclusions'):
        lines.insert(2, f"Skipped {len(result['unroutable_hint_exclusions'])} unsupported historical PDP selectors; their catalog models remain eligible for automatic category discovery.")
    if result.get('purpose') == 'readonly_category_routing_audit':
        lines.insert(2, 'Read-only category routing audit: retailer product pages were not fetched; no prices were written. This does not pass the catalog price coverage gate.')
    if result.get('purpose') == 'readonly_existing_catalog_target_preflight':
        lines.insert(2, 'Read-only selected-PDP parser proof against the current published catalog: one target, no publication and no catalog/spec writes. This does not pass the catalog price coverage gate.')
    for row in result['rows']:
        lines.append(f"| {row['slug']} | {row['retailer']} | {row['http_status']} | "
                     f"{row.get('reader_reason', row['reason'])} | "
                     f"{row.get('publication', {}).get('status', '')} |")
    lines += ['', 'Reasons: ' + json.dumps(counts, ensure_ascii=False), '',
              'Sources: ' + json.dumps(result.get('sources'), ensure_ascii=False), '',
              'No-hint models: ' + ', '.join(result['models_without_priority_hints'])]
    for retailer in ('Hepsiburada', 'Trendyol', 'Teknosa'):
        gaps = result.get(retailer.lower() + '_store_gap_discovery')
        if gaps:
            lines += ['', f"{retailer} platform gaps: {len(gaps['admitted_models'])}/{len(gaps['eligible_models'])} "
                      'models admitted after primary recovery; existing source limits and fresh selected-offer proof retained.']
    if result.get('trendyol_brand_category_discovery'):
        brands = result['trendyol_brand_category_discovery']
        lines += ['', 'Trendyol observed brand routing: ' + json.dumps({
            'seed_models': len(brands['seed_models']), 'model_limit': brands['initial_model_limit'],
            'observed_category_urls': len(brands['observed_category_urls']),
            'source_budget_shared': True, 'prices_or_offer_evidence_reused': False})]
    if result.get('n11_store_gap_discovery'):
        gaps = result['n11_store_gap_discovery']
        lines += ['', 'n11 platform-gap routing: ' + json.dumps({
            'eligible_models': len(gaps['eligible_models']), 'scheduled_models': len(gaps['admitted_models']),
            'same_run_query_reuses': gaps['same_run_query_reuses'], 'prices_or_offer_evidence_reused': False})]
    if result.get('manufacturer_radio_resolution'):
        radio = result['manufacturer_radio_resolution']
        lines += ['', 'Manufacturer radio resolution: ' + json.dumps({
            key: radio[key] for key in ('status', 'catalog_count', 'resolved', 'writes', 'price_writes', 'manufacturer_documents')})]
    for stage in ('before', 'after'):
        if result.get('priority_store_coverage_' + stage):
            lines += ['', 'Priority store coverage ' + stage + ': ' +
                      json.dumps(result['priority_store_coverage_' + stage], ensure_ascii=False)]
    if result.get('hepsiburada_category_continuation'):
        lines += ['', 'Hepsiburada category continuation: ' + json.dumps(
            result['hepsiburada_category_continuation'], ensure_ascii=False)]
    if result.get('hepsiburada_category_frontier_saved'):
        lines += ['', 'Hepsiburada category frontier saved: ' + json.dumps(
            result['hepsiburada_category_frontier_saved'], ensure_ascii=False)]
    if result.get('discovery_inventory'):
        lines += ['', 'Discovery inventory: ' + json.dumps(result['discovery_inventory'], ensure_ascii=False)]
    if result.get('coverage_priority'):
        priority = result['coverage_priority']
        lines += ['', 'Coverage priority: ' + json.dumps({
            'status': priority['status'],
            'retained_current_multi_merchant_models': len(priority['retained_current_multi_merchant_models']),
            'models_needing_recovery': len(priority['models_needing_recovery']),
            'retailer_observation_timestamps_renewed': False}, ensure_ascii=False)]
    if result.get('memory_variant_priority'):
        lines += ['', 'Memory variant priority: ' + json.dumps({key: value for key, value in
            result['memory_variant_priority'].items() if key != 'parent_priorities'}, ensure_ascii=False)]
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
            'remaining_observed_pages': discovery.get('remaining_observed_pages', 0),
            'category_budget_exhausted': discovery.get('category_budget_exhausted', False),
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


def target_preflight(*, client, url, slug, transport=None):
    """Fresh, read-only parser proof against one current published catalog SKU.

    This accepts no product/spec/price payload and never calls a publisher.
    URL text is only a negative identity guard; the ordinary full PDP parser
    must independently establish the actual selected physical variant.
    """
    from tr_price_scraper import TRPriceScraper
    from reviewed_offer_targets import with_reviewed_radio
    if (product_host(url) is None or not isinstance(slug, str)
            or not slug or len(slug) > 150):
        raise ValueError('invalid_existing_catalog_preflight_target')
    products = client.get_published_products()
    if (not products or len(products) > 200
            or len({p['id'] for p in products}) != len(products)
            or len({p['slug'] for p in products}) != len(products)):
        raise ValueError('invalid_or_oversized_catalog')
    product = next((p for p in products if p['slug'] == slug), None)
    if product is None:
        raise ValueError('preflight_product_not_published')
    specs = with_reviewed_radio(product, PriceUpdater.get_expected_specs(product.get('attributes')))
    if not TRPriceScraper().is_strict_match(product['name'], urlparse(url).path.replace('-', ' '), specs):
        raise ValueError('preflight_url_catalog_identity_conflict')
    transport = transport or PriorityRetailerTransport(maximum_per_host=2,
        n11_collector=getattr(client, 'n11_document_get', None))
    row = inspect_target(transport, url, product)
    row['expected_specs'] = {k: specs.get(k) for k in ('ram_gb', 'storage_gb', 'network_generation')}
    offer = row.get('prepared_offer')
    public_proof = ({key: offer.get(key) for key in (
        'platform', 'url', 'source_title', 'merchant', 'merchant_id', 'product_id', 'sku',
        'listing_id', 'item_number', 'gtin13', 'ram_gb', 'storage_gb', 'network_generation',
        'price', 'currency', 'condition', 'availability', 'observed_at', 'storage_evidence_source')}
        if isinstance(offer, dict) else None)
    return {'purpose': 'readonly_existing_catalog_target_preflight', 'catalog_count': len(products),
            'status': 'completed' if row.get('prepared_offer') is not None else 'rejected',
            'publish': False, 'writes': False, 'accepted': 0, 'queued': 1, 'rows': [row],
            'public_parser_proof': public_proof,
            'models_without_priority_hints': [], 'sources': transport.report()}


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


def priority_store_coverage_readback(identity, products):
    """Count fully matched current listing resources, not unverified hints.

    The audit retains every valid platform/color listing even when one seller
    has a cheaper offer elsewhere. Listing totals never become seller counts.
    """
    rows = _matched_rows(identity)
    slugs = {p['slug'] for p in products}
    if not products or len(slugs) != len(products) or set(rows) != slugs:
        raise ValueError('priority_store_coverage_catalog_changed')
    counts = {retailer: {'models_with_current_listing': 0, 'current_listing_rows': 0}
              for retailer in HOSTS.values()}
    for offers in rows.values():
        present = set()
        for offer in offers:
            host = product_host(offer.get('source_url')) if isinstance(offer, dict) else None
            if host is not None:
                retailer = HOSTS[host]
                counts[retailer]['current_listing_rows'] += 1
                present.add(retailer)
        for retailer in present:
            counts[retailer]['models_with_current_listing'] += 1
    return {'status': 'verified_current_commercial_readback', 'audited_at': identity['auditedAt'],
            'catalog': len(products), 'retailers': counts,
            'listing_rows_are_independent_seller_counts': False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--n11-preflight', action='store_true')
    parser.add_argument('--coverage-first', action='store_true')
    parser.add_argument('--hepsiburada-category-audit', action='store_true')
    parser.add_argument('--target-preflight', metavar='PUBLIC_PDP_URL')
    parser.add_argument('--target-slug', metavar='EXISTING_PUBLISHED_SLUG')
    parser.add_argument('--memory-variants', action='store_true',
                        help='Discover other capacities and independently prepare/publish existing-model variants')
    parser.add_argument('--manufacturer-radio', action='store_true',
                        help='Before the memory sweep, prove only absent radio from exact manufacturer model/code/memory')
    parser.add_argument('--memory-variant-preflight', action='store_true',
                        help='Read-only automatic capacity hints and selected technical-source diagnostic')
    args = parser.parse_args()
    if args.manufacturer_radio and (not args.memory_variants or any((args.memory_variant_preflight,
            args.n11_preflight, args.target_preflight, args.target_slug, args.hepsiburada_category_audit))):
        raise ValueError('manufacturer_radio_requires_memory_sweep')
    if args.memory_variant_preflight:
        if any((args.publish, args.memory_variants, args.target_preflight, args.target_slug, args.n11_preflight,
                args.coverage_first, args.hepsiburada_category_audit)):
            raise ValueError('memory_variant_preflight_is_read_only_and_separate')
        from automatic_memory_variants import memory_target_preflight, prepare_memory_variants
        from daily_discovery import write_json_checkpoint
        client = OfferIngestionClient.from_env()
        products = client.get_published_products()
        if not products or len(products) > 200:
            raise ValueError('invalid_or_oversized_catalog')
        transport = PriorityRetailerTransport(maximum_per_host=48, n11_collector=client.n11_document_get)
        result = memory_target_preflight(products, transport)
        save_report(result)
        print(Path('priority-catalog-sweep.md').read_text(), flush=True)
        def checkpoint_variant(report):
            write_json_checkpoint(Path('automatic-memory-variants.json'), report)
        report = prepare_memory_variants(result, products, transport=PriorityRetailerTransport(
            maximum_per_host=24, n11_collector=client.n11_document_get), checkpoint=checkpoint_variant)
        checkpoint_variant(report)
        for row in report['rows']:
            print('Automatic memory variant: ' + json.dumps(row, ensure_ascii=False), flush=True)
        for diagnostic in report.get('technical_diagnostics', []):
            print('Technical memory discovery: ' + json.dumps(diagnostic, ensure_ascii=False), flush=True)
        print('Automatic memory variant summary: ' + json.dumps({key: report[key] for key in
            ('observed_candidates', 'verified_candidates', 'technical_documents', 'created', 'existing', 'writes')}, ensure_ascii=False), flush=True)
        if report['observed_candidates'] and not report['verified_candidates']:
            raise SystemExit('No selected technical and live retailer variant passed; no writes')
        return
    if args.target_preflight is not None:
        if args.publish or args.n11_preflight or args.coverage_first or args.hepsiburada_category_audit or args.memory_variants:
            raise ValueError('The selected target preflight is read-only and cannot run a sweep')
        result = target_preflight(client=OfferIngestionClient.from_env(),
                                  url=args.target_preflight, slug=args.target_slug)
        save_report(result)
        print(Path('priority-catalog-sweep.md').read_text())
        print('Selected parser evidence: ' + json.dumps(result['public_parser_proof'], ensure_ascii=False))
        if result['status'] != 'completed':
            raise SystemExit('Selected target did not pass the full parser proof; no writes')
        return
    if args.target_slug is not None:
        raise ValueError('A target slug is only valid with --target-preflight')
    if args.hepsiburada_category_audit and (args.publish or args.n11_preflight or args.memory_variants):
        raise ValueError('The category routing audit is read-only')
    if args.n11_preflight:
        if args.publish or args.coverage_first or args.memory_variants:
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
    radio_report = None
    if args.manufacturer_radio:
        from manufacturer_catalog_radio import resolve_catalog_radios
        from daily_discovery import write_json_checkpoint
        def checkpoint_radio(report):
            write_json_checkpoint(Path('catalog-radio-resolution.json'), report)
        # A missing radio is resolved before the sweep takes its original
        # identity baseline, never in the middle of variant publication.
        radio_report = resolve_catalog_radios(client, publish=args.publish, checkpoint=checkpoint_radio)
        print('Manufacturer catalog radio: ' + json.dumps(radio_report, ensure_ascii=False), flush=True)
    try:
        listing_inventory = client.get_offer_identity_readiness()
    except IngestionClientError:
        # A missing optimization cannot suppress the ordinary fresh reads.
        listing_inventory = {}
    memory_recovery = None
    sweep_transport = None
    if args.memory_variants:
        from incremental_memory_recovery import IncrementalMemoryRecovery
        from source_ingestion import SourceIngestionClient
        from daily_discovery import write_json_checkpoint
        variant_rows_reported = 0
        variant_diagnostics_reported = 0
        def variant_checkpoint(report):
            nonlocal variant_rows_reported, variant_diagnostics_reported
            write_json_checkpoint(Path('automatic-memory-variants.json'), report)
            for row in report['rows'][variant_rows_reported:]:
                print('Automatic memory variant: ' + json.dumps(row, ensure_ascii=False), flush=True)
            variant_rows_reported = len(report['rows'])
            diagnostics = report.get('technical_diagnostics', [])
            for diagnostic in diagnostics[variant_diagnostics_reported:]:
                print('Automatic memory technical diagnostic: ' + json.dumps(diagnostic, ensure_ascii=False), flush=True)
            variant_diagnostics_reported = len(diagnostics)
        sweep_transport = PriorityRetailerTransport(maximum_per_host=350,
            n11_collector=getattr(client, 'n11_document_get', None))
        memory_recovery = IncrementalMemoryRecovery(client=client, transport=sweep_transport,
            publish=args.publish, source_client=SourceIngestionClient.from_env() if args.publish else None,
            checkpoint=variant_checkpoint)
    from hepsiburada_category_frontier import save_frontier, STATE_PATH
    result = sweep(client=client, publish=args.publish, checkpoint=checkpoint, transport=sweep_transport,
                   discover_n11=not args.hepsiburada_category_audit,
                   discover_teknosa=not args.hepsiburada_category_audit,
                   discover_trendyol=not args.hepsiburada_category_audit,
                   discover_hepsiburada=True, current_listing_inventory=listing_inventory,
                   coverage_first=args.coverage_first or args.hepsiburada_category_audit,
                   category_audit_only=args.hepsiburada_category_audit,
                   memory_discovery=args.memory_variants,
                   memory_recovery=memory_recovery,
                   category_frontier_path=None if args.hepsiburada_category_audit else STATE_PATH)
    if radio_report is not None:
        result['manufacturer_radio_resolution'] = radio_report
        result['writes'] |= radio_report['writes']
    if args.hepsiburada_category_audit:
        save_report(result)
        print(Path('priority-catalog-sweep.md').read_text())
        return
    result['hepsiburada_category_frontier_saved'] = save_frontier(STATE_PATH, result)
    save_report(result)  # Keep category progress if public price readback is temporarily unavailable.
    if args.memory_variants:
        from automatic_memory_variants import verify_variant_catalog_readback
        products = memory_recovery.before
        variant_report = memory_recovery.report
        variant_checkpoint(variant_report)
        readback = verify_variant_catalog_readback(products, client.get_published_products(), variant_report)
        result['automatic_memory_variants'] = {key: variant_report[key] for key in
            ('observed_candidates', 'eligible_source_candidates', 'deferred_source_candidates',
             'attempted_candidates', 'technical_documents', 'created', 'existing', 'writes', 'score_refresh_pending')}
        result['automatic_memory_variants']['catalog_readback'] = readback
        if memory_recovery.failed:
            result['automatic_memory_variants']['failure_reason'] = variant_report['failure_reason']
        current_prices = client.get_public_price_priority()
        result['original_catalog_coverage'] = coverage_readback({**result, 'catalog_count': len(products)},
            {p['id']: current_prices[p['id']] for p in products if p['id'] in current_prices})
        result['public_coverage'] = coverage_readback({**result, 'catalog_count': readback['catalog_after']}, current_prices)
        result['writes'] |= variant_report['writes']
        if variant_report['score_refresh_pending'] and result['status'] == 'completed':
            result['status'] = 'variant_scores_pending'
        save_report(result)
        print('Automatic memory variant summary: ' + json.dumps(result['automatic_memory_variants']), flush=True)
    else:
        result['public_coverage'] = coverage_readback(result, client.get_public_price_priority())
    try:
        current_products = client.get_published_products()
        result['priority_store_coverage_after'] = priority_store_coverage_readback(
            client.get_offer_identity_readiness(), current_products)
    except (IngestionClientError, ValueError) as error:
        # Missing diagnostics are unknown, never fake zero coverage or a
        # substitute for individual publication readback / the public 80% gate.
        result['priority_store_coverage_after'] = {'status': 'unavailable', 'error_type': type(error).__name__}
    save_report(result)
    print(Path('priority-catalog-sweep.md').read_text())
    if result['status'] != 'completed':
        raise SystemExit('Publication verification failed; remaining queue inspected without writes')
    if not result['public_coverage']['passed']:
        raise SystemExit('Public catalog coverage gate failed; queue completion alone is insufficient')


if __name__ == '__main__':
    main()
