import html
import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from automatic_memory_variants import (TechnicalVariantReader, PublicBrowserTechnicalReader, exact_capacity, selected_technical_document,
    technical_records, variant_identity, observed_variant_candidates, memory_variant_destinations,
    prepare_memory_variants, verify_variant_catalog_readback, memory_target_preflight, canonical_observation_time, CONTRACT)
from observed_memory_variants import inspect_trendyol_memory_variant
from test_observed_memory_variants import alternate_document, ALT_URL
from test_trendyol_catalog_source import NAME, SPECS

TECH_URL = 'https://www.kimovil.com/en/where-to-buy-samsung-galaxy-a54-5g'
ALT_TECH_URL = TECH_URL + 'global-8gb-256gb'
PARENT = {'id': 123, 'name': NAME, 'slug': 'samsung-galaxy-a54-5g', 'attributes': SPECS}


def tech_document(url=ALT_TECH_URL, ram=8, storage=256, name=NAME):
    sections = {'Technical sheet': {'Release date': 'March 2023'},
        'Design & Materials': {'Diagonal': '6.4 inches'},
        'Performance & Hardware': {'Memory RAM': f'{ram} GB', 'Capacity': f'{storage} GB',
                                    'Model': 'Exynos 1380', 'Nanometer': '5 nm'},
        'Camera': {'Resolution': '50 MP'}, 'Connectivity': {'5G': 'n78'},
        'Battery': {'Capacity': '5000 mAh'}, 'Software': {'OS': 'Android 13'}}
    meta = {'name': name, 'partials': {'hardware': 8.0, 'camera': 8.0, 'battery': 8.0, 'design': 8.0}}
    compare = {'name': name, 'image': 'https://cdn-files.kimovil.com/phone-a54_big.jpg'}
    body = ''.join('<section class="container-sheet-' + str(i) + '"><h2>' + title + '</h2><table class="k-dltable">' +
                   ''.join(f'<tr><th class="label">{key}</th><td class="value">{value}</td></tr>' for key, value in rows.items()) +
                   '</table></section>' for i, (title, rows) in enumerate(sections.items()))
    return (f'<html><head><title>{name}</title><link rel="canonical" href="{url}">' +
            '<meta name="deviceki" content="' + html.escape(json.dumps(meta), quote=True) + '">' +
            '<meta name="devicecompare" content="' + html.escape(json.dumps(compare), quote=True) + '">' +
            '</head><body>' + body + '</body></html>')


