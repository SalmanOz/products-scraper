from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import redirect_stdout
import io
import json
import os
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from manufacturer_catalog_radio import (CONTRACT, HOST, MAP_URL, SPEC_PATH,
    PublicInfinixRadioReader, CatalogRadioPublicationError, parse_infinix_radio, resolve_catalog_radios)
from offer_ingestion_client import OfferIngestionClient, IngestionClientError
from test_offer_ingestion_client import FakeResponse, FakeSession, TEST_SECRET
from update_prices import PriceUpdater

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/infinix-note40-public-radio-spec.json').read_text())


def product():
    return {'id': 219, 'name': 'Infinix Note 40', 'slug': 'infinix-note-40',
            'attributes': {'ram_gb': 8, 'storage_gb': 256,
                           'Technical sheet': {'Aliases': 'X6853'},
                           'Connectivity': {'4G LTE': 'B1 (2100)', 'WiFi': '802.11n 5GHz'}}}


def change_field(fixture, section, label, value):
    soup = BeautifulSoup(fixture['response']['data']['spec_content'], 'html.parser')
    group = next(g for g in soup.select('.size') if g.select_one(':scope > p.title').get_text(' ', strip=True).casefold() == section)
    row = next(r for r in group.select('.twoFth') if r.select_one('p.twoTitle') and
               r.select_one('p.twoTitle').get_text(' ', strip=True).casefold() == label)
    if value is None:
        row.decompose()
    else:
        row.select_one('.threeText').clear()
        row.select_one('.threeText').append(value)
    fixture['response']['data']['spec_content'] = str(soup)


class GrowingCatalogTests(unittest.TestCase):
    def test_radio_pass_preserves_the_whole_growing_catalog_without_extra_source_budget(self):
        from types import SimpleNamespace
        from catalog_recovery_limits import MAX_RECOVERY_CATALOG_PRODUCTS
        catalog = [{'id': i, 'slug': f'model-{i}', 'name': f'Catalog model {i}',
                    'attributes': {'network_generation': '4g'}} for i in range(1, 202)]
        before = deepcopy(catalog)
        client = SimpleNamespace(get_published_products=lambda: catalog)
        reader = SimpleNamespace(documents=0, blocked=False)
        result = resolve_catalog_radios(client, publish=True, reader=reader)
        self.assertEqual(result['catalog_count'], 201)
        self.assertEqual((result['resolved'], result['manufacturer_documents']), (0, 0))
        self.assertFalse(result['writes'])
        self.assertEqual(catalog, before)
        catalog.extend({'id': i, 'slug': f'model-{i}', 'name': f'Catalog model {i}',
                        'attributes': {'network_generation': '4g'}}
                       for i in range(202, MAX_RECOVERY_CATALOG_PRODUCTS + 2))
        with self.assertRaisesRegex(ValueError, 'invalid_or_oversized_catalog'):
            resolve_catalog_radios(client, publish=True, reader=reader)


class HttpResponse:
    def __init__(self, url, body, status=200):
        self.url, self.status_code, self.closed = url, status, False
        self.body = body.encode() if isinstance(body, str) else body
    def iter_content(self, chunk_size):
        yield self.body
    def close(self):
        self.closed = True


class PublicHttp:
    def __init__(self, status=200):
        self.calls, self.responses, self.status = [], [], status
    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith('/robots.txt'):
            response = HttpResponse(url, 'User-agent: *\nAllow: /\n')
        elif url == MAP_URL:
            response = HttpResponse(url, json.dumps({'code': 200, 'data': {'note40': FIXTURE['mapping_entry']}}), self.status)
        else:
            assert url == HOST + SPEC_PATH + '?id=' + FIXTURE['mapping_entry']['pid']
            response = HttpResponse(url, json.dumps(FIXTURE['response']), self.status)
        self.responses.append(response)
        return response


class CatalogClient:
    def __init__(self, *, bad_readback=False):
        self.products = [product()]
        self.calls, self.bad_readback = [], bad_readback
    def get_published_products(self):
        return deepcopy(self.products)
    def resolve_catalog_radio(self, p, observation, *, publish):
        self.calls.append((p['id'], deepcopy(observation), publish))
        generation = '5g' if '5g' in observation['network_technologies'] else '4g'
        if publish:
            self.products[0]['attributes']['network_generation'] = generation
            if self.bad_readback:
                self.products[0]['attributes']['ram_gb'] = 16
        return {'contract': CONTRACT, 'committed': publish, 'writes': publish, 'eligible': True,
                'network_generation': generation, 'product_id': p['id'], 'product_slug': p['slug'],
                'ram_storage_unchanged': True, 'outcome': 'resolved', 'scores_recalculated': True}


