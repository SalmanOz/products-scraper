import unittest

from priority_catalog_budget import priority_catalog_source_budgets
from priority_retailer_transport import PriorityRetailerTransport
from test_pasaj_source import Response


class BudgetTests(unittest.TestCase):
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
