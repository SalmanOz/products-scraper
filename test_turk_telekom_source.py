"""Offline identity, stock, cash-price and runner-audit checks."""

import unittest

from audit_turk_telekom_source import run_audit
from retailer_offer_refresh import RetailerOfferRefresher, _safe_url
from turk_telekom_source import (BASE, CATEGORY_URL, ROBOTS_URL,
                                TurkTelekomSource, _selected_offer, product_url)


URLS = (
    BASE + '/tr/products/samsung/samsung-galaxy-a57-5g?v=2692',
    BASE + '/tr/products/samsung/samsung-galaxy-s26?v=2630',
    BASE + '/tr/products/xiaomi/xiaomi-17t?v=2761',
)
TITLES = ('SAMSUNG GALAXY A57 5G 8GB/128GB BUZ MAVİSİ',
          'SAMSUNG GALAXY S26 12/256GB Siyah', 'XIAOMI 17T 12/256GB MOR')
CAPACITIES = ('8GB/128GB', '12/256GB', '12/256GB')
PRICES = ('33.499', '75.599', '54.999')
IDS = ('500000031250', '500000030373', '500000031712')


class Response:
    def __init__(self, url, body, status=200):
        self.url, self.status_code = url, status
        self.data = body.encode()
        self.headers = {'Content-Length': str(len(self.data))}

    def iter_content(self, chunk_size):
        for start in range(0, len(self.data), chunk_size):
            yield self.data[start:start + chunk_size]

    def close(self):
        pass


class Fetch:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def __call__(self, url, **_kwargs):
        self.calls.append(url)
        if url not in self.pages:
            raise AssertionError(f'Unexpected request: {url}')
        return self.pages[url]


def page(url, title, capacity, price, offer_id, *, cash_selected=True):
    model = title.split(' 8GB/')[0].split(' 12/')[0]
    selected = url.rsplit('=', 1)[-1]
    selected_price = (f'<input type="radio" name="price_list" value="{offer_id}" checked>'
                      if cash_selected else '')
    return f'''<html><head><title>{model} - Türk Telekom | eMağaza</title>
      <link rel="canonical" href="{url.split('?')[0]}"></head><body>
      <h1></h1><h1>{model}</h1>
      <div class="product-variation-flag" data-product-variation-id="{selected}"></div>
      <form class="commerce-order-item-add-to-cart-form" action="{url}">
        <input type="radio" name="{capacity}" checked>{selected_price}
      </form><div class="title-and-product-name"><p class="product-name">{title}</p></div>
      <div class="payment-group"><h5 class="group-title">Peşin Ödeme</h5>
        <div class="payment-row"><div class="cell-title">Kredi Kartı ile Peşin</div>
          <div class="cell-price"><span class="price">{price} <span>TL</span></span></div>
          <button class="paymentSelectBtn" data-offer-id="{offer_id}">Seç</button>
        </div></div>
      <div class="payment-group"><h5 class="group-title">Türk Telekom Faturaya Ek - Taksitli</h5>
        <div class="payment-row"><div class="cell-title">Faturaya Ek 12 Ay</div>
          <div class="cell-price"><span class="price">1.999 TL / Ay</span></div>
          <button class="paymentSelectBtn" data-offer-id="123">Seç</button>
        </div></div></body></html>'''


def fixture(*, status=200):
    robots = 'User-agent: *\nDisallow: /admin/\n'
    cards = ''.join(f'<a class="dls-device-card" href="{url}">'
                    f'<h3 class="dls-device-card-heading-text">{title}</h3></a>'
                    for url, title in zip(URLS, TITLES))
    # Ambiguous A16 4/128 must not map to the catalog's explicit 4G row.
    cards += ('<a class="dls-device-card" href="/tr/products/samsung/samsung-galaxy-a16?v=1814">'
              '<h3 class="dls-device-card-heading-text">SAMSUNG GALAXY A16 4GB/128GB</h3></a>')
    pages = {ROBOTS_URL: Response(ROBOTS_URL, robots)}
    for n in range(3):
        url = CATEGORY_URL + (f'&page={n}' if n else '')
        pages[url] = Response(url, '<html><h1>Akıllı Telefon</h1>' + (cards if n == 0 else '') + '</html>')
    for url, title, capacity, price, offer_id in zip(URLS, TITLES, CAPACITIES, PRICES, IDS):
        pages[url] = Response(url, page(url, title, capacity, price, offer_id), status if url == URLS[1] else 200)
    fetch = Fetch(pages)
    return TurkTelekomSource(get=fetch, sleep=lambda _: None), fetch


