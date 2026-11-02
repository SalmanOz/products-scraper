"""Read-only check of one exact Z Fold7 MediaMarkt marketplace buy box.

The catalog spells Fold7 without a space; this reviewed PDP spells Fold 7.
Only the fixed SKU below may use that display-name alias. No price is saved.
"""

from datetime import datetime, timezone
import json
import math
import re

from bs4 import BeautifulSoup
import requests

from mediamarkt_marketplace_audit import catalog_variant_matches, selected_marketplace_offer
from mediamarkt_source import BOT, MAX_HTML_BYTES, MediaMarktSource


CATALOG = {'id': 282, 'slug': 'samsung-galaxy-z-fold7',
           'name': 'Samsung Galaxy Z Fold7', 'ram_gb': 12,
           'storage_gb': 256, 'network_generation': '5g'}
URL = ('https://www.mediamarkt.com.tr/tr/product/'
       '_samsung-galaxy-z-fold-7-12-gb-256-gb-akilli-telefon-gece-siyahi-167997743.html')
TITLE = 'SAMSUNG Galaxy Z Fold 7 5G 12 GB 256 GB Akıllı Telefon Gece Siyahı'
SELLER_URL = 'https://www.mediamarkt.com.tr/tr/marketplace/seller/12945'
SELLER = 'Altınkoza Teknolojim'
LEGAL_NAME = ('ALTINKOZA İLETİŞİM TEKNOLOJİ ELEKTRONİK OTOMOTİV '
              'SANAYİ VE TİCARET LİMİTED ŞİRKETİ')
TAX_NUMBER = '0630478463'
MERSIS_NUMBER = '00630478460001'


def exact_fold7_offer(evidence, catalog=CATALOG):
    """Fail closed on any SKU, merchant, radio or catalog identity drift."""
    if catalog != CATALOG or not isinstance(evidence, dict):
        return False
    price = evidence.get('price')
    return (evidence.get('url') == URL and evidence.get('source_title') == TITLE
            and evidence.get('platform') == 'MediaMarkt'
            and evidence.get('seller_name') == SELLER
            and evidence.get('seller_id') == '12945'
            and evidence.get('sku') == '167997743'
            and evidence.get('gtin13') == '8806097425007'
            and isinstance(evidence.get('offer_id'), str)
            and re.fullmatch(r'\d{1,15}', evidence['offer_id']) is not None
            and evidence.get('condition') == 'new'
            and evidence.get('availability') == 'in_stock'
            and evidence.get('currency') == 'TRY'
            and isinstance(price, (int, float)) and not isinstance(price, bool)
            and math.isfinite(price) and 0 < price <= 5_000_000
            and catalog_variant_matches(evidence, 'Samsung Galaxy Z Fold 7',
                {'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g'}))


def exact_seller_identity(html):
    text = ' '.join(BeautifulSoup(html, 'html.parser').get_text(' ', strip=True).split())
    return (f'Firma adı: {LEGAL_NAME}' in text
            and f'MERSİS No: {MERSIS_NUMBER}' in text
            and f'Vergi No: {TAX_NUMBER}' in text)


def run_audit(transport=None):
    """Robots preflight, then two ordinary public GETs; no browser or DB writes."""
    result = {'purpose': 'read_only_exact_fold7_marketplace_audit',
              'checked_at': datetime.now(timezone.utc).isoformat(),
              'catalog': CATALOG, 'url': URL, 'seller_url': SELLER_URL,
              'verified': False, 'reason': None, 'offer': None, 'writes': False}
    source = transport or MediaMarktSource()
    try:
        source._load_robots()
        if source._rules is None:
            result['reason'] = 'robots_unavailable'
            return result
        if not all(source._rules.can_fetch(BOT, url) for url in (URL, SELLER_URL)):
            result['reason'] = 'robots_disallowed'
            return result
        body, reason, status = source._direct(URL, MAX_HTML_BYTES, 'text/html')
        result['pdp_http_status'] = status
        if reason != 'ready':
            result['reason'] = 'pdp_' + reason
            return result
        evidence = selected_marketplace_offer(body, URL)
        if not exact_fold7_offer(evidence):
            result['reason'] = 'selected_offer_or_catalog_mismatch'
            return result
        seller_body, reason, status = source._direct(SELLER_URL, MAX_HTML_BYTES, 'text/html')
        result['seller_http_status'] = status
        if reason != 'ready' or not exact_seller_identity(seller_body):
            result['reason'] = 'seller_identity_unverified'
            return result
        result['verified'] = True
        result['reason'] = 'selected_new_cash_offer_and_legal_seller_verified'
        result['offer'] = evidence
        return result
    except (requests.RequestException, ValueError, TypeError, UnicodeError, OSError) as error:
        result['reason'] = type(error).__name__
        return result


if __name__ == '__main__':
    print(json.dumps(run_audit(), ensure_ascii=False, sort_keys=True))
