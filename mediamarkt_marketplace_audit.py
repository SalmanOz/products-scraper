"""Read-only proof of a selected MediaMarkt marketplace phone offer.

This module is intentionally separate from the production first-party reader.
It does not fetch pages, create publishable offers, or write prices. Callers must
supply a fresh, exact PDP response and its final URL.
"""

import json
import math
import re
from html import unescape
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from mediamarkt_source import _PRICE, _USED, product_url


_PROMOTION = re.compile(
    r'sepette|kupon|üyelere|uyelere|üyelik|uyelik|loyalty|cashback', re.I)
_STATE_PREFIX = re.compile(r'^\s*window\.__PRELOADED_STATE__\s*=\s*', re.S)
_SELLER_URL = 'https://www.mediamarkt.com.tr/tr/marketplace/seller/'


def _normal(text):
    return ' '.join(text.split()) if isinstance(text, str) else ''


def _one(nodes):
    return nodes[0] if len(nodes) == 1 else None


def _state(soup):
    scripts = [script.get_text() for script in soup.select('script')
               if 'window.__PRELOADED_STATE__' in script.get_text()]
    if len(scripts) != 1 or len(scripts[0]) > 3_000_000:
        return None
    match = _STATE_PREFIX.match(scripts[0])
    if not match:
        return None
    value = scripts[0][match.end():].strip().rstrip(';')
    # MediaMarkt currently emits four unquoted JavaScript `undefined` values
    # outside Apollo. Convert only object values, then require valid JSON.
    value = re.sub(r':undefined(?=[,}])', ':null', value)
    try:
        state = json.loads(value)
    except (TypeError, ValueError):
        return None
    apollo = state.get('apolloState') if isinstance(state, dict) else None
    return apollo if isinstance(apollo, dict) else None


def _ref(apollo, parent, name, typename, expected_id):
    ref = parent.get(name) if isinstance(parent, dict) else None
    if (not isinstance(ref, dict) or set(ref) != {'__ref'} or
            not isinstance(ref.get('__ref'), str)):
        return None
    node = apollo.get(ref['__ref'])
    if (not isinstance(node, dict) or node.get('__typename') != typename
            or node.get('id') != expected_id):
        return None
    return node


def _price(raw, *, allow_cents=False):
    if (isinstance(raw, bool) or
            not re.fullmatch(r'\d+(?:\.\d{1,2})?', str(raw))):
        return None
    value = float(raw)
    if (not math.isfinite(value) or not 0 < value <= 5_000_000 or
            (not allow_cents and value != int(value))):
        return None
    return value


def _gtin13(raw):
    """Accept an actual EAN-13, not merely a 13-digit-looking string."""
    if not isinstance(raw, str) or not re.fullmatch(r'\d{13}', raw):
        return None
    total = sum((1 if index % 2 == 0 else 3) * int(digit)
                for index, digit in enumerate(raw[:12]))
    return raw if (10 - total % 10) % 10 == int(raw[-1]) else None


