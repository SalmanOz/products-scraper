from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from hepsiburada_catalog_source import HepsiburadaCatalogSource, parse_catalog_buy_box
from publish_priority_candidates import refresh_candidates, CandidateObservationExpired
from retailer_offer_refresh import RetailerOfferRefresher
from test_hepsiburada_catalog_source import document, TARGET, NAME, SPECS


class Client:
    def __init__(self):
        self.product = {'id': 999, 'slug': 'samsung-galaxy-a36-5g', 'name': NAME,
                        'attributes': {**SPECS, 'Connectivity': {'5G': True}},
                        'offer_targets': [{'merchant_name': 'Hepsiburada', 'source_url': TARGET}],
                        'offer_targets_truncated': False}
        self.writes, self.scores = [], 0
        self.existing = []

    def get_published_products(self):
        return [deepcopy(self.product)]

    def ingest_offers(self, records, *, require_all_accepted=False):
        assert require_all_accepted is True
        self.writes.append(deepcopy(records))
        return {'committed': True, 'accepted': len(records), 'stale_ignored': 0, 'variant_rejected': 0}

    def reconcile_scores(self):
        self.scores += 1
        return {'total': 1, 'scored': 0, 'skipped': 1}

    def get_offer_identity_readiness(self):
        records = self.existing + [r for batch in self.writes for r in batch]
        return {'evidenceVersion': 'product-offer-match-v1', 'mode': 'shadow_audit_only',
                'auditedAt': datetime.now(timezone.utc).isoformat(),
                'productCoverage': [{'slug': self.product['slug'], 'matchedOffers': [
                    {'source_url': r['source_url'], 'merchant_name': r['seller'],
                     'price': int(r['price']) if float(r['price']).is_integer() else r['price']}
                    for r in records]}]}


def audit():
    offer, _ = parse_catalog_buy_box(document(), TARGET, NAME, SPECS)
    return {'purpose': 'automatic_priority_retailer_diagnosis', 'writes': False,
            'catalog_count': 1, 'rows': [{'slug': 'samsung-galaxy-a36-5g',
                'retailer': 'Hepsiburada', 'reason': 'document_read', 'http_status': 200,
                'prepared_offer': offer}]}


def reader():
    class Transport:
        def read_document(self, url):
            return document(), 'document_read', 200
    return RetailerOfferRefresher(hepsiburada_catalog_source=HepsiburadaCatalogSource(transport=Transport()))


