import unittest

from audit_marketplace_seller_budget import audit, summary
from offer_ingestion_client import IngestionClientError


class CatalogClient:
    def __init__(self, first_page, complete=None, inspection=None):
        self.first_page = first_page
        self.complete = complete
        self.inspection = inspection
        self.calls = []

    def get_published_products(self):
        self.calls.append('catalog_get')
        return [self.first_page]

    def get_complete_offer_targets(self, product_id):
        self.calls.append('complete_get')
        if self.complete is None:
            raise IngestionClientError('complete target reader unavailable')
        return self.complete

    def get_offer_target_inspection(self, product_id):
        self.calls.append('inspection_get')
        if self.inspection is None:
            raise IngestionClientError('single page unavailable')
        return self.inspection


class SellerBudgetAuditTests(unittest.TestCase):
    def setUp(self):
        self.first_seller = [
            {'source_url': f'https://www.mediamarkt.com.tr/tr/product/phone-blue-{n}.html',
             'seller_name': 'Güzel Teknoloji'}
            for n in range(11)
        ]
        self.other_store = {
            'source_url': 'https://www.vatanbilgisayar.com/phone.html',
            'seller_name': 'Vatan Bilgisayar',
        }
        self.second_seller = {
            'source_url': 'https://www.mediamarkt.com.tr/tr/product/phone-black.html',
            'seller_name': 'Başaran Ticaret',
        }
        self.product = {
            'id': 999, 'slug': 'other-phone', 'name': 'Other Phone',
            'attributes': {}, 'offer_targets': [*self.first_seller, self.other_store],
            'offer_targets_truncated': True,
        }

    def test_complete_catalog_reveals_second_seller_omitted_from_writer_budget(self):
        targets = [*self.product['offer_targets'], self.second_seller]
        client = CatalogClient(self.product, {
            'id': 999, 'slug': 'other-phone', 'name': 'Other Phone',
            'offer_targets': targets, 'offer_targets_total': len(targets),
        })

        report = audit(client)

        self.assertEqual(report['status'], 'complete')
        self.assertFalse(report['writes'])
        self.assertEqual(report['products_with_omitted_sellers'], 1)
        self.assertEqual(report['omitted_seller_groups'], 1)
        self.assertEqual(report['rows'][0]['omitted_sellers'], [{
            'merchant': 'MediaMarkt', 'seller': 'Başaran Ticaret',
            'source_url': self.second_seller['source_url'],
        }])
        self.assertEqual(client.calls, ['catalog_get', 'complete_get'])
        self.assertIn('omitted named seller groups 1', summary(report))

    def test_present_sellers_are_not_reported_as_omitted(self):
        product = {**self.product,
                   'offer_targets': [self.first_seller[0], self.other_store,
                                     self.second_seller],
                   'offer_targets_truncated': False}
        client = CatalogClient(product)

        report = audit(client)

        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['omitted_seller_groups'], 0)
        self.assertEqual(report['rows'], [])
        self.assertEqual(client.calls, ['catalog_get'])

    def test_incomplete_catalog_never_infers_missing_sellers(self):
        client = CatalogClient(self.product)

        report = audit(client)

        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['omitted_seller_groups'], 0)
        self.assertEqual(report['rows'], [])
        self.assertEqual(report['incomplete_products'], [{
            'slug': 'other-phone', 'reason': 'target_pages_unavailable',
        }])
        self.assertIn('Incomplete other-phone: target_pages_unavailable',
                      summary(report))
        self.assertEqual(client.calls, ['catalog_get', 'complete_get',
                                        'inspection_get'])

    def test_historical_invalid_row_can_use_complete_safe_single_page(self):
        safe_targets = [self.first_seller[0], self.other_store,
                        self.second_seller]
        product = {**self.product, 'offer_targets': safe_targets}
        client = CatalogClient(product, inspection={
            'id': 999, 'slug': 'other-phone', 'name': 'Other Phone',
            'offer_targets': safe_targets,
            'offer_targets_total': len(safe_targets),
        })

        report = audit(client)

        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['complete_products'], 1)
        self.assertEqual(report['omitted_seller_groups'], 0)
        self.assertEqual(client.calls, ['catalog_get', 'complete_get',
                                        'inspection_get'])


if __name__ == '__main__':
    unittest.main()
