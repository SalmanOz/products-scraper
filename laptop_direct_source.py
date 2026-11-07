"""Selected first-party laptop offers from reviewed public storefront templates."""
from datetime import datetime, timezone
from html import unescape
import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from laptop_retailer_sources import WORDS, _brand, _photo, laptop_facts
from retailer_barcode import retailer_gtin13
from retailer_offer_refresh import _schema_nodes
from trendyol_basket_price import amount

# Each storefront's checkout price and enabled control are scoped to its PDP.
# Legal labels are verified again on the store's own public company page.
DIRECT_STORES = {
    'www.snerteknoloji.com': (
        'Sner Teknoloji', '.productDetails .product-price-sale .price',
        '.productDetails a.product-cart-add.add-to-cart-btn',
        '/mesafeli-satis-sozlesmesi', 'ENSAR BİLİŞİM VE TEKNOLOJİ ÜRÜNLERİ TİC. LTD. ŞTİ.'),
    'pitrap.com': (
        'Pitrap', '.pdp .buybox .prices > .price',
        '.pdp .buybox form[method="post"][action^="/sepet/ekle/"] button',
        '/mesafeli-satis-sozlesmesi', 'Partner IT Bilgi Sistemleri Limited Şirketi'),
    'www.gaming.gen.tr': (
        'Gaming.Gen.TR', '.summary.entry-summary p.price',
        '.summary.entry-summary form.cart button.single_add_to_cart_button',
        '/iletisim/', 'Beta Oyun Teknoloji Anonim Şirketi'),
    'www.incehesap.com': (
        'İncehesap', 'div.prod div.price',
        'div.prod button',
        '/icerik/kisisel-verilerin-korunmasi', 'İncehesap Sanal Mağazacılık Ticaret Anonim Şirketi'),
    'www.teknostim.com': (
        'Teknostim', '.product-right .product-price-new',
        '.product-right a.add-to-cart-button',
        '/sayfa/mesafeli-satis-sozlesmesi', 'C A Bilgisayar Tic. Ltd. Şti.'),
    'www.netsiparis.com': (
        'Netsipariş', '.ns-product-right .pd-price',
        '.ns-product-right button.sepete-at-adetli',
        '/mesafeli-satis-sozlesmesi', 'Arslan Bilgisayar Elektronik ve Dekorasyon Dizayn Hizmetleri San.Tic.Ltd.Sti.'),
}


def selected_direct_laptop(html, url):
    host = urlparse(url).netloc
    platform, cash_selector, cart_selector, legal_path, _ = DIRECT_STORES[host]
    soup = BeautifulSoup(html, 'html.parser')
    canonical = soup.select('link[rel="canonical"]')
    if len(canonical) != 1 or canonical[0].get('href') != url:
        return None, 'direct_canonical_mismatch'
    if host == 'pitrap.com':
        # This storefront serializes JSON-LD type/context keys with doubled @.
        for script in soup.select('script[type="application/ld+json"]'):
            body = script.get_text()
            if '"@@type":"Product"' in body and '"@type"' not in body:
                script.string.replace_with(type(script.string)(
                    body.replace('"@@type":', '"@type":').replace('"@@context":', '"@context":')))
    products = [node for node in _schema_nodes(soup, url) if node.get('@type') == 'Product']
    headings = soup.select('h1')
    if host == 'www.incehesap.com':
        headings = soup.select('div.prod h1[itemprop="name"]')
        item = _incehesap_microdata(soup, url, headings)
        products = [item] if item else []
    elif host == 'www.teknostim.com':
        headings = soup.select('.product-right h1')
        item = _teknostim_microdata(soup, url)
        products = [item] if item else []
    if len(products) != 1:
        return None, 'direct_selected_product_missing'
    product = products[0]
    brand_node = product.get('brand')
    brand = _brand(brand_node.get('name') if isinstance(brand_node, dict) else brand_node)
    title = headings[0].get_text(' ', strip=True) if len(headings) == 1 else ''
    if host == 'www.gaming.gen.tr' and brand is None and title.startswith('HP '):
        brand = 'HP'
    if not title or WORDS(title) != WORDS(unescape(str(product.get('name') or ''))):
        return None, 'direct_selected_title_mismatch'
    facts = laptop_facts(title, {}, brand, product.get('mpn', ''))
    gtin = retailer_gtin13(product.get('gtin13') or product.get('gtin'))
    if not facts or not gtin:
        return None, 'direct_selected_identity_missing'

    offer = product.get('offers')
    if isinstance(offer, list) and len(offer) == 1:
        offer = offer[0]
    if not isinstance(offer, dict) or offer.get('@type') != 'Offer':
        return None, 'direct_selected_offer_missing'
    stock_label = soup.select_one('.summary.entry-summary .bss-avail')
    # The new-product WooCommerce template omits itemCondition in JSON-LD.
    # Require its own stock promise, HP model/barkod and a normal one-unit sale.
    ordinary_woo = (host == 'www.gaming.gen.tr' and offer.get('itemCondition') is None
                    and stock_label and 'Stoktan gönderim' in stock_label.get_text(' ', strip=True))
    if (offer.get('url') != url or offer.get('priceCurrency') != 'TRY'
            or offer.get('availability') != 'https://schema.org/InStock'
            or (offer.get('itemCondition') != 'https://schema.org/NewCondition' and not ordinary_woo)
            or any(key in offer for key in ('validForMemberTier', 'eligibleCustomerType', 'eligibleTransactionVolume'))):
        return None, 'direct_new_cash_stock_unproven'
    prices = soup.select(cash_selector)
    carts = [node for node in soup.select(cart_selector)
             if WORDS(node.get_text(' ', strip=True)) == 'sepete ekle']
    price = amount(offer.get('price'))
    if host == 'www.netsiparis.com':
        price = _netsiparis_cash(soup, prices, price)
    elif len(prices) != 1 or _visible_cash(prices[0]) != price:
        price = None
    if not price:
        return None, 'direct_visible_price_mismatch'
    if (len(carts) != 1 or carts[0].has_attr('disabled')
            or carts[0].get('aria-disabled') == 'true'
            or 'disabled' in carts[0].get('class', [])
            or WORDS(carts[0].get_text(' ', strip=True)) != 'sepete ekle'):
        return None, 'direct_checkout_unavailable'
    images = product.get('image')
    if isinstance(images, str):
        images = [images]
    photo = next((img for src in images or [] if (img := _photo(src, host))), None)
    return {'platform': platform, 'merchant': platform, 'merchant_id': host,
            'seller_url': 'https://' + host + legal_path, 'facts': facts,
            'gtin13': gtin, '_image': photo, 'price': float(price), 'currency': 'TRY',
            'condition': 'new', 'availability': 'in_stock', 'source_title': title,
            'url': url, 'observed_at': datetime.now(timezone.utc).isoformat()}, 'selected_direct_offer_observed'


