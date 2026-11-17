import copy
import json
from unittest import TestCase

from retail_catalog_format import decode_catalog, decode_product, encode_catalog, encode_product


class RetailCatalogFormatTest(TestCase):
    def test_lossless_roundtrip_groups_exact_metadata_without_promoting_undated_facts(self):
        product = {
            'gtin': '0197531690630', 'model': None,
            'specs': {'A': '1', 'B': '2', 'C': '3', 'D': '4'},
            'specSources': {
                'A': {'source': 'https://source.example/a', 'checkedAt': '2026-11-13T20:51:28+00:00'},
                'B': {'source': 'https://source.example/a', 'checkedAt': '2026-11-13T20:51:28+00:00'},
                'C': {'source': 'https://source.example/a'},
                'D': {'source': 'https://source.example/a', 'checkedAt': None},
            },
            'specConflicts': {'Other': [{'value': 'old', 'source': 'https://source.example/a'},
                                       {'value': 'new', 'source': 'https://source.example/b',
                                        'checkedAt': '2026-11-17T00:00:00+00:00'}]},
            'unknownFuture': {'value': None},
        }
        original = copy.deepcopy(product)
        encoded = encode_product(product)
        self.assertNotIn('specSources', encoded)
        self.assertEqual(len(encoded['specSourceGroups']), 3)
        self.assertEqual(encoded['specSourceGroups'][0]['fields'], ['A', 'B'])
        self.assertNotIn('checkedAt', encoded['specSourceGroups'][1])
        self.assertIsNone(encoded['specSourceGroups'][2]['checkedAt'])
        self.assertEqual(decode_product(json.loads(json.dumps(encoded))), original)
        self.assertEqual(product, original)

    def test_legacy_records_without_sources_empty_sources_and_null_are_unchanged(self):
        rows = [{'specs': {}}, {'specs': {}, 'specSources': {}}, {'specs': {}, 'specSources': None}, None]
        self.assertEqual(decode_catalog(encode_catalog(rows)), rows)
        self.assertEqual(decode_catalog({'scope': True}), {'scope': True})

    def test_explicit_legacy_field_overrides_group_and_unknown_metadata_roundtrips(self):
        row = {'specSourceGroups': [{'source': 'https://group.example', 'fields': ['A','B']}],
               'specSources': {'A': {'source': 'https://explicit.example', 'extra': {'unknown': None}}}}
        decoded = decode_product(row)
        self.assertEqual(decoded['specSources']['A']['source'], 'https://explicit.example')
        self.assertEqual(decoded['specSources']['B'], {'source': 'https://group.example'})
        self.assertEqual(decode_product(encode_product(decoded)), decoded)
        self.assertNotIn('specSources', encode_product(decoded))

    def test_encoding_is_idempotent_and_preserves_reserved_metadata(self):
        row = {'specSources': {'A': {'source': 'https://a.example', 'fields': ['future']}}}
        self.assertEqual(decode_product(encode_product(row)), row)
        ordinary = {'specSources': {'A': {'source': 'https://a.example'}}}
        self.assertEqual(encode_product(encode_product(ordinary)), encode_product(ordinary))

    def test_conflicting_groups_and_malformed_fields_fail_without_silent_evidence_loss(self):
        with self.assertRaises(ValueError):
            decode_product({'specSourceGroups': [{'source': 'https://a.example', 'fields': ['A']},
                                                {'source': 'https://b.example', 'fields': ['A']}]})
        with self.assertRaises(ValueError):
            decode_product({'specSourceGroups': [{'source': 'https://a.example', 'fields': [None]}]})
