import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from source_ingestion import CatalogProduct
from repair_classifications import run


class ClassificationRepairTests(unittest.TestCase):
    def fixture(self):
        client=Mock(base_url='https://teknoskor.com')
        client._json_body.side_effect=lambda response:response
        candidate={'slug':'xiaomi-17t','name':'Xiaomi 17T','fields':[],'expected_groups':{'Design & Materials':'a'*64}}
        manifest={'contract':'classification-repair-v1','products':[candidate]}
        client._request_with_retry.return_value=manifest
        return client,manifest

    def test_audit_is_get_only_and_never_calls_catalog_schema_or_sources_writes(self):
        client,_=self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            report=run(client,report_path=Path(directory)/'report.json')
        self.assertEqual(report['changed_products'],0)
        client._request_with_retry.assert_called_once_with('GET','https://teknoskor.com/api/ingestion/classification-repair',authenticated=True)
        client.fetch_catalog.assert_not_called()

    def test_apply_validates_then_commits_and_verifies_quality(self):
        client,manifest=self.fixture()
        client._request_with_retry.side_effect=[manifest,{'eligible':True,'committed':False},
            {'committed':True,'changed_fields':['/Design & Materials/Repairability'],'preserved_source_records':5}]
        client.fetch_catalog.return_value=[CatalogProduct(id=1,name='Xiaomi 17T',slug='xiaomi-17t',attributes={'Design & Materials':{'Repairability':'Class C'}},data_quality_status='verified')]
        batch={'reviewed_on':'2026-10-19','products':[{'slug':'xiaomi-17t','name':'Xiaomi 17T',
            'readings':[{'group':'Design & Materials','key':'Repairability','value':'Class C'}]}]}
        with tempfile.TemporaryDirectory() as directory:
            report=run(client,apply=True,batch=batch,report_path=Path(directory)/'report.json')
        self.assertEqual(report['changed_fields'],1)
        self.assertEqual(report['results'][0]['status'],'verified')
        self.assertEqual([call.args[0] for call in client._request_with_retry.call_args_list],['GET','POST','POST'])

    def test_missing_reviewed_batch_cannot_write(self):
        client,_=self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                run(client,apply=True,report_path=Path(directory)/'report.json')
        self.assertEqual(client._request_with_retry.call_count,1)
