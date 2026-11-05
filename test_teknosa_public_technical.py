from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest

from bs4 import BeautifulSoup

from audit_priority_retailers import inspect_target
from observed_memory_variants import inspect_teknosa_memory_variant
from priority_retailer_transport import PriorityRetailerTransport
from selected_sku_evidence import selected_sku_evidence
from teknosa_public_technical import classification_url
from teknosa_reviewed_source import parse_catalog_offer, TeknosaCatalogSource
from test_pasaj_source import Response

URL = 'https://www.teknosa.com/samsung-galaxy-s25-ultra-5g-512gb-titanyum-gumus-akilli-telefon-p-125079845'
FEATURE_URL = 'https://www.teknosa.com/getClassificationData?productCode=125079845'
SPECS = {'ram_gb': 12, 'storage_gb': 512, 'network_generation': '5g'}
PROFILE = {'name': 'Samsung Galaxy S25 Ultra', 'specs': SPECS}


def initial_document():
    # Existing actual selected PDP capture was taken after clicking its native
    # tab. Reproduce the observed initial empty-container contract, and retain
    # all original commercial fields/prices without inventing a quote.
    html = Path('fixtures/teknosa-selected-s25-ultra-512-physical-table.html').read_text()
    soup = BeautifulSoup(html, 'html.parser')
    soup.select_one('#tableContainer').clear()
    trigger = soup.new_tag('button', id='js-psh-btn')
    trigger['data-product-code'] = '125079845'  # Observed on the live native PDP.
    soup.select_one('.pdp').append(trigger)
    return str(soup)


def observation():
    return {'document_url': URL, 'source_url': FEATURE_URL,
            'observed_at': datetime.now(timezone.utc).isoformat(),
            'body': Path('fixtures/teknosa-s25-ultra-512-public-classification.json').read_text()}


