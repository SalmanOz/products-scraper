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
    'www.lowell.com.tr': (
        'Lowell', '.productDetails .product-price-sale .price',
        '.product-button-container a.product-cart-add.add-to-cart-btn[data-productid]',
        '/mesafeli-satis-sozlesmesi', 'LOWELL TEKNOLOJİ BİLİŞİM SANAYİ TİCARET LİMİTED ŞİRKETİ'),
    'www.uzmanlarpc.com': (
        'UzmanlarPC', 'div:has(> .pPriceTitle:-soup-contains("Kredi Kartı Fiyatı")) > .pPriceDeger[name="tryPrice"].d-block',
        'button.wc-buy-cart', '/sartlar-kosullar', 'Uzmanlar oto gıda bilgisayar inş san tic ltd şti'),
    'www.sinerji.gen.tr': (
        'Sinerji', '.productDetail .priceWrapper > .price', '.productDetail button.addToCart[type="submit"]',
        '/degisim-iptal-ve-iade-politikasi-cp-480', 'Sinerji Bilgisayar Elektronik ve Sanal Mağazacılık Limited Şirketi'),
    'www.tebilon.com': (
        'Tebilon', '.productDetail__topPrice .price > .disc',
        'a#sepeteEkleTagMng.sepeteEkleAdet[data-id][role="button"]',
        '/mesafeli-satis-sozlesmesi/', 'TEBİLON BİLİŞİM TEKNOLOJİLERİ SAN. VE TİC. A.Ş.'),
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
    'nevavm.com': (
        'Neva AVM', '.summary.entry-summary p.price > ins bdi, .summary.entry-summary p.price > .amount bdi',
        '.summary.entry-summary form.cart button[name="add-to-cart"]',
        '/mesafeli-satis-sozlesmesi/', 'Neva AVM'),
    'tasozbilgisayar.com': (
        'Taşöz Bilgisayar', '.product-details > .product-price > ins.new-price',
        '.product-details a.btn-cart-emphasis[data-sepete-ekle]',
        '/sayfa/mesafeli-satis-sozlesmesi', '4A Bilişim & Gayrimenkul'),
}


def selected_direct_laptop(html, url, *, read_facts=None):
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
    if host == 'www.sinerji.gen.tr':
        # Sinerji's selected product JSON-LD ends in a JavaScript semicolon.
        for script in soup.select('script[type="application/ld+json"]'):
            if script.string and script.string.strip().endswith(';'):
                script.string.replace_with(type(script.string)(script.string.strip().removesuffix(';')))
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
    elif host == 'www.uzmanlarpc.com':
        headings = soup.select('h1.wc-pd-title')
    if len(products) != 1:
        return None, 'direct_selected_product_missing'
    product = products[0]
    brand_node = product.get('brand')
    brand = _brand(brand_node.get('name') if isinstance(brand_node, dict) else brand_node)
    title = headings[0].get_text(' ', strip=True) if len(headings) == 1 else ''
    if host == 'www.gaming.gen.tr' and brand is None and title.startswith('HP '):
        brand = 'HP'
    schema_title = unescape(str(product.get('name') or ''))
    if host == 'nevavm.com':
        schema_title = schema_title.removesuffix(' | Neva AVM')
    if not title or WORDS(title) != WORDS(schema_title):
        return None, 'direct_selected_title_mismatch'
    facts = read_facts(title, brand_node.get('name') if isinstance(brand_node, dict) else brand_node) if read_facts else (
        laptop_facts(title, {}, brand, product.get('mpn', '')))
    gtin = retailer_gtin13(product.get('gtin13') or product.get('gtin'))
    if host == 'tasozbilgisayar.com':
        gtin = _tasoz_gtin(soup, gtin)
    elif host == 'www.uzmanlarpc.com':
        barcodes = [retailer_gtin13(node.get_text(' ', strip=True))
                    for node in soup.select('span.bid:-soup-contains("Barkod:") > span.pDeger')]
        if barcodes != [gtin]:
            return None, 'direct_selected_identity_missing'
    elif host == 'www.sinerji.gen.tr':
        barcodes = []
        for row in soup.select('#nav-technical-specs table tr'):
            cells = row.find_all('td', recursive=False)
            if len(cells) == 2 and WORDS(cells[0].get_text(' ', strip=True)) == 'barkod gtin':
                barcodes.append(retailer_gtin13(cells[1].get_text(' ', strip=True)))
        if barcodes != [gtin] or not product.get('mpn') or product['mpn'] != product.get('sku'):
            return None, 'direct_selected_identity_missing'
    if not facts or not gtin:
        return None, 'direct_selected_identity_missing'

    offer = product.get('offers')
    if isinstance(offer, list) and len(offer) == 1:
        offer = offer[0]
    if not isinstance(offer, dict) or offer.get('@type') != 'Offer':
        return None, 'direct_selected_offer_missing'
    if host == 'www.uzmanlarpc.com' and offer.get('seller') != {
            '@type': 'Organization', '@id': 'https://www.uzmanlarpc.com/#organization', 'name': 'UzmanlarPC'}:
        return None, 'direct_selected_seller_mismatch'
    stock_label = soup.select_one('.summary.entry-summary .bss-avail')
    # The new-product WooCommerce template omits itemCondition in JSON-LD.
    # Require its own stock promise, HP model/barkod and a normal one-unit sale.
    ordinary_woo = (host == 'www.gaming.gen.tr' and offer.get('itemCondition') is None
                    and stock_label and 'Stoktan gönderim' in stock_label.get_text(' ', strip=True))
    is_new = (offer.get('itemCondition') == 'https://schema.org/NewCondition'
              or host == 'www.sinerji.gen.tr' and offer.get('itemCondition') == 'http://schema.org/NewCondition')
    if (offer.get('url') != url or offer.get('priceCurrency') != 'TRY'
            or offer.get('availability') not in {'https://schema.org/InStock', 'http://schema.org/InStock'}
            or not (is_new or ordinary_woo)
            or any(key in offer for key in ('validForMemberTier', 'eligibleCustomerType', 'eligibleTransactionVolume'))):
        return None, 'direct_new_cash_stock_unproven'
    prices = soup.select(cash_selector)
    carts = [node for node in soup.select(cart_selector)
             if WORDS(node.get_text(' ', strip=True)) == 'sepete ekle']
    price = amount(offer.get('price'))
    if host == 'www.netsiparis.com':
        price = _netsiparis_cash(soup, prices, price)
    elif host == 'www.gaming.gen.tr' and len(prices) == 1 and prices[0].select('.from'):
        price = _gaming_base_cash(prices[0], carts, url, price)
    elif len(prices) != 1 or _visible_cash(prices[0]) != price:
        price = None
    if host == 'tasozbilgisayar.com' and len(carts) == 1:
        cart = carts[0]
        product_id = urlparse(url).path.rstrip('/').rsplit('-', 1)[-1]
        if (cart['data-sepete-ekle'] != product_id or WORDS(cart.get('data-urun-ad')) != WORDS(title)
                or amount(str(cart.get('data-urun-fiyat', '')).replace('.', '').replace(',', '.')) != price):
            return None, 'direct_selected_cart_mismatch'
    if host == 'www.lowell.com.tr' and len(carts) == 1:
        if WORDS(carts[0].get('data-productname')) != WORDS(title):
            return None, 'direct_selected_cart_mismatch'
    if host == 'www.sinerji.gen.tr' and len(carts) == 1:
        product_id = urlparse(url).path.rsplit('-p-', 1)[-1]
        if carts[0].get('value') != product_id or not soup.body or soup.body.get('id') != 'P' + product_id:
            return None, 'direct_selected_cart_mismatch'
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
    photo_hosts = (host, 'static.sinerji.gen.tr') if host == 'www.sinerji.gen.tr' else (host,)
    photo = next((img for src in images or [] if (img := _photo(src, *photo_hosts))), None)
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


