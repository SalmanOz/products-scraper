"""Read-only check of one exact Galaxy A56 MediaMarkt marketplace buy box."""

from datetime import datetime, timezone
import json
import math

from bs4 import BeautifulSoup
import requests

from mediamarkt_marketplace_audit import catalog_variant_matches, selected_marketplace_offer
from mediamarkt_source import MAX_HTML_BYTES, MediaMarktSource


CATALOG = {'id': 242, 'slug': 'samsung-galaxy-a56',
           'name': 'Samsung Galaxy A56', 'ram_gb': 8,
           'storage_gb': 128, 'network_generation': '5g'}
URL = ('https://www.mediamarkt.com.tr/tr/product/'
       '_samsung-galaxy-a56-5g-8-gb-128-gb-akilli-telefon-antrasit-168636028.html')
TITLE = 'SAMSUNG Galaxy A56 5G 8 GB 128 GB Akıllı Telefon Antrasit'
SELLER_URL = 'https://www.mediamarkt.com.tr/tr/marketplace/seller/12099'
SELLER = 'Birtane Telekom'
LEGAL_NAME = 'BİRTANE TELEKOMÜNİKASYON SANAL MAĞAZACILIK LİMİTED ŞİRKETİ'
TAX_NUMBER = '1780105625'
MERSIS_NUMBER = '178010562500016'


def exact_a56_offer(evidence, catalog=CATALOG):
    """Fail closed on any SKU, merchant, radio or catalog identity drift."""
    if catalog != CATALOG or not isinstance(evidence, dict):
        return False
    price = evidence.get('price')
    return (evidence.get('url') == URL and evidence.get('source_title') == TITLE
            and evidence.get('platform') == 'MediaMarkt'
            and evidence.get('seller_name') == SELLER
            and evidence.get('seller_id') == '12099'
            and evidence.get('sku') == '168636028'
            and evidence.get('gtin13') == '8806097054603'
            and evidence.get('offer_id') == '1869831177'
            and evidence.get('condition') == 'new'
            and evidence.get('availability') == 'in_stock'
            and evidence.get('currency') == 'TRY'
            and isinstance(price, (int, float)) and not isinstance(price, bool)
            and math.isfinite(price) and price == 30000.0
            and catalog_variant_matches(evidence, CATALOG['name'],
                {'ram_gb': 8, 'storage_gb': 128, 'network_generation': '5g'}))


def exact_seller_identity(html):
    text = ' '.join(BeautifulSoup(html, 'html.parser').get_text(' ', strip=True).split())
    return (f'Firma adı: {LEGAL_NAME}' in text
            and f'MERSİS No: {MERSIS_NUMBER}' in text
            and f'Vergi No: {TAX_NUMBER}' in text)


def run_audit(transport=None):
    """Bounded public GETs; no DB writes."""
    result = {'purpose': 'read_only_exact_a56_marketplace_audit',
              'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
              'catalog': CATALOG, 'url': URL, 'seller_url': SELLER_URL,
              'verified': False, 'reason': None, 'offer': None, 'writes': False,
              'pdp_observed_at': None}
    source = transport or MediaMarktSource()
    try:
        body, reason, status = source._direct(URL, MAX_HTML_BYTES, 'text/html')
        result['pdp_direct_http_status'] = status
        if reason == 'http_403':
            # Use the same bounded browser GET that the reviewed MediaMarkt
            # reader uses. Only a 403 on this exact PDP may
            # trigger it; a changed final URL or challenge HTML still fails.
            body, final_url, reason = source._browser_get(URL)
            if reason == 'ready' and final_url != URL:
                reason = 'unexpected_browser_url'
            status = 200 if reason == 'ready' else 403
            result['pdp_read_mode'] = 'browser_after_403'
        else:
            result['pdp_read_mode'] = 'direct'
        result['pdp_http_status'] = status
        if reason != 'ready':
            result['reason'] = 'pdp_' + reason
            return result
        evidence = selected_marketplace_offer(body, URL)
        if not exact_a56_offer(evidence):
            result['reason'] = 'selected_offer_or_catalog_mismatch'
            return result
        result['pdp_observed_at'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        seller_body, reason, status = source._direct(SELLER_URL, MAX_HTML_BYTES, 'text/html')
        result['seller_http_status'] = status
        if reason != 'ready' or not exact_seller_identity(seller_body):
            result['reason'] = 'seller_identity_unverified'
            return result
        result['verified'] = True
        result['reason'] = 'selected_new_cash_offer_and_legal_seller_verified'
        result['offer'] = evidence
        result['checked_at'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        return result
    except (requests.RequestException, ValueError, TypeError, UnicodeError, OSError) as error:
        result['reason'] = type(error).__name__
        return result


if __name__ == '__main__':
    print(json.dumps(run_audit(), ensure_ascii=False, sort_keys=True))
