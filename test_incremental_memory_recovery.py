from copy import deepcopy
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from audit_priority_retailers import inspect_target
from automatic_memory_variants import CONTRACT, TechnicalVariantReader
from hepsiburada_catalog_source import HepsiburadaCatalogSource
from incremental_memory_recovery import FreshVariantTransport, IncrementalMemoryRecovery
from priority_retailer_transport import PriorityRetailerTransport
from retailer_offer_refresh import RetailerOfferRefresher
from sweep_priority_catalog import main, sweep
from test_hepsiburada_catalog_source import document, TARGET, NAME, SPECS
from test_pasaj_source import Response
from test_priority_candidates import Client


class GrowingClient(Client):
    def __init__(self, failure=None):
        super().__init__()
        self.product['attributes']['ram_gb'] = 6
        self.catalog = [self.product]
        self.variant_offers = {}
        self.events = []
        self.failure = failure

    def get_published_products(self):
        self.events.append(('catalog', len(self.catalog)))
        return deepcopy(self.catalog)

    def get_public_price_priority(self):
        return {p['id']: (p['slug'], p['slug'] in self.variant_offers) for p in self.catalog}

    def get_offer_identity_readiness(self):
        self.events.append(('commercial', len(self.catalog)))
        rows = deepcopy(self.variant_offers)
        for batch in self.writes:
            for record in batch:
                rows.setdefault(record['product_slug'], []).append({
                    'source_url': record['source_url'], 'merchant_name': record['seller'], 'price': record['price']})
        if self.failure == 'price':
            for offers in rows.values():
                for offer in offers:
                    offer['price'] += 1
        return {'evidenceVersion': 'product-offer-match-v1', 'mode': 'shadow_audit_only',
            'auditedAt': datetime.now(timezone.utc).isoformat(),
            'productCoverage': [{'slug': p['slug'], 'matchedOffers': rows.get(p['slug'], [])}
                                for p in self.catalog]}

    def atomic_client(self):
        def request(method, url, **kwargs):
            payload = kwargs['json']
            variant = payload['variant']
            child = {'id': 1000 + len(self.catalog), 'name': variant['name'], 'slug': variant['slug'],
                'attributes': {'ram_gb': variant['ram_gb'], 'storage_gb': variant['storage_gb'],
                               'Connectivity': {'5G': variant['network_generation'] == '5g'}},
                'offer_targets': [], 'offer_targets_truncated': False}
            self.catalog.append(child)
            self.variant_offers[child['slug']] = deepcopy(payload['offers'])
            self.events.append(('atomic', child['slug']))
            if self.failure == 'parent':
                self.product['attributes']['ram_gb'] = 12
            if self.failure == 'unknown':
                self.catalog.append({**deepcopy(child), 'id': 9000, 'slug': 'unacknowledged'})
            return {'committed': self.failure != 'ack', 'contract': CONTRACT, 'outcome': 'created',
                'slug': child['slug'], 'product_id': child['id'], 'parent_product_id': self.product['id'],
                'parent_unchanged': True, 'scores_recalculated': True}
        return SimpleNamespace(base_url='https://teknoskor.com', secret='fixture-only',
            session=SimpleNamespace(request=request), _json_body=lambda response: response)


def source(client, *, category_denial=False):
    def get(url, **kwargs):
        if url.endswith('robots.txt'):
            return Response(url, 'User-agent: *\nAllow: /\n')
        if url == TARGET:
            client.events.append(('pdp', url))
            return Response(url, document())
        client.events.append(('category', url))
        return Response(url, 'denied', status=403 if category_denial else 200)
    def post(url, **kwargs):
        return Response(url, json.dumps({'status': 'ok', 'solution': {
            'url': kwargs['json']['url'], 'status': 403, 'response': 'denied'}}))
    return PriorityRetailerTransport(get=get, post=post, sleep=lambda _: None, maximum_per_host=100)