class ManufacturerCatalogRadioTests(unittest.TestCase):
    def test_actual_manufacturer_table_binds_model_code_and_physical_memory_ignoring_extended_ram_and_wifi(self):
        original = product()
        before = deepcopy(original)
        observation = parse_infinix_radio(original, FIXTURE['mapping_entry'], FIXTURE['response'])
        self.assertEqual((observation['model_code'], observation['ram_gb'], observation['storage_gb']), ('X6853', 8, 256))
        self.assertEqual(observation['network_technologies'], ['4g', '3g', '2g'])
        self.assertEqual(observation['source_url'], FIXTURE['source_url'])
        self.assertEqual(original, before)
        for text in ['16GB Extended RAM', '802.11 a/b/g/n/ac (2.4G, 5G)']:
            self.assertIn(text, json.dumps(FIXTURE))

    def test_wrong_alias_memory_missing_physical_cells_and_nonexplicit_network_are_not_filled_from_catalog(self):
        for section, label, value in [('size and weight', 'model', 'X6853A'),
                ('memory', 'ram', '16GB'), ('memory', 'ram', None), ('memory', 'rom', '128GB'),
                ('network', 'technology', None), ('network', 'technology', 'WiFi 5G'),
                ('network', 'technology', '4G/4G'), ('network', 'technology', '3G/2G')]:
            f = deepcopy(FIXTURE); change_field(f, section, label, value)
            with self.assertRaises(ValueError):
                parse_infinix_radio(product(), f['mapping_entry'], f['response'])
        f = deepcopy(FIXTURE); f['mapping_entry']['name'] = 'NOTE 40 Pro'
        with self.assertRaisesRegex(ValueError, 'manufacturer_model_mapping_mismatch'):
            parse_infinix_radio(product(), f['mapping_entry'], f['response'])
        f = deepcopy(FIXTURE); f['response']['data']['spec_status'] = True
        with self.assertRaises(ValueError):
            parse_infinix_radio(product(), f['mapping_entry'], f['response'])

    def test_public_reader_uses_observed_mapping_id_with_no_auth_cookie_redirect_or_unbounded_reads(self):
        anonymous = PublicInfinixRadioReader()
        self.assertFalse(anonymous._session.trust_env)
        self.assertIsNone(anonymous._session.auth)
        anonymous._session.close()
        http = PublicHttp(); reader = PublicInfinixRadioReader(get=http, sleep=lambda _: None)
        observation = reader.observe(product())
        self.assertEqual(observation['model_code'], 'X6853')
        self.assertEqual(reader.documents, 2)
        self.assertEqual(len(http.calls), 2)
        for _, kwargs in http.calls:
            self.assertFalse(kwargs['allow_redirects'])
            self.assertTrue(kwargs['stream'])
            self.assertNotIn('Authorization', kwargs['headers'])
            self.assertNotIn('Cookie', kwargs['headers'])
            self.assertNotIn('auth', kwargs)
        self.assertTrue(all(r.closed for r in http.responses))
        small = PublicInfinixRadioReader(get=PublicHttp(), sleep=lambda _: None, maximum_documents=1)
        with self.assertRaisesRegex(ValueError, 'manufacturer_document_budget_exhausted'):
            small.observe(product())
        self.assertEqual(small.documents, 1)

    def test_known_radio_and_unsupported_manufacturer_make_no_source_requests_and_denial_closes_source(self):
        http = PublicHttp(); reader = PublicInfinixRadioReader(get=http, sleep=lambda _: None)
        for change in ['canonical', 'connectivity', 'name', 'unsupported']:
            p = product()
            if change == 'canonical': p['attributes']['network_generation'] = '4g'
            elif change == 'connectivity': p['attributes']['Connectivity']['5G'] = False
            elif change == 'name': p['name'] += ' 4G'
            else: p['name'] = 'Tecno Spark 20 Pro'
            self.assertIsNone(reader.observe(p))
        self.assertEqual(http.calls, [])
        for status in [403, 429]:
            http = PublicHttp(status); reader = PublicInfinixRadioReader(get=http, sleep=lambda _: None)
            with self.assertRaises(ValueError): reader.observe(product())
            count = len(http.calls)
            with self.assertRaisesRegex(ValueError, 'manufacturer_source_circuit_open'): reader.observe(product())
            self.assertEqual(len(http.calls), count)

    def test_readonly_and_published_resolution_preserve_complete_catalog_without_price_writes(self):
        for publish in [False, True]:
            client = CatalogClient(); before = deepcopy(client.products)
            report = resolve_catalog_radios(client, publish=publish,
                reader=PublicInfinixRadioReader(get=PublicHttp(), sleep=lambda _: None))
            self.assertFalse(report['price_writes'])
            self.assertEqual(report['writes'], publish)
            self.assertEqual([call[2] for call in client.calls], [False, True] if publish else [False])
            if publish:
                self.assertEqual(client.products[0]['attributes'], {**before[0]['attributes'], 'network_generation': '4g'})
                self.assertTrue(report['rows'][0]['catalog_readback_verified'])
                self.assertEqual(report['resolved'], 1)
            else:
                self.assertEqual(client.products, before)
                self.assertEqual(report['resolved'], 0)
    def test_bad_readback_stops_before_any_later_publication(self):
        client = CatalogClient(bad_readback=True)
        with self.assertRaises(CatalogRadioPublicationError) as error:
            resolve_catalog_radios(client, publish=True,
                reader=PublicInfinixRadioReader(get=PublicHttp(), sleep=lambda _: None))
        self.assertTrue(error.exception.report['writes'])
        self.assertEqual(error.exception.report['status'], 'publication_verification_failed')
        self.assertEqual([c[2] for c in client.calls], [False, True])

    def test_dedicated_client_requires_readonly_preflight_and_committed_scored_identity_acknowledgement(self):
        p = product(); observation = parse_infinix_radio(p, FIXTURE['mapping_entry'], FIXTURE['response'])
        for publish in [False, True]:
            body = CatalogClient().resolve_catalog_radio(p, observation, publish=publish)
            session = FakeSession([FakeResponse(200, body)])
            client = OfferIngestionClient('https://teknoskor.com', TEST_SECRET, session=session)
            client.resolve_catalog_radio(p, observation, publish=publish)
            method, url, options = session.calls[0]
            self.assertEqual((method, url), ('POST', 'https://teknoskor.com/api/ingestion/catalog-radio'))
            self.assertEqual(options['headers']['Authorization'], 'Bearer ' + TEST_SECRET)
            for key, bad in ([('writes', True), ('committed', True)] if not publish else
                             [('product_id', 1), ('ram_storage_unchanged', False), ('scores_recalculated', None)]):
                invalid = {**body, key: bad}
                client = OfferIngestionClient('https://teknoskor.com', TEST_SECRET,
                    session=FakeSession([FakeResponse(200, invalid)]))
                with self.assertRaises(IngestionClientError): client.resolve_catalog_radio(p, observation, publish=publish)

    def test_canonical_radio_is_consumed_and_explicit_conflicting_catalog_values_fail_closed(self):
        attrs = product()['attributes']
        self.assertNotIn('network_generation', PriceUpdater.get_expected_specs(attrs))
        self.assertEqual(PriceUpdater.get_expected_specs({**attrs, 'network_generation': '4g'})['network_generation'], '4g')
        with self.assertRaisesRegex(ValueError, 'catalog_radio_conflict'):
            PriceUpdater.get_expected_specs({**attrs, 'network_generation': '4g', 'Connectivity': {'5G': True}})

    def test_unknown_outcome_prevents_starting_the_price_sweep(self):
        from sweep_priority_catalog import main
        report = {'writes': True, 'status': 'publication_verification_failed'}
        with patch('sys.argv', ['sweep', '--memory-variants', '--publish', '--manufacturer-radio']), \
                patch('sweep_priority_catalog.OfferIngestionClient.from_env', return_value=object()), \
                patch('manufacturer_catalog_radio.resolve_catalog_radios', side_effect=CatalogRadioPublicationError(report)), \
                patch('sweep_priority_catalog.sweep') as price_sweep:
            with self.assertRaises(CatalogRadioPublicationError): main()
        price_sweep.assert_not_called()

    def test_optional_resolution_runs_before_original_memory_baseline_and_never_changes_the_80_percent_gate(self):
        from sweep_priority_catalog import main
        client = CatalogClient()
        client.get_offer_identity_readiness = lambda: {'evidenceVersion': 'product-offer-match-v1',
            'mode': 'shadow_audit_only', 'auditedAt': datetime.now(timezone.utc).isoformat(),
            'productCoverage': [{'slug': p['slug'], 'matchedOffers': []} for p in client.products]}
        client.get_public_price_priority = lambda: {p['id']: (p['slug'], False) for p in client.products}
        baseline = []
        def fake_sweep(**kwargs):
            catalog = client.get_published_products()
            self.assertEqual(catalog[0]['attributes']['network_generation'], '4g')
            recovery = kwargs['memory_recovery']; recovery.start(catalog, {})
            baseline.extend(deepcopy(recovery.before))
            result = {'purpose': 'systematic_priority_catalog_recovery', 'catalog_count': len(catalog),
                'status': 'completed', 'rows': [], 'queued': 0, 'accepted': 0, 'writes': False,
                'models_without_priority_hints': [], 'sources': {}}
            recovery.finish(result, catalog, {})
            return result
        old_cwd = os.getcwd()
        with TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                with patch('sys.argv', ['sweep', '--memory-variants', '--publish', '--manufacturer-radio']), \
                        patch('sweep_priority_catalog.OfferIngestionClient.from_env', return_value=client), \
                        patch('source_ingestion.SourceIngestionClient.from_env', return_value=object()), \
                        patch('manufacturer_catalog_radio.PublicInfinixRadioReader', return_value=
                              PublicInfinixRadioReader(get=PublicHttp(), sleep=lambda _: None)), \
                        patch('sweep_priority_catalog.sweep', side_effect=fake_sweep), redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(SystemExit, 'coverage gate failed'): main()
                result = json.loads(Path('priority-catalog-sweep.json').read_text())
                self.assertTrue(result['writes'])
                self.assertTrue(result['manufacturer_radio_resolution']['rows'][0]['catalog_readback_verified'])
                self.assertEqual(result['public_coverage']['minimum_ratio'], 0.8)
                self.assertFalse(result['public_coverage']['passed'])
                self.assertEqual(baseline[0]['attributes']['network_generation'], '4g')
            finally:
                os.chdir(old_cwd)


if __name__ == '__main__':
    unittest.main()
