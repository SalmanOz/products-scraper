"""Offline evidence and transport gates for the read-only Pasaj candidate queue."""

from copy import deepcopy
import unittest

from audit_pasaj_unreviewed_candidates import (
    PasajCandidateSource, _queue, clean_candidate_url, run_audit,
    selected_cash_evidence, summary,
)
from pasaj_source import ROBOTS_URL
from test_pasaj_source import Response, page


URL = ('https://www.turkcell.com.tr/pasaj/cep-telefonu/android-telefonlar/'
       'samsung-galaxy-a54-5g-8gb-128-gb')
OTHER = ('https://www.turkcell.com.tr/pasaj/cep-telefonu/android-telefonlar/'
         'samsung-galaxy-a55-5g-8-gb-128-gb')
QUERY = URL + '?seller=18461&color=1009371'
TITLE = 'Samsung Galaxy A54 5G 8 GB 128 GB'


def product(ident=1, slug='samsung-galaxy-a54-5g', name='Samsung Galaxy A54 5G',
            urls=(URL, QUERY)):
    return {'id': ident, 'slug': slug, 'name': name,
            'attributes': {'ram_gb': 8, 'storage_gb': 128,
                           'Connectivity': {'5G': True}},
            'offer_targets': [{'source_url': url} for url in urls]}


def selected_page(**changes):
    values = {'url': URL, 'title': TITLE, 'brand': 'Samsung', 'sku': 1159284,
              'ram': '8 GB', 'storage': '128 GB', 'network': '5G',
              'cash': '19.499', 'price': 19499}
    values.update(changes)
    html = page(**values)
    color = ('<select class="m-select-color"><option selected '
             'data-stock-available="true" data-at-basket="false" '
             'data-seller-name="Turkcell Satış A.Ş." data-price="19.499" '
             'data-device-product-uid="1159284-1-1004412">Beyaz'
             '</option></select>')
    return html.replace('<form class="m-form">',
                        '<form class="m-form">' + color)


class Client:
    def __init__(self, products=None, prices=None):
        self.products = products if products is not None else [product()]
        self.prices = prices if prices is not None else {1: ('samsung-galaxy-a54-5g', False)}
        self.writes = []

    def get_published_products(self):
        return deepcopy(self.products)

    def get_public_price_priority(self):
        return deepcopy(self.prices)


class Source:
    def __init__(self, html=None):
        self.html = html if html is not None else selected_page()
        self.urls = []

    def read_document(self, url):
        self.urls.append(url)
        return self.html, 'document_read', 200

    def report(self):
        return {'robots_status': 'allowed', 'pdp_requests': len(self.urls)}


