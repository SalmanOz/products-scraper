"""Verify selected Özgür İletişim products on the public PDP and legal page."""
from datetime import datetime, timezone
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from retailer_barcode import retailer_gtin13
from retailer_offer_refresh import _is_conditional_price, _schema_nodes
from teknosa_reviewed_source import _amount, _cash_price
from tr_price_scraper import TRPriceScraper
from tws_retailer_sources import selected_photo


OZGUR_HOST = 'ozguriletisimankara.com'
OZGUR_BASE = 'https://' + OZGUR_HOST
OZGUR_TERMS = OZGUR_BASE + '/mesafeli-satis'


def ozgur_phone_url(url):
    return isinstance(url, str) and bool(re.fullmatch(
        re.escape(OZGUR_BASE) + r'/urun/[a-z0-9-]{6,250}', url))


def _selected_offer(html, legal_html, *, url, matches_model, mpn=None, gtin=None):
    if not ozgur_phone_url(url):
        raise ValueError('ozgur_invalid_phone_url')
    soup = BeautifulSoup(html, 'html.parser')
    canonical = soup.select_one('link[rel="canonical"]')
    roots = soup.select('.oi-product-detail[data-product-id][data-oi-view-variant]')
    products = [item for item in _schema_nodes(soup, url) if item.get('@type') == 'Product']
    if not canonical or canonical.get('href') != url or len(products) != 1 or len(roots) != 1:
        raise ValueError('ozgur_selected_page_unverified')
    product, root = products[0], roots[0]
    identifiers = (root['data-product-id'], root['data-oi-view-variant'])
    if not all(re.fullmatch(r'[1-9]\d{0,19}', value) for value in identifiers):
        raise ValueError('ozgur_invalid_selected_ids')
    barcodes = {retailer_gtin13(product[key]) for key in ('gtin', 'gtin12', 'gtin13') if key in product}
    if len(barcodes) != 1 or None in barcodes:
        raise ValueError('ozgur_selected_barcode_unverified')
    observed_gtin = next(iter(barcodes))
    observed_mpn = product.get('mpn')
    observed_brand = product.get('brand')
    observed_brand = observed_brand.get('name') if isinstance(observed_brand, dict) else None
    matcher = TRPriceScraper()
    offer = product.get('offers')
    if not isinstance(offer, dict) or offer.get('@type') != 'Offer':
        raise ValueError('ozgur_selected_offer_missing')
    box = root.select_one('.oi-pd-buybox')
    if box is None:
        raise ValueError('ozgur_buybox_missing')
    heading = box.select_one('h1.oi-pd-title')
    title = heading.get_text(' ', strip=True) if heading else ''
    model = box.select_one('.oi-pd-model span')
    stock = box.select_one('.oi-pd-stockline.is-in')
    visible = box.select_one('[data-oi-price]')
    cart = box.select_one('[data-oi-add-cart]')
    photo = root.select_one('img#oi-product-main-img[src]')
    photo_url = selected_photo(urljoin(OZGUR_BASE + '/', photo['src']), {OZGUR_HOST}) if photo else None
    legal = BeautifulSoup(legal_html, 'html.parser').get_text(' ', strip=True)
    company = re.search(r'Satıcı:\s*(.+?)\s+Vergi dairesi:', legal)
    legal_name = matcher._normalize_words(company[1]) if company else ''
    price = _amount(offer.get('price'))
    if (not legal_name or not title or product.get('name') != title or product.get('url') != url
            or not matches_model(title, observed_brand)
            or not isinstance(observed_mpn, str) or not re.fullmatch(r'[A-Za-z0-9./-]{4,80}', observed_mpn)
            or not model or model.get_text(strip=True) != observed_mpn
            or mpn is not None and observed_mpn != mpn or gtin is not None and observed_gtin != gtin
            or not photo_url
            or photo_url not in product.get('image', []) or offer.get('url') != url
            or (offer.get('seller') or {}).get('name') != 'Özgür İletişim'
            or offer.get('availability') != 'https://schema.org/InStock'
            or offer.get('itemCondition') != 'https://schema.org/NewCondition' or offer.get('priceCurrency') != 'TRY'
            or not stock or stock.get_text(' ', strip=True) != 'Stokta' or not cart
            or cart.get('data-id') != root['data-product-id'] or cart.get_text(' ', strip=True) != 'Sepete Ekle'
            or cart.has_attr('disabled') or cart.get('aria-disabled') == 'true'
            or price is None or not visible or _cash_price(visible.get_text(' ', strip=True)) != price
            or _is_conditional_price(box, offer, float(price))):
        raise ValueError('ozgur_exact_sku_cash_offer_unverified')
    merchant = 'Özgür İletişim'
    result = {'merchant': merchant, 'platform': merchant, '_legal_name': legal_name,
              'price': float(price), 'currency': 'TRY', 'availability': 'in_stock', 'gtin13': observed_gtin,
              'source_title': title, 'url': url, 'observed_at': datetime.now(timezone.utc).isoformat()}
    return result, photo_url, product, identifiers


def ozgur_tws_offer(html, legal_html, *, url, name, model_name):
    offer, photo, _, _ = _selected_offer(html, legal_html, url=url,
        matches_model=lambda title, observed_brand: model_name(title, observed_brand) == name)
    return {**offer, 'model': name, 'condition': 'new', '_image': photo}


def ozgur_offer(html, legal_html, *, url, name, specs, mpn=None, gtin=None, brand=None):
    matcher = TRPriceScraper()
    expected_brand = matcher._normalize_words(brand or name.split()[0])

    def matches_model(title, observed_brand):
        return (matcher._normalize_words(observed_brand) == expected_brand
                and matcher.is_strict_match(name, title, specs))

    result, photo_url, product, identifiers = _selected_offer(
        html, legal_html, url=url, matches_model=matches_model, mpn=mpn, gtin=gtin)
    merchant = result['merchant']
    if mpn is None or gtin is None:
        return result, photo_url
    if not isinstance(product.get('sku'), str) or not re.fullmatch(r'[A-Z0-9-]{4,60}', product['sku']):
        raise ValueError('ozgur_reviewed_sku_missing')
    result['source_variant_evidence'] = {
        'version': 'selected-retailer-sku-v1', 'platform': merchant, 'merchant_id': 'ozgur-iletisim',
        'merchant_name': merchant, 'product_id': identifiers[0], 'listing_id': identifiers[1],
        'sku': product['sku'], 'gtin13': gtin, **specs,
    }
    return result, photo_url
