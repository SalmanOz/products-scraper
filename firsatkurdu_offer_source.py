"""Read the selected FırsatKurdu offer from its public product page."""

from datetime import datetime, timezone
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ayaz_phone_source import only, value
from evofone_offer_source import displayed
from retailer_barcode import retailer_gtin13
from teknosa_reviewed_source import _amount, _cash_price
from tr_price_scraper import TRPriceScraper


HOST = 'www.firsatkurdu.com'
BASE = 'https://' + HOST
TERMS = BASE + '/sayfa/mesafeli-satis-sozlesmesi'
MERCHANT = 'FırsatKurdu'
COMPANY = 'matris teknoloji urunleri ve elektronik ltd sti'
PATH = re.compile(r'/urun/([a-z0-9-]{6,225}-[1-9]\d{2,8})')
CATEGORIES = {'phone': '/kategori/akilli-telefonlar-ve-tabletler', 'tws': '/kategori/bluetooth-kulakliklar'}
NORMALIZE = TRPriceScraper._normalize_words


def product_url(url):
    return (isinstance(url, str) and url.startswith(BASE) and PATH.fullmatch(url.removeprefix(BASE)) is not None
            and not re.search(r'\b(?:yenilenmis|ikinci el|teshir|outlet|yedek|kilif)\b', NORMALIZE(url)))


def seller_identity(html):
    soup = BeautifulSoup(html, 'html.parser')
    names = [NORMALIZE(node.get_text(' ', strip=True)) for node in soup.select('p')
             if re.match(r'^Ünvanı\s*:', node.get_text(' ', strip=True), re.I)]
    return COMPANY if value(soup, 'link[rel=canonical]') == TERMS and names == ['unvani ' + COMPANY] else None


def selected_offer(html, url, company, *, kind):
    if company != COMPANY or not product_url(url):
        return None, 'firsatkurdu_url_or_seller_unverified'
    soup = BeautifulSoup(html, 'html.parser')
    root = only(soup, '#product-detail-container')
    product = only(soup, '[itemscope][itemtype="https://schema.org/Product"]')
    if not root or not product or value(soup, 'link[rel=canonical]') != url:
        return None, 'firsatkurdu_selected_page_unverified'
    offer = only(product, '[itemprop=offers][itemscope][itemtype="https://schema.org/Offer"]')
    title = ' '.join(value(root, 'h1').split())
    brand = value(product, ':scope > [itemprop=brand] > [itemprop=name]')
    barcode = retailer_gtin13(value(product, ':scope > [itemprop=gtin13]'))
    category = NORMALIZE(value(product, ':scope > [itemprop=category]'))
    sku = value(product, ':scope > [itemprop=sku]')
    cart = only(root, '[data-context=detail][data-selector=add-to-cart][data-product-id]')
    selected = re.findall(r'\bpageParams\s*=\s*\{product:\s*\{id:\s*"([1-9]\d{0,11})",\s*sku:\s*"([A-Za-z0-9-]{1,40})",', html)
    if (not offer or value(offer, ':scope > [itemprop=url]') != url or not title or not brand or not barcode
            or ' '.join(value(product, ':scope > [itemprop=name]').split()) != title
            or not category or (category == 'kablosuz kulakliklar') != (kind == 'tws')):
        return None, 'firsatkurdu_selected_sku_unverified'
    if (not displayed(cart) or 'disabled' in cart.get('class', []) or selected != [(cart['data-product-id'], sku)]
            or cart.get_text(' ', strip=True) != 'Sepete Ekle'
            or root.select('[data-selector=stock-warning]')
            or value(offer, ':scope > [itemprop=availability]') != 'https://schema.org/InStock'
            or value(product, '[itemprop=itemCondition]') not in ('http://schema.org/NewCondition', 'https://schema.org/NewCondition')):
        return None, 'firsatkurdu_selected_stock_unverified'
    price = _amount(value(offer, ':scope > [itemprop=price]'))
    price_box = only(root, '.product-price')
    visible = (value(price_box, '.product-price-new') or value(price_box, '.product-price-old')) if price_box else ''
    if (price is None or value(offer, ':scope > [itemprop=priceCurrency]') != 'TRY' or _cash_price(visible) != price
            or value(price_box, '.product-price-title') != 'KDV Dahil'
            or re.search(r'\b(?:sepette|kuponla|uyelere ozel|sepete ozel)\b', NORMALIZE(price_box.get_text(' ', strip=True)))):
        return None, 'firsatkurdu_selected_cash_unverified'
    image = urlsplit(value(soup, 'meta[property="og:image"]'))
    photo = (image._replace(query='').geturl() if image.scheme == 'https' and image.netloc == HOST and not image.fragment
             and re.fullmatch(r'/idea/jv/99/myassets/products/[0-9]+/[a-z0-9_.-]+\.(?:png|jpe?g|webp)', image.path, re.I) else None)
    facts = {}
    if kind == 'phone':
        ram, storage, ambiguous = TRPriceScraper._unambiguous_listing_capacities(title)
        radio = TRPriceScraper._radio_generation(title)
        declared_radio = []
        for row in root.select('.product-detail-tab-content .product-detail tr'):
            cells = row.select(':scope > td')
            if len(cells) == 2 and NORMALIZE(cells[0].get_text(' ', strip=True)) == 'mobil erisim teknolojisi':
                declared_radio.append(cells[1].get_text(' ', strip=True).removeprefix(':').strip().casefold())
        if declared_radio:
            if len(declared_radio) != 1 or declared_radio[0] not in ('4g', '5g') or radio not in (None, declared_radio[0]):
                return None, 'firsatkurdu_selected_radio_conflict'
            radio = declared_radio[0]
        if ram and storage and radio and not ambiguous:
            facts = {'selected_firsatkurdu_variant_verified': True, 'ram_gb': ram,
                     'storage_gb': storage, 'network_generation': radio,
                     'listing_id': url.rsplit('-', 1)[-1]}
    return {**facts, 'platform': MERCHANT, 'merchant': MERCHANT, 'merchant_id': HOST, '_legal_name': company,
            'gtin13': barcode, 'product_id': cart['data-product-id'], 'sku': sku, 'source_title': title,
            'url': url, 'brand': brand, '_image': photo, 'currency': 'TRY', 'condition': 'new',
            'price': float(price), 'availability': 'in_stock',
            'observed_at': datetime.now(timezone.utc).isoformat()}, 'verified'
