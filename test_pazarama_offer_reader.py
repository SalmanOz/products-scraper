"""Offline selected-PDP ordinary-price contract for Pazarama."""

from copy import deepcopy
import json
import unittest

from bs4 import BeautifulSoup

from retailer_offer_refresh import RetailerOfferRefresher, _product_offer


URL = ('https://www.pazarama.com/samsung-galaxy-a26-5g-128-gb-6-gb-ram-siyah-'
       'cep-telefonu-samsung-turkiye-garantili-p-8806097100638')
TITLE = 'Samsung Galaxy A26 5G 128 GB 6 GB Ram Siyah Cep Telefonu'
SELLER = 'TURKCELL EMRE İLETİŞİM'
SPECS = {'ram_gb': 6, 'storage_gb': 128, 'network_generation': '5g'}


def schema():
    return {'@context': 'https://schema.org', '@type': 'Product', 'url': URL,
            'name': TITLE, 'sku': 'sample-selected-product',
            'brand': {'@type': 'Brand', 'name': 'Samsung'},
            'itemCondition': 'https://schema.org/NewCondition',
            'offers': {'@type': 'Offer', 'price': 19000, 'priceCurrency': 'TRY',
                       'availability': 'https://schema.org/InStock', 'seller': SELLER}}


def page(product=None, *, heading=TITLE, regular='20.000,00 TL',
         discount='Sepette 19.000,00 TL', seller=SELLER, cart=True,
         main_class='text-4xl', extra='', second_price=''):
    selected = product if product is not None else schema()
    special = (f'<div data-testid="product-card-discount">{discount}</div>'
               if discount is not None else '')
    button = ('<button data-testid="base-addbasket-button">Sepete Ekle</button>' if cart else '')
    return ('<html><div class="product-detail"><h1>' + heading + '</h1>'
            '<div class="w-full"><p class="' + main_class + '">' + regular + '</p>'
            + special + second_price + '</div>' + button
            + '<p><span>Satıcı:</span><span>' + seller + '</span></p>' + extra
            + '</div><script type="application/ld+json">'
            + json.dumps(selected, ensure_ascii=False) + '</script></html>')


def observed(html):
    return _product_offer(BeautifulSoup(html, 'html.parser'), URL)


class Response:
    def __init__(self, html):
        self.text, self.status_code, self.url, self.headers = html, 200, URL, {}