class PublicTechnicalTests(unittest.TestCase):
    def test_exact_readonly_identity_preflight_keeps_selected_commerce_and_independent_radio_separate(self):
        from types import SimpleNamespace
        from sweep_priority_catalog import target_preflight

        url = 'https://www.teknosa.com/samsung-galaxy-a16-6gb-128gb-gri-akilli-telefon-p-125079845'
        witness_url = 'https://www.trendyol.com/samsung/galaxy-a16-128-gb-6-gb-ram-gri-p-879171111'
        witness_html = Path('fixtures/trendyol-a16-6gb-selected-unknown-ram-property.html').read_text()
        old_title = 'Samsung Galaxy S25 Ultra 5G 512GB Titanyum Gümüş Akıllı Telefon'
        new_title = 'Samsung Galaxy A16 6 GB RAM 128 GB Gri Akıllı Telefon'
        html = (initial_document().replace(old_title, new_title)
                .replace(old_title.removeprefix('Samsung '), new_title.removeprefix('Samsung '))
                .replace('8806097015154', '8806095832838')
                .replace(URL, url).replace(URL.removeprefix('https://www.teknosa.com'),
                                            url.removeprefix('https://www.teknosa.com')))
        fields = json.loads(observation()['body'])
        storage = next(field for field in fields[0]['features'] if field['name'] == 'Dahili Hafıza')
        radio = next(field for field in fields[0]['features'] if field['name'] == '5G Desteği')
        storage['featureValues'][0]['value'] = '128 GB'
        product = {'id': 16, 'name': 'Samsung Galaxy A16 4G 6GB 128GB', 'slug': 'a16-4g-6gb-128gb',
                   'attributes': {'ram_gb': 6, 'storage_gb': 128, 'Connectivity': {'5G': False}},
                   'offer_targets': [{'source_url': witness_url}]}
        client = SimpleNamespace(get_published_products=lambda: [product])

        for features, body, expected, witness_read in [
            ([storage], html, 'independent_radio_and_selected_commerce_verified', True),
            ([storage, radio], html, 'selected_technical_classification_conflict', False),
            ([storage], html.replace('8806095832838', '8806097015154'),
             'independent_selected_gtin_unavailable', True),
        ]:
            with self.subTest(status=expected):
                calls = []
                class Transport:
                    def read_document(self, target):
                        calls.append(target)
                        return (body if target == url else witness_html), 'document_read', 200

                    def read_teknosa_classification(self, document_url, target):
                        self.assert_source(document_url, target)
                        return json.dumps([{**fields[0], 'features': features}]), 'document_read', 200

                    def assert_source(self, document_url, target):
                        if document_url != url or target != FEATURE_URL:
                            raise AssertionError('Wrong selected classification URL')

                    def resolved_url(self, target): return target
                    def report(self): return {}

                result = target_preflight(client=client, url=url, slug=product['slug'],
                                          transport=Transport(), probe_teknosa_identity=True)
                row, = result['rows']
                self.assertIn('independent_radio_preflight', row, row['reader_reason'])
                linked = row['independent_radio_preflight']
                self.assertEqual(linked['status'], expected)
                self.assertEqual(result['status'], 'completed' if expected.endswith('_verified') else 'rejected')
                self.assertEqual(calls, [url, witness_url] if witness_read else [url])
                self.assertFalse(result['writes'])
                self.assertIsNone(result['public_parser_proof'])
                self.assertIsNone(row['prepared_offer'])
                self.assertNotIn('price', linked)

    def test_real_public_feature_response_completes_same_selected_sku_not_a_catalog_default(self):
        html, proof = initial_document(), observation()
        self.assertEqual(classification_url(URL, html), FEATURE_URL)
        self.assertIsNone(parse_catalog_offer(html, URL, PROFILE)[0])
        offer, reason = parse_catalog_offer(html, URL, PROFILE, proof)
        self.assertEqual(reason, 'selected_teknosa_marketplace_cash_offer')
        selected = selected_sku_evidence(offer)
        self.assertEqual((selected['ram_gb'], selected['storage_gb'], selected['network_generation']), (12, 512, '5g'))
        self.assertEqual(offer['source_title'], 'Samsung Galaxy S25 Ultra 5G 512GB Titanyum Gümüş Akıllı Telefon')
        for fields in ({'ram_gb': 8}, {'storage_gb': 256}, {'network_generation': '4g'}):
            self.assertIsNone(parse_catalog_offer(html, URL, {**PROFILE, 'specs': {**SPECS, **fields}}, proof)[0])
        # Other-memory discovery uses the source's capacities, and the ordinary
        # reader still proves the whole live offer before it becomes a preview.
        other = inspect_teknosa_memory_variant(html, URL, PROFILE['name'], {**SPECS, 'storage_gb': 256}, proof)
        self.assertEqual(other['observed_specs'], SPECS)
        self.assertFalse(other['writes'])

    def test_wrong_request_source_model_timestamp_or_partial_response_cannot_supply_facts(self):
        html, proof = initial_document(), observation()
        cases = []
        for key, value in [('document_url', URL + '?shopId=3736'),
                           ('document_url', URL.replace('125079845', '125079843')),
                           ('source_url', FEATURE_URL.replace('125079845', '125079843')),
                           ('source_url', FEATURE_URL + '&token=private'),
                           ('source_url', FEATURE_URL.replace('.com/', '.com.evil.test/')),
                           ('observed_at', (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()),
                           ('observed_at', (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()),
                           ('observed_at', datetime.now().isoformat()), ('body', '{}'), ('body', 'x' * 65537)]:
            cases.append({**proof, key: value})
        original = json.loads(proof['body'])
        for label, value in [('Bellek Kapasitesi (RAM)', '12+12 GB'), ('Bellek Kapasitesi (RAM)', '8 GB'),
                             ('Dahili Hafıza', '256 GB'), ('5G Desteği', 'Hayır'),
                             ('Model Kodu', 'Galaxy S25'), ('Model Kodu', 'Galaxy S25 Ultra Plus'),
                             ('Model Kodu', ''), ('Model Kodu', 'Galaxy'), ('Model Kodu', 'Ultra')]:
            data = deepcopy(original)
            next(r for r in data[0]['features'] if r['name'] == label)['featureValues'][0]['value'] = value
            cases.append({**proof, 'body': json.dumps(data)})
        for field in ['code', 'range', 'featureValues']:
            data = deepcopy(original)
            ram = next(r for r in data[0]['features'] if r['name'] == 'Bellek Kapasitesi (RAM)')
            ram[field] = {'code': 'wrong-field', 'range': True, 'featureValues': [{'value': '12 GB'}, {'value': '8 GB'}]}[field]
            cases.append({**proof, 'body': json.dumps(data)})
        for change in ['missing', 'duplicate', 'two_roots']:
            data = deepcopy(original)
            if change == 'missing': data[0]['features'] = [r for r in data[0]['features'] if r['name'] != 'Dahili Hafıza']
            if change == 'duplicate': data[0]['features'].append(deepcopy(next(r for r in data[0]['features'] if r['name'] == 'Dahili Hafıza')))
            if change == 'two_roots': data.append(deepcopy(data[0]))
            cases.append({**proof, 'body': json.dumps(data)})
        cases.append({**proof, 'body': proof['body'].replace('"range": false', '"range": true, "range": false', 1)})
        data = deepcopy(original); data[0]['features'][0]['name'] = []
        cases.append({**proof, 'body': json.dumps(data)})
        for changed in cases:
            self.assertIsNone(parse_catalog_offer(html, URL, PROFILE, changed)[0], {k:v for k,v in changed.items() if k != 'body'})
        # The actual 17T Pro response omits all four required fields; it cannot
        # borrow parent capacities or turn a numeric phone-book field into RAM.
        data = deepcopy(original); data[0]['features'] = [r for r in data[0]['features'] if r['name'] == 'Ürün Rengi']
        self.assertIsNone(parse_catalog_offer(html, URL, PROFILE, {**proof, 'body': json.dumps(data)})[0])

    def test_existing_contradictory_or_outside_tables_and_commercial_drifts_still_reject(self):
        html, proof = initial_document(), observation()
        for selector, attr, value in [('button#addToCartButton', 'data-product-ean', '8806095919042'),
                                      ('button#addToCartButton', 'data-shop-name', 'Other'),
                                      ('button#addToCartButton', 'data-product-shop-id', '1001')]:
            soup = BeautifulSoup(html, 'html.parser'); soup.select_one(selector)[attr] = value
            self.assertIsNone(parse_catalog_offer(str(soup), URL, PROFILE, proof)[0])
        for selector in ['#pdp-technical', '#pdp-technical h2', '#tableContainer']:
            soup = BeautifulSoup(html, 'html.parser'); soup.select_one(selector).decompose()
            self.assertIsNone(classification_url(URL, str(soup)))
            self.assertIsNone(parse_catalog_offer(str(soup), URL, PROFILE, proof)[0])
        soup = BeautifulSoup(html, 'html.parser'); soup.select_one('#tableContainer').append(BeautifulSoup('<table class="tableFeature"><tr><th>wrong</th></tr></table>', 'html.parser'))
        self.assertIsNone(classification_url(URL, str(soup)))
        self.assertIsNone(parse_catalog_offer(str(soup), URL, PROFILE, proof)[0])
        for selector, attr, value in [('#js-psh-btn', 'data-product-code', '125079843'),
                                      ('#addToCartButton', 'data-product-id', '125079843')]:
            soup = BeautifulSoup(html, 'html.parser'); soup.select_one(selector)[attr] = value
            self.assertIsNone(classification_url(URL, str(soup)))
            self.assertIsNone(parse_catalog_offer(str(soup), URL, PROFILE, proof)[0])
        soup = BeautifulSoup(html, 'html.parser')
        trigger = soup.select_one('#js-psh-btn'); trigger.extract(); soup.body.append(trigger)
        self.assertIsNone(classification_url(URL, str(soup)))
        self.assertIsNone(parse_catalog_offer(str(soup), URL, PROFILE, proof)[0])

    def test_lazy_tab_shares_robots_denial_circuit_and_total_document_cap(self):
        calls = []
        def get(url, **kwargs):
            calls.append(url)
            if url.endswith('robots.txt'): return Response(url, 'User-agent: *\nAllow: /\n')
            return Response(url, observation()['body'] if url == FEATURE_URL else initial_document())
        transport = PriorityRetailerTransport(get=get, sleep=lambda _: None, maximum_per_host=2)
        self.assertEqual(transport.read_document(URL)[1], 'document_read')
        self.assertEqual(transport.read_teknosa_classification(URL, FEATURE_URL)[1], 'document_read')
        self.assertEqual(transport.read_teknosa_classification(URL, FEATURE_URL, fresh=True)[1], 'pdp_budget_exhausted')
        self.assertEqual(transport.report()['Teknosa']['document_requests'], 2)
        self.assertEqual(transport.report()['Teknosa']['classification_requests'], 1)
        for wrong in [FEATURE_URL + '&x=1', FEATURE_URL.replace('125079845', '125079843'), FEATURE_URL.replace('https:', 'http:')]:
            self.assertEqual(transport.read_teknosa_classification(URL, wrong)[1], 'invalid_selected_technical_url')
        denied = PriorityRetailerTransport(get=lambda url, **_: Response(url, 'User-agent: *\nDisallow: /getClassificationData\n'), sleep=lambda _: None)
        self.assertEqual(denied.read_teknosa_classification(URL, FEATURE_URL)[1], 'robots_disallowed')
        def blocked_get(url, **_):
            return Response(url, 'User-agent: *\nAllow: /\n' if url.endswith('robots.txt') else '', status=200 if url.endswith('robots.txt') else 429)
        denied = PriorityRetailerTransport(get=blocked_get, sleep=lambda _: None)
        self.assertEqual(denied.read_teknosa_classification(URL, FEATURE_URL)[1], 'http_429')
        self.assertEqual(denied.read_document(URL)[1], 'source_circuit_open')

    def test_browser_json_wrapper_and_fresh_variant_recheck_keep_the_existing_read_allowance(self):
        from incremental_memory_recovery import FreshVariantTransport
        from unittest.mock import Mock
        from html import escape
        html, proof = initial_document(), observation()
        wrapped = {**proof, 'body': '<html><body><pre>' + escape(proof['body']) + '</pre></body></html>'}
        self.assertIsNotNone(parse_catalog_offer(html, URL, PROFILE, wrapped)[0])
        for extra in ['<script>private_state</script>', '<form></form>', '<p>Another product</p>']:
            self.assertIsNone(parse_catalog_offer(html, URL, PROFILE, {**wrapped, 'body': wrapped['body'] + extra})[0])
        underlying = Mock()
        underlying.read_fresh_document.return_value = ('html', 'document_read', 200)
        underlying.read_teknosa_classification.return_value = (proof['body'], 'document_read', 200)
        fresh = FreshVariantTransport(underlying)
        for _ in range(12):
            self.assertEqual(fresh.read_document(URL)[1], 'document_read')
            self.assertEqual(fresh.read_teknosa_classification(URL, FEATURE_URL)[1], 'document_read')
        self.assertEqual(fresh.read_document(URL)[1], 'variant_recheck_budget_exhausted')
        self.assertEqual(fresh.read_teknosa_classification(URL, FEATURE_URL)[1], 'variant_recheck_budget_exhausted')
        self.assertEqual(underlying.read_fresh_document.call_count, 12)
        self.assertEqual(underlying.read_teknosa_classification.call_count, 12)
        underlying.read_teknosa_classification.assert_called_with(URL, FEATURE_URL, fresh=True)

    def test_worker_preview_and_fresh_catalog_reader_both_read_the_actual_tab(self):
        calls = []
        def get(url, **_):
            calls.append(url)
            return Response(url, 'User-agent: *\nAllow: /\n' if url.endswith('robots.txt') else observation()['body'] if url == FEATURE_URL else initial_document())
        transport = PriorityRetailerTransport(get=get, sleep=lambda _: None, maximum_per_host=4)
        product = {'id': 348, 'slug': 'samsung-galaxy-s25-ultra-12gb-512gb', 'name': PROFILE['name'], 'attributes': SPECS}
        row = inspect_target(transport, URL, product)
        self.assertEqual(row['reader_reason'], 'selected_teknosa_marketplace_cash_offer')
        self.assertIsNotNone(selected_sku_evidence(row['prepared_offer']))
        # Publication source has its own bounded source; it must independently
        # re-read both PDP and tab, never reuse the discovery's amount or facts.
        source = TeknosaCatalogSource(transport=PriorityRetailerTransport(get=get, sleep=lambda _: None, maximum_per_host=4))
        source.register_catalog_targets(PROFILE['name'], [{'source_url': URL}], SPECS)
        offer, reason, _ = source.read_offer(URL)
        self.assertEqual(reason, 'selected_teknosa_marketplace_cash_offer')
        self.assertIsNotNone(selected_sku_evidence(offer))
        self.assertEqual(calls.count(FEATURE_URL), 2)


if __name__ == '__main__':
    unittest.main()