class PriorityCandidateTests(unittest.TestCase):
    def test_n11_default_redirect_rechecks_only_the_new_selected_source_before_atomic_publication(self):
        from audit_priority_retailers import inspect_target
        from n11_catalog_source import N11CatalogSource
        from priority_retailer_transport import PriorityRetailerTransport
        from test_n11_catalog_source import URL, NAME, SPECS, document
        from test_pasaj_source import Response
        old = URL + '?magaza=retired&renk=siyah&dahili-hafiza=128-gb'
        class ScopedClient(Client):
            def ingest_listing_offers(self, rows, *, require_all_accepted):
                return {**super().ingest_offers(rows, require_all_accepted=require_all_accepted),
                        'offer_identity_contract': 'seller-source-url-v1', 'server_scores_recalculated': True}
        class FreshTransport:
            def __init__(self, changed): self.calls, self.changed = [], changed
            def read_document(self, target):
                self.calls.append(target)
                return document((lambda state, soup: state['product'].__setitem__('defaultGtin', 'bad'))
                                if self.changed else None), 'document_read', 200
            def resolved_url(self, target): return target
        for changed in (False, True):
            client = ScopedClient()
            client.product.update(name=NAME, slug='samsung-galaxy-a57',
                attributes={**SPECS, 'Connectivity': {'5G': True}},
                offer_targets=[{'merchant_name': 'Retired Store', 'source_url': old}])
            calls = []
            def get(url, **kwargs):
                calls.append(url)
                response = Response(url, 'User-agent: *\nAllow: /\n' if url.endswith('robots.txt') else document())
                if url == old:
                    response.status_code = 301
                    response.headers['Location'] = URL
                return response
            row = inspect_target(PriorityRetailerTransport(get=get, sleep=lambda _: None), old, client.product)
            self.assertEqual(calls, [old, URL])
            fresh = FreshTransport(changed)
            result = refresh_candidates({'purpose': 'automatic_priority_retailer_diagnosis', 'writes': False,
                'catalog_count': 1, 'rows': [row]}, client=client,
                reader=RetailerOfferRefresher(n11_catalog_source=N11CatalogSource(transport=fresh)))
            self.assertEqual(fresh.calls, [URL])
            self.assertEqual(len(client.writes), 0 if changed else 1)
            if not changed:
                record = client.writes[0][0]
                self.assertEqual(record['source_url'], URL)
                self.assertEqual(record['source_variant_evidence']['storage_gb'], 256)
                self.assertNotEqual(record['seller'], 'Retired Store')
                self.assertTrue(result['commercial_readback_verified'])
                self.assertTrue(result['scoring']['scores_recalculated'])

    def test_product_group_missing_first_g_requires_fresh_selected_quote_before_atomic_write_and_readback(self):
        from pathlib import Path
        from trendyol_catalog_source import TrendyolCatalogSource,parse_catalog_listing
        html=Path('fixtures/trendyol-selected-product-group-missing-first-g.html').read_text()
        url='https://www.trendyol.com/xiaomi/17t-pro-12-256g-mor-xiaomi-turkiye-garantili-p-1142620581'
        name='Xiaomi 17T Pro 12GB 256GB';specs={'ram_gb':12,'storage_gb':256,'network_generation':'5g'}
        observed,reason=parse_catalog_listing(html,url,name,specs)
        self.assertIsNotNone(observed,reason)
        class ScopedClient(Client):
            def ingest_listing_offers(self,rows,*,require_all_accepted):
                return {**super().ingest_offers(rows,require_all_accepted=require_all_accepted),
                    'offer_identity_contract':'seller-source-url-v1','server_scores_recalculated':True}
        class Source:
            def __init__(self,body): self.body,self.calls=body,[]
            def read_document(self,target): self.calls.append(target);return self.body,'document_read',200
            def resolved_url(self,target): return target
        for change in ('none','price','model','stock'):
            with self.subTest(change=change):
                client=ScopedClient();client.product.update(name=name,slug='xiaomi-17t-pro-12gb-256gb',
                    attributes={**specs,'Connectivity':{'5G':True}},offer_targets=[])
                body=(html.replace('62999','62998').replace('62.999','62.998') if change=='price' else
                      html.replace('17T Pro','17T Ultra') if change=='model' else
                      html.replace('"sellable": true','"sellable": false') if change=='stock' else html)
                source=Source(body)
                report={'purpose':'automatic_priority_retailer_diagnosis','writes':False,'catalog_count':1,
                    'rows':[{'slug':client.product['slug'],'retailer':'Trendyol','reason':'document_read',
                             'http_status':200,'prepared_offer':observed}]}
                result=refresh_candidates(report,client=client,reader=RetailerOfferRefresher(
                    trendyol_catalog_source=TrendyolCatalogSource(transport=source)))
                self.assertEqual(source.calls,[url])
                if change!='none': self.assertEqual(client.writes,[])
                else:
                    self.assertEqual(len(client.writes),1)
                    self.assertTrue(result['commercial_readback_verified'])
                    self.assertTrue(result['scoring']['scores_recalculated'])
                    self.assertEqual(client.writes[0][0]['price'],62999)
                    self.assertEqual(client.writes[0][0]['source_title'],observed['source_title'])
                    self.assertEqual(client.writes[0][0]['source_variant_evidence']['storage_gb'],256)

    def test_n11_missing_url_qualifier_uses_selected_hint_only_for_fresh_atomic_publication(self):
        from pathlib import Path
        from n11_catalog_source import N11CatalogSource, parse_catalog_listing
        html = Path(__file__).with_name('fixtures').joinpath('n11-selected-s26plus-omitted-url-qualifier.html').read_text()
        url = 'https://www.n11.com/urun/samsung-galaxy-s26-256-gb-samsung-turkiye-garantili-120594803?magaza=mediamarkt&renk=kobalt-mor'
        name, specs = 'Samsung Galaxy S26+', {'ram_gb': 12, 'storage_gb': 256, 'network_generation': '5g'}
        observed, reason = parse_catalog_listing(html, url, name, specs)
        self.assertIsNotNone(observed, reason)
        class ScopedClient(Client):
            def ingest_listing_offers(self, rows, *, require_all_accepted):
                return {**super().ingest_offers(rows, require_all_accepted=require_all_accepted),
                        'offer_identity_contract': 'seller-source-url-v1', 'server_scores_recalculated': True}
        class SourceTransport:
            def __init__(self, body):
                self.body, self.calls = body, []
            def read_document(self, target):
                self.calls.append(target)
                return self.body, 'document_read', 200
            def resolved_url(self, target):
                return target
        for change in ('none', 'title', 'price'):
            with self.subTest(change=change):
                client = ScopedClient()
                client.product.update(name=name, slug='samsung-galaxy-s26-plus',
                    attributes={**specs, 'Connectivity': {'5G': True}},
                    offer_targets=[{'merchant_name': observed['merchant'], 'source_url': url}])
                report = {'purpose': 'automatic_priority_retailer_diagnosis', 'writes': False, 'catalog_count': 1,
                    'rows': [{'slug': client.product['slug'], 'retailer': 'n11', 'reason': 'document_read',
                              'http_status': 200, 'prepared_offer': observed}]}
                body = (html.replace('S26+', 'S26 Ultra') if change == 'title' else
                        html.replace('77600', '77500').replace('77.600', '77.500') if change == 'price' else html)
                source = SourceTransport(body)
                refresher = RetailerOfferRefresher(n11_catalog_source=N11CatalogSource(transport=source))
                result = refresh_candidates(report, client=client, reader=refresher)
                self.assertEqual(source.calls, [url])
                if change != 'none':
                    self.assertEqual(client.writes, [])
                    self.assertFalse(result['writes'])
                else:
                    self.assertEqual(len(client.writes), 1)
                    self.assertTrue(result['commercial_readback_verified'])
                    self.assertEqual(client.writes[0][0]['price'], 77600)
                    self.assertEqual(client.writes[0][0]['source_variant_evidence']['group_id'], '120594803')
                    self.assertNotIn('observed_offer', client.writes[0][0])

    def test_teknosa_omitted_ram_offer_requires_fresh_physical_table_before_exact_publication(self):
        from pathlib import Path
        from teknosa_marketplace_source import parse_selected_marketplace
        from teknosa_reviewed_source import TeknosaCatalogSource
        html = Path(__file__).with_name('fixtures').joinpath('teknosa-selected-s25-ultra-512-physical-table.html').read_text()
        url = 'https://www.teknosa.com/samsung-galaxy-s25-ultra-5g-512gb-titanyum-gumus-akilli-telefon-p-125079845'
        name, specs = 'Samsung Galaxy S25 Ultra 12GB 512GB', {'ram_gb': 12, 'storage_gb': 512, 'network_generation': '5g'}
        observed, reason = parse_selected_marketplace(html, url, {'name': name, 'specs': specs})
        self.assertIsNotNone(observed, reason)
        class ScopedClient(Client):
            def ingest_listing_offers(self, rows, *, require_all_accepted):
                return {**super().ingest_offers(rows, require_all_accepted=require_all_accepted),
                        'offer_identity_contract': 'seller-source-url-v1', 'server_scores_recalculated': True}
        class SourceTransport:
            def __init__(self, body):
                self.body, self.calls = body, []
            def read_document(self, target):
                self.calls.append(target)
                return self.body, 'document_read', 200
        for changed in (False, True):
            client = ScopedClient()
            client.product.update(name=name, slug='samsung-galaxy-s25-ultra-12gb-512gb',
                attributes={**specs, 'Connectivity': {'5G': True}},
                offer_targets=[{'merchant_name': observed['merchant'], 'source_url': url}])
            report = {'purpose': 'automatic_priority_retailer_diagnosis', 'writes': False, 'catalog_count': 1,
                'rows': [{'slug': client.product['slug'], 'retailer': 'Teknosa', 'reason': 'document_read',
                          'http_status': 200, 'prepared_offer': observed}]}
            source = SourceTransport(html.replace('>12 GB</td>', '>16 GB</td>') if changed else html)
            refresher = RetailerOfferRefresher(teknosa_catalog_source=TeknosaCatalogSource(transport=source))
            result = refresh_candidates(report, client=client, reader=refresher)
            self.assertEqual(source.calls, [url])
            if changed:
                self.assertEqual(client.writes, [])
                self.assertFalse(result['writes'])
            else:
                self.assertEqual(len(client.writes), 1)
                self.assertTrue(result['commercial_readback_verified'])
                self.assertEqual(result['ingestion']['accepted'], 1)
                proof = client.writes[0][0]['source_variant_evidence']
                self.assertEqual((proof['platform'], proof['ram_gb'], proof['storage_gb']), ('Teknosa', 12, 512))

    def test_identical_technical_copies_are_rechecked_before_atomic_write_and_commercial_readback(self):
        from bs4 import BeautifulSoup
        from unittest.mock import patch
        html = BeautifulSoup(document(), 'html.parser')
        table = html.select_one('#techSpecs')
        table.insert_after(deepcopy(table))
        with patch(__name__ + '.document', return_value=str(html)):
            client = Client()
            result = refresh_candidates(audit(), client=client, reader=reader())
        self.assertEqual(len(client.writes), 1)
        self.assertEqual(client.scores, 1)
        self.assertTrue(result['commercial_readback_verified'])
        self.assertEqual(result['ingestion']['accepted'], 1)

    def test_catalog_delay_expiry_is_typed_before_any_reader_or_writer_runs(self):
        from unittest.mock import Mock, patch
        clock=[datetime.now(timezone.utc)]
        class DelayedCatalog(Client):
            def get_published_products(self):
                result=super().get_published_products()
                clock[0]+=timedelta(minutes=6)
                return result
        client, report, source=DelayedCatalog(), audit(), Mock()
        with patch('publish_priority_candidates.datetime') as timer:
            timer.fromisoformat.side_effect=datetime.fromisoformat
            timer.now.side_effect=lambda _:clock[0]
            with self.assertRaisesRegex(CandidateObservationExpired,'candidate_observation_expired'):
                refresh_candidates(report,client=client,reader=source)
        source.refresh_existing_offer_targets.assert_not_called()
        self.assertEqual((client.writes,client.scores),([],0))

    def test_readback_delay_expiry_keeps_the_final_prewrite_age_guard_without_any_write(self):
        from unittest.mock import patch
        clock=[datetime.now(timezone.utc)]
        class DelayedReadback(Client):
            def get_offer_identity_readiness(self):
                result=super().get_offer_identity_readiness()
                clock[0]+=timedelta(minutes=6)
                result['auditedAt']=clock[0].isoformat()
                return result
        client=DelayedReadback()
        with patch('publish_priority_candidates.datetime') as timer:
            timer.fromisoformat.side_effect=datetime.fromisoformat
            timer.now.side_effect=lambda _:clock[0]
            with self.assertRaisesRegex(CandidateObservationExpired,'prepared_observation_expired'):
                refresh_candidates(audit(),client=client,reader=reader())
        self.assertEqual((client.writes,client.scores),([],0))

    def test_confirmed_server_scoring_avoids_duplicate_recalculation_but_keeps_exact_readback(self):
        class ServerScoredClient(Client):
            def ingest_offers(self, records, *, require_all_accepted=False):
                return {**super().ingest_offers(records, require_all_accepted=require_all_accepted),
                        'server_scores_recalculated': True}
            def reconcile_scores(self):
                raise AssertionError('An explicitly successful server calculation must not repeat')
        client = ServerScoredClient()
        result = refresh_candidates(audit(), client=client, reader=reader())
        self.assertEqual(len(client.writes), 1)
        self.assertEqual(result['scoring']['source'], 'committed_ingestion_acknowledgement')
        self.assertTrue(result['commercial_readback_verified'])

        class MissingReadback(ServerScoredClient):
            def get_offer_identity_readiness(self):
                result = super().get_offer_identity_readiness()
                result['productCoverage'][0]['matchedOffers'] = []
                return result
        missing = MissingReadback()
        with self.assertRaisesRegex(ValueError, 'not_visible'):
            refresh_candidates(audit(), client=missing, reader=reader())
        self.assertEqual(len(missing.writes), 1)

    def test_missing_or_nonboolean_server_acknowledgement_still_requires_successful_reconciliation(self):
        for ack in (None, False, 'true', 1):
            class UnconfirmedClient(Client):
                def ingest_offers(self, records, *, require_all_accepted=False):
                    return {**super().ingest_offers(records, require_all_accepted=require_all_accepted),
                            'server_scores_recalculated': ack}
            client = UnconfirmedClient()
            result = refresh_candidates(audit(), client=client, reader=reader())
            self.assertEqual(client.scores, 1)
            self.assertTrue(result['commercial_readback_verified'])

    def test_selected_hb_seller_is_read_again_and_retained_in_the_published_source(self):
        from urllib.parse import urlencode
        client = Client()
        report = audit()
        original = report['rows'][0]['prepared_offer']
        target = TARGET + '?' + urlencode({'magaza': original['merchant']})
        scoped, why = parse_catalog_buy_box(document(), target, NAME, SPECS)
        self.assertIsNotNone(scoped, why)
        report['rows'][0]['prepared_offer'] = scoped
        result = refresh_candidates(report, client=client, reader=reader())
        self.assertEqual(result['ingestion']['accepted'], 1)
        self.assertEqual(client.writes[0][0]['source_url'], target)
        self.assertEqual(client.writes[0][0]['seller'], original['merchant'])

    def test_a37_priority_listing_uses_strict_resource_writer_without_touching_the_paired_mediamarkt_job(self):
        # The paired MediaMarkt route remains protected. This fixture exercises
        # an independently observed priority-retailer resource on the same model.
        from unittest.mock import patch
        from sweep_priority_catalog import sweep
        html = document().replace('A36', 'A37').replace('a36', 'a37').replace('8 GB RAM', '6 GB RAM').replace('8-gb-ram', '6-gb-ram')
        url = TARGET.replace('a36', 'a37').replace('8-gb-ram', '6-gb-ram')
        name = 'Samsung Galaxy A37 5G'
        specs = dict(SPECS, ram_gb=6)
        observed, why = parse_catalog_buy_box(html, url, name, specs)
        self.assertIsNotNone(observed, why)
        class ScopedClient(Client):
            def ingest_listing_offers(self, records, *, require_all_accepted=True):
                return {**super().ingest_offers(records, require_all_accepted=require_all_accepted),
                        'offer_identity_contract': 'seller-source-url-v1'}
            def ingest_offers(self, *args, **kwargs):
                raise AssertionError('No legacy seller-only writes')
        client = ScopedClient()
        client.product.update(id=278, slug='samsung-galaxy-a37-5g', name=name, attributes=specs,
                              offer_targets=[{'source_url': url, 'merchant_name': 'Hepsiburada'}])
        client.get_public_price_priority = lambda: {}
        report = audit()
        report['rows'] = [{'slug': client.product['slug'], 'retailer': 'Hepsiburada',
            'reason': 'document_read', 'http_status': 200, 'prepared_offer': observed}]
        class Transport:
            def configure_catalog_budget(self, count):
                pass
            def read_document(self, target):
                return html, 'document_read', 200
            def report(self):
                return {}
        source = RetailerOfferRefresher(hepsiburada_catalog_source=HepsiburadaCatalogSource(transport=Transport()))
        with patch('sweep_priority_catalog.inspect_target', return_value=report['rows'][0]):
            result = sweep(client=client, publish=True, transport=Transport(), reader=source)
        self.assertEqual(result['accepted'], 1)
        self.assertEqual(client.writes[0][0]['source_url'], url)
        self.assertEqual(client.writes[0][0]['product_id'], 278)
        legacy = Client()
        legacy.product = deepcopy(client.product)
        with self.assertRaisesRegex(ValueError, 'unbound_stale_or_duplicate_candidate'):
            refresh_candidates(report, client=legacy, reader=source)
        self.assertEqual(legacy.writes, [])

    def test_real_source_reread_and_all_live_gates_precede_one_atomic_batch(self):
        client = Client()
        result = refresh_candidates(audit(), client=client, reader=reader())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual((len(client.writes), len(client.writes[0]), client.scores), (1, 1, 1))
        self.assertEqual(client.writes[0][0]['source_url'], TARGET)
        self.assertEqual(client.writes[0][0]['source_title'], audit()['rows'][0]['prepared_offer']['source_title'])
        self.assertTrue(result['commercial_readback_verified'])

    def test_same_price_recently_published_by_scheduled_run_is_not_written_again(self):
        client, report = Client(), audit()
        offer = report['rows'][0]['prepared_offer']
        client.product['offer_targets'] = [{'source_url': TARGET, 'merchant_name': offer['merchant'],
                                           'checked_at': offer['observed_at']}]
        client.existing = [{'source_url': TARGET, 'seller': offer['merchant'], 'price': offer['price']}]
        result = refresh_candidates(report, client=client, reader=reader())
        self.assertEqual(result['status'], 'already_current')
        self.assertEqual(len(result['already_current']), 1)
        self.assertEqual((client.writes, client.scores), ([], 0))

    def test_price_drift_unknown_seller_incomplete_targets_and_catalog_drift_never_write(self):
        for kind in ('price', 'unknown_seller', 'incomplete', 'catalog_name'):
            client, report = Client(), audit()
            if kind == 'price':
                report['rows'][0]['prepared_offer']['price'] -= 1
            elif kind == 'unknown_seller':
                client.product['offer_targets'][0]['merchant_name'] = ''
            elif kind == 'incomplete':
                client.product['offer_targets_truncated'] = True
            else:
                client.product['name'] = 'Samsung Galaxy S26'
            with self.subTest(kind=kind):
                result = refresh_candidates(report, client=client, reader=reader())
                self.assertEqual(result['status'], 'no_verified_candidates')
                self.assertEqual((client.writes, client.scores), ([], 0))

    def test_committed_but_missing_commercial_readback_is_reported_as_failure(self):
        class MissingReadback(Client):
            def get_offer_identity_readiness(self):
                result = super().get_offer_identity_readiness()
                result['productCoverage'][0]['matchedOffers'] = []
                return result
        client = MissingReadback()
        with self.assertRaisesRegex(ValueError, 'not_visible'):
            refresh_candidates(audit(), client=client, reader=reader())
        self.assertEqual(len(client.writes), 1)

    def test_selected_sku_radio_proof_is_carried_with_the_unchanged_ambiguous_heading(self):
        from test_trendyol_catalog_source import document as ty_document, URL as ty_url
        from trendyol_catalog_source import TrendyolCatalogSource, parse_catalog_listing
        html = ty_document().replace('A54 5G', 'A15').replace('a54-5g', 'a15')
        url = ty_url.split('?')[0].replace('a54-5g', 'a15')
        name = 'Samsung Galaxy A15'
        observed, _ = parse_catalog_listing(html, url, name, SPECS)
        self.assertIsNotNone(observed)  # Actual SKU attributes prove the radio.
        class Transport:
            def read_document(self, target):
                return html, 'document_read', 200
        source = RetailerOfferRefresher(trendyol_catalog_source=TrendyolCatalogSource(transport=Transport()))
        client = Client()
        client.product.update(name=name, slug='samsung-galaxy-a15-5g', offer_targets=[
            {'merchant_name': 'Trendyol', 'source_url': url}])
        report = audit()
        report['rows'] = [{'slug': client.product['slug'], 'retailer': 'Trendyol',
                           'reason': 'document_read', 'http_status': 200, 'prepared_offer': observed}]
        result = refresh_candidates(report, client=client, reader=source)
        self.assertEqual(result['status'], 'completed')
        written = client.writes[0][0]
        self.assertEqual(written['source_title'], observed['source_title'])
        self.assertNotIn('5G', written['source_title'])
        self.assertEqual(written['source_variant_evidence']['network_generation'], '5g')
        self.assertEqual(written['source_variant_evidence']['sku'], str(observed['item_number']))

    def test_url_keyed_writer_can_add_the_same_seller_on_another_platform_without_legacy_clearance(self):
        class ScopedClient(Client):
            def ingest_listing_offers(self, records, *, require_all_accepted=True):
                result = super().ingest_offers(records, require_all_accepted=require_all_accepted)
                return {**result, 'offer_identity_contract': 'seller-source-url-v1'}
            def ingest_offers(self, *args, **kwargs):
                raise AssertionError('Never fall back to the legacy writer')
        report = audit()
        observed = report['rows'][0]['prepared_offer']
        client = ScopedClient()
        old_url = 'https://www.n11.com/urun/samsung-galaxy-a36-8gb-128gb-12345678'
        client.product.update(offer_targets=[{'merchant_name': observed['merchant'], 'source_url': old_url}],
                              offer_targets_truncated=True)
        result = refresh_candidates(report, client=client, reader=reader())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(client.writes[0][0]['source_url'], TARGET)
        self.assertEqual(client.writes[0][0]['seller'], observed['merchant'])
        self.assertEqual(result['rows'][0]['target_proof']['listing_identity_contract'], 'seller-source-url-v1')
        self.assertEqual(client.product['offer_targets'][0]['source_url'], old_url)
        # Without that dedicated contract, every original collision/completeness
        # boundary stays in force. A method name is not permission to use the
        # old upsert behavior with relaxed source checks.
        legacy = Client()
        legacy.product.update(offer_targets=client.product['offer_targets'], offer_targets_truncated=True)
        blocked = refresh_candidates(report, client=legacy, reader=reader())
        self.assertEqual(blocked['status'], 'no_verified_candidates')
        self.assertEqual(legacy.writes, [])

    def test_stale_duplicate_cross_platform_and_protected_a37_candidates_abort_before_writes(self):
        for kind in ('stale', 'duplicate', 'host', 'a37', 'catalog_count', 'writes', 'too_many'):
            client, report = Client(), audit()
            if kind == 'stale':
                report['rows'][0]['prepared_offer']['observed_at'] = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
            elif kind == 'duplicate':
                report['rows'].append(deepcopy(report['rows'][0]))
            elif kind == 'host':
                report['rows'][0]['prepared_offer']['url'] = 'https://attacker.example/phone'
            elif kind == 'a37':
                client.product['slug'] = report['rows'][0]['slug'] = 'samsung-galaxy-a37-5g'
            elif kind == 'catalog_count':
                report['catalog_count'] = 110
            elif kind == 'writes':
                report['writes'] = True
            else:
                report['rows'] = report['rows'] * 25
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                refresh_candidates(report, client=client, reader=reader())
            self.assertEqual((client.writes, client.scores), ([], 0))


if __name__ == '__main__':
    unittest.main()
