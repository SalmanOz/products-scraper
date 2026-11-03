"""Exercise automatic catalog -> sitemap -> PDP -> ingestion preparation, GET only."""
import json
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse
from offer_ingestion_client import OfferIngestionClient
from retailer_offer_refresh import RetailerOfferRefresher
from reviewed_offer_targets import with_reviewed_radio
from update_prices import PriceUpdater
from mediamarkt_source import _phone_hint_path


def main():
    client = OfferIngestionClient.from_env()
    products = client.get_published_products()
    status = client.get_public_price_priority()
    reader = RetailerOfferRefresher()
    updater = PriceUpdater(ingestion_client=client, offer_refresher=reader)
    products.sort(key=lambda p: (status.get(p['id'], ('', False))[1], -p['id']))
    queue = []
    for product in products:
        specs = with_reviewed_radio(product, updater.get_expected_specs(product.get('attributes')))
        targets = reader.mediamarkt_source.discover(product['name'], specs)
        if not targets:
            continue
        compact = any(_phone_hint_path(urlparse(t['source_url']).path) for t in targets)
        queue.append((product, specs, targets, compact))
    # Exercise newly supported phone URL shapes before repeating known PDPs.
    # All candidates still traverse the ordinary live reader and writer guards.
    queue.sort(key=lambda item: (status.get(item[0]['id'], ('', False))[1],
                                 not item[3], -item[0]['id']))
    rows = []
    for product, specs, targets, compact in queue[:12]:
        observed = reader.refresh_existing_offer_targets(product['name'], targets[:2], specs)
        accepted = [offer for offer in observed if updater.live_offer_is_valid(product, offer, specs)]
        now = datetime.now(timezone.utc).isoformat()
        prepared = updater.prepare_product_offers(product, accepted, now, now)
        rows.append({'slug': product['slug'], 'currently_priced': status.get(product['id'], ('', False))[1],
                     'compact_phone_path': compact,
                     'candidate_count': len(targets), 'observed': len(observed),
                     'prepared_count': len(prepared), 'prepared': prepared,
                     'rejected_by_catalog_or_seller': len(observed) - len(accepted),
                     'attempts': reader.target_report(product['name'], targets[:2], specs)})
    report = {'writes': False, 'catalog_count': len(products), 'rows': rows,
              'source': reader.mediamarkt_source.report()}
    Path('automatic-marketplace-audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    summary = ['### Automatic retailer pipeline (GET only)',
               f'Catalog: {len(products)}; candidate products: {len(rows)}; '
               f'prepared offers: {sum(row["prepared_count"] for row in rows)}',
               '', '| Model | Candidates | Observed | Prepared |', '|---|---:|---:|---:|']
    summary += [f'| {r["slug"]} | {r["candidate_count"]} | {r["observed"]} | {r["prepared_count"]} |' for r in rows]
    summary += ['', json.dumps(report['source']), '', 'No ingestion or IndexNow calls.']
    Path('automatic-marketplace-audit.md').write_text('\n'.join(summary))
    print('\n'.join(summary))
    return 0 if rows else 1


if __name__ == '__main__':
    raise SystemExit(main())
