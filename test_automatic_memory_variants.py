import html
import base64
import json
import unittest
import requests
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from automatic_memory_variants import (TechnicalVariantReader, PublicBrowserTechnicalReader, exact_capacity, selected_technical_document,
    technical_records, variant_identity, observed_variant_candidates, memory_variant_destinations,
    prepare_memory_variants, verify_variant_catalog_readback, memory_target_preflight, canonical_observation_time,
    declared_technical_targets, autocomplete_technical_variants, memory_parent_priority, existing_selected_memory_sibling,
    technical_model_matches, CONTRACT)
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
    def test_actual_native_one_tb_hint_precedes_another_ram_equal_capacity_and_still_needs_selected_proof(self):
        from pathlib import Path
        base = 'https://www.kimovil.com/en/where-to-buy-apple-iphone-17-pro'
        target = base + '-12gb-1tb'
        search = 'https://www.kimovil.com/_json/autocomplete_devicemodels_joined.json?device_type=0&name=Apple%20iPhone%2017%20Pro'
        root = Path('fixtures')
        documents = {search: (root / 'kimovil-iphone17pro-public-variant-routes.json').read_text(),
                     base: (root / 'kimovil-iphone17pro-base-public-technical.html').read_text(),
                     target: (root / 'kimovil-iphone17pro-1tb-public-technical.html').read_text()}
        class Source:
            def __init__(self): self.calls = []
            def read(self, url):
                self.calls.append(url)
                if url not in documents:
                    raise AssertionError('Exact native 1TB hint should precede unrelated capacity: ' + url)
                return documents[url]
        parent = {'name': 'Apple iPhone 17 Pro', 'attributes': {'ram_gb': 12, 'storage_gb': 256}}
        before = deepcopy(parent)
        requested = {'ram_gb': 12, 'storage_gb': 1024, 'network_generation': '5g'}
        source = Source(); reader = TechnicalVariantReader(source)
        document, proof = reader.locate(parent, requested)
        self.assertEqual(source.calls, [search, base, target])
        self.assertEqual((proof['model_name'], proof['ram_gb'], proof['storage_gb']), ('Apple iPhone 17 Pro', 12, 1024))
        self.assertEqual(parent, before)
        for patch in ({'ram_gb': 8}, {'storage_gb': 1000}, {'storage_gb': 512}, {'network_generation': '4g'}):
            self.assertIsNone(selected_technical_document(document, target, parent['name'], {**requested, **patch}, canonical_model_url=base))
        self.assertIsNone(selected_technical_document(document, target, 'Apple iPhone 17 Pro Max', requested, canonical_model_url=base))
        self.assertIsNone(selected_technical_document(document, target, parent['name'], requested, canonical_model_url=base+'-max'))

    def test_compact_iphone_name_uses_spaced_search_without_changing_catalog_identity_or_matching(self):
        root = 'https://www.kimovil.com/en/where-to-buy-apple-iphone16e'
        parent = {'id': 338, 'name': 'Apple iPhone16e 8GB 512GB', 'slug': 'apple-iphone16e-8gb-512gb',
                  'attributes': {'ram_gb': 8, 'storage_gb': 512, 'Connectivity': {'5G': True}}}
        original = deepcopy(parent)
        for discovered in ['Apple iPhone16e', 'Apple iPhone 16']:
            calls = []
            class Reader:
                def read(self, url):
                    calls.append(url)
                    if '_json/autocomplete' in url:
                        self_case.assertTrue(url.endswith('name=Apple%20iPhone%2016e'))
                        return json.dumps({'results': [{'full_name': discovered,
                            'url': root.rsplit('where-to-buy-', 1)[1]}]})
                    return tech_document(root, ram=8, storage=256, name=discovered)
            self_case = self
            reader = TechnicalVariantReader(Reader())
            specs = {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'}
            if discovered == 'Apple iPhone16e':
                _, evidence = reader.locate(parent, specs)
                self.assertEqual(evidence['model_name'], 'Apple iPhone16e')
                self.assertEqual(evidence['source_url'], root)
                self.assertEqual(len(calls), 2)
            else:
                with self.assertRaisesRegex(ValueError, 'technical_model_discovery_ambiguous_or_missing'):
                    reader.locate(parent, specs)
                self.assertEqual(len(calls), 1)
            self.assertEqual(parent, original)

    def test_requested_capacity_in_verified_base_region_precedes_another_regions_first_link(self):
        root = 'https://www.kimovil.com/en/where-to-buy-samsung-galaxy-s24fe'
        other = 'https://www.kimovil.com/en/where-to-buy-samsung-galaxy-s24f-8gb-256gb-sm-s721u'
        same = root + '-8gb-256gb'
        name = 'Samsung Galaxy S24FE'
        parent = {'id': 225, 'name': 'Samsung Galaxy S24 FE', 'slug': 'samsung-galaxy-s24fe',
                  'attributes': {'ram_gb': 8, 'storage_gb': 128, 'Connectivity': {'5G': True}},
                  'technical_source_urls': [root]}
        original = deepcopy(parent)
        base = tech_document(root, storage=128, name=name).replace('</body>',
            f'<ul class="versions-list"><li class="item" data-mk="NA"><a href="{other}">8GB 256GB</a></li></ul>'
            f'<ul class="versions-list"><li class="item active" data-mk="Global"><a href="{root}">8GB 128GB</a></li>'
            f'<li class="item" data-mk="Global"><a href="{same}">8GB 256GB</a></li></ul></body>')
        for actual_same_storage in [256, 512]:
            calls = []
            class Reader:
                def read(self, url):
                    calls.append(url)
                    if '_json/autocomplete' in url:
                        return json.dumps({'results': [{'full_name': name,
                            'url': root.rsplit('where-to-buy-', 1)[1]}]})
                    return {root: base, same: tech_document(same, storage=actual_same_storage, name=name),
                            other: tech_document(other, storage=256, name=name)}[url]
            reader = TechnicalVariantReader(Reader())
            _, evidence = reader.locate(parent, {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'})
            self.assertEqual(calls[:3], [root, calls[1], same])
            self.assertIn('_json/autocomplete', calls[1])
            if actual_same_storage == 256:
                self.assertEqual(evidence['source_url'], same)
                self.assertNotIn(other, calls)
            else:
                # A region label is no substitute for the selected physical
                # memory: the wrong-capacity page fails before the fallback.
                self.assertEqual(evidence['source_url'], other)
                self.assertEqual(calls[-2:], [same, other])
            diagnostic = next(d for d in reader.diagnostics if d['scope'] == 'observed_technical_capacity_links')
            self.assertEqual(diagnostic['selected_default_region'], 'Global')
            self.assertEqual(diagnostic['observed_capacity_urls'][:2], [same, other])
            self.assertEqual(parent, original)

    def test_persisted_root_is_reread_and_can_find_a_child_when_search_omits_the_actual_model(self):
        root = 'https://www.kimovil.com/en/where-to-buy-xiaomi-17'
        child = root + '-12gb-512gb'
        base = tech_document(root, ram=12, storage=256, name='Xiaomi 17').replace('</body>',
            f'<ul class="versions-list"><li class="item"><a href="{child}">12GB 512GB</a></li></ul></body>')
        docs = {root: base, child: tech_document(child, ram=12, storage=512, name='Xiaomi 17')}
        calls = []
        class Reader:
            def read(self, url):
                calls.append(url)
                if '_json/autocomplete' in url:
                    return json.dumps({'results': [{'full_name': 'Xiaomi Redmi 17 5G',
                        'url': 'xiaomi-redmi-17-5g', 'vers': 'Global · 12GB · 512GB=wrong-redmi-child'}]})
                return docs[url]
        parent = {'id': 284, 'name': 'Xiaomi 17', 'slug': 'xiaomi-17',
                  'attributes': {'ram_gb': 12, 'storage_gb': 256, 'Connectivity': {'5G': True}},
                  'technical_source_urls': [root]}
        before = deepcopy(parent)
        reader = TechnicalVariantReader(Reader())
        _, proof = reader.locate(parent, {'ram_gb': 12, 'storage_gb': 512, 'network_generation': '5g'})
        self.assertEqual(proof['source_url'], child)
        self.assertEqual(calls[0], root)
        self.assertEqual(calls[-1], child)
        self.assertEqual(len(calls), 3)
        self.assertEqual(parent, before)
        self.assertEqual(reader.diagnostics[0]['stored_facts_reused'], False)
        self.assertEqual(reader.diagnostics[0]['parent_memory_radio_verified'], True)

    def test_persisted_root_cannot_supply_missing_or_conflicting_parent_facts_or_foreign_destinations(self):
        root = 'https://www.kimovil.com/en/where-to-buy-xiaomi-17'
        for document in [tech_document(root, ram=8, storage=256, name='Xiaomi 17'),
                         tech_document(root, ram=12, storage=256, name='Xiaomi Redmi 17'),
                         tech_document(root, ram=12, storage=256, name='Xiaomi 17').replace('n78', 'No')]:
            calls = []
            class Reader:
                def read(self, url):
                    calls.append(url)
                    return json.dumps({'results': []}) if '_json/autocomplete' in url else document
            parent = {'id': 284, 'name': 'Xiaomi 17', 'slug': 'xiaomi-17',
                      'attributes': {'ram_gb': 12, 'storage_gb': 256, 'Connectivity': {'5G': True}},
                      'technical_source_urls': ['https://evil.example/phone', root + '?token=private', root]}
            with self.assertRaisesRegex(ValueError, 'technical_model_discovery_ambiguous_or_missing'):
                TechnicalVariantReader(Reader()).locate(parent, {'ram_gb': 12, 'storage_gb': 512,
                                                               'network_generation': '5g'})
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0], root)

    def test_flagship_technical_discovery_never_follows_a_redmi_result_with_the_same_number(self):
        calls = []
        class Reader:
            def read(self, url):
                calls.append(url)
                if '_json/autocomplete' in url:
                    return json.dumps({'results': [{'full_name': 'Xiaomi Redmi 17 5G',
                        'url': 'xiaomi-redmi-17-5g', 'is_rumor': False,
                        'vers': 'Global · 12GB · 512GB=xiaomi-redmi-17-5g-12gb-512gb'}]})
                raise AssertionError('Wrong model family must not consume a PDP request')
        parent = {'id': 284, 'name': 'Xiaomi 17', 'slug': 'xiaomi-17',
                  'attributes': {'ram_gb': 12, 'storage_gb': 256, 'Connectivity': {'5G': True}}}
        with self.assertRaisesRegex(ValueError, 'technical_model_discovery_ambiguous_or_missing'):
            TechnicalVariantReader(Reader()).locate(parent, {'ram_gb': 12, 'storage_gb': 512,
                                                           'network_generation': '5g'})
        self.assertEqual(len(calls), 1)
        wrong = 'https://www.kimovil.com/en/where-to-buy-xiaomi-redmi-17-5g'
        # Even matching memory/radio must never turn that different family into proof.
        self.assertIsNone(selected_technical_document(
            tech_document(wrong, ram=12, storage=512, name='Xiaomi Redmi 17 5G'),
            wrong, 'Xiaomi 17', {'ram_gb': 12, 'storage_gb': 512, 'network_generation': '5g'}))

    def test_technical_model_family_keeps_radio_spelling_compatibility_and_rejects_qualifier_drift(self):
        for expected, observed in [('Samsung Galaxy A36 5G', 'Samsung Galaxy A36'),
                                   ('Xiaomi Redmi Note 14 4G', 'Xiaomi Redmi Note 14'),
                                   ('Xiaomi 17', 'Xiaomi 17 5G')]:
            self.assertTrue(technical_model_matches(expected, observed), (expected, observed))
        for observed in ['Xiaomi Redmi 17 5G', 'Xiaomi Redmi Note 17', 'Xiaomi 17 Pro',
                         'Xiaomi 17 Pro Max', 'Xiaomi 17T', 'Xiaomi 16']:
            self.assertFalse(technical_model_matches('Xiaomi 17', observed), observed)

    def test_storage_only_iphone_card_routes_to_an_unseen_storage_without_copying_parent_ram(self):
        parent = {'id': 1, 'name': 'Apple iPhone 16 Pro Max', 'slug': 'apple-iphone-16-pro-max',
                  'attributes': {'ram_gb': 8, 'storage_gb': 256, 'Connectivity': {'5G': True}}}
        card = {'source_url': 'https://www.trendyol.com/apple/iphone-16-pro-max-512-gb-p-123456789',
                'discovery_title': 'Apple iPhone 16 Pro Max 512 GB', 'stock_priority': True}
        before = deepcopy(parent), deepcopy(card)
        self.assertEqual(memory_variant_destinations([card], [parent]), [(parent, card)])
        self.assertEqual((parent, card), before)
        self.assertNotIn('ram_gb', card)
        self.assertNotIn('observed_specs', card)
        self.assertNotIn('price', card)
        known = {**parent, 'id': 2, 'name': parent['name'] + ' 8GB 512GB',
                 'attributes': {'ram_gb': 8, 'storage_gb': 512, 'Connectivity': {'5G': True}}}
        self.assertEqual(memory_variant_destinations([card], [parent, known]), [])
        for title in ['Apple iPhone 16 Pro Max 256/512 GB', 'Apple iPhone 16 Pro Max 256 GB',
                      'Apple iPhone 16 Pro Max 512 GB Kılıf', 'Apple iPhone 16 Pro 512 GB',
                      'Apple iPhone 17 Pro Max 512 GB', 'Apple iPhone 16 Pro Max']:
            self.assertEqual(memory_variant_destinations([{**card, 'discovery_title': title}], [parent]), [], title)

    def test_provider_declared_version_recovers_missing_menu_without_inventing_a_capacity_url(self):
        from bs4 import BeautifulSoup
        base = tech_document(TECH_URL, storage=128)
        child = BeautifulSoup(tech_document(), 'html.parser')
        child.select_one('link[rel="canonical"]')['href'] = TECH_URL
        meta = child.select_one('meta[name="devicecompare"]')
        value = json.loads(meta['content']); value['slug'] = ALT_TECH_URL.rsplit('where-to-buy-', 1)[1]
        meta['content'] = json.dumps(value)
        versions = f'Global · 8GB · 128GB={TECH_URL.rsplit("where-to-buy-", 1)[1]}#Global · 8GB · 256GB={value["slug"]}'
        calls = []
        class Reader:
            def read(self, url):
                calls.append(url)
                if '_json/autocomplete' in url:
                    return json.dumps({'results': [{'full_name': NAME, 'url': TECH_URL.rsplit('where-to-buy-', 1)[1], 'vers': versions}]})
                return {TECH_URL: base, ALT_TECH_URL: str(child)}[url]
        reader = TechnicalVariantReader(Reader())
        _, proof = reader.locate(PARENT, dict(SPECS, storage_gb=256))
        self.assertEqual(calls[1:], [TECH_URL, ALT_TECH_URL])
        self.assertEqual(proof['source_url'], ALT_TECH_URL)
        self.assertEqual(proof['storage_gb'], 256)
        hinted_reader = TechnicalVariantReader(Reader())
        _, hinted_proof = hinted_reader.locate({**PARENT, 'technical_source_urls': [TECH_URL]},
                                               dict(SPECS, storage_gb=256))
        self.assertEqual(hinted_proof['source_url'], ALT_TECH_URL)
        self.assertEqual(hinted_reader.documents, 3)
        self.assertIsNone(selected_technical_document(str(child), ALT_TECH_URL, NAME, dict(SPECS, storage_gb=256), canonical_model_url=TECH_URL))
        missing_slug = BeautifulSoup(str(child), 'html.parser')
        missing_meta = missing_slug.select_one('meta[name="devicecompare"]')
        missing_value = json.loads(missing_meta['content']); del missing_value['slug']
        missing_meta['content'] = json.dumps(missing_value)
        for bad in [str(child).replace('256 GB', '128 GB'), str(missing_slug),
                    str(child).replace('</body>', f'<ul class="versions-list"><li class="item active"><a href="{TECH_URL}">8GB 128GB</a></li></ul></body>')]:
            self.assertIsNone(selected_technical_document(bad, ALT_TECH_URL, NAME, dict(SPECS, storage_gb=256),
                canonical_model_url=TECH_URL, provider_declared_variant=ALT_TECH_URL))
        self.assertIsNone(selected_technical_document(str(child), ALT_TECH_URL, NAME, dict(SPECS, storage_gb=256),
            canonical_model_url=TECH_URL, provider_declared_variant=TECH_URL))

    def test_provider_version_labels_are_only_routing_hints_and_foreign_or_generated_targets_are_not_used(self):
        slug = ALT_TECH_URL.rsplit('where-to-buy-', 1)[1]
        self.assertEqual(autocomplete_technical_variants({'vers': f'Global · 8GB · 256GB={slug}'}),
            [{'source_url': ALT_TECH_URL, 'ram_gb': 8, 'storage_gb': 256}])
        for value in [f'8GB · 256GB=https://evil.test/{slug}', f'8GB · 256GB={slug}?token=private',
                      '8GB · 256GB=', f'8GB · 12GB · 256GB={slug}', f'256GB={slug}', 'x' * 32769]:
            self.assertEqual(autocomplete_technical_variants({'vers': value}), [])

    def test_an_existing_sibling_needs_unique_same_family_memory_radio_and_complete_selected_seller_proof(self):
        preview = self.candidate_result()['rows'][0]['other_memory_variant']
        sibling = {**PARENT, 'id': 124, **variant_identity(PARENT, 8, 256),
                   'attributes': {'ram_gb': 8, 'storage_gb': 256, 'Connectivity': {'5G': True}}}
        self.assertEqual(existing_selected_memory_sibling(PARENT, preview, [PARENT, sibling]), sibling)
        for wrong in [{**sibling, 'attributes': {'ram_gb': 12, 'storage_gb': 256, 'Connectivity': {'5G': True}}},
                      {**sibling, 'name': sibling['name'].replace('A54', 'A55')},
                      {**sibling, 'attributes': {'ram_gb': 8, 'storage_gb': 256, 'Connectivity': {'5G': False}}}]:
            self.assertIsNone(existing_selected_memory_sibling(PARENT, preview, [PARENT, wrong]))
        self.assertIsNone(existing_selected_memory_sibling(PARENT, preview, [PARENT, sibling, {**sibling, 'id': 125}]))
        for mutate in [lambda p: p.update(writes=True), lambda p: p['offer'].update(merchant='Other'),
                       lambda p: p['observed_specs'].update(network_generation='4g')]:
            bad = deepcopy(preview); mutate(bad)
            self.assertIsNone(existing_selected_memory_sibling(PARENT, bad, [PARENT, sibling]))

    def test_memory_priority_counts_a_priced_sibling_but_never_merges_radio_or_unknown_public_rows(self):
        def phone(product_id, name, ram, storage, five_g):
            return {'id': product_id, 'slug': f'phone-{product_id}', 'name': name,
                    'attributes': {'ram_gb': ram, 'storage_gb': storage, 'Connectivity': {'5G': five_g}}}
        catalog = [phone(1, 'Samsung Galaxy A34 5G', 6, 128, True),
                   phone(2, 'Samsung Galaxy A34 5G 8GB 128GB', 8, 128, True),
                   phone(3, 'Samsung Galaxy A35 5G', 6, 128, True),
                   phone(4, 'Samsung Galaxy A15 4G', 4, 128, False),
                   phone(5, 'Samsung Galaxy A15 5G', 4, 128, True),
                   phone(6, 'Samsung Galaxy A55 5G', 8, 128, True)]
        status = {p['id']: (p['slug'], p['id'] in (2, 5)) for p in catalog}
        status[6] = ('different-slug', False)
        before = deepcopy(catalog), deepcopy(status)
        result = memory_parent_priority(catalog, status)
        self.assertEqual(result['parent_priorities'], {'phone-1': 1, 'phone-2': 1, 'phone-3': 0,
                                                       'phone-4': 0, 'phone-5': 1, 'phone-6': 2})
        self.assertEqual((result['models_without_current_price'], result['models_with_current_price'],
                          result['unknown_models']), (2, 2, 1))
        self.assertTrue(result['prices_are_routing_hints_only'])
        self.assertEqual((catalog, status), before)

    def test_a_missing_model_precedes_extra_capacity_of_a_family_with_a_priced_sibling(self):
        from test_n11_catalog_source import document, URL as n11_url, NAME as n11_name
        from observed_memory_variants import inspect_n11_memory_variant
        missing = {'id': 124, 'slug': 'samsung-galaxy-a57', 'name': n11_name,
                   'attributes': {'ram_gb': 6, 'storage_gb': 128, 'Connectivity': {'5G': True}}}
        sibling = {'id': 125, 'slug': 'a54-six-gb', 'name': NAME + ' 6GB 128GB',
                   'attributes': {'ram_gb': 6, 'storage_gb': 128, 'Connectivity': {'5G': True}}}
        n11_preview = inspect_n11_memory_variant(document(), n11_url, n11_name,
            {'ram_gb': 6, 'storage_gb': 128, 'network_generation': '5g'})
        result = self.candidate_result()
        result['rows'].append({'slug': missing['slug'], 'other_memory_variant': n11_preview})
        catalog = [PARENT, missing, sibling]
        result['memory_variant_priority'] = memory_parent_priority(catalog, {
            PARENT['id']: (PARENT['slug'], False), missing['id']: (missing['slug'], False),
            sibling['id']: (sibling['slug'], True)})
        candidates = observed_variant_candidates(result, catalog)
        self.assertEqual([c['parent']['slug'] for c in candidates], [missing['slug'], PARENT['slug']])
        self.assertEqual(candidates[0]['targets'], [n11_url])

    def test_each_missing_model_gets_a_first_capacity_attempt_before_second_options(self):
        from test_n11_catalog_source import document, URL as n11_url, NAME as n11_name
        from test_trendyol_catalog_source import document as ty_document, URL as ty_url
        from observed_memory_variants import inspect_n11_memory_variant
        parent = {**PARENT, 'attributes': {'ram_gb': 6, 'storage_gb': 64, 'network_generation': '5g'}}
        missing = {'id': 124, 'slug': 'samsung-galaxy-a57', 'name': n11_name,
                   'attributes': {'ram_gb': 6, 'storage_gb': 128, 'Connectivity': {'5G': True}}}
        first = inspect_trendyol_memory_variant(ty_document(), ty_url, NAME, parent['attributes'])
        second = inspect_trendyol_memory_variant(alternate_document(), ALT_URL, NAME, parent['attributes'])
        other = inspect_n11_memory_variant(document(), n11_url, n11_name,
            {'ram_gb': 6, 'storage_gb': 128, 'network_generation': '5g'})
        result = {'rows': [{'slug': parent['slug'], 'other_memory_variant': first},
                           {'slug': parent['slug'], 'other_memory_variant': second},
                           {'slug': missing['slug'], 'other_memory_variant': other}]}
        catalog = [parent, missing]
        result['memory_variant_priority'] = memory_parent_priority(catalog,
            {p['id']: (p['slug'], False) for p in catalog})
        candidates = observed_variant_candidates(result, catalog)
        self.assertEqual([(c['parent']['slug'], c['specs']['storage_gb']) for c in candidates],
                         [(parent['slug'], 128), (missing['slug'], 256), (parent['slug'], 256)])

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

    def test_failed_selected_capacity_reports_observed_facts_without_document_state(self):
        base = tech_document(TECH_URL, storage=128).replace('</body>',
            f'<ul class="versions-list"><li class="item"><a href="{ALT_TECH_URL}">8 GB 256 GB</a></li></ul></body>')
        child = tech_document(ram=6).replace('</body>',
            '<script>PRIVATE_DOCUMENT_STATE</script><div>DO_NOT_RETAIN_PAGE_TEXT</div></body>')
        class Reader:
            def read(self, url):
                if '_json/autocomplete' in url:
                    return json.dumps({'results': [{'full_name': NAME, 'url': TECH_URL.rsplit('where-to-buy-', 1)[1]}]})
                return {TECH_URL: base, ALT_TECH_URL: child}[url]
        reader = TechnicalVariantReader(Reader())
        with self.assertRaisesRegex(ValueError, 'exact_selected_technical_memory_not_observed'):
            reader.locate(PARENT, dict(SPECS, storage_gb=256))
        diagnostic = reader.diagnostics[-1]
        self.assertEqual(diagnostic['phase'], 'requested_capacity')
        self.assertEqual(diagnostic['requested_specs'], {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '5g'})
        self.assertEqual(diagnostic['observed_specs'], {'ram_gb': 6, 'storage_gb': 256, 'network_generation': '5g'})
        self.assertEqual(diagnostic['canonical_urls'], [ALT_TECH_URL])
        self.assertEqual(diagnostic['metadata'][0]['model_name'], NAME)
        serialized = json.dumps(reader.diagnostics)
        for private in ('PRIVATE_DOCUMENT_STATE', 'DO_NOT_RETAIN_PAGE_TEXT', '<script>', 'partials', 'phone-a54_big'):
            self.assertNotIn(private, serialized)

    def test_rejected_model_family_reports_missing_metadata_and_stays_rejected(self):
        class Reader:
            def read(self, url):
                if '_json/autocomplete' in url:
                    return json.dumps({'results': [{'full_name': NAME, 'url': TECH_URL.rsplit('where-to-buy-', 1)[1]}]})
                return tech_document(TECH_URL, storage=128).replace('name="deviceki"', 'name="other"').replace(
                    'name="devicecompare"', 'name="othercompare"')
        reader = TechnicalVariantReader(Reader())
        with self.assertRaisesRegex(ValueError, 'technical_model_family_not_verified'):
            reader.locate(PARENT, dict(SPECS, storage_gb=256))
        diagnostic = reader.diagnostics[-1]
        self.assertEqual(diagnostic['phase'], 'model_family')
        self.assertEqual([item['count'] for item in diagnostic['metadata']], [0, 0])
        self.assertEqual(diagnostic['observed_specs']['storage_gb'], 128)

    def test_observed_4g_metadata_and_positive_lte_table_prove_radio_without_inventing_a_5g_cell(self):
        from bs4 import BeautifulSoup
        name = 'Samsung Galaxy A15 4G'
        url = 'https://www.kimovil.com/en/where-to-buy-samsung-galaxy-a15-4g'
        document = tech_document(url, ram=4, storage=128, name=name)
        # Actual A15 source shape: a dedicated LTE band row, no 5G cell, and
        # operator coverage widgets that mention unsupported 5G frequencies.
        document = document.replace('<th class="label">5G</th><td class="value">n78</td>',
            '<th class="label">4G LTE</th><td class="value">B1 (2100), B3 (1800), B20 (800)</td>')
        specs = {'ram_gb': 4, 'storage_gb': 128, 'network_generation': '4g'}
        proof = selected_technical_document(document, url, name, specs)
        self.assertIsNotNone(proof)
        self.assertNotIn('"5G":', document)
        for bad in [document.replace('B1 (2100), B3 (1800), B20 (800)', 'Unknown'),
                    document.replace('4G LTE', 'Coverage'),
                    document.replace(name, name.removesuffix(' 4G')),
                    document.replace('4G LTE', '5G NR'),
                    document.replace('</table>', '<tr><th class="label">5G</th><td class="value">Unknown</td></tr></table>')]:
            self.assertIsNone(selected_technical_document(bad, url, name, specs))
        self.assertIsNone(selected_technical_document(document, url, name, dict(specs, network_generation='5g')))
        conflicting = document.replace('</table>', '<tr><th class="label">5G</th><td class="value">n78</td></tr></table>')
        self.assertIsNone(selected_technical_document(conflicting, url, name, specs))
        missing_meta = BeautifulSoup(document, 'html.parser')
        missing_meta.select_one('meta[name="devicecompare"]').decompose()
        self.assertIsNone(selected_technical_document(str(missing_meta), url, name, specs))

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

    def test_real_encoded_navigation_shape_discovers_and_verifies_an_unhydrated_selected_variant(self):
        from bs4 import BeautifulSoup
        encoded = lambda url: base64.b64encode(url.encode()).decode()
        menu = lambda active: ('<ul class="versions-list">' + ''.join(
            f'<li class="item{" active" if active == url else ""}" data-ram="8192" data-rom="{storage*1024}">'
            f'<a data-kdecode="{encoded(url)}" rel="nofollow">8GB · {storage}GB No prices</a></li>'
            for url, storage in [(TECH_URL, 128), (ALT_TECH_URL, 256)]) + '</ul>')
        base = tech_document(TECH_URL, storage=128).replace('</body>', menu(TECH_URL) + '</body>')
        child = BeautifulSoup(tech_document(), 'html.parser')
        child.select_one('link[rel="canonical"]')['href'] = TECH_URL
        meta = child.select_one('meta[name="devicecompare"]')
        value = json.loads(meta['content']); value['slug'] = ALT_TECH_URL.rsplit('where-to-buy-', 1)[1]
        meta['content'] = json.dumps(value)
        child.body.append(BeautifulSoup(menu(ALT_TECH_URL), 'html.parser'))
        calls = []
        class Reader:
            def read(self, url):
                calls.append(url)
                if '_json/autocomplete' in url:
                    return json.dumps({'results': [{'full_name': NAME, 'url': TECH_URL.rsplit('where-to-buy-', 1)[1]}]})
                return {TECH_URL: base, ALT_TECH_URL: str(child)}[url]
        _, proof = TechnicalVariantReader(Reader()).locate(PARENT, dict(SPECS, storage_gb=256))
        self.assertEqual(calls[1:], [TECH_URL, ALT_TECH_URL])
        self.assertEqual(proof['storage_gb'], 256)
        for raw in ['not base64!', encoded('https://evil.test/en/where-to-buy-phone'),
                    encoded(ALT_TECH_URL + '?token=private'), 'A' * 5000]:
            node = BeautifulSoup(f'<li><a data-kdecode="{raw}">8GB · 256GB</a></li>', 'html.parser').li
            self.assertEqual(declared_technical_targets(node, TECH_URL), [])

    def test_observed_radio_qualified_model_is_preferred_and_still_needs_actual_4g_facts(self):
        base = 'https://www.kimovil.com/en/where-to-buy-xiaomi-redmi-note-15-4g'
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
                self_outer.assertIn(url, (base, base.removesuffix('-4g')))
                return tech_document(url, name='Xiaomi Redmi Note 15').replace('n78', 'No')
        self_outer = self
        reader = TechnicalVariantReader(Reader())
        specs = {'ram_gb': 8, 'storage_gb': 256, 'network_generation': '4g'}
        _, proof = reader.locate(parent, specs)
        self.assertEqual(proof['source_url'], base)
        self.assertTrue(reader.diagnostics[0]['radio_qualified_discovery'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1], base)
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

    def test_failed_technical_observation_is_checkpointed_before_remaining_candidates(self):
        reader = SimpleNamespace(documents=2, diagnostics=[])
        snapshots = []
        client, store = unittest.mock.Mock(), unittest.mock.Mock()
        def reject(*args):
            reader.diagnostics.append({'scope': 'rejected_selected_technical_document',
                                       'observed_specs': {'ram_gb': 6, 'storage_gb': 256}})
            raise ValueError('exact_selected_technical_memory_not_observed')
        with patch('automatic_memory_variants.technical_records', side_effect=reject), \
             patch('audit_priority_retailers.inspect_target') as inspect:
            report = prepare_memory_variants(self.candidate_result(), [PARENT],
                transport=SimpleNamespace(report=lambda: {}), technical_reader=reader,
                publish=True, source_client=client, image_store=store,
                checkpoint=lambda r: snapshots.append(deepcopy(r)))
        self.assertEqual(snapshots[0]['technical_diagnostics'], reader.diagnostics)
        self.assertEqual(report['rows'][0]['status'], 'rejected')
        self.assertFalse(report['writes'])
        inspect.assert_not_called()
        client.session.request.assert_not_called()
        store.upload.assert_not_called()

    def test_fresh_retailer_rejection_preserves_its_reason_without_source_bodies_or_writes(self):
        client, store = unittest.mock.Mock(), unittest.mock.Mock()
        response = {'prepared_offer': None, 'reader_reason': 'schema_selected_cash_offer_mismatch',
                    'http_status': 200, 'private_state': {'visitor': 'not-for-report'}}
        with patch('automatic_memory_variants.technical_records', return_value=([], {}, ['image'])), \
                patch('audit_priority_retailers.inspect_target', return_value=response):
            report = prepare_memory_variants(self.candidate_result(), [PARENT],
                transport=SimpleNamespace(report=lambda: {}), technical_reader=SimpleNamespace(documents=3),
                publish=True, source_client=client, image_store=store)
        row = report['rows'][0]
        self.assertEqual(row['reason'], 'fresh_selected_variant_offer_missing')
        self.assertEqual(row['retailer_checks'], [{'source_url': ALT_URL,
            'reader_reason': 'schema_selected_cash_offer_mismatch', 'http_status': 200,
            'selected_sku_verified': False, 'offer_evidence_verified': False}])
        self.assertNotIn('private_state', json.dumps(report))
        self.assertFalse(report['writes'])
        client.session.request.assert_not_called()
        store.upload.assert_not_called()

    def test_blocked_only_candidate_does_not_consume_the_next_reachable_models_attempt(self):
        from test_n11_catalog_source import document, URL as n11_url, NAME as n11_name
        from observed_memory_variants import inspect_n11_memory_variant
        parent = {'id': 124, 'slug': 'samsung-galaxy-a57', 'name': n11_name,
                  'attributes': {'ram_gb': 6, 'storage_gb': 128, 'Connectivity': {'5G': True}}}
        preview = inspect_n11_memory_variant(document(), n11_url, n11_name,
            {'ram_gb': 6, 'storage_gb': 128, 'network_generation': '5g'})
        result = self.candidate_result()
        result['rows'].append({'slug': parent['slug'], 'other_memory_variant': preview})
        result['sources'] = {'Trendyol': {'blocked': True}, 'n11': {'blocked': False}}
        original = deepcopy(result)
        with patch('automatic_memory_variants.MAX_VARIANTS', 1), \
             patch('automatic_memory_variants.technical_records', return_value=([], {}, ['source-image'])) as technical, \
             patch('audit_priority_retailers.inspect_target', return_value={'prepared_offer': preview['offer']}) as inspect:
            report = prepare_memory_variants(result, [PARENT, parent],
                transport=SimpleNamespace(report=lambda: {}), technical_reader=SimpleNamespace(documents=3))
        self.assertEqual((report['observed_candidates'], report['eligible_source_candidates'],
                          report['deferred_source_candidates'], report['attempted_candidates']), (2, 1, 1, 1))
        self.assertEqual(report['rows'][0]['parent_slug'], parent['slug'])
        self.assertEqual(report['rows'][0]['status'], 'technical_and_live_offer_verified')
        self.assertEqual(report['source_deferrals'][0]['parent_slug'], PARENT['slug'])
        self.assertEqual(report['source_deferrals'][0]['reason'], 'all_observed_variant_sources_blocked_for_run')
        self.assertEqual(technical.call_count, 1)
        self.assertEqual(inspect.call_args.args[1], n11_url)
        self.assertFalse(report['writes'])
        self.assertEqual(result, original)

    def test_all_sources_blocked_preserves_candidates_as_deferrals_without_technical_requests(self):
        result = self.candidate_result()
        result['sources'] = {'Trendyol': {'blocked': True}}
        client, store = unittest.mock.Mock(), unittest.mock.Mock()
        with patch('automatic_memory_variants.technical_records') as technical, \
             patch('audit_priority_retailers.inspect_target') as inspect:
            report = prepare_memory_variants(result, [PARENT],
                transport=SimpleNamespace(report=lambda: {}), technical_reader=SimpleNamespace(documents=0),
                publish=True, source_client=client, image_store=store)
        self.assertEqual(report['deferred_source_candidates'], 1)
        self.assertEqual(report['attempted_candidates'], 0)
        self.assertEqual(report['rows'], [])
        self.assertFalse(report['writes'])
        technical.assert_not_called()
        inspect.assert_not_called()
        client.session.request.assert_not_called()
        store.upload.assert_not_called()

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

    def test_lost_commit_reply_or_failed_score_can_replay_without_duplicate_variant_or_source_reads(self):
        for lost_reply in (True, False):
            result = self.candidate_result()
            original = deepcopy(PARENT)
            fresh = deepcopy(result['rows'][0]['other_memory_variant']['offer'])
            identity = variant_identity(PARENT, 8, 256)
            child = {'id': 987, **identity, 'attributes': {'ram_gb': 8, 'storage_gb': 256,
                                                         'Connectivity': {'5G': True}}}
            catalog = [deepcopy(PARENT)]
            calls = []
            def request(method, url, **kwargs):
                calls.append(kwargs)
                acknowledgement = {'committed': True, 'contract': CONTRACT, 'parent_unchanged': True,
                    'outcome': 'created' if len(calls) == 1 else 'existing', 'slug': identity['slug'],
                    'product_id': child['id'], 'parent_product_id': PARENT['id'], 'scores_recalculated': len(calls) == 2}
                if len(calls) == 1:
                    catalog.append(deepcopy(child))
                    if lost_reply:
                        raise requests.ReadTimeout('reply lost after commit')
                return SimpleNamespace(status_code=503 if len(calls) == 1 else 200, body=acknowledgement)
            client = SimpleNamespace(base_url='https://teknoskor.com', secret='fixture-only',
                session=SimpleNamespace(request=request), _json_body=lambda response: response.body)
            store = unittest.mock.Mock(upload=unittest.mock.Mock(return_value='https://cdn.teknoskor.com/fixture.webp'))
            with patch('automatic_memory_variants.technical_records', return_value=([{'attribute_key': 'ram_gb'}], {}, ['source-image'])), \
                 patch('audit_priority_retailers.inspect_target', return_value={'prepared_offer': fresh}) as inspect, \
                 patch('automatic_memory_variants.time.sleep'):
                report = prepare_memory_variants(result, [PARENT], transport=SimpleNamespace(report=lambda: {}),
                    technical_reader=SimpleNamespace(documents=3), publish=True, source_client=client, image_store=store)
            self.assertEqual(len(calls), 2)
            self.assertIs(calls[0]['json'], calls[1]['json'])
            self.assertEqual(inspect.call_count, 1)
            store.upload.assert_called_once()
            row = report['rows'][0]
            self.assertEqual((report['created'], report['existing'], row['status']), (1, 0, 'created'))
            self.assertEqual(row['server_outcome'], 'existing')
            self.assertEqual(row['publication_attempts'], 2)
            self.assertTrue(row['publication_reply_recovered'])
            self.assertFalse(row['score_refresh_pending'])
            self.assertEqual(verify_variant_catalog_readback([PARENT], catalog, report)['new_variants_verified'], 1)
            self.assertEqual(PARENT, original)

    def test_two_uncertain_publication_replies_never_count_an_unacknowledged_commit_as_coverage(self):
        result = self.candidate_result()
        fresh = deepcopy(result['rows'][0]['other_memory_variant']['offer'])
        identity = variant_identity(PARENT, 8, 256)
        child = {'id': 987, **identity, 'attributes': {'ram_gb': 8, 'storage_gb': 256, 'Connectivity': {'5G': True}}}
        client = unittest.mock.Mock(base_url='https://teknoskor.com', secret='fixture-only')
        client.session.request.side_effect = requests.ReadTimeout('reply unknown')
        with patch('automatic_memory_variants.technical_records', return_value=([{'attribute_key': 'ram_gb'}], {}, ['source-image'])), \
             patch('audit_priority_retailers.inspect_target', return_value={'prepared_offer': fresh}), \
             patch('automatic_memory_variants.time.sleep'):
            report = prepare_memory_variants(result, [PARENT], transport=SimpleNamespace(report=lambda: {}),
                technical_reader=SimpleNamespace(documents=3), publish=True, source_client=client,
                image_store=unittest.mock.Mock(upload=unittest.mock.Mock(return_value='https://cdn.teknoskor.com/fixture.webp')))
        self.assertEqual(client.session.request.call_count, 2)
        self.assertEqual((report['created'], report['existing']), (0, 0))
        self.assertEqual(report['rows'][0]['reason'], 'ReadTimeout')
        self.assertTrue(report['rows'][0]['publication_reply_uncertain'])
        self.assertTrue(report['writes'])
        with self.assertRaisesRegex(ValueError, 'published_variant_catalog_readback_incomplete'):
            verify_variant_catalog_readback([PARENT], [PARENT, child], report)

    def test_explicit_variant_quality_rejection_is_never_retried(self):
        result = self.candidate_result()
        fresh = deepcopy(result['rows'][0]['other_memory_variant']['offer'])
        client = unittest.mock.Mock(base_url='https://teknoskor.com', secret='fixture-only')
        client.session.request.return_value = SimpleNamespace(status_code=422)
        client._json_body.return_value = {'committed': False, 'issues': ['technical_model_identity_mismatch']}
        with patch('automatic_memory_variants.technical_records', return_value=([{'attribute_key': 'ram_gb'}], {}, ['source-image'])), \
             patch('audit_priority_retailers.inspect_target', return_value={'prepared_offer': fresh}), \
             patch('automatic_memory_variants.time.sleep') as sleep:
            report = prepare_memory_variants(result, [PARENT], transport=SimpleNamespace(report=lambda: {}),
                technical_reader=SimpleNamespace(documents=3), publish=True, source_client=client,
                image_store=unittest.mock.Mock(upload=unittest.mock.Mock(return_value='https://cdn.teknoskor.com/fixture.webp')))
        client.session.request.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual(report['created'], 0)
        self.assertEqual(report['rows'][0]['server_issues'], ['technical_model_identity_mismatch'])

    def test_first_reply_cannot_claim_an_unexpected_existing_id_as_our_new_variant(self):
        result = self.candidate_result()
        fresh = deepcopy(result['rows'][0]['other_memory_variant']['offer'])
        identity = variant_identity(PARENT, 8, 256)
        client = unittest.mock.Mock(base_url='https://teknoskor.com', secret='fixture-only')
        client.session.request.return_value = SimpleNamespace(status_code=200)
        client._json_body.return_value = {'committed': True, 'contract': CONTRACT, 'parent_unchanged': True,
            'outcome': 'existing', 'product_id': 987, 'slug': identity['slug'],
            'parent_product_id': PARENT['id'], 'scores_recalculated': True}
        with patch('automatic_memory_variants.technical_records', return_value=([{'attribute_key': 'ram_gb'}], {}, ['source-image'])), \
             patch('audit_priority_retailers.inspect_target', return_value={'prepared_offer': fresh}):
            report = prepare_memory_variants(result, [PARENT], transport=SimpleNamespace(report=lambda: {}),
                technical_reader=SimpleNamespace(documents=3), publish=True, source_client=client,
                image_store=unittest.mock.Mock(upload=unittest.mock.Mock(return_value='https://cdn.teknoskor.com/fixture.webp')))
        client.session.request.assert_called_once()
        self.assertEqual((report['created'], report['existing']), (0, 0))
        self.assertEqual(report['rows'][0]['reason'], 'recovered_variant_identity_conflict')

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
                         {'parent_identities_unchanged': 1, 'parent_attributes_unchanged': 1,
                          'new_variants_verified': 1, 'catalog_after': 2})
        for after in [[PARENT], [added], [PARENT, {**added, 'attributes': dict(SPECS)}],
                      [PARENT, {**added, 'slug': 'wrong'}], [PARENT, added, {**added, 'id': 988}]]:
            with self.assertRaises(ValueError):
                verify_variant_catalog_readback([PARENT], after, report)
        with self.assertRaises(ValueError):
            verify_variant_catalog_readback([PARENT], [{**PARENT, 'attributes': dict(SPECS, ram_gb=12)}, added], report)

    def test_catalog_readback_preserves_all_technical_attributes_with_exact_json_types(self):
        original=deepcopy(PARENT)
        original['attributes'].update(battery_mah=5000,Camera={'Resolution':'50 MP'},
                                      Software={'OS':'Android 14'},flag=1)
        after=deepcopy(original)
        after['offer_targets']=[{'source_url':'https://www.n11.com/urun/samsung-galaxy-a54-12345001'}]
        self.assertEqual(verify_variant_catalog_readback([original],[after],{'rows':[]})['parent_attributes_unchanged'],1)
        for key,value in [('battery_mah',4999),('Camera',{'Resolution':'48 MP'}),
                          ('Software',{'OS':'Android 15'}),('flag',True),('extra','unexpected')]:
            changed=deepcopy(after);changed['attributes'][key]=value
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'original_catalog_attributes_changed_after_variants'):
                verify_variant_catalog_readback([original],[changed],{'rows':[]})
        changed=deepcopy(after);del changed['attributes']['Camera']
        with self.assertRaisesRegex(ValueError,'original_catalog_attributes_changed_after_variants'):
            verify_variant_catalog_readback([original],[changed],{'rows':[]})


if __name__ == '__main__':
    unittest.main()
