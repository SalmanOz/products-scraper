"""Review, then optionally publish the separately indexed S25 FE 8/256 SKU.

The default mode is read-only. --publish is intentionally a separate, scoped
writer: it stages a draft, current provenance and its own image before asking
the website's strict publication gate to promote the new product.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from tempfile import gettempdir

import requests

from catalog_expansion import expand_catalog, load_reviewed_batch
from main import KimovilScraper
from offer_ingestion_client import OfferIngestionClient
from product_images import R2ProductImageStore
from retailer_offer_refresh import RetailerOfferRefresher
from source_ingestion import SourceIngestionClient
from update_prices import PriceUpdater
from vatan_reviewed_source import VatanReviewedSource


BATCH_ID = 's25-fe-256-2026-11'
SLUG = 'samsung-galaxy-s25-fe-256gb'
NAME = 'Samsung Galaxy S25 FE 256GB'
SKU = 'SM-S731BDBGTUR'
SPECS = {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'}
SOURCE_NAME = 'Samsung Galaxy S25 FE'
SOURCE_URL = 'https://www.kimovil.com/en/where-to-buy-samsung-galaxy-s25-feglobal-8gb-256gb'
SAMSUNG_PDP = 'https://www.samsung.com/tr/smartphones/galaxy-s/galaxy-s25-fe-navy-256gb-sm-s731bdbgtur/buy/'
SAMSUNG_PDF = 'https://images.samsung.com/is/content/samsung/assets/tr/terms-and-conditions/9430_D2C_FF7-S25FE_Trade_In_LegalV1.pdf'
MEDIAMARKT_URLS = (
    'https://www.mediamarkt.com.tr/tr/product/_samsung-galaxy-s25fe-8256gb-lacivert-1249060.html',
    'https://www.mediamarkt.com.tr/tr/product/_samsung-galaxy-s25fe-8256gb-buz-mavisi-1249062.html',
)
VATAN_URL = 'https://www.vatanbilgisayar.com/samsung-galaxy-s25-fe-8-256-gb-akilli-telefon-buz-mavisi.html'
TARGETS = tuple({'source_url': url} for url in (MEDIAMARKT_URLS[0], VATAN_URL, MEDIAMARKT_URLS[1]))
MAX_PDF_BYTES = 8 * 1024 * 1024
OUT = Path(os.environ.get('S25_FE_PUBLISH_OUTPUT_DIR', gettempdir()))


class PublicationPreflightError(RuntimeError):
    def __init__(self, reason: str, *, details: dict | None = None):
        super().__init__(reason)
        self.details = details or {}


@dataclass
class Preflight:
    records: list[dict]
    offers: list[dict]
    source_attempts: list[dict]
    source_keys: list[str]


class NoImageStore:
    enabled = False


def official_sku_line(text: str) -> bool:
    """Require adjacent Samsung 8+256 and SKU cells, allowing PDF line breaks."""
    normalized = re.sub(r'\s+', ' ', str(text)).strip()
    return bool(re.search(
        r'\bGalaxy\s+S25\s*FE\s+8\s*\+\s*256\s+' + re.escape(SKU) + r'\b',
        normalized, re.I,
    ))


def fetch_official_sku_pdf(fetch=requests.get, reader_factory=None) -> dict:
    """Read one bounded first-party PDF; retain the URL, never its old promotion."""
    if reader_factory is None:
        from pypdf import PdfReader
        reader_factory = PdfReader
    response = fetch(SAMSUNG_PDF, timeout=(7, 30), stream=True,
                     allow_redirects=False,
                     headers={'User-Agent': 'TeknoSkorBot/1.0 (+https://teknoskor.com)',
                              'Accept': 'application/pdf'})
    try:
        if response.status_code != 200 or response.url != SAMSUNG_PDF:
            raise PublicationPreflightError('official_pdf_unavailable_or_redirected')
        length = response.headers.get('Content-Length', '')
        if length.isdigit() and int(length) > MAX_PDF_BYTES:
            raise PublicationPreflightError('official_pdf_oversized')
        content = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            content.extend(chunk)
            if len(content) > MAX_PDF_BYTES:
                raise PublicationPreflightError('official_pdf_oversized')
        if not content.startswith(b'%PDF-'):
            raise PublicationPreflightError('official_pdf_invalid_content')
        try:
            pdf = reader_factory(BytesIO(content))
            text = '\n'.join(page.extract_text() or '' for page in pdf.pages[:3])
        except Exception as error:
            raise PublicationPreflightError('official_pdf_unreadable') from error
        if not official_sku_line(text):
            raise PublicationPreflightError('official_pdf_selected_sku_memory_missing')
        return {'url': SAMSUNG_PDF, 'sku': SKU, 'ram_gb': 8, 'storage_gb': 256,
                'observed': True}
    finally:
        response.close()


def source_records(scraper=None, *, upload_images=False):
    records, images = [], []
    if scraper is None:
        scraper = KimovilScraper(
            require_images=upload_images,
            image_store=R2ProductImageStore(required=True) if upload_images else NoImageStore(),
        )
    ok = scraper.scrape_product_details(
        SOURCE_URL, product_slug=SLUG, expected_name=SOURCE_NAME,
        expected_memory={'ram_gb': 8, 'storage_gb': 256},
        seed_physical_attributes=True, existing_attributes={}, existing_images=[],
        record_sink=records, image_sink=images,
    )
    if not ok:
        raise PublicationPreflightError('exact_technical_variant_unavailable')
    by_key = {row.get('attribute_key'): row for row in records}
    required = {'ram_gb', 'storage_gb', 'performance_score', 'camera_score',
                'battery_score', 'screen_score', 'battery_mah', 'screen_size_inch',
                'Technical sheet'}
    if not required.issubset(by_key):
        raise PublicationPreflightError('technical_source_missing_required_fields')
    if by_key['ram_gb'].get('value') != 8 or by_key['storage_gb'].get('value') != 256:
        raise PublicationPreflightError('technical_source_memory_mismatch')
    sheet = by_key['Technical sheet'].get('value')
    if not isinstance(sheet, dict) or not sheet.get('Release date'):
        raise PublicationPreflightError('technical_source_release_date_missing')
    if any(row.get('product_slug') != SLUG for row in records):
        raise PublicationPreflightError('technical_source_product_mismatch')
    if upload_images:
        urls = [url for batch in images for url in batch.get('urls', [])]
        prefix = f'https://cdn.teknoskor.com/products/{SLUG}/'
        if not urls or any(not url.startswith(prefix) or not url.endswith('.webp') for url in urls):
            raise PublicationPreflightError('exact_variant_image_missing')
    return records, images


def add_official_capacity_sources(records: list[dict]) -> list[dict]:
    if {r.get('attribute_key') for r in records}.issuperset({'ram_gb', 'storage_gb'}) is False:
        raise PublicationPreflightError('technical_capacity_sources_missing')
    result = []
    for record in records:
        if record['attribute_key'] in {'ram_gb', 'storage_gb'}:
            value = 8 if record['attribute_key'] == 'ram_gb' else 256
            if record.get('value') != value:
                raise PublicationPreflightError('technical_capacity_changed')
            result.append({**record, 'value_origin': 'manufacturer', 'source_url': SAMSUNG_PDF,
                           'is_estimate': False, 'confidence': 'high'})
        else:
            result.append(record)
    return result


def exact_live_offers(reader=None, updater=None) -> tuple[list[dict], list[dict]]:
    reader = reader or RetailerOfferRefresher(
        vatan_source=VatanReviewedSource())
    updater = updater or PriceUpdater(ingestion_client=object(), offer_refresher=reader)
    offers = reader.refresh_existing_offer_targets(NAME, TARGETS, SPECS)
    allowed = set(MEDIAMARKT_URLS) | {VATAN_URL}
    valid = [offer for offer in offers if offer.get('url') in allowed
             and offer.get('availability') in {'in_stock', 'limited'}
             and updater.live_offer_is_valid({'name': NAME, 'slug': SLUG}, offer, SPECS)]
    attempts = reader.target_report(NAME, TARGETS, SPECS)
    merchants = {offer['merchant'] for offer in valid}
    if not {'MediaMarkt', 'Vatan Bilgisayar'}.issubset(merchants):
        def public_offer(offer):
            return {key: offer.get(key) for key in
                    ('merchant', 'url', 'price', 'currency', 'availability',
                     'source_title', 'observed_at')}
        raise PublicationPreflightError('two_exact_live_merchants_missing', details={
            'source_attempts': attempts,
            'raw_offers': [public_offer(offer) for offer in offers],
            'valid_offers': [public_offer(offer) for offer in valid],
            'raw_merchants': sorted({str(offer.get('merchant')) for offer in offers}),
            'valid_merchants': sorted(merchants),
        })
    return valid, attempts


def preflight(*, pdf_check=fetch_official_sku_pdf, scraper=None, reader=None, updater=None) -> Preflight:
    batch = load_reviewed_batch(BATCH_ID)
    if batch.get('mode') != 'draft' or len(batch['products']) != 1 or batch['products'][0] != {
        'name': NAME, 'slug': SLUG, 'brandSlug': 'samsung',
        'evidenceUrl': SAMSUNG_PDP, 'segment': 'popular_missing',
    }:
        raise PublicationPreflightError('reviewed_draft_manifest_mismatch')
    pdf_check()
    records, _ = source_records(scraper)
    offers, attempts = exact_live_offers(reader, updater)
    return Preflight(records, offers, attempts,
                     sorted({r['attribute_key'] for r in records}))


def publish_draft(client, product_id: int, images: list[str]) -> dict:
    payload = {'product_id': product_id, 'name': NAME, 'slug': SLUG,
               'evidence_url': SAMSUNG_PDP, 'ram_gb': 8, 'storage_gb': 256,
               'images': images}
    response = client.session.request(
        'POST', f'{client.base_url}/api/ingestion/catalog-expansion/publish',
        json=payload, headers={'Authorization': f'Bearer {client.secret}',
                               'Accept': 'application/json'}, timeout=(10, 120),
    )
    body = client._json_body(response)
    if body.get('committed') is not True or body.get('slug') != SLUG or body.get('product_id') != product_id:
        raise PublicationPreflightError(
            'publication_gate_rejected:' + str(body.get('error') or response.status_code)
            + ':' + ','.join(str(issue) for issue in body.get('issues', [])[:8]))
    return body


def publish(preflight_result: Preflight, *, source_client=None, offer_client=None,
            catalog_expander=expand_catalog, source_scraper=None, reader=None,
            updater=None) -> dict:
    source_client = source_client or SourceIngestionClient.from_env()
    offer_client = offer_client or OfferIngestionClient.from_env()
    catalog = catalog_expander(BATCH_ID)
    if len(catalog.get('products', [])) != 1:
        raise PublicationPreflightError('catalog_expansion_result_mismatch')
    product = catalog['products'][0]
    if product.get('slug') != SLUG or product.get('name') != NAME or not isinstance(product.get('id'), int):
        raise PublicationPreflightError('catalog_expansion_identity_mismatch')
    status = product.get('status')
    if status not in {'draft', 'published'}:
        raise PublicationPreflightError('catalog_expansion_status_mismatch')
    result = {'product_id': product['id'], 'draft_status': status,
              'source_committed': False, 'published': status == 'published'}
    if status == 'draft':
        records, image_batches = source_records(source_scraper, upload_images=True)
        records = add_official_capacity_sources(records)
        committed = source_client.submit_sources(records)
        if committed.get('committed') is not True or (
            committed.get('accepted', 0) + committed.get('stale_ignored', 0) != len(records)
        ):
            raise PublicationPreflightError('draft_provenance_not_committed')
        result['source_committed'] = True
        images = [url for batch in image_batches for url in batch['urls']]
        publication = publish_draft(source_client, product['id'], images)
        result['published'] = publication.get('committed') is True
        result['scores_recalculated_at_publish'] = publication.get('scores_recalculated')

    # Recheck the same observations against the server's offer payload policy.
    reader = reader or RetailerOfferRefresher(
        vatan_source=VatanReviewedSource())
    updater = updater or PriceUpdater(ingestion_client=offer_client, offer_refresher=reader)
    current_offers = [offer for offer in preflight_result.offers
                      if updater.live_offer_is_valid({'name': NAME, 'slug': SLUG}, offer, SPECS)]
    if not {'MediaMarkt', 'Vatan Bilgisayar'}.issubset({offer['merchant'] for offer in current_offers}):
        raise PublicationPreflightError('preflight_offers_expired_before_ingestion')
    timestamp = updater.utc_timestamp()
    payload = updater.prepare_product_offers(product, current_offers, timestamp, timestamp)
    if not {'MediaMarkt', 'Vatan Bilgisayar'}.issubset({row['seller'] for row in payload}):
        raise PublicationPreflightError('two_merchant_price_sanity_rejected')
    ingested = offer_client.ingest_offers(payload)
    if ingested.get('committed') is not True or ingested.get('variant_rejected') != 0:
        raise PublicationPreflightError('strict_offer_ingestion_rejected_variant')
    result['offers'] = {'accepted': ingested.get('accepted'),
                        'stale_ignored': ingested.get('stale_ignored'),
                        'merchants': sorted({row['seller'] for row in payload})}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--publish', action='store_true', help='Enable scoped production writes')
    args = parser.parse_args(argv)
    report = {'purpose': 'reviewed_s25_fe_8_256_draft_publication',
              'mode': 'publish' if args.publish else 'preflight',
              'slug': SLUG, 'technical_source': SOURCE_URL,
              'official_capacity_source': SAMSUNG_PDF,
              'writes': False}
    try:
        checked = preflight()
        report['technical_source_keys'] = checked.source_keys
        report['source_attempts'] = checked.source_attempts
        report['live_offers'] = [{key: offer.get(key) for key in
                                 ('merchant', 'price', 'currency', 'availability', 'url', 'source_title')}
                                for offer in checked.offers]
        if args.publish:
            # A later gate may fail after a draft or source transaction commits.
            # The audit must never claim that a failed writer made no changes.
            report['writes'] = True
            report['result'] = publish(checked)
        report['status'] = 'passed'
        code = 0
    except Exception as error:
        report['status'] = 'failed'
        report['reason'] = str(error) if isinstance(error, PublicationPreflightError) else type(error).__name__
        if isinstance(error, PublicationPreflightError):
            report.update(error.details)
        code = 1
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 's25-fe-256-publication.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
    lines = ['## S25 FE 8/256 reviewed catalog publication',
             f"Mode: {report['mode']}; status: {report['status']}; writes: {report['writes']}."]
    for offer in report.get('live_offers', []):
        lines.append(f"- {offer['merchant']}: {offer['price']} TRY, {offer['availability']}; {offer['url']}")
    if report.get('reason') == 'two_exact_live_merchants_missing':
        lines.append(f"- Raw merchants: {', '.join(report.get('raw_merchants', [])) or 'none'}; "
                     f"valid merchants: {', '.join(report.get('valid_merchants', [])) or 'none'}.")
        for attempt in report.get('source_attempts', []):
            lines.append(f"- {attempt.get('merchant')}: {attempt.get('reason')} "
                         f"HTTP {attempt.get('http_status')}; {attempt.get('source_url')}")
        for offer in report.get('raw_offers', []):
            lines.append(f"- Raw {offer.get('merchant')}: {offer.get('price')} "
                         f"{offer.get('currency')} {offer.get('availability')}; {offer.get('url')}")
    if report.get('reason'):
        lines.append(f"- Reason: {report['reason']}")
    (OUT / 's25-fe-256-publication.md').write_text('\n'.join(lines) + '\n')
    print('\n'.join(lines))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
