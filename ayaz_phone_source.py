"""Verify Ayaz İletişim's selected phone and its own public offer."""

from datetime import datetime, timezone
import re

from bs4 import BeautifulSoup

from retailer_barcode import retailer_gtin13
from teknosa_reviewed_source import _amount, _cash_price
from tr_price_scraper import TRPriceScraper


AYAZ_HOST = 'www.ayazgrupiletisim.com.tr'
AYAZ_BASE = 'https://' + AYAZ_HOST
AYAZ_TERMS = AYAZ_BASE + '/sayfa/mesafeli-satis-sozlesmesi'
AYAZ_MERCHANT = 'Ayaz İletişim'
AYAZ_COMPANY = ('ayaz iletisim petrol urunleri insaat turizm elektronik gida tekstil '
                'ithalat ihracat sanayi ticaret limited sirketi')


def ayaz_phone_url(url):
    return isinstance(url, str) and bool(re.fullmatch(
        re.escape(AYAZ_BASE) + r'/urun/[a-z0-9-]{6,250}', url))


def only(root, selector):
    matches = root.select(selector)
    return matches[0] if len(matches) == 1 else None


def value(root, selector):
    node = only(root, selector)
    return (node.get('content') or node.get('href') or ' '.join(node.stripped_strings)) if node else ''


def ayaz_offer(html, legal_html, *, url, name, specs):
    if not ayaz_phone_url(url):
        return None
    legal = BeautifulSoup(legal_html, 'html.parser')
    companies = [TRPriceScraper._normalize_words(node.get_text(' ', strip=True))
                 for node in legal.select('p') if re.match(r'^Ünvanı\s*:', node.get_text(' ', strip=True), re.I)]
    if companies != ['unvani ' + AYAZ_COMPANY]:
        return None
    soup = BeautifulSoup(html, 'html.parser')
    root = only(soup, '#product-detail-container')
    product = only(soup, '[itemscope][itemtype="https://schema.org/Product"]')
    canonical = value(soup, 'link[rel=canonical]')
    if not root or not product or canonical != url:
        return None
    offer = only(product, '[itemprop=offers][itemscope][itemtype="https://schema.org/Offer"]')
    if not offer or value(offer, ':scope > [itemprop=url]') != url:
        return None
    title = ' '.join(value(root, 'h1').split())
    if (title != ' '.join(value(product, ':scope > [itemprop=name]').split())
            or not TRPriceScraper().has_observed_offer_evidence(name, {'source_title': title, 'url': url}, specs)):
        return None
    barcode = retailer_gtin13(value(product, ':scope > [itemprop=gtin13]'))
    visible_barcodes = [value(row, ':scope > .product-list-content') for row in root.select('.product-list-row')
                        if value(row, ':scope > .product-list-title') == 'Barkod Kodu']
    cart = only(root, '[data-context=detail][data-selector=add-to-cart][data-product-id]')
    sku = value(product, ':scope > [itemprop=sku]')
    selected = re.findall(r'\bpageParams\s*=\s*\{product:\s*\{id:\s*"([1-9]\d{0,11})",'
                          r'\s*sku:\s*"([A-Za-z0-9-]{1,40})",', html)
    price = _amount(value(offer, ':scope > [itemprop=price]'))
    price_box = only(root, '.product-price-wrapper')
    visible = value(price_box, '.product-price-new') if price_box else ''
    if (not barcode or visible_barcodes != [barcode] or not cart or selected != [(cart['data-product-id'], sku)]
            or cart.has_attr('disabled') or cart.get('aria-disabled') == 'true' or 'disabled' in cart.get('class', [])
            or not cart.select('span') or set(cart.stripped_strings) != {'Sepete Ekle'}
            or value(offer, ':scope > [itemprop=availability]') != 'https://schema.org/InStock'
            or value(product, '[itemprop=itemCondition]') not in
                ('http://schema.org/NewCondition', 'https://schema.org/NewCondition')
            or value(offer, ':scope > [itemprop=priceCurrency]') != 'TRY'
            or price is None or not visible.startswith('₺') or _cash_price(visible[1:].strip() + ' TL') != price
            or re.search(r'\b(?:sepette|kuponla|uyelere ozel|sepete ozel)\b',
                         TRPriceScraper._normalize_words(price_box.get_text(' ', strip=True)))):
        return None
    return {'platform': AYAZ_MERCHANT, 'merchant': AYAZ_MERCHANT, '_legal_name': AYAZ_COMPANY,
            'price': float(price), 'currency': 'TRY', 'availability': 'in_stock', 'gtin13': barcode,
            'source_title': title, 'url': url, 'observed_at': datetime.now(timezone.utc).isoformat()}