def _product_capacities(apollo, graph_product, sku):
    """Read RAM, storage and optional radio from this SKU's feature graph."""
    product_ref = graph_product.get('product')
    if product_ref != {'__ref': 'GraphqlProduct:Media:tr-TR:' + sku}:
        return None
    product = apollo.get(product_ref['__ref'])
    if (not isinstance(product, dict) or product.get('id') != sku or
            product.get('featureGroupsWithProductId') !=
                {'__ref': 'GraphqlProductFeatureGroups:' + sku}):
        return None
    groups = apollo.get('GraphqlProductFeatureGroups:' + sku)
    if not isinstance(groups, dict) or groups.get('productId') != sku:
        return None
    found = {'RAM Bellek Boyutu': [], 'Bellek Kapasitesi': [],
             'Mobil Telefon Standardı': []}
    for group in groups.get('featureGroups', []):
        if not isinstance(group, dict) or not isinstance(group.get('features'), list):
            return None
        for link in group['features']:
            if not isinstance(link, dict) or not isinstance(link.get('__ref'), str):
                return None
            feature = apollo.get(link['__ref'])
            if not isinstance(feature, dict) or feature.get('__typename') != 'GraphqlProductFeature':
                return None
            name = feature.get('name')
            if name in found:
                found[name].append(str(feature.get('value', '')))
    if any(len(found[name]) != 1 or
           not re.fullmatch(r'\d{1,4}', found[name][0])
           for name in ('RAM Bellek Boyutu', 'Bellek Kapasitesi')):
        return None
    ram = int(found['RAM Bellek Boyutu'][0])
    storage = int(found['Bellek Kapasitesi'][0])
    radio_values = found['Mobil Telefon Standardı']
    if len(radio_values) > 1:
        return None
    radio = radio_values[0].strip().casefold() if radio_values else None
    radio = radio if radio in {'4g', '5g'} else None
    return (ram, storage, radio) if 1 <= ram <= 64 and 32 <= storage <= 4096 else None


def catalog_variant_matches(evidence, expected_name, expected_specs):
    """Read-only catalog gate for the exact PDP observation.

    MediaMarkt titles like `12 GB 256 GB` omit a RAM label, so the generic
    observed-title gate cannot prove RAM. This requires the SKU-bound technical
    feature graph and explicit radio evidence for model families with 4G/5G.
    """
    from tr_price_scraper import TRPriceScraper

    if not isinstance(evidence, dict) or not isinstance(expected_specs, dict):
        return False
    matcher = TRPriceScraper()
    if not matcher.is_strict_match(expected_name, evidence.get('source_title', ''),
                                   expected_specs):
        return False
    try:
        ram = int(expected_specs['ram_gb'])
        storage = int(expected_specs['storage_gb'])
    except (KeyError, TypeError, ValueError):
        return False
    if (ram != evidence.get('ram_gb') or storage != evidence.get('storage_gb')):
        return False
    expected_radio = expected_specs.get('network_generation') or matcher._radio_generation(expected_name)
    observed_radio = evidence.get('network_generation')
    return not expected_radio or observed_radio == expected_radio


