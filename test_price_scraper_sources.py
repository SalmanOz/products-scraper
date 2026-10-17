"""Offline regression tests for direct retailer markup and search URLs."""

import json
from unittest.mock import patch

from bs4 import BeautifulSoup

from tr_price_scraper import TRPriceScraper


SOURCE_FIXTURES = {
    'Trendyol': """
        <div class="product-card">
          <span class="product-name">Samsung Galaxy A16 4 GB 128 GB Cep Telefonu</span>
          <span class="sale-price">9.899 TL</span>
          <a href="/samsung-galaxy-a16-p-1">Ürün</a>
        </div>
    """,
    'n11': """
        <a class="product-item" href="/urun/samsung-galaxy-a16-1">
          <h2 class="product-item-title">Samsung Galaxy A16 4 GB 128 GB</h2>
          <h3 class="price-currency">10.419,90 TL</h3>
        </a>
    """,
    'PttAVM': """
        <article class="article__abc">
          <a class="card__abc" href="/samsung-galaxy-a16-p-1">
            <h2 class="name__abc">Samsung Galaxy A16 128 GB 4 GB Ram Siyah</h2>
            <div class="price__abc"><span>11.249</span><span> TL</span></div>
          </a>
        </article>
    """,
    'MediaMarkt': """
        <article data-test="mms-product-card">
          <a href="/tr/product/_samsung-galaxy-a16.html">Ürün</a>
          <h2 data-test="product-title">SAMSUNG Galaxy A16 4 GB 128 GB Akıllı Telefon</h2>
          <div data-test="mms-price"><span>₺11.749,–</span></div>
        </article>
    """,
    'Pasaj': """
        <a class="m-p-pc-new" href="/pasaj/samsung-galaxy-a16">
          <h3 class="m-p-pc-new__title">Samsung Galaxy A16 4 GB 128 GB</h3>
          <div class="m-p-pc-new__price">11.749 TL</div>
        </a>
    """,
}


def test_current_source_markup():
    scraper = TRPriceScraper()
    expected_specs = {'ram_gb': 4, 'storage_gb': 128}
    failures = []

    for site_name, html in SOURCE_FIXTURES.items():
        offer = scraper.parse_site_offer(
            site_name,
            scraper.site_configs[site_name],
            html,
            'Samsung Galaxy A16 4G',
            expected_specs,
        )
        if not offer:
            failures.append(f'{site_name}: current markup produced no offer')
        elif offer['price'] <= 5000:
            failures.append(f"{site_name}: invalid parsed price {offer['price']}")
        elif not offer['url'].startswith('https://'):
            failures.append(f"{site_name}: relative URL was not normalized")

    assert not failures, '\n' + '\n'.join(failures)


def test_search_urls():
    scraper = TRPriceScraper()
    pasaj_url = scraper._build_search_url(
        scraper.site_configs['Pasaj'], 'Samsung Galaxy A16'
    )
    vatan_url = scraper._build_search_url(
        scraper.site_configs['Vatan Bilgisayar'], 'Samsung Galaxy A16'
    )

    assert pasaj_url == (
        'https://www.turkcell.com.tr/pasaj/search?qx=Samsung+Galaxy+A16'
    )
    assert vatan_url == (
        'https://www.vatanbilgisayar.com/arama/Samsung%20Galaxy%20A16/'
    )


def test_wrong_capacity_offer_is_rejected():
    scraper = TRPriceScraper()
    html = SOURCE_FIXTURES['n11'].replace('4 GB 128 GB', '8 GB 256 GB')
    offer = scraper.parse_site_offer(
        'n11',
        scraper.site_configs['n11'],
        html,
        'Samsung Galaxy A16 4G',
        {'ram_gb': 4, 'storage_gb': 128},
    )
    assert offer is None


def test_direct_retailer_forwards_observed_title_not_search_name():
    scraper = TRPriceScraper()
    offer = scraper.parse_site_offer(
        'n11', scraper.site_configs['n11'], SOURCE_FIXTURES['n11'],
        'Samsung Galaxy A16 4G', {'ram_gb': 4, 'storage_gb': 128},
    )
    assert offer['source_title'] == 'Samsung Galaxy A16 4 GB 128 GB'


def test_generic_shopping_forwards_compact_observed_card_text():
    scraper = TRPriceScraper()
    soup = BeautifulSoup('''
        <article><span>Samsung Galaxy A16 4G 128GB Akıllı Telefon</span>
          <span>Hepsiburada.com</span><span>12.999 TL</span>
          <a href="https://www.hepsiburada.com/galaxy-a16-p-1">Mağazaya git</a>
        </article>
    ''', 'html.parser')
    offers = scraper._parse_shopping_cards_generic(soup, 'Samsung Galaxy A16 4G')
    assert len(offers) == 1
    assert offers[0]['source_title'] == (
        'Samsung Galaxy A16 4G 128GB Akıllı Telefon Hepsiburada.com 12.999 TL Mağazaya git'
    )


