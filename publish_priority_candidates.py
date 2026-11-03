"""One bounded manual refresh of automatically audited priority retailer candidates."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path

from offer_ingestion_client import OfferIngestionClient
from priority_retailer_transport import HOSTS, product_host
from retailer_offer_refresh import RetailerOfferRefresher
from reviewed_offer_targets import with_reviewed_radio
from update_prices import PriceUpdater


class CandidatePublicationError(ValueError):
    def __init__(self, reason, result):
        super().__init__(reason)
        self.result = result


def _fresh(value, seconds=300):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.tzinfo is not None and -60 <= (datetime.now(timezone.utc) - stamp).total_seconds() <= seconds
    except (AttributeError, ValueError, TypeError):
        return False


def _matched_rows(identity):
    if (not isinstance(identity, dict) or identity.get('evidenceVersion') != 'product-offer-match-v1'
            or identity.get('mode') != 'shadow_audit_only' or not _fresh(identity.get('auditedAt'))
            or not isinstance(identity.get('productCoverage'), list)):
        raise ValueError('current_commercial_readback_unavailable')
    rows = {}
    for product in identity['productCoverage']:
        if not isinstance(product, dict) or not isinstance(product.get('matchedOffers'), list):
            raise ValueError('invalid_commercial_readback')
        slug = product.get('slug')
        if not isinstance(slug, str) or slug in rows:
            raise ValueError('ambiguous_commercial_readback')
        rows[slug] = product['matchedOffers']
    return rows


def _exact_current(record, current):
    for row in current:
        if (not isinstance(row, dict) or row.get('source_url') != record['source_url']
                or row.get('merchant_name') != record['seller']):
            continue
        try:
            if Decimal(str(row.get('price'))) == Decimal(str(record['price'])):
                return True
        except (InvalidOperation, ValueError, TypeError):
            continue
    return False


def refresh_candidates(audit, *, client, reader=None):
    if (not isinstance(audit, dict) or audit.get('purpose') != 'automatic_priority_retailer_diagnosis'
            or audit.get('writes') is not False or not isinstance(audit.get('rows'), list)
            or not 1 <= len(audit['rows']) <= 32):
        raise ValueError('invalid_same_run_audit')
    rows = [row for row in audit['rows'] if isinstance(row, dict) and row.get('prepared_offer') is not None]
    if len(rows) > 24:
        raise ValueError('candidate_budget_exceeded')
    catalog = client.get_published_products()
    products = {p['slug']: p for p in catalog}
    if len(products) != len(catalog) or len(catalog) != audit.get('catalog_count'):
        raise ValueError('catalog_changed')
    reader = reader or RetailerOfferRefresher()
    updater = PriceUpdater(ingestion_client=client, offer_refresher=reader)
    result = {'purpose': 'single_manual_priority_candidate_refresh', 'writes': False,
              'status': 'prepared', 'candidates': len(rows), 'rows': [], 'prepared_offers': []}
    seen, url_owners = set(), {}
    for row in rows:
        old = row['prepared_offer']
        slug = row.get('slug')
        url = old.get('url') if isinstance(old, dict) else None
        host = product_host(url)
        if (slug not in products or slug == 'samsung-galaxy-a37-5g' or host not in HOSTS
                or row.get('http_status') != 200 or row.get('reason') != 'document_read'
                or old.get('platform') != HOSTS[host] or row.get('retailer') != HOSTS[host]
                or not _fresh(old.get('observed_at')) or (slug, url) in seen
                or url in url_owners and url_owners[url] != slug):
            raise ValueError('unbound_stale_or_duplicate_candidate')
        seen.add((slug, url))
        url_owners[url] = slug
        product = products[slug]
        specs = with_reviewed_radio(product, updater.get_expected_specs(product.get('attributes')))
        # Read the exact newly observed source again; no offer/profile is copied
        # out of an old artifact and no new catalog row can be created here.
        offers = reader.refresh_existing_offer_targets(product['name'], [{'source_url': url}], specs)
        record = {'slug': slug, 'url': url, 'status': 'blocked'}
        result['rows'].append(record)
        if len(offers) != 1:
            record['reason'] = 'fresh_selected_offer_unavailable'
            continue
        fresh = offers[0]
        identity = ('url', 'platform', 'merchant', 'merchant_id', 'price', 'currency', 'condition',
                    'availability', 'gtin13', 'sku', 'product_id', 'listing_id', 'item_number',
                    'source_title', 'ram_gb', 'storage_gb', 'network_generation', 'observed_gtins')
        if (any(old.get(key) != fresh.get(key) for key in identity)
                or not _fresh(fresh.get('observed_at'), 60)):
            record['reason'] = 'selected_identity_or_price_changed'
            continue
        if not updater.offer_matcher.has_observed_offer_evidence(product['name'], fresh, specs):
            record['reason'] = 'commercial_title_identity_not_established'
            continue
        proof = {}
        updater.hydrate_marketplace_target_proof(product, [fresh], proof)
        record['target_proof'] = proof
        if not updater.live_offer_is_valid(product, fresh, specs):
            record['reason'] = 'catalog_variant_or_complete_seller_proof_failed'
            continue
        prepared = updater.prepare_product_offers(product, [fresh], updater.utc_timestamp(), updater.utc_timestamp())
        if len(prepared) != 1:
            record['reason'] = 'offer_preparation_failed'
            continue
        result['prepared_offers'].extend(prepared)
        record.update(status='ready', seller=fresh['merchant'], price=fresh['price'])
    # Recheck observation ages immediately before the one atomic HTTPS batch.
    prepared = result['prepared_offers']
    if any(not _fresh(o.get('observed_at')) for o in prepared):
        raise ValueError('prepared_observation_expired')
    result['already_current'] = []
    if prepared:
        current = _matched_rows(client.get_offer_identity_readiness())
        remaining = []
        for record in prepared:
            product = products[record['product_slug']]
            recent_target = any(t.get('source_url') == record['source_url']
                and (t.get('seller_name') or t.get('merchant_name')) == record['seller']
                and _fresh(t.get('checked_at'), 1800) for t in product.get('offer_targets', []))
            if recent_target and _exact_current(record, current.get(record['product_slug'], [])):
                result['already_current'].append(record)
                for row in result['rows']:
                    if row['slug'] == record['product_slug'] and row['url'] == record['source_url']:
                        row['status'] = 'already_current'
            else:
                remaining.append(record)
        prepared = result['prepared_offers'] = remaining
    if prepared:
        if any(not _fresh(o.get('observed_at')) for o in prepared):
            raise ValueError('prepared_observation_expired')
        try:
            ingestion = client.ingest_offers(prepared, require_all_accepted=True)
            result.update(writes=True, ingestion=ingestion)
            if (ingestion.get('committed') is not True or ingestion.get('accepted') != len(prepared)
                    or ingestion.get('stale_ignored') != 0 or ingestion.get('variant_rejected') != 0):
                raise ValueError('atomic_acceptance_not_confirmed')
            result['scoring'] = client.reconcile_scores()
            current = _matched_rows(client.get_offer_identity_readiness())
            if any(not _exact_current(record, current.get(record['product_slug'], [])) for record in prepared):
                raise ValueError('accepted_offer_not_visible_in_commercial_readback')
            result['commercial_readback_verified'] = True
        except Exception as error:
            result['status'] = 'publication_failed'
            result['publication_error'] = (str(error) if type(error) is ValueError and
                str(error) in {'atomic_acceptance_not_confirmed',
                               'accepted_offer_not_visible_in_commercial_readback'}
                else type(error).__name__)
            result['publication_outcome_unconfirmed'] = True
            raise CandidatePublicationError(result['publication_error'], result) from error
    result['status'] = ('completed' if prepared else 'already_current' if result['already_current']
                        else 'no_verified_candidates')
    return result


def main():
    audit = json.loads(Path('priority-retailer-audit.json').read_text())
    try:
        result = refresh_candidates(audit, client=OfferIngestionClient.from_env())
    except Exception as error:
        Path('priority-candidate-refresh.json').write_text(json.dumps({
            'purpose': 'single_manual_priority_candidate_refresh', 'status': 'failed',
            'error_type': type(error).__name__}))
        raise
    Path('priority-candidate-refresh.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    lines = ['## One-time automatic priority candidate refresh',
             f"Status: {result['status']}; accepted: {result.get('ingestion', {}).get('accepted', 0)}; "
             f"candidates: {result['candidates']}; writes: {result['writes']}."]
    for row in result['rows']:
        lines.append(f"- {row['slug']}: {row['status']}; {row.get('seller', '')} "
                     f"{row.get('price', '')}; {row.get('reason', '')}")
    Path('priority-candidate-refresh.md').write_text('\n'.join(lines))
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