class AutomaticMemoryVariantTests(unittest.TestCase):
    def test_rendered_public_reader_is_scoped_anonymous_and_stops_after_denial(self):
        calls = []
        def response(body):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)
        def post(url, **kwargs):
            calls.append((url, kwargs))
            payload = kwargs['json']
            if payload['cmd'] == 'sessions.create':
                return response({'status': 'ok'})
            return response({'status': 'ok', 'solution': {'status': 403, 'url': payload['url'], 'response': 'Denied'}})
        reader = PublicBrowserTechnicalReader(post)
        for invalid in ['https://evil.test/specs', TECH_URL + '?token=x', TECH_URL.replace('https:', 'http:'),
                        'https://www.kimovil.com/_json/autocomplete_devicemodels_joined.json?device_type=0&name=A54&name=Other']:
            with self.assertRaisesRegex(ValueError, 'unapproved'):
                reader.read(invalid)
        self.assertEqual(calls, [])
        with self.assertRaisesRegex(ValueError, 'http_403'):
            reader.read(TECH_URL)
        with self.assertRaisesRegex(ValueError, 'blocked_for_run'):
            reader.read(ALT_TECH_URL)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0], 'http://localhost:8191/v1')
        self.assertEqual(set(calls[1][1]['json']), {'cmd', 'url', 'session', 'maxTimeout', 'waitInSeconds'})
        self.assertEqual(calls[1][1]['json']['waitInSeconds'], 5)
        self.assertLessEqual(calls[1][1]['timeout'], 60)

    def test_rendered_public_reader_rejects_changed_url_and_challenge_documents(self):
        for solution in [{'status': 200, 'url': ALT_TECH_URL, 'response': tech_document()},
                         {'status': 200, 'url': TECH_URL, 'response': '<title>Just a moment</title>'}]:
            def post(_url, **kwargs):
                body = {'status': 'ok'} if kwargs['json']['cmd'] == 'sessions.create' else {'status': 'ok', 'solution': solution}
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)
            with self.assertRaises(ValueError):
                PublicBrowserTechnicalReader(post).read(TECH_URL)

    def test_cached_technical_document_retains_its_real_fetch_time(self):
        class Reader:
            def read(self, url):
                if '_json/autocomplete' in url:
                    return json.dumps({'results': [{'full_name': NAME, 'url': TECH_URL.rsplit('where-to-buy-', 1)[1]}]})
                return tech_document(TECH_URL, storage=128)
        reader = TechnicalVariantReader(Reader())
        with patch('automatic_memory_variants.utc_observation_time', return_value='2026-11-04T06:00:00Z'):
            _, first = reader.locate(PARENT, SPECS)
        with patch('automatic_memory_variants.utc_observation_time', return_value='2026-11-04T06:05:00Z'):
            _, second = reader.locate(PARENT, SPECS)
        self.assertEqual(first['observed_at'], second['observed_at'])
        self.assertEqual(reader.documents, 2)

    def test_observation_serialization_preserves_the_fetched_instant_without_renewing_it(self):
        self.assertEqual(canonical_observation_time('2026-11-04T06:50:56.370424Z'), '2026-11-04T06:50:56.370Z')
        self.assertEqual(canonical_observation_time('2026-11-04T09:50:56.370424+03:00'), '2026-11-04T06:50:56.370Z')
        with self.assertRaisesRegex(ValueError, 'timezone_missing'):
            canonical_observation_time('2026-11-04T06:50:56')
    def test_preflight_derives_hints_from_current_catalog_and_cannot_write(self):
        parent = {**PARENT, 'offer_targets': [{'source_url': ALT_URL}, {'source_url': 'https://evil.test/8gb-256gb'}]}
        calls = []
        transport = SimpleNamespace(report=lambda: {})
        def inspect(_transport, url, product):
            calls.append((url, product['id']))
            return {'slug': product['slug'], 'other_memory_variant': self.candidate_result()['rows'][0]['other_memory_variant']}
        with patch('audit_priority_retailers.inspect_target', side_effect=inspect):
            report = memory_target_preflight([parent], transport)
        self.assertEqual(calls, [(ALT_URL, PARENT['id'])])
        self.assertEqual(report['observed_capacity_hints'], 1)
        self.assertEqual((report['publish'], report['writes'], report['accepted']), (False, False, 0))

    def test_readonly_variant_cli_rejects_every_writing_or_unrelated_mode_before_clients(self):
        from sweep_priority_catalog import main
        for flag in ('--publish', '--memory-variants', '--n11-preflight', '--coverage-first', '--hepsiburada-category-audit'):
            with patch('sys.argv', ['sweep', '--memory-variant-preflight', flag]), \
                 patch('sweep_priority_catalog.OfferIngestionClient.from_env') as client:
                with self.assertRaisesRegex(ValueError, 'read_only_and_separate'):
                    main()
                client.assert_not_called()

    def candidate_result(self):
        preview = inspect_trendyol_memory_variant(alternate_document(), ALT_URL, NAME, SPECS)
        return {'rows': [{'slug': PARENT['slug'], 'other_memory_variant': preview}]}

    def test_single_capacity_cell_and_selected_canonical_require_actual_memory_and_model(self):
        for cell, expected in [('8 GB', 8), ('8 GB LPDDR4X', 8), ('1 TB UFS 3.1', 1024),
                               ('6/8 GB', None), ('8 GB or 12 GB', None), ('8+8 GB', None),
                               ('LPDDR4X 8 GB', None), ('8 GB; 12 GB', None), ('8', None)]:
            self.assertEqual(exact_capacity(cell), expected, cell)
        specs = dict(SPECS, storage_gb=256)
        document = tech_document()
        proof = selected_technical_document(document, ALT_TECH_URL, NAME, specs)
        self.assertEqual(proof['ram_gb'], 8)
        self.assertEqual(proof['storage_gb'], 256)
        for original, replacement in [('8 GB', '6 GB'), ('256 GB', '128/256 GB'), ('n78', 'No'),
                                      (NAME, NAME.replace('A54', 'A55')), ('rel="canonical"', 'rel="alternate"')]:
            self.assertIn(original, document)
            self.assertIsNone(selected_technical_document(document.replace(original, replacement), ALT_TECH_URL, NAME, specs))
        self.assertIsNone(selected_technical_document(document, TECH_URL, NAME, specs))

    def test_shared_model_canonical_needs_exact_active_capacity_link_and_selected_metadata_slug(self):
        specs = dict(SPECS, storage_gb=256)
        document = tech_document().replace(f'href="{ALT_TECH_URL}"', f'href="{TECH_URL}"')
        old = html.escape(json.dumps({'name': NAME, 'image': 'https://cdn-files.kimovil.com/phone-a54_big.jpg'}), quote=True)
        new = html.escape(json.dumps({'name': NAME, 'slug': ALT_TECH_URL.rsplit('where-to-buy-', 1)[1],
                                      'image': 'https://cdn-files.kimovil.com/phone-a54_big.jpg'}), quote=True)
        document = document.replace(old, new).replace('</body>',
            f'<ul class="versions-list"><li class="item active"><a href="{ALT_TECH_URL}">8GB · 256GB</a></li></ul></body>')
        self.assertIsNone(selected_technical_document(document, ALT_TECH_URL, NAME, specs))
        self.assertIsNotNone(selected_technical_document(document, ALT_TECH_URL, NAME, specs, canonical_model_url=TECH_URL))
        for before, after in [('item active', 'item'),
                              ('&quot;slug&quot;', '&quot;other_slug&quot;'),
                              (f'href="{ALT_TECH_URL}"', f'href="{TECH_URL}"')]:
            self.assertIsNone(selected_technical_document(document.replace(before, after), ALT_TECH_URL, NAME, specs,
                                                         canonical_model_url=TECH_URL))
        self.assertIsNone(selected_technical_document(document, ALT_TECH_URL, NAME, specs, canonical_model_url=TECH_URL+'-pro'))

    def test_locates_only_observed_capacity_link_and_stages_fresh_technical_facts_not_parent_copy(self):
        base = tech_document(TECH_URL, storage=128).replace('</body>',
            '<ul class="versions-list"><li class="item"><a href="' + ALT_TECH_URL + '">Global 8 GB 256 GB</a></li>'
            '<li class="item"><a href="https://evil.test/8gb-256gb">8 GB 256 GB</a></li></ul></body>')
        visited = []
        class Reader:
            def read(self, url):
                visited.append(url)
                if '_json/autocomplete' in url:
                    return json.dumps({'results': [{'full_name': NAME, 'url': TECH_URL.rsplit('where-to-buy-', 1)[1]}]})
                return {TECH_URL: base, ALT_TECH_URL: tech_document()}[url]
        reader = TechnicalVariantReader(Reader())
        identity = variant_identity(PARENT, 8, 256)
        records, evidence, images = technical_records(PARENT, dict(SPECS, storage_gb=256), identity, reader)
        self.assertEqual(visited[1:], [TECH_URL, ALT_TECH_URL])
        self.assertEqual(len(visited), 3)
        self.assertEqual(evidence['source_url'], ALT_TECH_URL)
        values = {record['attribute_key']: record['value'] for record in records}
        self.assertEqual((values['ram_gb'], values['storage_gb']), (8, 256))
        self.assertNotIn('parent_only_claim', values)
        self.assertEqual(images, ['https://cdn-files.kimovil.com/phone-a54_big.jpg'])
        self.assertTrue(all(row['product_slug'] == identity['slug'] for row in records))

    def test_observed_capacity_path_or_price_panel_fragment_is_only_a_hint_for_fresh_selected_proof(self):
        target = TECH_URL + '-512gb'
        base = tech_document(TECH_URL, storage=128).replace('</body>',
            f'<ul class="versions-list"><li class="item"><a href="{target}#open-prices">No prices</a></li></ul></body>')
        calls = []
        class Reader:
            def read(self, url):
                calls.append(url)
                if '_json/autocomplete' in url:
                    return json.dumps({'results': [{'full_name': NAME, 'url': TECH_URL.rsplit('where-to-buy-', 1)[1]}]})
                return {TECH_URL: base, target: tech_document(target, ram=12, storage=512)}[url]
        specs = dict(SPECS, ram_gb=12, storage_gb=512)
        _, proof = TechnicalVariantReader(Reader()).locate(PARENT, specs)
        self.assertEqual(proof['source_url'], target)
        self.assertEqual((proof['ram_gb'], proof['storage_gb']), (12, 512))
        self.assertEqual(calls[-1], target)
        self.assertNotIn('#', calls[-1])
        class WrongReader(Reader):
            def read(self, url):
                return tech_document(target, ram=8, storage=512) if url == target else super().read(url)
        with self.assertRaisesRegex(ValueError, 'not_observed'):
            TechnicalVariantReader(WrongReader()).locate(PARENT, specs)

    def test_non_anchor_version_card_uses_only_its_declared_public_target(self):
        base = tech_document(TECH_URL, storage=128).replace('</body>',
            f'<ul class="versions-list"><li class="item" data-url="{ALT_TECH_URL}">8GB · 256GB</li></ul></body>')
        class Reader:
            def read(self, url):
                if '_json/autocomplete' in url:
                    return json.dumps({'results': [{'full_name': NAME, 'url': TECH_URL.rsplit('where-to-buy-', 1)[1]}]})
                return {TECH_URL: base, ALT_TECH_URL: tech_document()}[url]
        _, proof = TechnicalVariantReader(Reader()).locate(PARENT, dict(SPECS, storage_gb=256))
        self.assertEqual(proof['source_url'], ALT_TECH_URL)

    def test_two_observed_radio_aliases_can_use_the_bare_model_only_with_actual_4g_facts(self):
        base = 'https://www.kimovil.com/en/where-to-buy-xiaomi-redmi-note-15'
        parent = {'id': 456, 'name': 'Xiaomi Redmi Note 15 4G', 'slug': 'xiaomi-redmi-note-15-4g',
                  'attributes': {'ram_gb': 6, 'storage_gb': 128, 'Connectivity': {'5G': False}}}
        calls = []
        class Reader:
            def read(self, url):
                calls.append(url)
                if '_json/autocomplete' in url:
                    return json.dumps({'results': [
                        {'full_name': 'Xiaomi Redmi Note 15 4G', 'url': 'xiaomi-redmi-note-15-4g'},
                        {'full_name': 'Xiaomi Redmi Note 15', 'url': 'xiaomi-redmi-note-15'}]})
                self_outer.assertEqual(url, base)
                return tech_document(base, name='Xiaomi Redmi Note 15').replace('n78', 'No')
        self_outer = self
        reader = TechnicalVariantReader(Reader())
        specs = {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '4g'}
        _, proof = reader.locate(parent, specs)
        self.assertEqual(proof['source_url'], base)
        self.assertTrue(reader.diagnostics[0]['observed_radio_aliases'])
        self.assertEqual(len(calls), 2)
        with self.assertRaises(ValueError):
            TechnicalVariantReader(Reader()).locate(parent, dict(specs, network_generation='5g'))

    def test_discovery_deduplicates_existing_variants_and_never_uses_other_memory_as_parent_price(self):
        result = self.candidate_result()
        result['rows'] *= 2
        before = deepcopy(PARENT)
        candidates = observed_variant_candidates(result, [PARENT])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['targets'], [ALT_URL])
        self.assertEqual(PARENT, before)
        existing = {**PARENT, 'id': 124, **variant_identity(PARENT, 8, 256), 'attributes': dict(SPECS, storage_gb=256)}
        self.assertEqual(observed_variant_candidates(result, [PARENT, existing]), [])
        alias = {**existing, 'name': NAME.replace(' 5G', '') + ' 8GB 256GB',
                 'attributes': {'ram_gb': 8, 'storage_gb': 256, 'Connectivity': {'5G': True}}}
        self.assertEqual(observed_variant_candidates(result, [PARENT, alias]), [])
        result['rows'][0]['other_memory_variant']['offer']['merchant'] = 'Different seller'
        self.assertEqual(observed_variant_candidates(result, [PARENT]), [])

    def test_category_cards_route_only_unambiguous_same_model_new_capacities_without_prices(self):
        cards = [{'source_url': ALT_URL, 'discovery_title': NAME + ' 256 GB 8 GB RAM', 'stock_priority': True}]
        found = memory_variant_destinations(cards, [PARENT])
        self.assertEqual(found, [(PARENT, cards[0])])
        for changes in [{'discovery_title': NAME + ' Pro 256 GB 8 GB RAM'},
                        {'discovery_title': NAME + ' 128/256 GB 8 GB RAM'},
                        {'discovery_title': NAME.replace('A54', 'A55') + ' 256 GB 8 GB RAM'},
                        {'discovery_title': NAME.replace('5G', '4G') + ' 256 GB 8 GB RAM'}]:
            self.assertEqual(memory_variant_destinations([{**cards[0], **changes}], [PARENT]), [])

    def test_readonly_preparation_rechecks_listing_and_has_no_image_or_catalog_writes(self):
        result = self.candidate_result()
        preview = result['rows'][0]['other_memory_variant']
        fresh = deepcopy(preview['offer'])
        read = SimpleNamespace(documents=3)
        transport = SimpleNamespace(report=lambda: {})
        source_client, image_store = unittest.mock.Mock(), unittest.mock.Mock()
        with patch('automatic_memory_variants.technical_records', return_value=([{'attribute_key': 'ram_gb'}], {}, ['source-image'])), \
             patch('audit_priority_retailers.inspect_target', return_value={'prepared_offer': fresh}) as inspect:
            report = prepare_memory_variants(result, [PARENT], transport=transport, technical_reader=read,
                source_client=source_client, image_store=image_store)
        self.assertEqual(report['rows'][0]['status'], 'technical_and_live_offer_verified')
        self.assertFalse(report['writes'])
        self.assertEqual(inspect.call_args.args[2]['attributes']['storage_gb'], 256)
        self.assertEqual(PARENT['attributes']['storage_gb'], 128)
        source_client.session.request.assert_not_called()
        image_store.upload.assert_not_called()

    def test_atomic_writer_receives_new_capacity_proof_and_immutable_parent_in_one_payload(self):
        result = self.candidate_result()
        fresh = deepcopy(result['rows'][0]['other_memory_variant']['offer'])
        source_client = unittest.mock.Mock(base_url='https://teknoskor.com', secret='test-only-secret')
        source_client._json_body.return_value = {'committed': True, 'contract': CONTRACT, 'parent_unchanged': True,
            'outcome': 'created', 'slug': variant_identity(PARENT, 8, 256)['slug'], 'product_id': 987, 'parent_product_id': PARENT['id']}
        image_store = unittest.mock.Mock()
        image_store.upload.return_value = 'https://cdn.teknoskor.com/products/new-variant/image.webp'
        with patch('automatic_memory_variants.technical_records', return_value=([{'attribute_key': 'ram_gb'}], {}, ['source-image'])), \
             patch('audit_priority_retailers.inspect_target', return_value={'prepared_offer': fresh}):
            report = prepare_memory_variants(result, [PARENT], transport=SimpleNamespace(report=lambda: {}),
                technical_reader=SimpleNamespace(documents=3), publish=True, source_client=source_client, image_store=image_store)
        self.assertEqual(report['created'], 1)
        payload = source_client.session.request.call_args.kwargs['json']
        self.assertEqual(payload['parent']['storage_gb'], 128)
        self.assertEqual(payload['variant']['storage_gb'], 256)
        self.assertEqual(payload['offers'][0]['source_variant_evidence']['storage_gb'], 256)
        self.assertEqual(payload['offers'][0]['source_title'], fresh['source_title'])
        self.assertEqual(payload['offers'][0]['observed_at'], canonical_observation_time(fresh['observed_at']))
        self.assertNotIn('product_id', payload['offers'][0])

    def test_one_failed_source_image_can_recover_but_no_image_cannot_create_a_variant(self):
        result = self.candidate_result()
        fresh = deepcopy(result['rows'][0]['other_memory_variant']['offer'])
        client = unittest.mock.Mock(base_url='https://teknoskor.com', secret='test-only-secret')
        client._json_body.return_value = {'committed': True, 'contract': CONTRACT, 'parent_unchanged': True,
            'outcome': 'created', 'slug': variant_identity(PARENT, 8, 256)['slug'], 'product_id': 987,
            'parent_product_id': PARENT['id'], 'scores_recalculated': True}
        for success in (True, False):
            store = unittest.mock.Mock()
            store.upload.side_effect = [ValueError('source_image_not_available'), 'https://cdn.teknoskor.com/products/fixture/ok.webp' if success else ValueError('not_available')]
            client.session.request.reset_mock()
            with patch('automatic_memory_variants.technical_records', return_value=([{'attribute_key': 'ram_gb'}], {}, ['source-one', 'source-two'])), \
                 patch('audit_priority_retailers.inspect_target', return_value={'prepared_offer': fresh}):
                report = prepare_memory_variants(result, [PARENT], transport=SimpleNamespace(report=lambda: {}),
                    technical_reader=SimpleNamespace(documents=3), publish=True, source_client=client, image_store=store)
            self.assertEqual(report['created'], int(success))
            self.assertEqual(report['rows'][0]['source_image_failures'], 1 if success else 2)
            self.assertEqual(client.session.request.call_count, int(success))
            self.assertTrue(report['writes'])  # Image attempts are never reported as a dry run.

    def test_catalog_readback_preserves_all_original_identities_and_requires_each_created_capacity(self):
        variant = {**variant_identity(PARENT, 8, 256), 'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'}
        report = {'rows': [{'status': 'created', 'variant': variant, 'publication': {'product_id': 987}}]}
        added = {'id': 987, 'name': variant['name'], 'slug': variant['slug'],
                 'attributes': {'ram_gb': 8, 'storage_gb': 256, 'Connectivity': {'5G': True}}}
        self.assertEqual(verify_variant_catalog_readback([PARENT], [PARENT, added], report),
                         {'parent_identities_unchanged': 1, 'new_variants_verified': 1, 'catalog_after': 2})
        for after in [[PARENT], [added], [PARENT, {**added, 'attributes': dict(SPECS)}],
                      [PARENT, {**added, 'slug': 'wrong'}], [PARENT, added, {**added, 'id': 988}]]:
            with self.assertRaises(ValueError):
                verify_variant_catalog_readback([PARENT], after, report)
        with self.assertRaises(ValueError):
            verify_variant_catalog_readback([PARENT], [{**PARENT, 'attributes': dict(SPECS, ram_gb=12)}, added], report)


if __name__ == '__main__':
    unittest.main()