def test_google_cards_require_a_nonempty_matching_observed_title():
    for title, expected in [
        ('', False), ('Samsung Galaxy A17 128GB Cep Telefonu', False),
        ('SAMSUNG Galaxy A16 4G 128GB Cep Telefonu', True),
    ]:
        scraper = TRPriceScraper()
        html = f'''
            <g-inner-card><h3>{title}</h3><span class="lmQWe">12.999 TL</span>
              <span class="WJMUdc">Teknosa</span>
              <a href="https://www.teknosa.com/galaxy-a16-p-1">Mağazaya git</a>
            </g-inner-card>
        '''
        class Response:
            def json(self):
                return {'status': 'ok', 'solution': {
                    'response': html, 'url': 'https://www.google.com/search?udm=28',
                }}
        with patch('tr_price_scraper.requests.post', return_value=Response()):
            offers = scraper.get_google_shopping_price('Samsung Galaxy A16 4G')
        assert bool(offers) is expected
        if expected:
            assert offers[0]['source_title'] == title


def test_akakce_forwards_only_offer_specific_titles():
    scraper = TRPriceScraper()
    data = {'name': 'Parent aggregate product name must not be merchant evidence',
            'offers': {'offers': [
                {'url': 'https://www.teknosa.com/phone', 'price': 12999,
                 'seller': {'name': 'Teknosa'},
                 'name': 'Samsung Galaxy A16 4G 128GB Siyah'},
                {'url': 'https://www.trendyol.com/phone', 'price': 13099,
                 'seller': {'name': 'Trendyol'},
                 'itemOffered': {'name': 'Samsung Galaxy A16 4G 128GB Mavi'}},
                {'url': 'https://www.n11.com/phone', 'price': 13199,
                 'seller': {'name': 'n11'}},
            ]}}
    html = '<h1>Samsung Galaxy A16</h1><script type="application/ld+json">' + json.dumps(data) + '</script>'
    with patch.object(scraper, 'get_via_flaresolverr', return_value={
        'response': html, 'url': 'https://www.akakce.com/phone.html',
    }):
        offers = scraper.get_akakce_price('Samsung Galaxy A16 4G')
    by_merchant = {offer['merchant']: offer for offer in offers}
    assert by_merchant['Teknosa']['source_title'] == 'Samsung Galaxy A16 4G 128GB Siyah'
    assert by_merchant['Trendyol']['source_title'] == 'Samsung Galaxy A16 4G 128GB Mavi'
    assert 'source_title' not in by_merchant['n11']


def test_akakce_html_forwards_actual_row_title():
    scraper = TRPriceScraper()
    html = '''<h1>Parent aggregate model</h1><div>
        <span>12.999 TL</span><span class="product-name">Samsung Galaxy A16 128GB Siyah</span>
        <a rel="nofollow" href="https://www.teknosa.com/phone"><img alt="Teknosa"></a>
    </div>'''
    with patch.object(scraper, 'get_via_flaresolverr', return_value={
        'response': html, 'url': 'https://www.akakce.com/phone.html',
    }):
        offers = scraper.get_akakce_price('Samsung Galaxy A16 4G')
    assert offers[0]['source_title'] == 'Samsung Galaxy A16 128GB Siyah'


def test_epey_parent_title_is_not_forwarded_as_merchant_title():
    scraper = TRPriceScraper()
    html = '''<h1>Samsung Galaxy A16 4G 128GB</h1>
        <a class="git" data-link="https://www.teknosa.com/phone" title="Teknosa mağazasına git">
          <span class="urun_fiyat">12.999 TL</span>
        </a>'''
    with patch.object(scraper, 'get_via_flaresolverr', return_value=html):
        offers = scraper.get_epey_price('Samsung Galaxy A16 4G')
    assert len(offers) == 1
    assert 'source_title' not in offers[0]


if __name__ == '__main__':
    test_current_source_markup()
    print(f'✅ All {len(SOURCE_FIXTURES)} retailer markup fixtures passed')
    test_search_urls()
    print('✅ Search URL regressions passed')
    test_wrong_capacity_offer_is_rejected()
    print('✅ Wrong-capacity source offer was rejected')
    test_direct_retailer_forwards_observed_title_not_search_name()
    test_generic_shopping_forwards_compact_observed_card_text()
    test_google_cards_require_a_nonempty_matching_observed_title()
    test_akakce_forwards_only_offer_specific_titles()
    test_akakce_html_forwards_actual_row_title()
    test_epey_parent_title_is_not_forwarded_as_merchant_title()
    print('✅ Observed source title forwarding and aggregator provenance passed')
