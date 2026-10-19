"""Offline regression tests for direct retailer markup and search URLs."""

import asyncio
import json
from unittest.mock import patch

from bs4 import BeautifulSoup

from tr_price_scraper import TRPriceScraper


SOURCE_FIXTURES = {
    'Trendyol': """
        <div class="product-card">
          <span class="product-name">Samsung Galaxy A16 4G 4 GB RAM 128 GB Cep Telefonu</span>
          <span class="sale-price">9.899 TL</span>
          <a href="/samsung-galaxy-a16-p-1">Ürün</a>
        </div>
    """,
    'n11': """
        <a class="product-item" href="/urun/samsung-galaxy-a16-1">
          <h2 class="product-item-title">Samsung Galaxy A16 4G 4 GB RAM 128 GB</h2>
          <h3 class="price-currency">10.419,90 TL</h3>
        </a>
    """,
    'PttAVM': """
        <article class="article__abc">
          <a class="card__abc" href="/samsung-galaxy-a16-p-1">
            <h2 class="name__abc">Samsung Galaxy A16 4G 128 GB 4 GB Ram Siyah</h2>
            <div class="price__abc"><span>11.249</span><span> TL</span></div>
          </a>
        </article>
    """,
    'MediaMarkt': """
        <article data-test="mms-product-card">
          <a href="/tr/product/_samsung-galaxy-a16.html">Ürün</a>
          <h2 data-test="product-title">SAMSUNG Galaxy A16 4G 4 GB RAM 128 GB Akıllı Telefon</h2>
          <div data-test="mms-price"><span>₺11.749,–</span></div>
        </article>
    """,
    'Pasaj': """
        <a class="m-p-pc-new" href="/pasaj/samsung-galaxy-a16">
          <h3 class="m-p-pc-new__title">Samsung Galaxy A16 4G 4 GB RAM 128 GB</h3>
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
    html = SOURCE_FIXTURES['n11'].replace('4 GB RAM 128 GB', '8 GB RAM 256 GB')
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
    assert offer['source_title'] == 'Samsung Galaxy A16 4G 4 GB RAM 128 GB'


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
    html = '''<h1>Samsung Galaxy A16 4G</h1><div>
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


def test_observed_evidence_fails_closed_and_preserves_radio_fallback():
    scraper = TRPriceScraper()
    specs = {'ram_gb': 4, 'storage_gb': 128}
    def candidate(title, path='phone'):
        return {'merchant': 'Teknosa', 'price': 12999, 'source_title': title,
                'url': f'https://www.teknosa.com/{path}'}
    assert scraper.has_observed_offer_evidence('Samsung Galaxy A16 4G',
        candidate('Samsung Galaxy A16 4G 4GB RAM 128GB'), specs)
    for title in [None, '', 'Samsung Galaxy A16 4GB RAM 128GB',
                  'Samsung Galaxy A16 5G 4GB RAM 128GB',
                  'Samsung Galaxy A16 4G 128GB',
                  'Samsung Galaxy A16 4G 4GB RAM 128/256GB',
                  'Samsung Galaxy A16 4G 4GB RAM 128GB veya 256GB',
                  'Samsung Galaxy A16 4G 4GB RAM + 4GB virtual RAM 128GB',
                  'Samsung Galaxy A16 4G 4GB+4GB RAM 128GB',
                  'Samsung Galaxy A16 4G 4/8GB RAM 128GB',
                  'Samsung Galaxy A16 4G 4GB RAM 128GB Yenilenmiş']:
        assert not scraper.has_observed_offer_evidence('Samsung Galaxy A16 4G', candidate(title), specs), title
    for path in ['samsung-galaxy-a16-256-gb', 'samsung-galaxy-a16-5g-128gb',
                 'samsung-galaxy-a17-128gb', 'samsung-galaxy-a16-pro-128gb',
                 'xiaomi-a16-128gb', 'samsung-galaxy-a16-kilif']:
        assert not scraper.has_observed_offer_evidence('Samsung Galaxy A16 4G',
            candidate('Samsung Galaxy A16 4G 4GB RAM 128GB', path), specs), path
    assert not scraper.has_observed_offer_evidence('Samsung Galaxy A16 4G',
        candidate('Samsung Galaxy A16 4G 4GB RAM 128GB'), {})


def test_nested_network_profile_and_iphone_ram_exception():
    from update_prices import PriceUpdater
    scraper = TRPriceScraper()
    attrs = {'ram_gb': 8, 'storage_gb': 128, 'Connectivity': {'5G': 'n1 (2100), n78 (3500)'}}
    specs = PriceUpdater.get_expected_specs(json.dumps(attrs))
    assert specs['network_generation'] == '5g'
    for value in [True, 'Yes', ['n1 (2100)', 'n78 (3500)']]:
        assert PriceUpdater.get_expected_specs({'Connectivity': {'5G': value}})['network_generation'] == '5g'
    for value in [False, 'Hayır', 'not supported']:
        assert PriceUpdater.get_expected_specs({'Connectivity': {'5G': value}})['network_generation'] == '4g'
    assert 'network_generation' not in PriceUpdater.get_expected_specs({'Connectivity': {'5G': '5G'}})
    listing = {'merchant': 'Teknosa', 'price': 12999, 'url': 'https://www.teknosa.com/phone'}
    assert scraper.has_observed_offer_evidence('Xiaomi Redmi Note 13 Pro',
        {**listing, 'source_title': 'Xiaomi Redmi Note 13 Pro 5G 8/128GB'}, specs)
    assert not scraper.has_observed_offer_evidence('Xiaomi Redmi Note 13 Pro',
        {**listing, 'source_title': 'Xiaomi Redmi Note 13 Pro 4G 8/128GB'}, specs)
    assert not scraper.has_observed_offer_evidence('Xiaomi Redmi Note 13 Pro',
        {**listing, 'source_title': 'Xiaomi Redmi Note 13 Pro 8/128GB'}, specs)
    apple = {'ram_gb': 8, 'storage_gb': 128}
    assert scraper.has_observed_offer_evidence('Apple iPhone 16 Pro',
        {**listing, 'source_title': 'Apple iPhone 16 Pro 128GB'}, apple)
    for title in ['Apple iPhone 16 Pro 12GB RAM 128GB', 'Apple iPhone 16 Pro 8+8GB RAM 128GB',
                  'Apple iPhone 16 Pro 8GB virtual RAM 128GB', 'Apple iPhone 16 Pro 12GB 128GB',
                  'Apple iPhone 16 Pro 128/256GB', 'Apple iPhone 16 Pro Max 128GB']:
        assert not scraper.has_observed_offer_evidence('Apple iPhone 16 Pro', {**listing, 'source_title': title}, apple), title


def test_direct_source_invalid_cheaper_destination_cannot_shadow_correct_offer():
    scraper = TRPriceScraper()
    correct = SOURCE_FIXTURES['n11']
    wrong = correct.replace('10.419,90', '6.000').replace('/urun/samsung-galaxy-a16-1', '/urun/samsung-galaxy-a16-256-gb')
    result = scraper.parse_site_offer('n11', scraper.site_configs['n11'], wrong + correct,
        'Samsung Galaxy A16 4G', {'ram_gb': 4, 'storage_gb': 128})
    assert result['price'] == 10419.90
    assert result['url'].endswith('/urun/samsung-galaxy-a16-1')


def test_akakce_wrong_variant_headings_and_offer_titles_fail_closed():
    scraper = TRPriceScraper()
    base = {'offers': {'offers': []}, 'hasVariant': [
        {'name': 'Samsung Galaxy A17 4G', 'offers': {'offers': [
            {'url': 'https://www.teknosa.com/wrong', 'price': 6000,
             'seller': {'name': 'Teknosa'}, 'name': 'Samsung Galaxy A16 4G 4/128GB'}]}},
        {'name': 'Samsung Galaxy A16 4G', 'offers': {'offers': [
            {'url': 'https://www.teknosa.com/also-wrong', 'price': 6500,
             'seller': {'name': 'Teknosa'}, 'name': 'Samsung Galaxy A17 4G 4/128GB'},
            {'url': 'https://www.teknosa.com/correct', 'price': 12999,
             'seller': {'name': 'Teknosa'}, 'name': 'Samsung Galaxy A16 4G 4/128GB'}]}},
    ]}
    html = '<h1>Samsung Galaxy A16 4G</h1><script type="application/ld+json">' + json.dumps(base) + '</script>'
    with patch.object(scraper, 'get_via_flaresolverr', return_value={'response': html, 'url': 'https://www.akakce.com/phone.html'}):
        rows = scraper.get_akakce_price('Samsung Galaxy A16 4G', {'ram_gb': 4, 'storage_gb': 128})
    assert [row['url'] for row in rows] == ['https://www.teknosa.com/correct']
    with patch.object(scraper, 'get_via_flaresolverr', return_value={'response': html.replace('Samsung Galaxy A16 4G</h1>', 'Samsung Galaxy A17 4G</h1>'), 'url': 'https://www.akakce.com/phone.html'}):
        assert scraper.get_akakce_price('Samsung Galaxy A16 4G') is None


def test_epey_wrong_direct_model_and_unobserved_offers_never_become_commercial():
    scraper = TRPriceScraper()
    html = '''<h1>Samsung Galaxy A17 4G</h1><a class="git" data-link="https://www.teknosa.com/phone" title="Teknosa"><span class="urun_fiyat">12.999 TL</span></a>'''
    with patch.object(scraper, 'get_via_flaresolverr', side_effect=[html, None]):
        assert scraper.get_epey_price('Samsung Galaxy A16 4G') is None
    assert not scraper.has_observed_offer_evidence('Samsung Galaxy A16 4G',
        {'merchant': 'Teknosa', 'price': 12999, 'url': 'https://www.teknosa.com/phone'},
        {'ram_gb': 4, 'storage_gb': 128})


def test_invalid_google_bargain_is_filtered_before_merchant_collapse():
    scraper = TRPriceScraper()
    def card(title, price, url):
        return f'''<g-inner-card><h3>{title}</h3><span class="lmQWe">{price} TL</span><span class="WJMUdc">Teknosa</span><a href="{url}">Mağazaya git</a></g-inner-card>'''
    html = card('Samsung Galaxy A16 4G 4/128GB', '6.000', 'https://www.teknosa.com/samsung-galaxy-a16-256gb') + card('Samsung Galaxy A16 4G 4/128GB', '12.999', 'https://www.teknosa.com/correct')
    class Response:
        def json(self):
            return {'status': 'ok', 'solution': {'response': html, 'url': 'https://www.google.com/search?udm=28'}}
    with patch('tr_price_scraper.requests.post', return_value=Response()):
        rows = scraper.get_google_shopping_price('Samsung Galaxy A16 4G', {'ram_gb': 4, 'storage_gb': 128})
    assert len(rows) == 1 and rows[0]['price'] == 12999


def test_akakce_equal_price_invalid_destination_cannot_win_dedupe():
    scraper = TRPriceScraper()
    title = 'Samsung Galaxy A16 4G 4/128GB'
    data = {'offers': {'offers': [
        {'url': 'https://www.teknosa.com/samsung-galaxy-a16-256gb', 'price': 12999,
         'seller': {'name': 'Teknosa'}, 'name': title},
        {'url': 'https://www.teknosa.com/correct', 'price': 12999,
         'seller': {'name': 'Teknosa'}, 'name': title},
    ]}}
    html = '<h1>Samsung Galaxy A16 4G</h1><script type="application/ld+json">' + json.dumps(data) + '</script>'
    with patch.object(scraper, 'get_via_flaresolverr', return_value={'response': html, 'url': 'https://www.akakce.com/phone.html'}):
        rows = scraper.get_akakce_price('Samsung Galaxy A16 4G', {'ram_gb': 4, 'storage_gb': 128})
    assert [row['url'] for row in rows] == ['https://www.teknosa.com/correct']


def test_invalid_merchant_count_does_not_suppress_fallback_or_correct_same_seller():
    scraper = TRPriceScraper()
    specs = {'ram_gb': 4, 'storage_gb': 128}
    bad = [{'merchant': merchant, 'price': 6000, 'url': 'https://shop.example/phone'}
           for merchant in ['Teknosa', 'n11', 'Trendyol', 'MediaMarkt']]
    correct = {'merchant': 'Teknosa', 'price': 12999, 'url': 'https://www.teknosa.com/correct',
               'source_title': 'Samsung Galaxy A16 4G 4/128GB'}
    async def no_offer(*args, **kwargs):
        return None
    with patch.object(scraper, 'get_google_shopping_price', return_value=bad), \
         patch.object(scraper, 'get_epey_price', return_value=None) as epey, \
         patch.object(scraper, 'get_akakce_price', return_value=[correct]) as akakce, \
         patch.object(scraper, 'scrape_site_async', new=no_offer):
        rows = asyncio.run(scraper.get_best_prices('Samsung Galaxy A16 4G', specs))
    epey.assert_called_once(); akakce.assert_called_once()
    assert rows == [correct]


def test_multiple_phone_models_are_not_verified_by_one_matching_model_token():
    scraper = TRPriceScraper()
    specs = {'ram_gb': 12, 'storage_gb': 128}
    for title in ['Samsung Galaxy S25 / S24 12GB RAM 128GB',
                  'Samsung Galaxy S25 veya S24 12GB RAM 128GB',
                  'Samsung Galaxy S25 S26 12/128GB']:
        listing = {'source_title': title, 'url': 'https://shop.example/phone'}
        assert not scraper.is_strict_match('Samsung Galaxy S25', title, specs), title
        assert not scraper.has_observed_offer_evidence('Samsung Galaxy S25', listing, specs), title
    assert not scraper.is_strict_match('Xiaomi Redmi Note 13 Pro',
        'Xiaomi Redmi Note 13 Pro veya Note 14 Pro 5G 8/128GB', {'ram_gb': 8, 'storage_gb': 128})


def test_repeated_matching_capacity_labels_do_not_make_a_conventional_pair_ambiguous():
    scraper = TRPriceScraper()
    specs = {'ram_gb': 12, 'storage_gb': 128}
    for title in ['Samsung Galaxy S25 12/128GB 12GB RAM',
                  'Samsung Galaxy S25 12GB RAM 12/128GB',
                  'Samsung Galaxy S25 12/128GB RAM 12GB',
                  'Samsung Galaxy S25 12/128GB 128GB ROM 12GB RAM']:
        listing = {'source_title': title, 'url': 'https://shop.example/phone'}
        # A RAM label immediately after a pair (third case) still describes an
        # ambiguous group; do not broaden that existing conservative rule.
        expected = '12/128GB RAM 12GB' not in title
        assert scraper.has_observed_offer_evidence('Samsung Galaxy S25', listing, specs) is expected, title
    for title in ['Samsung Galaxy S25 12/128GB 8GB RAM',
                  'Samsung Galaxy S25 12/128GB 256GB ROM',
                  'Samsung Galaxy S25 RAM 6/12GB 128GB']:
        assert not scraper.has_observed_offer_evidence('Samsung Galaxy S25',
            {'source_title': title, 'url': 'https://shop.example/phone'}, specs), title


def test_explicit_cross_family_galaxy_choices_do_not_establish_one_phone():
    scraper = TRPriceScraper()
    specs = {'ram_gb': 12, 'storage_gb': 128}
    for choice in ['S25 / A17', 'A17 / S25', 'S25 veya A17',
                   'S25 ya da Galaxy A17', 'S25 or Samsung Galaxy A17']:
        title = f'Samsung Galaxy {choice} 12GB RAM 128GB'
        assert not scraper.is_strict_match('Samsung Galaxy S25', title, specs), title
        assert not scraper.has_observed_offer_evidence('Samsung Galaxy S25',
            {'source_title': title, 'url': 'https://shop.example/phone'}, specs), title


def test_cpu_descriptions_and_unrelated_sku_tokens_are_not_phone_choices():
    scraper = TRPriceScraper()
    assert scraper.has_observed_offer_evidence('Apple iPhone 15',
        {'source_title': 'Apple iPhone 15 128GB A16 Bionic İşlemci', 'url': 'https://shop.example/phone'},
        {'ram_gb': 8, 'storage_gb': 128})
    for description in ['SKU A17', 'Snapdragon 8 Elite']:
        title = f'Samsung Galaxy S25 12GB RAM 128GB {description}'
        assert scraper.has_observed_offer_evidence('Samsung Galaxy S25',
            {'source_title': title, 'url': 'https://shop.example/phone'},
            {'ram_gb': 12, 'storage_gb': 128}), title


def test_redmi_note_15_pro_requires_observed_radio_for_separate_4g_and_5g_variants():
    scraper = TRPriceScraper()
    specs = {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'}
    for radio, expected in [('5G ', True), ('', False), ('4G ', False)]:
        title = f'Xiaomi Redmi Note 15 Pro {radio}8/256GB'
        assert scraper.has_observed_offer_evidence('Xiaomi Redmi Note 15 Pro',
            {'source_title': title, 'url': 'https://shop.example/phone'}, specs) is expected, title


def test_explicit_screen_parts_cannot_become_redmi_note_13_pro_phone_offers():
    scraper = TRPriceScraper()
    specs = {'ram_gb': 8, 'storage_gb': 128, 'network_generation': '5g'}
    for part in ['Çıtalı Orjinal Dokunmatik Ekran', 'Orijinal Dokunmatik Ekran']:
        title = f'Xiaomi Redmi Note 13 Pro 5G 8/128GB {part}'
        assert not scraper.is_strict_match('Xiaomi Redmi Note 13 Pro', title, specs), part
        assert not scraper.has_observed_offer_evidence('Xiaomi Redmi Note 13 Pro',
            {'source_title': title, 'url': 'https://shop.example/redmi-note-13-pro-citali-orjinal-dokunmatik-ekran'}, specs)
    # Legacy missing titles already fail closed at the source-side guard.
    assert not scraper.has_observed_offer_evidence('Xiaomi Redmi Note 13 Pro',
        {'source_title': None, 'url': 'https://shop.example/redmi-note-13-pro-citali-orjinal-dokunmatik-ekran'}, specs)


def test_trendyol_split_brand_and_model_use_only_same_card_literal_title_fragments():
    scraper = TRPriceScraper()
    html = '''<div class="product-card"><a href="/samsung-galaxy-s25-12gb-128gb-p-1">
        <span class="prdct-desc-cntnr-ttl">Samsung</span>
        <span class="prdct-desc-cntnr-name">Galaxy S25 12 GB RAM 128 GB Siyah</span>
        </a><span class="sale-price">29.999 TL</span></div>'''
    specs = {'ram_gb': 12, 'storage_gb': 128}
    offer = scraper.parse_site_offer('Trendyol', scraper.site_configs['Trendyol'], html,
                                    'Samsung Galaxy S25', specs)
    assert offer['source_title'] == 'Samsung Galaxy S25 12 GB RAM 128 GB Siyah'
    assert scraper.parse_site_offer('Trendyol', scraper.site_configs['Trendyol'],
                                   html.replace('>Samsung</span>', '>Apple</span>'),
                                   'Samsung Galaxy S25', specs) is None


def test_akakce_titleless_json_does_not_suppress_observed_dom_offer_title():
    scraper = TRPriceScraper()
    data = {'name': 'Aggregate heading is not listing evidence', 'offers': {'offers': [
        {'url': 'https://www.teknosa.com/samsung-galaxy-s25-12gb-128gb', 'price': 29999,
         'seller': {'name': 'Teknosa'}},
    ]}}
    html = '<h1>Samsung Galaxy S25</h1><script type="application/ld+json">' + json.dumps(data) + '''</script>
        <div><span>29.999 TL</span>
          <span class="product-name">Samsung Galaxy S25 12GB RAM 128GB Siyah</span>
          <a rel="nofollow" href="https://www.teknosa.com/samsung-galaxy-s25-12gb-128gb"><img alt="Teknosa"></a>
        </div>'''
    with patch.object(scraper, 'get_via_flaresolverr', return_value={
        'response': html, 'url': 'https://www.akakce.com/phone.html',
    }):
        offers = scraper.get_akakce_price('Samsung Galaxy S25', {'ram_gb': 12, 'storage_gb': 128})
    assert len(offers) == 1
    assert offers[0]['source_title'] == 'Samsung Galaxy S25 12GB RAM 128GB Siyah'


def test_akakce_direct_offer_and_graph_shapes_use_offer_name_never_parent_product_name():
    scraper = TRPriceScraper()
    listing = {'@type': 'Offer', 'url': 'https://www.teknosa.com/samsung-galaxy-s25-12gb-128gb',
               'price': 29999, 'seller': {'name': 'Teknosa'},
               'name': 'Samsung Galaxy S25 12GB RAM 128GB Siyah'}
    parent = {'@type': 'Product', 'name': 'Never copy this parent heading', 'offers': listing}
    for data in [parent, {'@graph': [parent]}, [parent], {**parent, 'offers': [listing]}]:
        html = '<h1>Samsung Galaxy S25</h1><script type="application/ld+json">' + json.dumps(data) + '</script>'
        with patch.object(scraper, 'get_via_flaresolverr', return_value={
            'response': html, 'url': 'https://www.akakce.com/phone.html',
        }):
            offers = scraper.get_akakce_price('Samsung Galaxy S25', {'ram_gb': 12, 'storage_gb': 128})
        assert len(offers) == 1
        assert offers[0]['source_title'] == listing['name']
    no_title = {key: value for key, value in listing.items() if key != 'name'}
    data = {'name': listing['name'], 'offers': no_title}
    html = '<h1>Samsung Galaxy S25</h1><script type="application/ld+json">' + json.dumps(data) + '</script>'
    with patch.object(scraper, 'get_via_flaresolverr', return_value={
        'response': html, 'url': 'https://www.akakce.com/phone.html',
    }):
        assert not scraper.get_akakce_price('Samsung Galaxy S25', {'ram_gb': 12, 'storage_gb': 128})


def test_epey_keeps_same_merchant_destinations_until_actual_pdp_match():
    scraper = TRPriceScraper()
    specs = {'ram_gb': 12, 'storage_gb': 128}
    wrong = 'https://www.teknosa.com/samsung-galaxy-s25-12gb-256gb'
    correct = 'https://www.teknosa.com/samsung-galaxy-s25-12gb-128gb'
    html = f'''<h1>Samsung Galaxy S25</h1>
        <a class="git" data-link="{wrong}" title="Teknosa mağazasına git"><span class="urun_fiyat">15.999 TL</span></a>
        <a class="git" data-link="{correct}" title="Teknosa mağazasına git"><span class="urun_fiyat">29.999 TL</span></a>'''
    with patch.object(scraper, 'get_via_flaresolverr', return_value=html):
        raw = scraper.get_epey_price('Samsung Galaxy S25', specs)
    assert [offer['url'] for offer in raw] == [wrong, correct]
    assert all('source_title' not in offer for offer in raw)

    class Refresher:
        def __init__(self):
            self.calls = []
        def refresh_existing_offer_targets(self, name, targets, expected):
            self.calls.append((name, targets, expected))
            return [{**offer, 'source_title': 'Samsung Galaxy S25 12GB RAM ' +
                     ('256GB' if offer['url'] == wrong else '128GB'),
                     'currency': 'TRY', 'availability': 'in_stock'} for offer in raw]
    refresher = scraper.retailer_offer_refresher = Refresher()
    with patch.object(scraper, 'get_google_shopping_price', return_value=None), \
         patch.object(scraper, 'get_epey_price', return_value=raw), \
         patch.object(scraper, 'get_akakce_price', return_value=None), \
         patch.object(scraper, 'scrape_site_async', return_value=None):
        result = asyncio.run(scraper.get_best_prices('Samsung Galaxy S25', specs))
    assert len(result) == 1 and result[0]['url'] == correct
    assert [target['source_url'] for target in refresher.calls[0][1]] == [wrong, correct]
    assert refresher.calls[0][2] == specs
    assert scraper.retailer_offer_refresher is refresher


def test_epey_parent_heading_or_old_aggregate_price_never_becomes_pdp_evidence():
    scraper = TRPriceScraper()
    raw = [{'merchant': 'Teknosa', 'url': 'https://www.teknosa.com/phone', 'price': 12000}]
    class NoEvidence:
        def refresh_existing_offer_targets(self, name, targets, expected):
            return []
    scraper.retailer_offer_refresher = NoEvidence()
    with patch.object(scraper, 'get_google_shopping_price', return_value=None), \
         patch.object(scraper, 'get_epey_price', return_value=raw), \
         patch.object(scraper, 'get_akakce_price', return_value=None), \
         patch.object(scraper, 'scrape_site_async', return_value=None):
        assert not asyncio.run(scraper.get_best_prices('Samsung Galaxy S25', {'ram_gb': 12, 'storage_gb': 128}))


def test_epey_pdp_stock_overrides_same_url_search_and_preserves_other_available_listing():
    scraper = TRPriceScraper()
    title = 'Samsung Galaxy S25 12GB RAM 128GB'
    source = {'merchant': 'Teknosa', 'price': 10000, 'source_title': title,
              'url': 'https://www.teknosa.com/phone-one'}
    other = {**source, 'price': 30000, 'url': 'https://www.teknosa.com/phone-two'}
    class StockEvidence:
        def refresh_existing_offer_targets(self, name, targets, expected):
            return [{**source, 'availability': 'out_of_stock'}]
    scraper.retailer_offer_refresher = StockEvidence()
    with patch.object(scraper, 'get_google_shopping_price', return_value=[source, other]), \
         patch.object(scraper, 'get_epey_price', return_value=[source]), \
         patch.object(scraper, 'get_akakce_price', return_value=None), \
         patch.object(scraper, 'scrape_site_async', return_value=None):
        result = asyncio.run(scraper.get_best_prices('Samsung Galaxy S25', {'ram_gb': 12, 'storage_gb': 128}))
    assert len(result) == 1 and result[0]['url'] == other['url']


def test_compact_unit_labelled_storage_and_ram_are_real_capacity_evidence():
    scraper = TRPriceScraper()
    for title in ['Samsung Galaxy S25 512GB12GB RAM', 'Samsung Galaxy S25 512GB12GBRAM']:
        assert scraper.has_observed_offer_evidence('Samsung Galaxy S25',
            {'source_title': title, 'url': 'https://shop.example/samsung-galaxy-s25-512gb12gb-ram'},
            {'ram_gb': 12, 'storage_gb': 512})
        assert not scraper.has_observed_offer_evidence('Samsung Galaxy S25',
            {'source_title': title, 'url': 'https://shop.example/phone'},
            {'ram_gb': 12, 'storage_gb': 256})
    assert not scraper.has_observed_offer_evidence('Samsung Galaxy S25',
        {'source_title': 'Samsung Galaxy S25 512GB12+12GB RAM', 'url': 'https://shop.example/phone'},
        {'ram_gb': 12, 'storage_gb': 512})


if __name__ == '__main__':
    test_compact_unit_labelled_storage_and_ram_are_real_capacity_evidence()
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
    test_observed_evidence_fails_closed_and_preserves_radio_fallback()
    test_nested_network_profile_and_iphone_ram_exception()
    test_direct_source_invalid_cheaper_destination_cannot_shadow_correct_offer()
    test_akakce_wrong_variant_headings_and_offer_titles_fail_closed()
    test_epey_wrong_direct_model_and_unobserved_offers_never_become_commercial()
    test_invalid_google_bargain_is_filtered_before_merchant_collapse()
    test_akakce_equal_price_invalid_destination_cannot_win_dedupe()
    test_invalid_merchant_count_does_not_suppress_fallback_or_correct_same_seller()
    test_multiple_phone_models_are_not_verified_by_one_matching_model_token()
    test_repeated_matching_capacity_labels_do_not_make_a_conventional_pair_ambiguous()
    test_explicit_cross_family_galaxy_choices_do_not_establish_one_phone()
    test_cpu_descriptions_and_unrelated_sku_tokens_are_not_phone_choices()
    test_redmi_note_15_pro_requires_observed_radio_for_separate_4g_and_5g_variants()
    test_explicit_screen_parts_cannot_become_redmi_note_13_pro_phone_offers()
    test_trendyol_split_brand_and_model_use_only_same_card_literal_title_fragments()
    test_akakce_titleless_json_does_not_suppress_observed_dom_offer_title()
    test_akakce_direct_offer_and_graph_shapes_use_offer_name_never_parent_product_name()
    test_epey_keeps_same_merchant_destinations_until_actual_pdp_match()
    test_epey_parent_heading_or_old_aggregate_price_never_becomes_pdp_evidence()
    test_epey_pdp_stock_overrides_same_url_search_and_preserves_other_available_listing()
    print('✅ Observed source title forwarding and aggregator provenance passed')
