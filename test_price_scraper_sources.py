"""Offline regression tests for direct retailer markup and search URLs."""

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


if __name__ == '__main__':
    test_current_source_markup()
    print(f'✅ All {len(SOURCE_FIXTURES)} retailer markup fixtures passed')
    test_search_urls()
    print('✅ Search URL regressions passed')
    test_wrong_capacity_offer_is_rejected()
    print('✅ Wrong-capacity source offer was rejected')