def _visible_cash(node):
    if node is None:
        return None
    match = re.fullmatch(r'(?:₺\s*)?(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)\s*(?:TL|₺)?',
                         node.get_text(' ', strip=True))
    return amount(match[1].replace('.', '').replace(',', '.')) if match else None


def _netsiparis_cash(soup, blocks, listed):
    credit = []
    for block in blocks:
        label = block.select_one('.pd-price-disc')
        if label and re.fullmatch(r'Tek çekimde %[1-9]\d? indirim', label.get_text(' ', strip=True)):
            credit.append(block)
    if len(credit) != 1 or listed is None or _visible_cash(credit[0].select_one('.pd-price-old')) != listed:
        return None
    payable = _visible_cash(credit[0].select_one('.pd-price-new'))
    one_payment = []
    for row in soup.select('table tr'):
        cells = row.find_all('td', recursive=False)
        if len(cells) == 3 and cells[0].get_text(' ', strip=True) == '1':
            one_payment.extend(_visible_cash(cell) for cell in cells[1:])
    return payable if payable and payable < listed and one_payment and set(one_payment) == {payable} else None


def _incehesap_microdata(soup, url, headings):
    if len(headings) != 1:
        return None
    barcode = soup.select('meta[itemprop="gtin14"]')
    price = soup.select('meta[itemprop="price"]')
    brand = soup.select('[itemprop="brand"] > meta[itemprop="name"]')
    offers = soup.select('[itemprop="offers"][itemtype="https://schema.org/AggregateOffer"]')
    if not len(barcode) == len(price) == len(brand) == len(offers) == 1:
        return None
    def attr(key, field='content'):
        selected = offers[0].find_all(attrs={'itemprop': key})
        return selected[0].get(field) if len(selected) == 1 else None
    if attr('offerCount') != '1' or amount(price[0].get('content')) != amount(attr('lowPrice')):
        return None
    return {'name': headings[0].get_text(' ', strip=True), 'brand': brand[0].get('content'),
            'gtin13': barcode[0].get('content'),
            'offers': {'@type': 'Offer', 'url': url, 'price': attr('lowPrice'),
                       'priceCurrency': attr('priceCurrency'), 'availability': attr('availability'),
                       'itemCondition': attr('itemCondition', 'href')}}


def _teknostim_microdata(soup, url):
    products = soup.select('[itemtype="https://schema.org/Product"]')
    if len(products) != 1:
        return None
    product = products[0]
    name = product.select_one(':scope > [itemprop="name"]')
    barcode = product.select_one('meta[itemprop="gtin13"]')
    brand = product.select_one('[itemprop="brand"] > meta[itemprop="name"]')
    offer = product.select_one('[itemprop="offers"][itemtype="https://schema.org/Offer"]')
    if name is None or barcode is None or brand is None or offer is None:
        return None
    values = {tag['itemprop']: tag.get('content') or tag.get('href') or tag.get_text(' ', strip=True)
              for tag in offer.select('[itemprop]')}
    conditions = product.select('[itemprop="itemCondition"]')
    if values.get('url') != url or len(conditions) != 1 or conditions[0].get('content') not in {
            'http://schema.org/NewCondition', 'https://schema.org/NewCondition'}:
        return None
    return {'name': name.get_text(' ', strip=True), 'brand': brand.get('content'),
            'gtin13': barcode.get('content'),
            'offers': {'@type': 'Offer', **values, 'itemCondition': 'https://schema.org/NewCondition'}}


def direct_company_identity(html, url):
    host = urlparse(url).netloc
    company = DIRECT_STORES[host][-1]
    text = BeautifulSoup(html, 'html.parser').get_text(' ', strip=True)
    name = WORDS(company)
    if not re.search(r'\b' + re.escape(name) + r'\b', WORDS(text)):
        return None
    tax = re.findall(r'\b(?:Vergi No\s*:|Vergi Dairesi / No\s+.{0,70}?–|Vno\s*:)\s*((?:\d\s*){10})(?!\d)',
                     ' '.join(text.split()), re.I)
    normalized = {re.sub(r'\s+', '', key) for key in tax}
    return {'_legal_name': name, '_tax_key': next(iter(normalized)) if len(normalized) == 1 else ''}
