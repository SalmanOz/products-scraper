"""Read-only same-run proof of two selected first-party Fold8 Ultra offers."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import gettempdir

import requests

from audit_turk_telekom_coverage import _catalog
from fold8_ultra_preflight import PROFILE, URLS
from retailer_offer_refresh import RetailerOfferRefresher
from reviewed_offer_targets import with_reviewed_radio
from tr_price_scraper import TRPriceScraper
from update_prices import PriceUpdater


OUT = Path(os.environ.get('FOLD8_ULTRA_AUDIT_OUTPUT_DIR', gettempdir()))


def run_audit(reader=None, catalog_get=None):
    reader = reader or RetailerOfferRefresher()
    get = catalog_get or requests.get
    product_id, slug, name, ram, storage, radio = PROFILE
    expected = {'ram_gb': ram, 'storage_gb': storage, 'network_generation': radio}
    report = {'purpose': 'read_only_fold8_ultra_two_first_party_runner_proof',
              'checked_at': None, 'status': 'failed', 'writes': False,
              'catalog_reason': 'not_checked', 'category_reason': 'not_checked',
              'product': {'id': product_id, 'slug': slug,
              'name': name, 'expected_specs': expected}, 'source_attempts': [],
              'verified_offers': [], 'sources': {}}
    try:
        catalog = _catalog(get)
        selected = [row for row in catalog if isinstance(row, dict) and row.get('id') == product_id]
        if len(selected) != 1:
            raise ValueError('catalog_identity_missing')
        product = selected[0]
        specs = with_reviewed_radio(product, PriceUpdater.get_expected_specs(product.get('attributes')))
        if (product.get('slug') != slug or product.get('name') != name
                or specs != expected):
            raise ValueError('catalog_identity_or_variant_changed')
        report['catalog_reason'] = 'catalog_exact'
        tt_url = next(url for url, seller in URLS.items() if seller == 'Türk Telekom eMağaza')
        discovered = reader.turk_telekom_source.discover(name, expected)
        if [row.get('source_url') for row in discovered] != [tt_url]:
            raise ValueError('turk_telekom_stock_card_not_exact')
        report['category_reason'] = 'exact_stock_card'
        targets = [{'source_url': url} for url in URLS]
        offers = reader.refresh_existing_offer_targets(name, targets, expected)
        attempts = reader.target_report(name, targets, expected)
        report['source_attempts'] = attempts
        matcher = TRPriceScraper()
        by_url = {offer.get('url'): offer for offer in offers if isinstance(offer, dict)}
        for url, merchant in URLS.items():
            offer = by_url.get(url)
            if (offer and offer.get('merchant') == merchant and offer.get('currency') == 'TRY'
                    and offer.get('availability') == 'in_stock'
                    and matcher.is_strict_match(name, offer.get('source_title', ''), expected)
                    and matcher.has_observed_offer_evidence(name, offer, expected)):
                report['verified_offers'].append({key: offer.get(key) for key in
                    ('merchant', 'price', 'currency', 'availability', 'source_title',
                     'url', 'observed_at')})
        tt_status = reader.turk_telekom_source.report()
        pasaj_status = reader.pasaj_source.report()
        report['sources'] = {'turk_telekom': tt_status, 'pasaj': pasaj_status}
        if (len(report['verified_offers']) == 2 and len(attempts) == 2
                and all(row.get('reason') == 'observed_exact_cash_offer'
                        and row.get('http_status') == 200 for row in attempts)
                and tt_status.get('status') == 'ready'):
            report['status'] = 'passed'
        else:
            report['status'] = 'no_two_exact_cash_offers'
    except (requests.RequestException, ValueError, TypeError, KeyError, UnicodeError) as error:
        report['error'] = str(error) if isinstance(error, ValueError) else type(error).__name__
    report['checked_at'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    return report


def summary(report):
    lines = ['## Fold8 Ultra 12/256 two first-party offer audit (read only)',
             f"Status: {report['status']}; catalog {report['catalog_reason']}; "
             f"stock card {report['category_reason']}; "
             f"selected offers {len(report['verified_offers'])}/2."]
    for row in report['source_attempts']:
        lines.append(f"- {row['merchant']}: {row['reason']} HTTP {row['http_status']} · {row['source_url']}")
    for offer in report['verified_offers']:
        lines.append(f"  {offer['merchant']}: {offer['price']:,.2f} TRY, "
                     f"{offer['availability']}, observed {offer['observed_at']}.")
    if report.get('error'):
        lines.append('Error: ' + report['error'])
    lines.append('No catalog, offer, score, image, or IndexNow writes.')
    return '\n'.join(lines) + '\n'


def main():
    report = run_audit()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'fold8-ultra-two-merchants.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    text = summary(report)
    (OUT / 'fold8-ultra-two-merchants.md').write_text(text)
    print(text, end='')
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
