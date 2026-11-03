"""Exercise automatic catalog -> sitemap -> PDP -> ingestion preparation, GET only."""
import argparse
import json
import re
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse
from offer_ingestion_client import OfferIngestionClient
from retailer_offer_refresh import RetailerOfferRefresher
from reviewed_offer_targets import with_reviewed_radio
from update_prices import PriceUpdater
from mediamarkt_source import _phone_hint_path, product_url
from vatan_sitemap_source import VatanSitemapSource
from offer_target_selection import select_offer_targets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--retailer', choices=('all', 'vatan'), default='all')
    parser.add_argument('--priced-only', action='store_true',
                        help='Inspect additional sellers for currently priced catalog models')
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--unitless-only', action='store_true',
                        help='Inspect Vatan phone URLs whose memory pair omits GB')
    args = parser.parse_args()
    if not 0 <= args.offset <= 1000:
        parser.error('offset must be between 0 and 1000')
    if args.unitless_only and args.retailer != 'vatan':
        parser.error('unitless-only requires retailer vatan')
    client = OfferIngestionClient.from_env()
    products = client.get_published_products()
    status = client.get_public_price_priority()
    reader = RetailerOfferRefresher()
    updater = PriceUpdater(ingestion_client=client, offer_refresher=reader)
    vatan = VatanSitemapSource()
    reader.vatan_reviewed_source.configure_catalog_budget(24)
    products.sort(key=lambda p: (status.get(p['id'], ('', False))[1], -p['id']))
    queue = []
    for product in products:
        if args.priced_only and not status.get(product['id'], ('', False))[1]:
            continue
        specs = with_reviewed_radio(product, updater.get_expected_specs(product.get('attributes')))
        targets = (reader.mediamarkt_source.discover(product['name'], specs)
                   if args.retailer == 'all' else [])
        # Some current compact PDPs are stored destinations but absent from
        # the advertised sitemap. Exercise those automatically selected hints too.
        compact_stored = [row for row in product.get('offer_targets', [])
                          if isinstance(row, dict) and product_url(row.get('source_url'))
                          and _phone_hint_path(urlparse(row['source_url']).path)
                          and args.retailer == 'all']
        vatan_targets = vatan.discover(product['name'], specs)
        if args.retailer == 'vatan':
            vatan_targets += [row for row in product.get('offer_targets', [])
                             if isinstance(row, dict)
                             and VatanSitemapSource._safe_product_url(row.get('source_url'))
                             and VatanSitemapSource._variant_matches(
                                 urlparse(row['source_url']).path, product['name'], specs)]
        targets = list({row['source_url']: row for row in targets + compact_stored + vatan_targets}.values())
        if args.unitless_only:
            targets = [row for row in targets if re.search(
                r'(?:^|-)\d{1,2}-\d{2,4}-akilli-telefon(?:-|\.html$)',
                urlparse(row['source_url']).path)]
        if not targets:
            continue
        compact = any(_phone_hint_path(urlparse(t['source_url']).path) for t in targets)
        queue.append((product, specs, targets, compact))
    # Exercise newly supported phone URL shapes before repeating known PDPs.
    # All candidates still traverse the ordinary live reader and writer guards.
    queue.sort(key=lambda item: (
        0 if not status.get(item[0]['id'], ('', False))[1] and item[3] else
        1 if item[0].get('offer_targets_truncated') is True else
        2 if not status.get(item[0]['id'], ('', False))[1] else 3,
        -item[0]['id']))
    rows = []
    for product, specs, targets, compact in queue[args.offset:args.offset + 12]:
        selected_targets = select_offer_targets(targets, maximum=2)
        observed = reader.refresh_existing_offer_targets(product['name'], selected_targets, specs)
        proof_report = {}
        updater.hydrate_marketplace_target_proof(product, observed, proof_report)
        accepted = [offer for offer in observed if updater.live_offer_is_valid(product, offer, specs)]
        now = datetime.now(timezone.utc).isoformat()
        prepared = updater.prepare_product_offers(product, accepted, now, now)
        rows.append({'slug': product['slug'], 'currently_priced': status.get(product['id'], ('', False))[1],
                     'compact_phone_path': compact,
                     **proof_report,
                     'candidate_count': len(targets), 'observed': len(observed),
                     'prepared_count': len(prepared), 'prepared': prepared,
                     'rejected_by_catalog_or_seller': len(observed) - len(accepted),
                     'attempts': reader.target_report(product['name'], selected_targets, specs)})
    report = {'writes': False, 'catalog_count': len(products), 'rows': rows,
              'retailer': args.retailer, 'priced_only': args.priced_only, 'offset': args.offset,
              'unitless_only': args.unitless_only,
              'source': reader.mediamarkt_source.report(),
              'vatan_sitemap': vatan.report(), 'vatan_pdp': reader.vatan_reviewed_source.report()}
    Path('automatic-marketplace-audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    summary = ['### Automatic retailer pipeline (GET only)',
               f'Catalog: {len(products)}; candidate products: {len(rows)}; '
               f'prepared offers: {sum(row["prepared_count"] for row in rows)}',
               f'Retailer: {args.retailer}; priced only: {args.priced_only}; '
               f'unitless only: {args.unitless_only}; offset: {args.offset}',
               '', '| Model | Candidates | Observed | Prepared |', '|---|---:|---:|---:|']
    summary += [f'| {r["slug"]} | {r["candidate_count"]} | {r["observed"]} | {r["prepared_count"]} |' for r in rows]
    for row in rows:
        proof = row.get('complete_target_proof', {})
        offers = '; '.join(f"{o['seller']} {o['price']} TRY" for o in row['prepared'])
        reasons = ', '.join(sorted({attempt['reason'] for attempt in row['attempts']}))
        summary.append(f"- {row['slug']}: proof {proof.get('status', 'not_needed')} "
                       f"{proof.get('reason', '')}; offers {offers or 'none'}; "
                       f"source reasons {reasons or 'none'}")
        for raw in row.get('raw_target_proofs', []):
            summary.append(f"  - Raw seller check: {raw.get('seller')}; {raw['status']}; "
                           f"rows {raw.get('raw_rows_checked', 0)}; "
                           f"rejected refresh hints {raw.get('rejected_refresh_hints', 0)}; "
                           f"{raw.get('reason', '')}")
    summary += ['', json.dumps(report['source']), json.dumps(report['vatan_sitemap']),
                json.dumps(report['vatan_pdp']), '', 'No ingestion or IndexNow calls.']
    Path('automatic-marketplace-audit.md').write_text('\n'.join(summary))
    print('\n'.join(summary))
    return 0 if rows else 1


if __name__ == '__main__':
    raise SystemExit(main())
