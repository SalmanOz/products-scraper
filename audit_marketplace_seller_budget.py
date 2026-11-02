"""Read-only audit of named marketplace sellers hidden by the daily URL budget.

The scheduled writer remains unchanged. This report compares its current
12-target selection with all authenticated, safe catalog targets. Stored URLs
are scheduling hints only; this tool never fetches a retailer PDP or posts an
offer.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from offer_ingestion_client import IngestionClientError, OfferIngestionClient
from retailer_offer_refresh import ALLOWED_HOSTS, _safe_url
from reviewed_offer_targets import daily_offer_targets, with_reviewed_radio
from update_prices import PriceUpdater


PURPOSE = 'read_only_marketplace_seller_target_budget_audit'


def _complete_targets(client, product):
    """Read a stable full target page, or report why completeness is unknown."""
    initial = product.get('offer_targets')
    if not isinstance(initial, list):
        return None, 'catalog_targets_unavailable'
    if product.get('offer_targets_truncated') is False:
        return initial, 'catalog_page'
    try:
        complete = client.get_complete_offer_targets(product['id'])
        mode = 'complete_pages'
    except IngestionClientError:
        # Historical rejected rows prevent the strict complete reader from
        # returning a page. The inspection reader can still prove completeness
        # when all safe targets fit one page.
        try:
            complete = client.get_offer_target_inspection(product['id'])
            mode = 'single_page_inspection'
        except IngestionClientError:
            return None, 'target_pages_unavailable'
    except Exception:
        return None, 'target_pages_unavailable'
    targets = complete.get('offer_targets') if isinstance(complete, dict) else None
    if (not isinstance(targets, list)
            or complete.get('id') != product.get('id')
            or complete.get('slug') != product.get('slug')
            or complete.get('name') != product.get('name')
            or type(complete.get('offer_targets_total')) is not int
            or complete['offer_targets_total'] != len(targets)
            or targets[:len(initial)] != initial):
        return None, 'target_snapshot_changed'
    return targets, mode


def _named_groups(targets):
    """Use only safe URLs with an explicit, distinct marketplace seller."""
    groups = {}
    for target in targets:
        if not isinstance(target, dict):
            continue
        url = target.get('source_url')
        parsed = _safe_url(url)
        seller = target.get('seller_name')
        if (not parsed or not isinstance(seller, str)
                or not 0 < len(seller.strip()) <= 100):
            continue
        merchant = ALLOWED_HOSTS[parsed.hostname]
        seller = seller.strip()
        if seller.casefold() == merchant.casefold():
            continue
        groups.setdefault((merchant, seller.casefold()),
                          {'merchant': merchant, 'seller': seller, 'source_url': url})
    return groups


def audit(client):
    """Compare the writer's unchanged selection with complete seller hints."""
    products = client.get_published_products()
    if not isinstance(products, list):
        raise ValueError('catalog_unavailable')
    report = {
        'purpose': PURPOSE, 'writes': False,
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'products_scanned': len(products), 'complete_products': 0,
        'products_with_omitted_sellers': 0, 'omitted_seller_groups': 0,
        'incomplete_products': [], 'rows': [],
    }
    for product in products:
        if not isinstance(product, dict):
            raise ValueError('catalog_product_invalid')
        specs = with_reviewed_radio(
            product, PriceUpdater.get_expected_specs(product.get('attributes')))
        selected = daily_offer_targets(product, specs)
        selected_urls = {row['source_url'] for row in selected}
        targets, mode = _complete_targets(client, product)
        if targets is None:
            report['incomplete_products'].append({
                'slug': product.get('slug'), 'reason': mode,
            })
            continue
        report['complete_products'] += 1
        all_groups = _named_groups(targets)
        selected_groups = _named_groups(
            [row for row in targets if isinstance(row, dict)
             and row.get('source_url') in selected_urls])
        omitted = [group for key, group in all_groups.items()
                   if key not in selected_groups]
        if omitted:
            report['products_with_omitted_sellers'] += 1
            report['omitted_seller_groups'] += len(omitted)
            report['rows'].append({
                'slug': product['slug'], 'target_mode': mode,
                'selected_urls': len(selected),
                'catalog_target_rows': len(targets),
                'omitted_sellers': omitted,
            })
    report['status'] = ('complete' if not report['incomplete_products']
                        else 'partial')
    return report


def summary(report):
    lines = [
        '## Marketplace seller target budget audit',
        'Authenticated catalog GET only; no retailer PDP fetch or price write.',
        (f"Status: {report['status']}; scanned {report['products_scanned']} products; "
         f"complete target view for {report['complete_products']}; "
         f"omitted named seller groups {report['omitted_seller_groups']} "
         f"on {report['products_with_omitted_sellers']} products; "
         f"incomplete target views {len(report['incomplete_products'])}."),
    ]
    for row in report['rows'][:20]:
        sellers = ', '.join(
            f"{offer['merchant']} / {offer['seller']}"
            for offer in row['omitted_sellers'])
        lines.append(
            f"- {row['slug']}: {sellers}; daily selection "
            f"{row['selected_urls']} URLs, catalog {row['catalog_target_rows']} target rows.")
    if len(report['rows']) > 20:
        lines.append(f"- {len(report['rows']) - 20} more affected products in artifact.")
    if report['incomplete_products']:
        for row in report['incomplete_products'][:10]:
            lines.append(f"- Incomplete {row['slug']}: {row['reason']}; "
                         'no omitted seller is inferred.')
        if len(report['incomplete_products']) > 10:
            lines.append(
                f"- {len(report['incomplete_products']) - 10} more incomplete "
                'products in the JSON artifact.')
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--summary', type=Path, required=True)
    args = parser.parse_args(argv)
    report = audit(OfferIngestionClient.from_env())
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    rendered = summary(report)
    args.summary.write_text(rendered)
    print(rendered, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