class PazaramaOfferReaderTests(unittest.TestCase):
    def test_four_reviewed_live_shapes_use_only_selected_visible_cash_price(self):
        cases = [
            (URL, TITLE, SELLER, '20.000,00 TL', 'Sepette 19.000,00 TL', 19000, 20000),
            ('https://www.pazarama.com/samsung-galaxy-a26-5g-128-gb-6-gb-ram-beyaz-'
             'cep-telefonu-samsung-turkiye-garantili-p-8806097100676',
             'Samsung Galaxy A26 5G 128 GB 6 GB Ram Beyaz Cep Telefonu',
             'Bittibitiyor', '20.199,00 TL', 'Sepette 18.987,06 TL', 18987.06, 20199),
            ('https://www.pazarama.com/samsung-galaxy-a37-5g-6gb128gb-antrasit-'
             'cep-telefonu-p-8806099054977',
             'Samsung Galaxy A37 5G 6GB/128GB Antrasit Cep Telefonu',
             'BİRTANE TELEKOM', '22.500,00 TL', 'Sepette 21.150,00 TL', 21150, 22500),
            ('https://www.pazarama.com/samsung-galaxy-s24-fe-gri-p-8806095750101',
             'Samsung Galaxy S24 FE Gri 128 GB 8 GB Ram Akıllı Telefon',
             'aradığınız herşey burada', '38.999,00 TL', None, 38999, 38999),
        ]
        for url, title, seller, visible, discount, schema_price, expected in cases:
            with self.subTest(url=url):
                product = schema()
                product.update(url=url, name=title)
                product['offers'].update(price=schema_price, seller=seller)
                html = page(product, heading=title, regular=visible,
                            discount=discount, seller=seller)
                result = _product_offer(BeautifulSoup(html, 'html.parser'), url)
                self.assertIsNotNone(result)
                self.assertEqual(result['price'], expected)
                if discount:
                    self.assertNotEqual(result['price'], schema_price)

    def test_sepette_schema_price_is_replaced_only_by_visible_ordinary_price(self):
        result = observed(page())
        self.assertEqual(result['merchant'], 'Pazarama')
        self.assertEqual(result['price'], 20000)
        self.assertEqual(result['availability'], 'in_stock')
        self.assertEqual(result['url'], URL)
        self.assertNotEqual(result['price'], schema()['offers']['price'])

    def test_discount_panel_alternate_layout_requires_matching_sticky_price(self):
        normal = '<p class="text-4xl">20.000,00 TL</p>'
        panel = '<div class="cursor-pointer"><p class="text-lg font-bold text-red-600">20.000,00 TL</p></div>'
        sticky = '<div class="fixed top-0"><p class="text-lg text-gray-600 leading-tight">20.000,00 TL</p></div>'
        document = page(extra=sticky).replace(normal, panel)
        self.assertEqual(observed(document)['price'], 20000)
        self.assertIsNone(observed(document.replace(sticky, '')))
        self.assertIsNone(observed(document.replace(sticky, sticky.replace('20.000', '22.000'))))
        self.assertIsNone(observed(document.replace(panel, panel.replace('font-bold', 'font-bold line-through'))))

    def test_non_cart_selected_offer_requires_schema_and_visible_price_equality(self):
        product = schema()
        product['offers']['price'] = 20000
        self.assertEqual(observed(page(product, discount=None))['price'], 20000)
        product['offers']['price'] = 19999
        self.assertIsNone(observed(page(product, discount=None)))

    def test_condition_is_only_the_explicit_sepette_discount_and_matches_jsonld(self):
        cases = [
            {'discount': 'Kuponla 19.000,00 TL'},
            {'discount': 'Üyelere Özel 19.000,00 TL'},
            {'discount': 'Sepette 18.000,00 TL'},
            {'discount': None},
            {'extra': '<span>Kuponla 18.000,00 TL</span>'},
            {'main_class': 'text-4xl line-through'},
            {'regular': '19.000,00 TL'},
            {'regular': '20.000,00 TL aylık'},
            {'second_price': '<p class="text-4xl">99.999,00 TL</p>'},
            {'cart': False},
            {'seller': 'Different seller'},
            {'heading': 'Samsung Galaxy A26 5G 256 GB 8 GB Ram'},
        ]
        for options in cases:
            with self.subTest(options=options):
                self.assertIsNone(observed(page(**options)))

    def test_product_offer_identity_condition_stock_and_currency_fail_closed(self):
        for label, mutation in [
            ('other_url', lambda p: p.update(url='https://www.pazarama.com/other-p-1')),
            ('used', lambda p: p.update(itemCondition='https://schema.org/UsedCondition')),
            ('out_of_stock', lambda p: p['offers'].update(availability='https://schema.org/OutOfStock')),
            ('wrong_seller', lambda p: p['offers'].update(seller='Other')),
            ('wrong_currency', lambda p: p['offers'].update(priceCurrency='USD')),
            ('member_offer', lambda p: p['offers'].update(validForMemberTier='Premium')),
            ('coupon_name', lambda p: p['offers'].update(name='Kuponla fiyat')),
            ('unreviewed_price_spec', lambda p: p['offers'].update(priceSpecification={'price': 20000})),
            ('aggregate', lambda p: p['offers'].update(**{'@type': 'AggregateOffer', 'lowPrice': 19000})),
        ]:
            with self.subTest(label=label):
                product = deepcopy(schema())
                mutation(product)
                self.assertIsNone(observed(page(product)))

    def test_existing_url_reader_rechecks_catalog_memory_and_model_suffix(self):
        html = page()
        reader = RetailerOfferRefresher(fetch=lambda *_args, **_kwargs: None)
        reader.pazarama_source.fetch = lambda *_args, **_kwargs: Response(html)
        valid = reader.refresh_existing_offer_targets('Samsung Galaxy A26 5G', [URL], SPECS)
        self.assertEqual([row['price'] for row in valid], [20000])
        self.assertEqual(reader.refresh_existing_offer_targets('Samsung Galaxy A26 5G', [URL], {
            'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g',
        }), [])
        self.assertEqual(reader.refresh_existing_offer_targets('Samsung Galaxy A27 5G', [URL], SPECS), [])


if __name__ == '__main__':
    unittest.main()