class IncrementalMemoryRecoveryTests(unittest.TestCase):
    def recovery(self, client, transport, *, publish=True):
        return IncrementalMemoryRecovery(client=client, transport=transport, publish=publish,
            source_client=client.atomic_client(), image_store=Mock(upload=Mock(return_value='https://cdn.teknoskor.com/fixture.webp')),
            technical_reader=SimpleNamespace(documents=1, attempts=1, diagnostics=[]))

    def test_missing_capacity_published_and_commercially_verified_before_later_category_denial(self):
        client = GrowingClient()
        transport = source(client, category_denial=True)
        recovery = self.recovery(client, transport)
        original = deepcopy(client.product)
        # Technical acquisition is independently tested in automatic_memory_variants;
        # the actual retailer preview, fresh recheck and writer run here unchanged.
        class SharedOfferTransport(FreshVariantTransport):
            def configure_catalog_budget(self, count):
                self.transport.configure_catalog_budget(count)
        with patch('automatic_memory_variants.technical_records', return_value=([], {}, ['source-image'])):
            reader = RetailerOfferRefresher(hepsiburada_catalog_source=HepsiburadaCatalogSource(
                transport=SharedOfferTransport(transport)))
            result = sweep(client=client, transport=transport, reader=reader, publish=True, discover_hepsiburada=True,
                           memory_discovery=True, memory_recovery=recovery)
        self.assertFalse(recovery.failed, recovery.report)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual((result['initial_catalog_count'], result['catalog_count']), (1, 2))
        self.assertEqual(recovery.report['created'], 1)
        self.assertTrue(recovery.report['rows'][0]['commercial_readback_verified'])
        self.assertEqual(client.product, original)
        kinds = [event[0] for event in client.events]
        self.assertLess(kinds.index('atomic'), kinds.index('category'))
        self.assertIn('commercial', kinds[kinds.index('atomic'):kinds.index('category')])
        self.assertEqual(recovery.report['catalog_readback']['parent_identities_unchanged'], 1)
        self.assertTrue(result['sources']['Hepsiburada']['blocked'])
        self.assertEqual(recovery.report['attempted_candidates'], 1)
        # The ordinary publisher after child adoption must accept the new whole
        # catalog count and reread the actual child, never the original 6GB SKU.
        publications = [r['publication'] for r in result['rows'] if r.get('publication')]
        self.assertTrue(any(p.get('commercial_readback_verified') for p in publications), publications)
        self.assertEqual(len(client.writes), 1)
        self.assertNotEqual(client.writes[0][0]['product_slug'], client.product['slug'])

    def test_unknown_commit_parent_drift_or_wrong_public_quote_stops_all_later_writes(self):
        for failure in ('ack', 'parent', 'unknown', 'price'):
            with self.subTest(failure=failure):
                client = GrowingClient(failure)
                transport = source(client, category_denial=True)
                recovery = self.recovery(client, transport)
                with patch('automatic_memory_variants.technical_records', return_value=([], {}, ['source-image'])):
                    result = sweep(client=client, transport=transport, publish=True, discover_hepsiburada=True,
                                   memory_discovery=True, memory_recovery=recovery)
                self.assertTrue(recovery.failed)
                self.assertEqual(result['status'], 'variant_publication_verification_failed')
                self.assertTrue(result['writes'])  # Includes uploaded source images.
                self.assertEqual(client.writes, [])
                self.assertEqual(sum(e[0] == 'atomic' for e in client.events), 1)
                self.assertTrue(any(e[0] == 'category' for e in client.events))

    def test_cli_uses_shared_transport_and_preserves_original_and_expanded_coverage_gates(self):
        client = GrowingClient()
        transport = source(client, category_denial=True)
        class SharedOfferTransport(FreshVariantTransport):
            def configure_catalog_budget(self, count):
                self.transport.configure_catalog_budget(count)
        reader = RetailerOfferRefresher(hepsiburada_catalog_source=HepsiburadaCatalogSource(
            transport=SharedOfferTransport(transport)))
        directory = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as temporary, \
                    patch('sys.argv', ['sweep_priority_catalog.py', '--memory-variants', '--publish']), \
                    patch('sweep_priority_catalog.OfferIngestionClient.from_env', return_value=client), \
                    patch('sweep_priority_catalog.PriorityRetailerTransport', return_value=transport), \
                    patch('sweep_priority_catalog.RetailerOfferRefresher', return_value=reader), \
                    patch('source_ingestion.SourceIngestionClient.from_env', return_value=client.atomic_client()), \
                    patch('automatic_memory_variants.DiscoveryImageStore', return_value=Mock(
                        upload=Mock(return_value='https://cdn.teknoskor.com/fixture.webp'))), \
                    patch('automatic_memory_variants.technical_records', return_value=([], {}, ['source-image'])), \
                    patch('hepsiburada_category_frontier.save_frontier', return_value={'status': 'fixture'}), \
                    redirect_stdout(io.StringIO()):
                os.chdir(temporary)
                with self.assertRaisesRegex(SystemExit, 'Public catalog coverage gate failed'):
                    main()
                result = json.loads(Path('priority-catalog-sweep.json').read_text())
                variants = json.loads(Path('automatic-memory-variants.json').read_text())
        finally:
            os.chdir(directory)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['original_catalog_coverage']['catalog'], 1)
        self.assertEqual(result['original_catalog_coverage']['priced'], 0)
        self.assertEqual(result['public_coverage']['catalog'], 2)
        self.assertEqual(result['public_coverage']['priced'], 1)
        self.assertFalse(result['public_coverage']['passed'])
        self.assertEqual(variants['created'], 1)
        self.assertTrue(variants['rows'][0]['commercial_readback_verified'])
        self.assertEqual(variants['fresh_recheck_attempts']['Hepsiburada'], 1)
        self.assertEqual(result['priority_store_coverage_before']['catalog'], 1)
        self.assertEqual(result['priority_store_coverage_before']['retailers']['Hepsiburada']['models_with_current_listing'], 0)
        self.assertEqual(result['priority_store_coverage_after']['catalog'], 2)
        self.assertEqual(result['priority_store_coverage_after']['retailers']['Hepsiburada']['models_with_current_listing'], 1)

    def test_readonly_early_and_final_pass_share_attempt_and_technical_reader_without_writes(self):
        client = GrowingClient()
        transport = source(client)
        recovery = self.recovery(client, transport, publish=False)
        products, prices = client.get_published_products(), client.get_public_price_priority()
        recovery.start(products, prices)
        row = inspect_target(transport, TARGET, products[0])
        result = {'status': 'completed', 'catalog_count': 1, 'writes': False, 'rows': [row]}
        with patch('automatic_memory_variants.technical_records', return_value=([], {}, ['source-image'])) as technical:
            recovery.observe(result, row, products, prices)
            recovery.observe(result, row, products, prices)
            recovery.finish(result, products, prices)
        self.assertFalse(recovery.failed)
        self.assertEqual(technical.call_count, 1)
        self.assertEqual(recovery.report['attempted_candidates'], 1)
        self.assertEqual(recovery.report['verified_candidates'], 1)
        self.assertFalse(recovery.report['writes'])
        recovery.image_store.upload.assert_not_called()
        self.assertFalse(any(e[0] == 'atomic' for e in client.events))

    def test_priced_family_can_use_one_bounded_early_slot_without_a_duplicate_final_attempt(self):
        client = GrowingClient()
        transport = source(client)
        recovery = self.recovery(client, transport, publish=False)
        products, prices = client.get_published_products(), client.get_public_price_priority()
        prices[products[0]['id']] = (products[0]['slug'], True)
        recovery.start(products, prices)
        row = inspect_target(transport, TARGET, products[0])
        result = {'status': 'completed', 'catalog_count': 1, 'writes': False, 'rows': [row]}
        with patch('automatic_memory_variants.technical_records', return_value=([], {}, ['source-image'])) as technical:
            recovery.observe(result, row, products, prices)
            self.assertEqual(technical.call_count, 1)
            self.assertEqual(recovery.report['early_covered_family_attempts'], 1)
            recovery.finish(result, products, prices)
            self.assertEqual(technical.call_count, 1)

    def test_covered_family_live_offer_is_published_and_verified_before_later_source_denial(self):
        class PricedClient(GrowingClient):
            def get_public_price_priority(self):
                return {p['id']: (p['slug'], True) for p in self.catalog}
        client = PricedClient()
        transport = source(client, category_denial=True)
        recovery = self.recovery(client, transport)
        original = deepcopy(client.product)
        with patch('automatic_memory_variants.technical_records', return_value=([], {}, ['source-image'])):
            result = sweep(client=client, transport=transport, publish=True, discover_hepsiburada=True,
                memory_discovery=True, memory_recovery=recovery)
        self.assertFalse(recovery.failed, recovery.report)
        self.assertEqual(recovery.report['created'], 1)
        self.assertEqual(recovery.report['early_covered_family_attempts'], 1)
        self.assertTrue(recovery.report['rows'][0]['commercial_readback_verified'])
        self.assertTrue(result['sources']['Hepsiburada']['blocked'])
        kinds = [event[0] for event in client.events]
        self.assertLess(kinds.index('atomic'), kinds.index('category'))
        self.assertEqual(client.product, original)

    def test_covered_family_early_cap_preserves_slots_for_a_later_missing_family(self):
        client = GrowingClient()
        parents, rows = [], []
        for number in (36, 37, 38, 39):
            parent = deepcopy(client.product)
            parent.update(id=number, slug=f'samsung-galaxy-a{number}-5g', name=NAME.replace('A36', f'A{number}'))
            url = TARGET.replace('a36', f'a{number}')
            html = document().replace('A36', f'A{number}').replace('a36', f'a{number}')
            transport = SimpleNamespace(read_document=lambda url, body=html: (body, 'document_read', 200),
                resolved_url=lambda url: url)
            rows.append(inspect_target(transport, url, parent))
            parents.append(parent)
        prices = {p['id']: (p['slug'], p['id'] != 39) for p in parents}
        recovery = self.recovery(client, SimpleNamespace(report=lambda: {}), publish=False)
        recovery.start(parents, prices)
        result = {'status': 'completed', 'catalog_count': 4, 'writes': False, 'rows': []}
        with patch('incremental_memory_recovery.MAX_VARIANTS', 4), \
             patch('automatic_memory_variants.technical_records', side_effect=ValueError('fixture_unavailable')) as technical:
            for row in rows:
                result['rows'].append(row)
                recovery.observe(result, row, parents, prices)
            self.assertEqual(technical.call_count, 3)
            self.assertEqual([call.args[0]['id'] for call in technical.call_args_list], [36, 37, 39])
            self.assertEqual(recovery.report['early_covered_family_attempts'], 2)
            self.assertEqual(recovery.report['early_unpriced_family_attempts'], 1)
            recovery.finish(result, parents, prices)
            self.assertEqual([call.args[0]['id'] for call in technical.call_args_list], [36, 37, 39, 38])
        self.assertFalse(recovery.failed, recovery.report)
        self.assertEqual(recovery.report['attempted_candidates'], 4)
        self.assertFalse(recovery.report['writes'])

    def test_category_only_covered_capacity_reaches_fresh_proof_and_publication_before_next_category_denial(self):
        from hepsiburada_catalog_discovery import CATEGORY_URL
        from test_hepsiburada_catalog_discovery import SAMSUNG
        class PricedClient(GrowingClient):
            def get_public_price_priority(self):
                return {p['id']: (p['slug'], True) for p in self.catalog}
        client = PricedClient()
        client.product['offer_targets'] = []
        original = deepcopy(client.product)
        title = NAME + ' 128 GB 8 GB RAM'
        category = (f'<h1>Cep Telefonu Fiyatları ve Modelleri</h1><h2 data-test-id="title-1">'
                    f'<a href="{TARGET}" title="{title}">{title}</a></h2><a href="{SAMSUNG}">Samsung</a>')
        def get(url, **kwargs):
            if url.endswith('robots.txt'):
                return Response(url, 'User-agent: *\nAllow: /\n')
            if url == CATEGORY_URL:
                client.events.append(('category_root', url))
                return Response(url, category)
            if url == TARGET:
                client.events.append(('pdp', url))
                return Response(url, document())
            client.events.append(('category_denied', url))
            return Response(url, 'denied', status=403)
        def post(url, **kwargs):
            return Response(url, json.dumps({'status': 'ok', 'solution': {
                'url': kwargs['json']['url'], 'status': 403, 'response': 'denied'}}))
        transport = PriorityRetailerTransport(get=get, post=post, sleep=lambda _: None, maximum_per_host=100)
        recovery = self.recovery(client, transport)
        class SharedOfferTransport(FreshVariantTransport):
            def configure_catalog_budget(self, count):
                self.transport.configure_catalog_budget(count)
        reader = RetailerOfferRefresher(hepsiburada_catalog_source=HepsiburadaCatalogSource(
            transport=SharedOfferTransport(transport)))
        with patch('automatic_memory_variants.technical_records', return_value=([], {}, ['source-image'])):
            result = sweep(client=client, transport=transport, reader=reader, publish=True,
                discover_hepsiburada=True, memory_discovery=True, memory_recovery=recovery)
        self.assertFalse(recovery.failed, recovery.report)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(recovery.report['created'], 1)
        self.assertEqual(recovery.report['early_covered_hint_families'], 1)
        self.assertEqual(recovery.report['early_covered_family_attempts'], 1)
        self.assertTrue(recovery.report['rows'][0]['commercial_readback_verified'])
        kinds = [event[0] for event in client.events]
        self.assertLess(kinds.index('category_root'), kinds.index('pdp'))
        self.assertLess(kinds.index('pdp'), kinds.index('atomic'))
        self.assertLess(kinds.index('atomic'), kinds.index('category_denied'))
        self.assertEqual(client.product, original)

    def test_unverified_category_hints_share_two_covered_family_read_slots_and_unknown_prices_get_none(self):
        from hepsiburada_catalog_discovery import CATEGORY_URL
        from test_hepsiburada_catalog_discovery import SAMSUNG
        for known_priced in (True, None):
            client = GrowingClient()
            parents, cards, targets = [], [], []
            for number in (36, 37, 38):
                parent = deepcopy(client.product)
                parent.update(id=number, slug=f'samsung-galaxy-a{number}-5g', name=NAME.replace('A36', f'A{number}'), offer_targets=[])
                target = TARGET.replace('a36', f'a{number}')
                title = parent['name'] + ' 128 GB 8 GB RAM'
                cards.append(f'<h2 data-test-id="title-{number}"><a href="{target}" title="{title}">{title}</a></h2>')
                parents.append(parent)
                targets.append(target)
            client.catalog = parents
            client.get_public_price_priority = lambda: {p['id']: (p['slug'], known_priced) for p in parents}
            category = '<h1>Cep Telefonu Fiyatları ve Modelleri</h1>' + ''.join(cards) + f'<a href="{SAMSUNG}">Samsung</a>'
            observed_pdps = []
            def get(url, **kwargs):
                if url.endswith('robots.txt'):
                    return Response(url, 'User-agent: *\nAllow: /\n')
                if url == CATEGORY_URL:
                    return Response(url, category)
                if url in targets:
                    observed_pdps.append(url)
                    return Response(url, '<h1>Unverified listing without selected SKU or seller proof</h1>')
                return Response(url, 'denied', status=403)
            def post(url, **kwargs):
                return Response(url, json.dumps({'status': 'ok', 'solution': {
                    'url': kwargs['json']['url'], 'status': 403, 'response': 'denied'}}))
            transport = PriorityRetailerTransport(get=get, post=post, sleep=lambda _: None, maximum_per_host=100)
            recovery = self.recovery(client, transport, publish=False)
            with patch('automatic_memory_variants.technical_records') as technical:
                result = sweep(client=client, transport=transport, publish=False, discover_hepsiburada=True,
                    memory_discovery=True, memory_recovery=recovery)
            self.assertFalse(recovery.failed, recovery.report)
            self.assertEqual(observed_pdps, targets[:2] if known_priced else [])
            self.assertEqual(recovery.report['early_covered_hint_families'], 2 if known_priced else 0)
            self.assertEqual(recovery.report['attempted_candidates'], 0)
            self.assertFalse(result['writes'])
            technical.assert_not_called()
            recovery.image_store.upload.assert_not_called()
            self.assertFalse(any(event[0] == 'atomic' for event in client.events))

    def test_unknown_public_price_hint_cannot_use_a_covered_family_early_slot(self):
        client = GrowingClient()
        transport = source(client)
        recovery = self.recovery(client, transport, publish=False)
        products = client.get_published_products()
        prices = {products[0]['id']: (products[0]['slug'], None)}
        recovery.start(products, prices)
        row = inspect_target(transport, TARGET, products[0])
        result = {'status': 'completed', 'catalog_count': 1, 'writes': False, 'rows': [row]}
        with patch('automatic_memory_variants.technical_records', return_value=([], {}, ['source-image'])) as technical:
            recovery.observe(result, row, products, prices)
            technical.assert_not_called()
            recovery.finish(result, products, prices)
            self.assertEqual(technical.call_count, 1)
        self.assertEqual(recovery.report['early_covered_family_attempts'], 0)

    def test_failed_category_reservation_cannot_block_already_selected_live_proof(self):
        client = GrowingClient()
        parents, rows = [], []
        for number in (36, 37, 38):
            parent = deepcopy(client.product)
            parent.update(id=number, slug=f'samsung-galaxy-a{number}-5g', name=NAME.replace('A36', f'A{number}'))
            url = TARGET.replace('a36', f'a{number}')
            html = document().replace('A36', f'A{number}').replace('a36', f'a{number}')
            transport = SimpleNamespace(read_document=lambda url, body=html: (body, 'document_read', 200),
                resolved_url=lambda url: url)
            rows.append(inspect_target(transport, url, parent))
            parents.append(parent)
        prices = {p['id']: (p['slug'], True) for p in parents}
        recovery = self.recovery(client, SimpleNamespace(report=lambda: {}), publish=False)
        recovery.start(parents, prices)
        result = {'status': 'completed', 'catalog_count': 3, 'writes': False, 'rows': rows}
        with patch('automatic_memory_variants.technical_records', side_effect=ValueError('fixture_unavailable')) as technical:
            recovery.observe(result, rows[0], parents, prices)
            self.assertTrue(recovery.admit_early_memory_hint(parents[1], parents, prices))
            self.assertFalse(recovery.admit_early_memory_hint(parents[2], parents, prices))
            recovery.observe(result, rows[1], parents, prices)
            recovery.observe(result, rows[2], parents, prices)
            self.assertEqual([call.args[0]['id'] for call in technical.call_args_list], [36, 37, 38])
            recovery.finish(result, parents, prices)
            self.assertEqual([call.args[0]['id'] for call in technical.call_args_list], [36, 37, 38])
        self.assertEqual(recovery.report['early_covered_family_attempts'], 3)
        self.assertFalse(recovery.failed, recovery.report)

    def test_proved_families_use_only_half_the_shared_allowance_before_source_closure(self):
        client = GrowingClient()
        parents, rows = [], []
        for number in range(36, 44):
            parent = deepcopy(client.product)
            parent.update(id=number, slug=f'samsung-galaxy-a{number}-5g', name=NAME.replace('A36', f'A{number}'))
            url = TARGET.replace('a36', f'a{number}')
            body = document().replace('A36', f'A{number}').replace('a36', f'a{number}')
            source = SimpleNamespace(read_document=lambda url, html=body: (html, 'document_read', 200),
                                     resolved_url=lambda url: url)
            parents.append(parent)
            rows.append(inspect_target(source, url, parent))
        prices = {p['id']: (p['slug'], p['id'] != 43) for p in parents}
        state = {'Hepsiburada': {'blocked': False}}
        recovery = self.recovery(client, SimpleNamespace(report=lambda: deepcopy(state)), publish=False)
        recovery.start(parents, prices)
        # Two routing-only reservations neither authorize offers nor consume
        # the six selected-proof slots when their PDP never proves a variant.
        self.assertTrue(recovery.admit_early_memory_hint(parents[0], parents, prices))
        self.assertTrue(recovery.admit_early_memory_hint(parents[1], parents, prices))
        self.assertFalse(recovery.admit_early_memory_hint(parents[2], parents, prices))
        result = {'status': 'completed', 'catalog_count': len(parents), 'writes': False, 'rows': []}
        with patch('automatic_memory_variants.technical_records', side_effect=ValueError('fixture_unavailable')) as technical:
            for row in rows:
                result['rows'].append(row)
                recovery.observe(result, row, parents, prices)
            # Six covered families then the later missing family; the seventh
            # covered family cannot take the missing family's reserved turn.
            self.assertEqual([call.args[0]['id'] for call in technical.call_args_list], [36, 37, 38, 39, 40, 41, 43])
            self.assertEqual(recovery.report['early_covered_family_limit'], 6)
            self.assertEqual(recovery.report['early_covered_hint_family_limit'], 2)
            state['Hepsiburada']['blocked'] = True
            recovery.finish(result, parents, prices)
            self.assertEqual(technical.call_count, 7)
        self.assertFalse(recovery.failed, recovery.report)
        self.assertEqual(recovery.report['early_covered_family_attempts'], 6)
        self.assertEqual(recovery.report['early_unpriced_family_attempts'], 1)
        self.assertEqual(recovery.report['deferred_source_candidates'], 1)
        self.assertEqual(recovery.report['attempt_limit'], 12)
        self.assertFalse(result['writes'])

    def test_global_candidate_allowance_is_shared_between_early_and_final_preparation(self):
        client = GrowingClient()
        parents, rows = [], []
        for number in (36, 37, 38):
            parent = deepcopy(client.product)
            parent.update(id=number, slug=f'samsung-galaxy-a{number}-5g', name=NAME.replace('A36', f'A{number}'))
            url = TARGET.replace('a36', f'a{number}')
            html = document().replace('A36', f'A{number}').replace('a36', f'a{number}')
            transport = SimpleNamespace(read_document=lambda url, body=html: (body, 'document_read', 200),
                                        resolved_url=lambda url: url)
            rows.append(inspect_target(transport, url, parent))
            parents.append(parent)
        prices = {p['id']: (p['slug'], False) for p in parents}
        base = SimpleNamespace(report=lambda: {})
        recovery = self.recovery(client, base, publish=False)
        recovery.start(parents, prices)
        result = {'status': 'completed', 'catalog_count': 3, 'writes': False, 'rows': []}
        with patch('incremental_memory_recovery.MAX_VARIANTS', 2), \
                patch('automatic_memory_variants.technical_records', side_effect=ValueError('fixture_unavailable')) as technical:
            for row in rows:
                result['rows'].append(row)
                recovery.observe(result, row, parents, prices)
            recovery.finish(result, parents, prices)
        self.assertFalse(recovery.failed, recovery.report)
        self.assertEqual(technical.call_count, 2)
        self.assertTrue(all(call.args[3] is recovery.technical_reader for call in technical.call_args_list))
        self.assertEqual(recovery.report['observed_candidates'], 3)
        self.assertEqual(recovery.report['attempted_candidates'], 2)

    def test_second_capacity_of_one_missing_family_waits_for_other_models(self):
        client = GrowingClient()
        transport = source(client)
        recovery = self.recovery(client, transport, publish=False)
        products, prices = client.get_published_products(), client.get_public_price_priority()
        recovery.start(products, prices)
        first = inspect_target(transport, TARGET, products[0])
        other_html = document().replace('128', '256')
        other_url = TARGET.replace('128', '256')
        other = inspect_target(SimpleNamespace(read_document=lambda url: (other_html, 'document_read', 200),
            resolved_url=lambda url: url), other_url, products[0])
        self.assertIsNotNone(other.get('other_memory_variant'))
        result = {'status': 'completed', 'catalog_count': 1, 'writes': False, 'rows': [first, other]}
        with patch('automatic_memory_variants.technical_records', side_effect=ValueError('fixture_unavailable')) as technical:
            recovery.observe(result, first, products, prices)
            recovery.observe(result, other, products, prices)
            self.assertEqual(technical.call_count, 1)
            recovery.finish(result, products, prices)
            self.assertEqual(technical.call_count, 2)
        self.assertFalse(recovery.failed, recovery.report)

    def test_fresh_reads_repeat_only_within_same_robots_circuit_and_total_budget(self):
        client = GrowingClient()
        transport = source(client)
        transport._maximum = 2
        fresh = FreshVariantTransport(transport)
        self.assertEqual(transport.read_document(TARGET)[1], 'document_read')
        self.assertEqual(transport.read_document(TARGET)[1], 'already_read')
        self.assertEqual(fresh.read_document(TARGET)[1], 'document_read')
        self.assertEqual(fresh.read_document(TARGET)[1], 'pdp_budget_exhausted')
        self.assertEqual(sum(e[0] == 'pdp' for e in client.events), 2)
        for url in ('https://evil.example/product', 'https://www.n11.com/arama?q=phone'):
            self.assertEqual(fresh.read_document(url)[1], 'invalid_priority_pdp')
        denied = source(client, category_denial=True)
        self.assertEqual(denied.read_document(TARGET)[1], 'document_read')
        category = 'https://www.hepsiburada.com/cep-telefonlari-c-371965'
        denied.read_hepsiburada_category(category)
        before = len(client.events)
        self.assertEqual(FreshVariantTransport(denied).read_document(TARGET)[1], 'source_circuit_open')
        self.assertEqual(len(client.events), before)

    def test_fresh_variant_allowance_is_24_per_source_not_reset_between_candidates(self):
        base = Mock()
        base.read_fresh_document.return_value = ('body', 'document_read', 200)
        fresh = FreshVariantTransport(base)
        for _ in range(24):
            self.assertEqual(fresh.read_document(TARGET)[1], 'document_read')
        self.assertEqual(fresh.read_document(TARGET)[1], 'variant_recheck_budget_exhausted')
        self.assertEqual(base.read_fresh_document.call_count, 24)

    def test_technical_document_allowance_cannot_restart_after_a_candidate(self):
        reader = TechnicalVariantReader(SimpleNamespace(read=lambda url: '<html/>'), maximum_documents=1)
        reader.read('https://www.kimovil.com/en/where-to-buy-fixture')
        with self.assertRaisesRegex(ValueError, 'technical_document_budget_exhausted'):
            reader.read('https://www.kimovil.com/en/where-to-buy-fixture2')
        self.assertEqual(reader.attempts, 1)


if __name__ == '__main__':
    unittest.main()