def selected_marketplace_offer(html, final_url, *,
                               allow_refurbished_alternatives=False,
                               allow_cents=False,
                               require_selected_condition_radio=False,
                               allow_delivery_card=False,
                               require_gtin_variant=False):
    """Return selected-offer evidence only when every independent layer agrees.

    The returned record is an audit observation, not an ingestion offer. Its
    seller is named separately from the MediaMarkt marketplace platform.
    """
    if not product_url(final_url) or not isinstance(html, (str, bytes)):
        return None
    soup = BeautifulSoup(html, 'html.parser')
    heading = _one(soup.select('h1'))
    canonical = _one(soup.select('link[rel="canonical"]'))
    if not heading or not canonical or canonical.get('href') != final_url:
        return None
    title = _normal(heading.get_text(' ', strip=True))
    if not title or _USED.search(title):
        return None

    selected = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            action = json.loads(script.get_text())
        except (TypeError, ValueError):
            continue
        if not isinstance(action, dict) or action.get('@type') != 'BuyAction':
            continue
        product = action.get('object')
        if (not isinstance(product, dict) or
                product.get('@type') not in {'Product', 'ProductGroup'} or
                product.get('url') != final_url or product.get('name') != title):
            continue
        offers = product.get('offers')
        offers = offers if isinstance(offers, list) else [offers]
        if len(offers) == 1 and isinstance(offers[0], dict):
            selected.append((product, offers[0]))
        elif allow_refurbished_alternatives and len(offers) > 1:
            # The selected NEW offer must be unique. The other schema offers
            # may only be refurbished alternatives for this exact PDP; the
            # Apollo default/best offer and checked radio are checked below.
            new = [row for row in offers if isinstance(row, dict) and
                   row.get('itemCondition') == 'https://schema.org/NewCondition']
            others = [row for row in offers if row not in new]
            if (len(new) == 1 and len(others) == len(offers) - 1 and
                    all(isinstance(row, dict) and row.get('@type') == 'Offer' and
                        row.get('url') == final_url and
                        row.get('itemCondition') ==
                        'https://schema.org/RefurbishedCondition'
                        for row in others)):
                selected.append((product, new[0]))
    if len(selected) != 1:
        return None
    product, offer = selected[0]
    sku = str(product.get('sku', ''))
    raw_gtin = product.get('gtin13')
    gtin = _gtin13(raw_gtin) if raw_gtin is not None else None
    if raw_gtin is not None and gtin is None:
        return None
    variants = product.get('hasVariant')
    selected_variant_gtin = None
    selected_variant_sku = None
    if variants is not None:
        if not isinstance(variants, list):
            return None
        selected_variants = [row for row in variants if isinstance(row, dict)
                             and str(row.get('sku', '')) == sku]
        if selected_variants and (len(selected_variants) != 1 or
                                  selected_variants[0].get('gtin13') != gtin):
            return None
        if selected_variants:
            selected_variant_gtin = selected_variants[0].get('gtin13')
        if require_gtin_variant:
            matched_gtins = [row for row in variants if isinstance(row, dict)
                             and row.get('gtin13') == gtin]
            if (gtin is None or len(matched_gtins) != 1 or
                    not re.fullmatch(r'\d{6,12}',
                                     str(matched_gtins[0].get('sku', '')))):
                return None
            selected_variant_gtin = gtin
            selected_variant_sku = str(matched_gtins[0]['sku'])
    elif require_gtin_variant:
        return None
    brand = product.get('brand')
    if (not re.fullmatch(r'\d{6,12}', sku) or
            not final_url.endswith('-' + sku + '.html') or
            not isinstance(brand, dict) or not _normal(brand.get('name')) or
            offer.get('@type') != 'Offer' or offer.get('url') != final_url or
            offer.get('priceCurrency') != 'TRY' or
            offer.get('itemCondition') != 'https://schema.org/NewCondition'):
        return None
    availability = str(offer.get('availability', '')).rstrip('/').rsplit('/', 1)[-1]
    if availability not in {'InStock', 'LimitedAvailability'}:
        return None
    price = _price(offer.get('price'), allow_cents=allow_cents)
    seller = offer.get('seller')
    if price is None or not isinstance(seller, dict) or seller.get('@type') != 'Organization':
        return None
    seller_name = _normal(seller.get('name'))
    seller_id = str(seller.get('id', ''))
    if (not seller_name or len(seller_name) > 100 or
            not re.fullmatch(r'\d{1,12}', seller_id)):
        return None

    apollo = _state(soup)
    if apollo is None:
        return None
    root = apollo.get('ROOT_QUERY')
    if not isinstance(root, dict):
        return None
    aggregates = [value for key, value in root.items()
                  if key.startswith('cofrProductAggregate(') and
                  isinstance(value, dict) and value.get('productId') == sku and
                  value.get('offerId') is None]
    aggregate = _one(aggregates)
    if aggregate is None:
        return None
    base_id = 'Media:tr:' + sku
    core = _ref(apollo, aggregate, 'cofrCoreFeature', 'CofrCoreFeature', base_id)
    priced = _ref(apollo, aggregate, 'cofrPriceFeature', 'CofrPriceFeature', base_id)
    online = _ref(apollo, aggregate, 'cofrOnlineStatusFeature',
                  'CofrOnlineStatusFeature', base_id)
    if not core or not priced or not online:
        return None
    offer_id = str(core.get('defaultOfferId', ''))
    marketplace = apollo.get('GraphqlMarketplaceOfferV3:' + offer_id)
    graph_product = apollo.get('GraphqlProductAggregate:Media:tr-TR:' + sku)
    if (not re.fullmatch(r'\d{1,15}', offer_id) or
            core.get('relevantOfferId') != offer_id or
            core.get('productName') != title or
            core.get('isProductOfTypeMarketplace') is not True or
            not isinstance(graph_product, dict) or
            graph_product.get('bestMarketplaceOfferV3') !=
                {'__ref': 'GraphqlMarketplaceOfferV3:' + offer_id} or
            not isinstance(marketplace, dict) or
            marketplace.get('__typename') != 'GraphqlMarketplaceOfferV3' or
            str(marketplace.get('offerId')) != offer_id or
            marketplace.get('productId') != sku or
            marketplace.get('offerCondition') != 'NEW' or
            marketplace.get('isBestOffer') is not True or
            marketplace.get('currencyIsoCode') != 'TRY' or
            _price(marketplace.get('price'), allow_cents=allow_cents) != price or
            _normal(marketplace.get('sellerName')) != seller_name or
            str(marketplace.get('sellerId')) != seller_id):
        return None
    features = _product_capacities(apollo, graph_product, sku)
    if features is None:
        return None
    capacities = features[:2]
    from tr_price_scraper import TRPriceScraper
    title_ram, title_storage = TRPriceScraper()._extract_listing_capacities(title)
    if (title_ram, title_storage) != capacities:
        return None
    site_seller = priced.get('marketplaceSeller')
    site_price = priced.get('price')
    if (priced.get('isProductOfTypeMarketplace') is not True or
            priced.get('currency') != 'TRY' or
            not isinstance(site_seller, dict) or
            _normal(site_seller.get('sellerName')) != seller_name or
            str(site_seller.get('sellerId')) != seller_id or
            not isinstance(site_price, dict) or
            _price(site_price.get('amount'), allow_cents=allow_cents) != price or
            online.get('isAvailableAndBuyable') is not True or
            online.get('isAvailableForDelivery') is not True or
            online.get('onlineStatus') != 'MP_OFFER'):
        return None

    # The offer-specific feature confirms NEW rather than the condition of an
    # unrelated marketplace card. It must be reachable from the selected offer.
    selected_aggregate = marketplace.get('cofrProductAggregate({})')
    selected_id = base_id + '[' + offer_id + ']'
    selected_core = _ref(apollo, selected_aggregate, 'cofrCoreFeature',
                         'CofrCoreFeature', selected_id)
    selected_price = _ref(apollo, selected_aggregate, 'cofrPriceFeature',
                          'CofrPriceFeature', selected_id)
    selected_online = _ref(apollo, selected_aggregate, 'cofrOnlineStatusFeature',
                           'CofrOnlineStatusFeature', selected_id)
    condition = _ref(apollo, selected_aggregate, 'cofrRefurbishedGoodsFeature',
                     'CofrRefurbishedGoodsFeature', selected_id)
    condition_offer = condition.get('offer') if condition else None
    selected_amount = selected_price.get('price') if selected_price else None
    if (not selected_core or not selected_price or not selected_online or
            selected_core.get('defaultOfferId') != offer_id or
            selected_core.get('relevantOfferId') != offer_id or
            selected_core.get('productName') != title or
            selected_core.get('isProductOfTypeMarketplace') is not True or
            selected_price.get('currency') != 'TRY' or
            selected_price.get('isProductOfTypeMarketplace') is not True or
            selected_price.get('marketplaceSeller') != site_seller or
            not isinstance(selected_amount, dict) or
            _price(selected_amount.get('amount'), allow_cents=allow_cents) != price or
            selected_online.get('isAvailableAndBuyable') is not True or
            selected_online.get('isAvailableForDelivery') is not True or
            selected_online.get('onlineStatus') != 'MP_OFFER' or
            not isinstance(condition_offer, dict) or
            condition_offer.get('conditionType') != 'NEW' or
            str(condition_offer.get('offerId')) != offer_id or
            _price(condition_offer.get('priceAmount'), allow_cents=allow_cents) != price or
            condition_offer.get('priceCurrency') != 'TRY'):
        return None

    visible_price = _one(soup.select('[data-test="cofr-price mms-branded-price"]'))
    visible_stock = _one(soup.select('[data-test="mms-pdp-details-availability"]'))
    delivery = (visible_stock.select('[data-test="mms-cofr-delivery_AVAILABLE"]')
                if visible_stock else [])
    if allow_delivery_card and not delivery:
        chosen = soup.select('input[name="pdp-condition-selector"][checked]')
        chosen_card = chosen[0].find_parent('li') if len(chosen) == 1 else None
        delivery = (chosen_card.select('[data-test="mms-cofr-delivery_AVAILABLE"]')
                    if chosen_card else [])
    if (visible_price is None or visible_stock is None or
            _PROMOTION.search(visible_price.get_text(' ', strip=True)) or
            not any('Adrese teslimata uygun.' in row.get_text(' ', strip=True)
                    for row in delivery)):
        return None
    whole = _one(visible_price.select('[data-test="branded-price-whole-value"]'))
    match = _PRICE.fullmatch(whole.get_text(' ', strip=True)) if whole else None
    decimal = _one(visible_price.select('[data-test="branded-price-decimal-value"]'))
    if allow_cents:
        decimal_text = decimal.get_text(' ', strip=True) if decimal else None
        displayed = (float(match[1].replace('.', '') + '.' + decimal_text)
                     if match and decimal_text and
                     re.fullmatch(r'\d{2}', decimal_text) else None)
    else:
        displayed = float(match[1].replace('.', '')) if match else None
    if displayed != price:
        return None
    if require_selected_condition_radio:
        checked = soup.select('input[name="pdp-condition-selector"][checked]')
        if (len(checked) != 1 or checked[0].get('id') != offer_id or
                checked[0].get('value') != offer_id):
            return None
        labels = soup.select('label[for="' + offer_id + '"]')
        if (len(labels) != 1 or checked[0] not in labels[0].descendants or
                _normal(labels[0].get_text(' ', strip=True)) !=
                'Yeni ürünler ' + seller_name):
            return None
    seller_link = [link for link in visible_price.select('a[href]')
                   if link.get('href') == _SELLER_URL + seller_id and
                   _normal(link.get_text(' ', strip=True)) == seller_name]
    if len(seller_link) != 1:
        return None
    buttons = soup.select('[data-test="mms-anchor-navigation-bar"] '
                          'button[data-test*="cofr-add-to-basket-button"]')
    if not any(button.get('aria-disabled') == 'false' and
               not button.has_attr('disabled') and
               button.get('aria-label') == 'Sepete Ekle ' + title and
               _normal(button.get_text(' ', strip=True)) == 'Sepete Ekle'
               for button in buttons):
        return None
    title_radios = {value.casefold() for value in re.findall(
        r'(?<![a-z0-9])([45]g)(?![a-z0-9])', title, re.I)}
    path_radios = {value.casefold() for value in re.findall(
        r'(?<![a-z0-9])([45]g)(?![a-z0-9])', urlparse(final_url).path, re.I)}
    if (len(title_radios) > 1 or len(path_radios) > 1 or
            (title_radios and path_radios and title_radios != path_radios)):
        return None
    radio = next(iter(title_radios or path_radios), None)
    if features[2] and radio and features[2] != radio:
        return None
    radio = radio or features[2]
    result = {'platform': 'MediaMarkt', 'seller_name': unescape(seller_name),
              'seller_id': seller_id, 'price': price, 'currency': 'TRY',
              'availability': 'in_stock' if availability == 'InStock' else 'limited',
              'condition': 'new', 'source_title': unescape(title),
              'sku': sku, 'gtin13': gtin,
              'selected_variant_gtin13': selected_variant_gtin,
              'offer_id': offer_id, 'url': final_url,
              'ram_gb': capacities[0], 'storage_gb': capacities[1],
              'network_generation': radio}
    if require_gtin_variant:
        result['selected_variant_sku'] = selected_variant_sku
    return result