class TurkTelekomSourceTests(unittest.TestCase):
    def test_selected_url_requires_single_numeric_v(self):
        self.assertTrue(product_url(URLS[0]))
        self.assertIsNotNone(_safe_url(URLS[0]))
        for url in (URLS[0].split('?')[0], URLS[0] + '&v=2691',
                    URLS[0] + '&token=x', URLS[0].replace('?v=2692', '?v=abc'),
                    URLS[0].replace('/tr/products/', '/tr/akilli-telefon/')):
            self.assertFalse(product_url(url))
            self.assertIsNone(_safe_url(url))

    def test_selected_cash_offer_excludes_installment_and_checks_variant(self):
        html = page(URLS[0], TITLES[0], CAPACITIES[0], PRICES[0], IDS[0])
        self.assertEqual(_selected_offer(html, URLS[0])['price'], 33499)
        self.assertIsNone(_selected_offer(html, URLS[0].replace('2692', '2691')))
        self.assertIsNone(_selected_offer(html.replace('8GB/128GB', '8GB/256GB', 1), URLS[0]))
        self.assertIsNone(_selected_offer(html.replace('33.499', '4.299 TL / Ay'), URLS[0]))
        self.assertIsNone(_selected_offer(html.replace(f'data-offer-id="{IDS[0]}"', 'data-offer-id="999"'), URLS[0]))

    def test_current_capacity_select_and_price_radio_must_agree_on_variant(self):
        html = page(URLS[1], TITLES[1], CAPACITIES[1], PRICES[1], IDS[1])
        html = html.replace(
            '<input type="radio" name="12/256GB" checked>',
            '<select name="purchased_entity[0][attributes][attribute_capacity]">'
            '<option value="100" selected>12/256GB</option>'
            '<option value="101">12/512GB</option></select>'
            '<input type="radio" id="selected-price-1" '
            'name="purchased_entity[0][attributes][field_price]" checked>'
            '<label for="selected-price-1">SAMSUNG GALAXY S26 12/256GB</label>',
        )
        self.assertEqual(_selected_offer(html, URLS[1])['price'], 75599)
        self.assertIsNone(_selected_offer(
            html.replace('<option value="100" selected>12/256GB</option>',
                         '<option value="100" selected>12/512GB</option>'), URLS[1]))
        self.assertIsNone(_selected_offer(
            html.replace('<label for="selected-price-1">SAMSUNG GALAXY S26 12/256GB</label>',
                         '<label for="selected-price-1">SAMSUNG GALAXY S26 12/512GB</label>'), URLS[1]))
        self.assertIsNone(_selected_offer(
            html.replace('<option value="101">12/512GB</option>',
                         '<option value="101" selected>12/512GB</option>'), URLS[1]))

    def test_out_of_stock_or_disabled_cash_control_is_rejected(self):
        html = page(URLS[0], TITLES[0], CAPACITIES[0], PRICES[0], IDS[0])
        self.assertIsNone(_selected_offer(html + '<div class="alert-warning">Ürün stoklarımızda bulunmamaktadır.</div>', URLS[0]))
        self.assertIsNone(_selected_offer(html.replace('>Seç</button>', ' disabled>Seç</button>', 1), URLS[0]))
        self.assertIsNone(_selected_offer(html.replace('Peşin Ödeme', 'Faturaya Ek'), URLS[0]))

    def test_stock_cards_discover_only_exact_variant_and_reuse_category(self):
        source, fetch = fixture()
        specs = {'ram_gb': 8, 'storage_gb': 128, 'network_generation': '5g'}
        self.assertEqual(source.discover('Samsung Galaxy A57 5G', specs), [{'source_url': URLS[0]}])
        self.assertEqual(source.discover('Samsung Galaxy A57 5G', specs), [{'source_url': URLS[0]}])
        self.assertEqual(source.discover('Samsung Galaxy A16 4G',
                                         {'ram_gb': 4, 'storage_gb': 128, 'network_generation': '4g'}), [])
        self.assertEqual(source.report()['category_requests'], 2)
        self.assertEqual(fetch.calls, [CATEGORY_URL, CATEGORY_URL + '&page=1'])

    def test_read_only_runner_audit_verifies_three_and_reports_access_failure(self):
        source, _ = fixture()
        report = run_audit(source)
        self.assertEqual(report['status'], 'passed')
        self.assertEqual([row['verified_offers'][0]['price'] for row in report['products']],
                         [33499, 75599, 54999])
        blocked_source, _ = fixture(status=403)
        blocked = run_audit(blocked_source)
        self.assertEqual(blocked['status'], 'partial')
        self.assertEqual(blocked['products'][1]['source_attempts'][0]['reason'], 'http_403')

    def test_retailer_refresher_uses_exact_selected_pdp_and_records_attempt(self):
        source, fetch = fixture()
        reader = RetailerOfferRefresher(turk_telekom_source=source)
        specs = {'ram_gb': 8, 'storage_gb': 128, 'network_generation': '5g'}
        offers = reader.refresh_existing_offer_targets(
            'Samsung Galaxy A57 5G', [{'source_url': URLS[0]}], specs)
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]['price'], 33499)
        self.assertEqual(offers[0]['merchant'], 'Türk Telekom eMağaza')
        self.assertEqual(reader.target_report('Samsung Galaxy A57 5G', [URLS[0]], specs)[0]['reason'],
                         'observed_exact_cash_offer')
        self.assertEqual(fetch.calls, [URLS[0]])


if __name__ == '__main__':
    unittest.main()
