import unittest

from tws_lead_audit import collect_leads, identify


class EarbudLeadAuditTest(unittest.TestCase):
    def test_known_earbud_model_uses_exact_family(self):
        self.assertEqual(identify('Sony WF-1000XM6'), 'Sony WF-1000XM6')
        self.assertIsNone(identify('Sony WH-1000XM6'))
        self.assertIsNone(identify('Sony WF-1000XM6 Yenilenmiş'))

    def test_canonical_links_with_conflicting_owners_do_not_get_read(self):
        url = 'https://www.n11.com/urun/sony-wf-1000xm6-12345678'
        first = {'title': 'Sony WF-1000XM6', 'category': 'tws', 'retailerLeads': [url]}
        second = {**first, 'title': 'Sony WF-1000XM5'}
        self.assertEqual(collect_leads({'products': {'a': first, 'b': second}}), {})
        self.assertEqual(collect_leads({'products': {'a': first}}),
                         {'www.n11.com': [('Sony WF-1000XM6', {'url': url})]})
