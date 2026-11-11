"""Publish the queued Galaxy S26 FE only with two live offers for its exact GTIN."""

import argparse
from datetime import datetime, timezone
import json
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from daily_discovery import CONTRACT, PublicReader, model_key, product_names, submit
from mediamarkt_source import MediaMarktSource, _selected_offer
from priority_retailer_transport import PriorityRetailerTransport, compatible_get
from product_images import R2ProductImageStore
from source_ingestion import SourceIngestionClient
from trendyol_catalog_source import parse_catalog_listing, selected_product_state
from tr_price_scraper import TRPriceScraper

NAME = 'Samsung Galaxy S26 FE'
SLUG = 'samsung-galaxy-s26-fe'
GTIN = '8806099409753'
SPECS = {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'}
MANUFACTURER = ('https://www.samsung.com/tr/smartphones/galaxy-s/'
                'galaxy-s26-fe-pistachio-256gb-sm-s741blggtur/')
SPEC_DATABASE = 'https://www.kimovil.com/en/where-to-buy-samsung-galaxy-s26-fe'
MEDIAMARKT = ('https://www.mediamarkt.com.tr/tr/product/'
              '_samsung-galaxy-s26-fe-5g-8256gb-akilli-telefon-yaban-mersini-1256671.html')
TRENDYOL = ('https://www.trendyol.com/samsung/'
            'galaxy-s26fe-8-256-gb-yaban-mersini-cep-telefonu-p-1197229480')
CANDIDATE = {'name': NAME, 'slug': SLUG, 'brandSlug': 'samsung', 'segment': 'new_release',
             'evidenceUrl': MANUFACTURER}


def sourced_specifications():
    # The selected 8/256 technical sheet supplies these core claims. Its
    # disputed brightness and Android version are omitted.
    observed = datetime.now(timezone.utc).isoformat()
    facts = {
        'ram_gb': 8,
        'storage_gb': 256,
        'battery_mah': 4900,
        'screen_size_inch': 6.7,
        'Technical sheet': {'Release date': '2026-09'},
        'Design & Materials': {'Screen': '6.7" Dynamic AMOLED 2X', 'Resolution': '1080 × 2340',
                               'Refresh rate': '120 Hz', 'Weight': '193 g', 'Resistance': 'IP68'},
        'Performance & Hardware': {'Processor': 'Samsung Exynos 2500', 'RAM': '8 GB', 'Storage': '256 GB'},
        'Camera': {'Main camera': '50 MP', 'Wide angle': '12 MP', 'Telephoto': '8 MP', 'Selfie': '12 MP'},
        'Connectivity': {'5G': True},
        'Batarya': {'Capacity': '4900 mAh', 'Fast charge': '45 W'},
        'Software': {'Operating system': 'Android'},
        'partials': {'design': 7.4, 'hardware': 8.5, 'camera': 8.5, 'connectivity': 8.8, 'battery': 7.0},
    }
    return [{'product_slug': SLUG, 'attribute_key': key,
             'value_origin': 'benchmark_source' if key == 'partials' else 'spec_database',
             'source_url': SPEC_DATABASE, 'observed_at': observed, 'is_estimate': False,
             'confidence': 'medium', 'value': value} for key, value in facts.items()]


def mediamarkt_offer():
    source = MediaMarktSource(get=compatible_get)
    source._load_robots()
    if source._rules is None or not source._rules.can_fetch('TeknoskorPriceBot', MEDIAMARKT):
        raise ValueError('mediamarkt_document_unavailable')
    body, reason, _ = source._direct(MEDIAMARKT, 5 * 1024 * 1024, 'text/html')
    if reason == 'http_403':
        body, resolved, reason = source._browser_get(MEDIAMARKT)
        if reason == 'ready' and resolved != MEDIAMARKT:
            raise ValueError('mediamarkt_selected_url_changed')
    if reason != 'ready' or not body:
        raise ValueError('mediamarkt_' + reason)

    def verify(product, title, brand):
        if (product.get('gtin13') != GTIN or brand.casefold() != 'samsung'
                or not TRPriceScraper().is_strict_match(NAME, title, SPECS)):
            return None
        images = product.get('image')
        images = images if isinstance(images, list) else []
        photo = next((image for image in images if isinstance(image, str)
                      and (url := urlsplit(image)).scheme == 'https' and url.netloc == 'assets.mmsrg.com'
                      and url.path.startswith('/isr/') and not url.query and not url.fragment), None)
        return {'gtin13': GTIN, 'photo': photo, 'company': 'media markt turkey ticaret limited sirketi'}

    offer = _selected_offer(body, MEDIAMARKT, verify_product=verify)
    if not offer or not offer['photo']:
        raise ValueError('mediamarkt_exact_gtin_or_photo_unverified')
    return offer


def trendyol_offer():
    transport = PriorityRetailerTransport(maximum_per_host=4, browser_fallback=False, get=compatible_get)
    body, reason, _ = transport.read_document(TRENDYOL)
    if not body or transport.resolved_url(TRENDYOL) != TRENDYOL:
        raise ValueError('trendyol_' + reason)
    offer, reason = parse_catalog_listing(body, TRENDYOL, NAME, SPECS)
    product, _ = selected_product_state(BeautifulSoup(body, 'html.parser'))
    seller = (product or {}).get('merchantListing', {}).get('merchant', {})
    company = TRPriceScraper._normalize_words(seller.get('officialName'))
    if (not offer or offer.get('gtin13') != GTIN or offer.get('merchant') != 'Trendyol'
            or not company or not str(seller.get('id')) == str(offer['merchant_id'])):
        raise ValueError('trendyol_selected_gtin_or_seller_unverified_' + reason)
    return {**offer, 'company': company}


def prepare():
    page = PublicReader().read(MANUFACTURER)
    if not any(model_key(name) == model_key(NAME) for name in product_names(page, MANUFACTURER)):
        raise ValueError('manufacturer_model_unverified')
    primary, second = mediamarkt_offer(), trendyol_offer()
    offers = [primary, second]
    if (len({row['company'].replace(' ', '') for row in offers}) != 2
            or any(row['gtin13'] != GTIN or row['availability'] != 'in_stock'
                   or row['currency'] != 'TRY' for row in offers)):
        raise ValueError('two_independent_in_stock_offers_required')
    return offers, primary['photo']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    rows, photo = prepare()
    print(json.dumps({'gtin': GTIN, 'offers': [{key: row[key] for key in ('merchant', 'price', 'url', 'observed_at')}
                      for row in rows], 'photo': photo}, ensure_ascii=False), flush=True)
    if not args.publish:
        return
    publish_verified(CANDIDATE, sourced_specifications(), rows, photo)


def publish_verified(candidate, sources, rows, photo):
    slug = candidate['slug']
    client = SourceIngestionClient.from_env()
    capability = client._json_body(client._request_with_retry('GET',
        f'{client.base_url}/api/ingestion/catalog-discovery', authenticated=True))
    if capability.get('contract') != CONTRACT:
        raise ValueError('production_catalog_contract_unavailable')
    if any(item.slug == slug for item in client.fetch_catalog()):
        raise ValueError('model_already_published')
    offers = [{'merchant_name': row['merchant'], 'price': row['price'], 'currency': 'TRY',
               'availability': 'in_stock', 'source_url': row['url'], 'source_title': row['source_title'],
               'observed_at': row['observed_at'], 'checked_at': row['observed_at']} for row in rows]
    image_url = f'https://cdn.teknoskor.com/products/{slug}/{slug}-1.webp'
    payload = {'mode': 'validate', 'candidate': candidate, 'sources': sources,
               'images': [image_url], 'offers': offers}
    validation = submit(client, payload)
    if validation.get('eligible') is not True:
        raise ValueError('catalog_validation_rejected')
    image = R2ProductImageStore(required=True).upload(photo, slug, 1)
    if image != image_url:
        raise ValueError('catalog_image_destination_changed')
    result = submit(client, {**payload, 'mode': 'publish'})
    if result.get('committed') is not True or result.get('slug') != slug:
        raise ValueError('catalog_publication_unconfirmed')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
