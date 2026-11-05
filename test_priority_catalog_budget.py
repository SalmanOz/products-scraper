import unittest

from priority_catalog_budget import priority_catalog_source_budgets
from priority_retailer_transport import PriorityRetailerTransport
from test_pasaj_source import Response


class BudgetTests(unittest.TestCase):
    def test_trendyol_native_tail_page_indexes_do_not_expand_the_shared_100_request_quota(self):
        calls = []
        def get(url, **_):
            calls.append(url)
            return Response(url, 'User-agent: *\nAllow: /\n' if url.endswith('robots.txt') else '<h1>Phone category</h1>')
        source = PriorityRetailerTransport(get=get, sleep=lambda _: None, maximum_per_host=1)
        base = 'https://www.trendyol.com/cep-telefonu-x-c103498'
        for page in range(101, 201):
            self.assertEqual(source.read_trendyol_category(base + f'?pi={page}')[1], 'document_read')
        self.assertEqual(source.read_trendyol_category(base + '?pi=201')[1], 'category_budget_exhausted')
        self.assertEqual(source.read_trendyol_category(base + '?pi=1000')[1], 'invalid_catalog_category')
        self.assertEqual(len(calls), 101)  # One robots GET, exactly 100 category GETs.
        self.assertEqual(source.report()['Trendyol']['category_requests'], 100)

    def test_expanded_observed_hb_category_budget_stays_bounded_and_preserves_product_quota(self):
        from hepsiburada_catalog_discovery import CATEGORY_URL, MAX_CATEGORY_PAGES
        calls = []
        def get(url, **_):
            calls.append(url)
            return Response(url, 'User-agent: *\nAllow: /\n' if url.endswith('robots.txt') else '<h1>Public</h1>')
        source = PriorityRetailerTransport(get=get, sleep=lambda _: None, maximum_per_host=1)
        self.assertEqual(MAX_CATEGORY_PAGES, 80)
        for page in range(1, 81):
            url = CATEGORY_URL + (f'?sayfa={page}' if page > 1 else '')
            self.assertEqual(source.read_hepsiburada_category(url)[1], 'document_read')
        self.assertEqual(source.read_hepsiburada_category(CATEGORY_URL + '?sayfa=81')[1], 'category_budget_exhausted')
        self.assertEqual(len(calls), 81)  # The 81st category was not requested; robots was.
        pdp = 'https://www.hepsiburada.com/samsung-galaxy-a54-128gb-p-HBCV0000TEST01'
        self.assertEqual(source.read_document(pdp)[1], 'document_read')
        self.assertEqual(source.read_document(pdp + '2')[1], 'pdp_budget_exhausted')
        self.assertEqual(source.report()['Hepsiburada']['category_requests'], 80)
        self.assertEqual(source.report()['Hepsiburada']['pdp_requests'], 1)

    def test_all_catalog_targets_and_same_product_redirects_have_room_after_discovery(self):
        products = [{'offer_targets': [
            {'source_url': f'https://www.trendyol.com/samsung/galaxy-a54-128gb-p-{100000 + model * 3 + color}'}
            for color in range(2)]} for model in range(110)]
        budgets = priority_catalog_source_budgets(products)
        self.assertEqual(budgets['www.trendyol.com'], 400)
        source = PriorityRetailerTransport(get=lambda url, **_: Response(url,
            'User-agent: *\nAllow: /\n' if url.endswith('robots.txt') else '<h1>Phone</h1>'), sleep=lambda _: None)
        source.configure_catalog_budget(budgets['www.trendyol.com'])
        for product in products:
            for target in product['offer_targets']:
                self.assertEqual(source.read_document(target['source_url'])[1], 'document_read')
        self.assertEqual(source.report()['Trendyol']['pdp_requests'], 220)

    def test_seller_hints_share_one_document_and_invalid_targets_do_not_inflate_the_budget(self):
        url = 'https://www.teknosa.com/samsung-galaxy-a54-128gb-p-100000123456'
        products = [{'offer_targets': [{'source_url': url}, {'source_url': url + '?shopId=1234'},
            {'source_url': url + '?shopId=5678'}, {'source_url': 'https://evil.test/phone'},
            {'source_url': url + '?token=private'}]}]
        self.assertEqual(priority_catalog_source_budgets(products)['www.teknosa.com'], 2)
        self.assertEqual(priority_catalog_source_budgets(products)['www.hepsiburada.com'], 1)


if __name__ == '__main__':
    unittest.main()