class PasajCandidateTests(unittest.TestCase):
    def test_clean_url_rejects_query_redirect_and_encoded_paths(self):
        self.assertTrue(clean_candidate_url(URL))
        for url in (QUERY, URL + '#x', URL.replace('https:', 'http:'),
                    URL.replace('www.turkcell.com.tr', 'www.turkcell.com.tr.evil'),
                    URL.replace('www.turkcell.com.tr', 'www.turkcell.com.tr:444'),
                    URL.replace('galaxy', '%67alaxy'),
                    'https://www.turkcell.com.tr/pasaj/c/android-telefonlar'):
            with self.subTest(url=url):
                self.assertFalse(clean_candidate_url(url))

    def test_selected_document_requires_catalog_variant_cash_seller_stock_and_color(self):
        row = product()
        evidence = selected_cash_evidence(selected_page(), URL, row)
        self.assertEqual(evidence['status'], 'source_verified_pending_manual_review')
        mobile_heading = selected_page().replace('<h1>', '<h1></h1><h1>', 1)
        self.assertEqual(selected_cash_evidence(mobile_heading, URL, row)['status'],
                         'source_verified_pending_manual_review')
        self.assertEqual((evidence['sku'], evidence['cash_price_try'], evidence['seller']),
                         ('1159284', 19499.0, 'Turkcell Satış A.Ş.'))
        cases = (
            (selected_page(title=TITLE.replace('A54', 'A55')),
             'catalog_variant_unproven'),
            (selected_page(ram='6 GB'), 'catalog_variant_unproven'),
            (selected_page(storage='256 GB'), 'catalog_variant_unproven'),
            (selected_page(network='4G'), 'catalog_variant_unproven'),
            (selected_page(cash='18.499'), 'selected_cash_seller_color_or_basket_unproven'),
            (selected_page(seller='Other'), 'selected_first_party_stock_unproven'),
            (selected_page(availability='https://schema.org/OutOfStock'),
             'selected_first_party_stock_unproven'),
            (selected_page(button='Stokta Yok'),
             'selected_cash_seller_color_or_basket_unproven'),
            (selected_page(canonical=OTHER),
             'canonical_or_selected_title_unproven'),
            (selected_page().replace('data-stock-available="true"',
                                     'data-stock-available="false"'),
             'selected_cash_seller_color_or_basket_unproven'),
            (selected_page().replace('data-device-product-uid="1159284-',
                                     'data-device-product-uid="1159285-'),
             'selected_cash_seller_color_or_basket_unproven'),
        )
        for html, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(selected_cash_evidence(html, URL, row)['status'],
                                 expected)

    def test_queue_uses_exact_unpriced_identity_and_six_read_only_gets(self):
        rows = [product(i, f'samsung-galaxy-a54-5g-{i}',
                        'Samsung Galaxy A54 5G',
                        (URL.replace('a54-5g', f'a54-5g-{i}'),))
                for i in range(1, 10)]
        prices = {row['id']: (row['slug'], False) for row in rows}
        source = Source()
        report = run_audit(client=Client(rows, prices), source=source)
        self.assertEqual(report['status'], 'completed')
        self.assertFalse(report['writes'])
        self.assertEqual((report['candidate_urls'], report['probed'], len(source.urls)),
                         (9, 6, 6))
        self.assertEqual(report['source_verified'], 0)
        self.assertEqual(report['candidates'][0]['status'],
                         'canonical_or_selected_title_unproven')
        self.assertIn('No price, catalog, or score writes', summary(report))
        self.assertEqual(Client().writes, [])
        later = Source()
        second = run_audit(client=Client(rows, prices), source=later, offset=6)
        self.assertEqual((second['candidate_urls'], second['probed'], len(later.urls)),
                         (9, 3, 3))
        self.assertTrue(set(source.urls).isdisjoint(later.urls))

    def test_query_and_priced_targets_are_excluded_before_request(self):
        rows = [product(), product(2, 'samsung-galaxy-a55-5g',
                                   'Samsung Galaxy A55 5G', (OTHER,))]
        prices = {1: ('samsung-galaxy-a54-5g', False),
                  2: ('samsung-galaxy-a55-5g', True)}
        queue, ambiguous, excluded = _queue(rows, prices)
        self.assertEqual([url for url, _ in queue], [URL])
        self.assertEqual((ambiguous, excluded), ([], 1))
        source = Source()
        report = run_audit(client=Client(rows, prices), source=source)
        self.assertEqual(report['source_verified'], 1)
        self.assertEqual(source.urls, [URL])
        priced = run_audit(client=Client(rows, prices), source=Source(), priced=True)
        self.assertEqual([row['slug'] for row in priced['candidates']], ['samsung-galaxy-a55-5g'])

    def test_same_live_document_reaches_automatic_preparation_without_writes(self):
        report = run_audit(client=Client(), source=Source())
        self.assertFalse(report['writes'])
        self.assertEqual(report['automatic_prepared_offers'], 1)
        self.assertEqual(report['candidates'][0]['automatic_prepared'][0]['seller'], 'Pasaj')
        self.assertIn('automatic prepared offers: 1', summary(report))

    def test_same_clean_url_on_two_catalog_products_is_not_probed(self):
        rows = [product(), product(2, 'samsung-galaxy-a55-5g',
                                   'Samsung Galaxy A55 5G', (URL,))]
        prices = {1: ('samsung-galaxy-a54-5g', False),
                  2: ('samsung-galaxy-a55-5g', False)}
        source = Source()
        report = run_audit(client=Client(rows, prices), source=source)
        self.assertEqual((report['candidate_urls'], report['probed']), (0, 0))
        self.assertEqual(len(report['ambiguous_urls']), 1)
        self.assertEqual(source.urls, [])

    def test_unreviewed_transport_obeys_robots_redirect_and_six_pdp_cap(self):
        calls = []

        def get(url, **kwargs):
            calls.append((url, kwargs))
            if url == ROBOTS_URL:
                return Response(url, 'User-agent: *\nAllow: /pasaj\n',
                                content_type='text/plain')
            return Response(url, selected_page())

        source = PasajCandidateSource(get=get, sleep=lambda _delay: None)
        body, reason, status = source.read_document(URL)
        self.assertEqual((reason, status), ('document_read', 200))
        self.assertIn(TITLE, body)
        self.assertEqual([url for url, _ in calls], [ROBOTS_URL, URL])
        self.assertEqual(source.read_document(QUERY)[1], 'not_unreviewed_clean_pdp')
        self.assertEqual(len(calls), 2)
        for _ in range(5):
            self.assertEqual(source.read_document(URL)[1], 'document_read')
        self.assertEqual(source.read_document(URL)[1], 'pdp_budget_exhausted')
        self.assertEqual(source.report()['pdp_requests'], 6)
        self.assertTrue(all(not kwargs['allow_redirects'] and kwargs['stream']
                            for _, kwargs in calls))

        blocked = PasajCandidateSource(
            get=lambda url, **_kw: Response(
                url, 'User-agent: *\nDisallow: /pasaj\n',
                content_type='text/plain'), sleep=lambda _delay: None)
        self.assertEqual(blocked.read_document(URL)[1], 'robots_disallowed')
        self.assertEqual(blocked.report()['pdp_requests'], 0)

        def redirected(url, **_kwargs):
            if url == ROBOTS_URL:
                return Response(url, 'User-agent: *\nAllow: /pasaj\n',
                                content_type='text/plain')
            return Response(OTHER, selected_page(), status=302)

        changed = PasajCandidateSource(get=redirected, sleep=lambda _delay: None)
        self.assertEqual(changed.read_document(URL)[1], 'unexpected_final_url')

    def test_catalog_failure_does_not_leak_client_secret(self):
        class Broken(Client):
            def get_published_products(self):
                raise RuntimeError('bearer-secret-and-private-response')
        report = run_audit(client=Broken(), source=Source())
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['error'], 'RuntimeError')
        self.assertNotIn('bearer-secret', str(report))


if __name__ == '__main__':
    unittest.main()
