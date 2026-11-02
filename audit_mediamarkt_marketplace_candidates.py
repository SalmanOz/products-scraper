"""Bounded, read-only candidate queue for unpriced MediaMarkt marketplace phones.

Sitemap URLs are hints. A candidate is source verified only after a fresh,
robots-allowed selected PDP read proves SKU, GTIN, seller, cash price, stock,
and the catalog's RAM/storage/radio. Private offer-target checks are separate:
this report never declares a row ready for publication or writes an offer.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from tempfile import gettempdir

import requests

from audit_mediamarkt_unpriced_coverage import (
    _catalog, _has_current_price, _specs, selected_rejection_evidence,
)
from mediamarkt_marketplace_audit import catalog_variant_matches, selected_marketplace_offer
from mediamarkt_source import BOT, MAX_HTML_BYTES, MediaMarktSource, product_url


MAX_PROBES = 12
MAX_BROWSER_FAILURES = 2
OUTPUT_DIR = Path(os.environ.get('MEDIAMARKT_MARKETPLACE_CANDIDATES_DIR', gettempdir()))
PURPOSE = 'read_only_mediamarkt_unpriced_marketplace_candidate_queue'


def _candidate_queue(rows, source):
    """Find unpriced exact URL hints and remove ambiguous catalog mappings."""
    by_url = {}
    unpriced = 0
    for row in sorted(rows, key=lambda item: item['slug']):
        if _has_current_price(row):
            continue
        unpriced += 1
        specs = _specs(row)
        for target in source.discover(row['name'], specs):
            url = target.get('source_url') if isinstance(target, dict) else None
            if product_url(url):
                by_url.setdefault(url, []).append((row, specs))
    unique, ambiguous = [], []
    for url, matches in sorted(by_url.items()):
        if len(matches) == 1:
            row, specs = matches[0]
            unique.append((url, row, specs))
        else:
            ambiguous.append({'source_url': url,
                              'catalog_slugs': sorted({row['slug'] for row, _ in matches}),
                              'reason': 'ambiguous_catalog_mapping'})
    return unique, ambiguous, unpriced


def _read_selected(source, url):
    """One direct read, with one exact URL browser retry for a 403 only."""
    if (not product_url(url) or source._rules is None or
            not source._rules.can_fetch(BOT, url)):
        return None, {'reason': 'robots_disallowed_or_invalid_url', 'http_status': None}
    try:
        body, reason, status = source._direct(url, MAX_HTML_BYTES, 'text/html')
        if reason == 'http_403':
            body, final_url, browser_reason = source._browser_get(url)
            if browser_reason != 'ready' or final_url != url:
                return None, {'reason': 'browser_' + browser_reason
                              if browser_reason != 'ready' else 'changed_selected_url',
                              'http_status': status, 'browser_failed': True}
        elif reason != 'ready':
            return None, {'reason': reason, 'http_status': status}
        evidence = selected_marketplace_offer(body, url)
        if evidence is None:
            rejection = selected_rejection_evidence(body, url)
            return None, {'reason': 'selected_offer_unverified', 'http_status': 200,
                          'rejection': rejection}
        return evidence, {'reason': 'selected_marketplace_offer', 'http_status': 200}
    except (requests.RequestException, OSError, TypeError, ValueError, AttributeError):
        return None, {'reason': 'transport_error', 'http_status': None}


def _review(evidence, row, specs):
    if evidence is None:
        return 'selected_offer_unverified'
    if not isinstance(evidence.get('gtin13'), str) or not re.fullmatch(
            r'\d{13}', evidence['gtin13']):
        return 'selected_gtin_missing'
    expected_radio = specs.get('network_generation')
    observed_radio = evidence.get('network_generation')
    if expected_radio in {'4g', '5g'} and observed_radio is None:
        return 'catalog_radio_unproven_in_selected_pdp'
    if expected_radio in {'4g', '5g'} and observed_radio != expected_radio:
        return 'catalog_radio_conflict'
    if not catalog_variant_matches(evidence, row['name'], specs):
        return 'catalog_variant_mismatch'
    return 'source_verified_pending_private_target_review'


def run_audit(*, catalog_get=None, source=None, offset=0):
    """Produce one bounded page of source candidates; zero production writes."""
    if type(offset) is not int or offset < 0 or offset > 1000:
        raise ValueError('offset_must_be_0_to_1000')
    source = source or MediaMarktSource()
    report = {'purpose': PURPOSE, 'writes': False, 'status': 'failed',
              'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
              'catalog_products': 0, 'unpriced_products': 0,
              'candidate_urls': 0, 'ambiguous_urls': [], 'offset': offset,
              'probe_budget': MAX_PROBES, 'probed': 0, 'source_verified': 0,
              'candidates': [], 'source': {}}
    try:
        rows = _catalog(catalog_get or requests.get)
        report['catalog_products'] = len(rows)
        unique, ambiguous, unpriced = _candidate_queue(rows, source)
        report['unpriced_products'] = unpriced
        report['candidate_urls'] = len(unique)
        report['ambiguous_urls'] = ambiguous
        if source.report().get('sitemap_status') != 'ready':
            raise ValueError('mediamarkt_sitemap_unavailable')
        browser_failures = 0
        for url, row, specs in unique[offset:offset + MAX_PROBES]:
            if browser_failures >= MAX_BROWSER_FAILURES:
                report['candidates'].append({
                    'source_url': url, 'catalog_id': row['id'],
                    'catalog_slug': row['slug'], 'reason': 'browser_failure_circuit'})
                continue
            evidence, attempt = _read_selected(source, url)
            report['probed'] += 1
            browser_failures += int(attempt.get('browser_failed') is True)
            review = _review(evidence, row, specs)
            offer = ({key: evidence.get(key) for key in (
                'source_title', 'sku', 'gtin13', 'ram_gb', 'storage_gb',
                'network_generation', 'seller_name', 'seller_id', 'offer_id',
                'platform', 'condition', 'availability', 'price', 'currency')}
                if evidence is not None else None)
            report['candidates'].append({
                'source_url': url, 'catalog_id': row['id'],
                'catalog_slug': row['slug'], 'catalog_name': row['name'],
                'catalog_specs': {key: specs.get(key) for key in
                                  ('ram_gb', 'storage_gb', 'network_generation')},
                'source_attempt': attempt, 'selected_offer': offer,
                'review_status': review,
            })
            report['source_verified'] += int(
                review == 'source_verified_pending_private_target_review')
        report['status'] = 'completed'
    except (requests.RequestException, RuntimeError, ValueError, TypeError,
            KeyError, AttributeError, OSError) as error:
        report['error'] = str(error) if isinstance(error, ValueError) else type(error).__name__
    report['source'] = source.report()
    return report


def summary(report):
    lines = [
        '## MediaMarkt marketplace unpriced candidate queue (read only)',
        f"Status: {report['status']}; catalog {report['catalog_products']}, "
        f"unpriced {report['unpriced_products']}; unique sitemap hints "
        f"{report['candidate_urls']}, ambiguous {len(report['ambiguous_urls'])}; "
        f"probed {report['probed']}/{report['probe_budget']} from offset "
        f"{report['offset']}; source verified {report['source_verified']}.",
    ]
    for row in report['candidates']:
        offer = row.get('selected_offer') or {}
        lines.append(
            f"- {row['catalog_slug']}: {row['reason'] if 'reason' in row else row['review_status']} "
            f"at {row['source_url']}; "
            f"{offer.get('seller_name') or '?'} {offer.get('price') or '?'} TRY, "
            f"SKU {offer.get('sku') or '?'}, GTIN {offer.get('gtin13') or '?'}, "
            f"RAM {offer.get('ram_gb') or '?'}, storage {offer.get('storage_gb') or '?'}, "
            f"radio {offer.get('network_generation') or '?'}."
        )
    if report.get('error'):
        lines.append('Error: ' + report['error'])
    lines.append('Source verified means private target/collision review remains; no price, score or catalog writes.')
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--offset', type=int, default=0)
    args = parser.parse_args(argv)
    report = run_audit(offset=args.offset)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / 'mediamarkt-marketplace-candidates.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    (OUTPUT_DIR / 'mediamarkt-marketplace-candidates.md').write_text(summary(report))
    print(summary(report), end='')
    return 0 if report['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
