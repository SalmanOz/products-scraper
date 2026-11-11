import unittest
from unittest.mock import patch

from tws_lead_audit import audit, collect_leads, identify


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

    def test_catalog_target_counts_only_if_reverified_on_the_same_barcode(self):
        name = 'Sony WF-1000XM6'
        gtin = '4548736171770'
        lead = 'https://www.n11.com/urun/sony-wf-1000xm6-12345678'
        target = 'https://www.mediamarkt.com.tr/tr/product/_sony-wf-1000xm6-siyah-1256567.html'
        snapshot = {'models': [{'name': name, 'gtin': gtin, 'offers': [{'url': target, 'price': 10}]}]}
        state = {'products': {'x': {'title': name, 'category': 'tws', 'retailerLeads': [lead]}}}

        def collect(host, rows, statuses, *, discover, document_reader=None):
            url = rows[0][1]['url']
            statuses[url] = 'verified'
            return {name: [{'url': url, 'merchant': host, 'platform': host, 'gtin13': gtin,
                            'price': 25499.0, '_legal_name': host}]}, {}

        with patch('tws_lead_audit.reread', side_effect=collect):
            variants = audit(state, snapshot=snapshot)['verified'][0]['verifiedVariants']
        self.assertEqual([r['gtin13'] for r in variants], [gtin])
        self.assertTrue(variants[0]['hasNewTarget'])
        self.assertEqual([o['price'] for o in variants[0]['offers']], [25499.0, 25499.0])