def _gaming_base_cash(block, carts, url, listed):
    """A WooCommerce bundle may sell its parent alone with zero optional items."""
    labels = block.select(':scope > .from')
    prices = block.select(':scope > .woocommerce-Price-amount > bdi')
    if (listed is None or len(labels) != 1 or WORDS(labels[0].get_text()) != 'en dusuk'
            or len(prices) != 1 or _visible_cash(prices[0]) != listed or len(carts) != 1):
        return None
    route = re.fullmatch(r'/urun/(\d+)/[a-z0-9-]+/', urlparse(url).path)
    form = carts[0].find_parent('form', class_='bundle_form')
    if not form or not route or carts[0].get('name') != 'add-to-cart' or carts[0].get('value') != route[1]:
        return None
    if form.get('action') not in (None, '', url) or form.get('method', '').lower() != 'post':
        return None
    data = form.select(':scope > .bundle_data[data-bundle_form_data]')
    quantities = form.select('input[name="quantity"]')
    amounts = form.select('#main_price_include_tax')
    products = form.select('.bundled_product')
    if (len(data) != 1 or data[0].get('data-bundle_id') != route[1]
            or len(quantities) != 1 or quantities[0].get('value') != '1' or quantities[0].get('min') != '1'
            or len(amounts) != 1 or amount(amounts[0].get('data-initial')) != listed
            or amount(amounts[0].get_text(strip=True)) != listed or not products
            or form.select('input[checked]:not([value=""]), select')):
        return None
    try:
        state = json.loads(data[0]['data-bundle_form_data'])
    except ValueError:
        return None
    if (not isinstance(state, dict) or state.get('zero_items_allowed') != 'yes'
            or state.get('is_purchasable') != 'yes' or state.get('availability_class') != 'in-stock'
            or amount(state.get('raw_bundle_price_min')) != listed):
        return None
    for product in products:
        choices = product.select('input.bundled_product_checkbox[type="checkbox"]')
        if ('bundled_item_optional' not in product.get('class', []) or len(choices) != 1
                or choices[0].has_attr('checked')):
            return None
    return listed


def _tasoz_gtin(soup, schema_gtin):
    codes = []
    for table in ('table.product-specs-table', 'table#as_SpecData'):
        matches = []
        for row in soup.select(f'#product-tab-description {table} tr'):
            cells = row.find_all('td', recursive=False)
            if len(cells) == 2 and cells[0].get_text(' ', strip=True) == 'EAN / UPC / JAN':
                matches.append(retailer_gtin13(cells[1].get_text(' ', strip=True)))
        if len(matches) != 1 or not matches[0]:
            return None
        codes.extend(matches)
    return codes[0] if len(set(codes)) == 1 and schema_gtin in (None, codes[0]) else None


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
    tax = re.findall(r'\b(?:Vergi No\s*:|Vergi Dairesi / No\s+.{0,70}?–|Vno\s*:)\s*((?:\d\s*){10,11})(?!\d)',
                     ' '.join(text.split()), re.I)
    normalized = {re.sub(r'\s+', '', key) for key in tax}
    if host in {'nevavm.com', 'tasozbilgisayar.com'} and len(normalized) != 1:
        return None
    return {'_legal_name': name, '_tax_key': next(iter(normalized)) if len(normalized) == 1 else ''}
