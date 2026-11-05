"""Verify missing capacities while the discovery source is still available.

All observations remain hints until the ordinary technical and selected-offer
checks pass. Early and final preparation share one allowance and source circuit.
"""
from collections import Counter
from copy import deepcopy
import re

from automatic_memory_variants import (
    CONTRACT, MAX_VARIANTS, TechnicalVariantReader, family_key, memory_parent_priority,
    observed_variant_candidates, prepare_memory_variants, variant_identity,
    verify_variant_catalog_readback, parent_specs,
)
from offer_ingestion_client import IngestionClientError
from priority_retailer_transport import HOSTS, product_host
from publish_priority_candidates import _exact_current, _matched_rows
from tr_price_scraper import TRPriceScraper

MAX_EARLY_COVERED_FAMILIES = 6
MAX_EARLY_COVERED_HINT_FAMILIES = 2


def covered_preparation_limit():
    # Leave at least half of the shared twelve attempts for missing families.
    return min(MAX_EARLY_COVERED_FAMILIES, MAX_VARIANTS // 2)


class FreshVariantTransport:
    """Bound fresh publication reads without resetting the underlying source."""
    def __init__(self, transport):
        self.transport = transport
        self.attempts = Counter()

    def read_document(self, url):
        host = product_host(url)
        if host is None:
            return None, 'invalid_priority_pdp', None
        if self.attempts[host] >= 24:
            return None, 'variant_recheck_budget_exhausted', None
        self.attempts[host] += 1
        return self.transport.read_fresh_document(url)

    def resolved_url(self, url):
        return self.transport.resolved_url(url)

    def read_teknosa_classification(self, document_url, url):
        host = product_host(document_url)
        if host != 'www.teknosa.com':
            return None, 'invalid_selected_technical_url', None
        if self.attempts[host] >= 24:
            return None, 'variant_recheck_budget_exhausted', None
        self.attempts[host] += 1
        return self.transport.read_teknosa_classification(document_url, url, fresh=True)

    def report(self):
        return self.transport.report()


def candidate_key(candidate):
    specs = candidate['specs']
    identity = variant_identity(candidate['parent'], specs['ram_gb'], specs['storage_gb'])
    return identity['slug'], specs['network_generation']


class IncrementalMemoryRecovery:
    def __init__(self, *, client, transport, publish=False, source_client=None,
                 technical_reader=None, image_store=None, checkpoint=None):
        self.client = client
        self.transport = FreshVariantTransport(transport)
        self.publish = publish
        self.source_client = source_client
        self.technical_reader = technical_reader or TechnicalVariantReader()
        self.image_store = image_store
        self.checkpoint = checkpoint
        self.failed = False
        self.attempted = set()
        self.early_families = set()
        self.early_covered_families = set()
        self.early_covered_hint_families = set()
        self.report = {'contract': CONTRACT, 'purpose': 'automatic_existing_model_memory_variants',
            'timing': 'during_discovery_and_final_remainder', 'publish': publish,
            'writes': False, 'rows': [], 'created': 0, 'existing': 0, 'attempt_limit': MAX_VARIANTS}

    def start(self, products, prices):
        self.before = deepcopy(products)

    def _preview_key(self, row):
        parent = next((p for p in self.before if p['slug'] == row.get('slug')), None)
        preview = row.get('other_memory_variant')
        if parent is None or not isinstance(preview, dict):
            return None
        specs = preview.get('observed_specs')
        if not isinstance(specs, dict):
            return None
        try:
            return candidate_key({'parent': parent, 'specs': specs})
        except (KeyError, ValueError, TypeError):
            return None

    def _synchronize(self, products, prices, partial):
        after = self.client.get_published_products()
        readback = verify_variant_catalog_readback(self.before, after, self.report)
        # The atomic acknowledgement alone cannot establish a public quote.
        published = [r for r in partial['rows'] if r['status'] in ('created', 'existing')]
        if published:
            current = _matched_rows(self.client.get_offer_identity_readiness())
            if set(current) != {p['slug'] for p in after}:
                raise ValueError('variant_commercial_catalog_readback_incomplete')
            for row in published:
                slug = row['publication']['slug']
                for offer in row['offers']:
                    record = {**offer, 'seller': offer['merchant_name']}
                    if not _exact_current(record, current.get(slug, [])):
                        raise ValueError('variant_commercial_readback_incomplete')
                row['commercial_readback_verified'] = True
        self.report['catalog_readback'] = readback
        products[:] = after
        try:
            prices.update(self.client.get_public_price_priority())
        except IngestionClientError:
            pass  # Ordering hints are optional; live quote readback is not.

    def _prepare(self, result, products, prices, selected, limit):
        keys = {candidate_key(c) for c in selected}
        rows = [row for row in result['rows'] if self._preview_key(row) in keys]
        subset = {**result, 'rows': rows, 'sources': self.transport.report()}
        partial = prepare_memory_variants(subset, products, transport=self.transport,
            technical_reader=self.technical_reader, publish=self.publish,
            source_client=self.source_client, image_store=self.image_store,
            maximum_variants=limit)
        for row in partial['rows']:
            key = row['variant']['slug'], row['variant']['network_generation']
            if key in self.attempted:
                raise ValueError('duplicate_incremental_variant_attempt')
            self.attempted.add(key)
        self.report['rows'].extend(partial['rows'])
        for field in ('created', 'existing'):
            self.report[field] += partial[field]
        self.report['writes'] |= partial['writes']
        result['writes'] |= partial['writes']
        if self.publish and partial['writes']:
            self._synchronize(products, prices, partial)
            result['catalog_count'] = len(products)

    def _summary(self, result):
        observed = observed_variant_candidates(result, self.before)
        remaining = [c for c in observed if candidate_key(c) not in self.attempted]
        sources = self.transport.report()
        deferred = []
        for candidate in remaining:
            retailers = {HOSTS[product_host(url)] for url in candidate['targets']}
            if all(sources.get(retailer, {}).get('blocked') is True for retailer in retailers):
                specs = candidate['specs']
                deferred.append({'parent_slug': candidate['parent']['slug'],
                    'variant': {**variant_identity(candidate['parent'], specs['ram_gb'], specs['storage_gb']), **specs},
                    'reason': 'all_observed_variant_sources_blocked_for_run', 'retailers': sorted(retailers)})
        self.report.update(observed_candidates=len(observed), attempted_candidates=len(self.attempted),
            early_covered_family_attempts=len(self.early_covered_families),
            early_covered_hint_families=len(self.early_covered_hint_families),
            early_covered_family_limit=covered_preparation_limit(),
            early_covered_hint_family_limit=min(MAX_EARLY_COVERED_HINT_FAMILIES, max(0, MAX_VARIANTS - 1)),
            early_unpriced_family_attempts=len(self.early_families - self.early_covered_families),
            eligible_source_candidates=len(observed) - len(deferred),
            deferred_source_candidates=len(deferred), source_deferrals=deferred,
            technical_documents=self.technical_reader.documents,
            technical_request_attempts=getattr(self.technical_reader, 'attempts', self.technical_reader.documents),
            technical_diagnostics=getattr(self.technical_reader, 'diagnostics', []), sources=sources,
            fresh_recheck_attempts={HOSTS[host]: count for host, count in self.transport.attempts.items()},
            score_refresh_pending=sum(r.get('score_refresh_pending') is True for r in self.report['rows']),
            verified_candidates=sum(r['status'] in ('technical_and_live_offer_verified', 'created', 'existing')
                                    for r in self.report['rows']))
        result['sources'] = sources
        if self.checkpoint:
            self.checkpoint(self.report)

    def _fail(self, error):
        self.failed = True
        reason = str(error)
        self.report['failure_reason'] = (reason if isinstance(error, ValueError)
            and re.fullmatch(r'[a-z0-9_]{1,100}', reason) else type(error).__name__)

    def _covered_early_allowed(self, family):
        # An unverified category URL cannot consume a selected-offer proof
        # attempt. Both phases still share the same overall/source budgets.
        return family not in self.early_families and len(self.early_covered_families) < covered_preparation_limit()

    def admit_early_memory_hint(self, parent, products, prices):
        """Reserve a bounded category-to-PDP read, never price/SKU evidence.

        At most two covered families may spend speculative category-to-PDP
        reads. Existing proved observations get a separate preparation limit;
        failed hints cannot reserve those verification attempts indefinitely.
        """
        if self.failed or len(self.attempted) >= MAX_VARIANTS:
            return False
        level = memory_parent_priority(products, prices)['parent_priorities'].get(parent['slug'], 2)
        if level == 0:
            return True
        if level != 1:
            return False
        model = TRPriceScraper._catalog_model_name(parent['name'], parent['attributes'])
        radio = parent_specs(parent).get('network_generation')
        if not model or radio not in ('4g', '5g'):
            return False
        family = family_key(model), radio
        reserved = self.early_covered_families | self.early_covered_hint_families
        limit = min(MAX_EARLY_COVERED_HINT_FAMILIES, max(0, MAX_VARIANTS - 1))
        if (family in self.early_families or not self._covered_early_allowed(family)
                or family not in reserved and len(reserved) >= limit):
            return False
        self.early_covered_hint_families.add(family)
        return True

    def observe(self, result, row, products, prices):
        if self.failed or len(self.attempted) >= MAX_VARIANTS:
            return
        try:
            # Other capacities of a covered model can disappear behind a
            # source circuit by the final pass. Give already selected/proved
            # observations up to half of the shared allowance before that
            # source disappears; reserve the other half for missing families.
            # Unproved category hints retain their smaller independent cap.
            priorities = memory_parent_priority(products, prices)['parent_priorities']
            level = priorities.get(row['slug'], 2)
            if level not in (0, 1):
                return
            subset = {**result, 'rows': [row]}
            candidates = [c for c in observed_variant_candidates(subset, products)
                          if candidate_key(c) not in self.attempted]
            if candidates:
                candidate = candidates[0]
                model = TRPriceScraper._catalog_model_name(candidate['parent']['name'], candidate['parent']['attributes'])
                family = family_key(model), candidate['specs']['network_generation']
                if family in self.early_families:
                    return
                if level == 1 and not self._covered_early_allowed(family):
                    return
                previous = len(self.attempted)
                self._prepare(subset, products, prices, candidates, 1)
                if len(self.attempted) > previous:
                    self.early_families.add(family)
                    if level == 1:
                        self.early_covered_families.add(family)
                self._summary(result)
        except Exception as error:
            self._fail(error)
        finally:
            result['writes'] |= self.report['writes']
            result['catalog_count'] = len(products)

    def finish(self, result, products, prices):
        if not self.failed and result['status'] == 'completed':
            try:
                candidates = [c for c in observed_variant_candidates(result, products)
                              if candidate_key(c) not in self.attempted]
                self._prepare(result, products, prices, candidates, MAX_VARIANTS - len(self.attempted))
            except Exception as error:
                self._fail(error)
        if self.failed:
            result['status'] = 'variant_publication_verification_failed'
        self._summary(result)
        return self.report
