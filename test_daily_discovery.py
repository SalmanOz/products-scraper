import json
import unittest
from unittest.mock import Mock, patch

import daily_discovery as discovery
from source_ingestion import CatalogProduct, SourceIngestionError


def listing(*names):
    return ''.join(f'<a href="/tr/smartphones/galaxy-a/galaxy-a{number}/">Galaxy A{number}</a>' for number in names)


class DiscoveryTests(unittest.TestCase):
    def test_official_listings_only_extract_single_models(self):
        html = '''<a href="/tr/products/v60">V60 yeni</a>
          <a href="/tr/products/v60">vivo V60</a>
          <a href="/tr/products/headphones">vivo Headphones</a>
          <a href="https://evil.test/tr/products/v70">V70</a>
          <a href="/global/products/v70">V70</a>
          <a href="/tr/products/v80">V80 256 GB</a>'''
        rows = discovery.discover_candidates('vivo', html, discovery.LISTINGS['vivo'])
        self.assertEqual([row['slug'] for row in rows], ['vivo-v60'])
        self.assertEqual(rows[0]['evidenceUrl'], 'https://www.vivo.com/tr/products/v60')

    def test_all_supported_brand_link_shapes(self):
        for brand, html, slug in [
            ('apple', '<a href="/tr/iphone-air/">iPhone Air</a>', 'apple-iphone-air'),
            ('samsung', '<a href="/tr/smartphones/galaxy-a/galaxy-a56-5g/">Galaxy A56 5G</a>', 'samsung-galaxy-a56-5g'),
            ('xiaomi', '<a href="/tr/product/redmi-note-15/">Redmi Note 15</a>', 'xiaomi-redmi-note-15'),
            ('xiaomi', '<a href="/tr/product/poco-f7/">POCO F7</a>', 'poco-f7'),
        ]:
            self.assertEqual(discovery.discover_candidates(brand, html, discovery.LISTINGS[brand])[0]['slug'], slug)

    def test_wrong_hosts_locales_queries_and_variants_are_not_candidates(self):
        for raw in ['http://www.apple.com/tr/iphone-air/', 'https://apple.com.evil.test/tr/iphone-air/',
                    'https://www.apple.com/tr/iphone-air/?token=secret', 'https://user:pass@www.apple.com/tr/iphone-air/',
                    'https://www.apple.com:8443/tr/iphone-air/', 'https://127.0.0.1/tr/iphone-air/',
                    'https://www.apple.com/iphone-air/', 'https://www.apple.com/tr/iphone/']:
            self.assertFalse(discovery.safe_product_url('apple', raw), raw)
        self.assertIsNone(discovery.candidate_name('apple', 'iPhone 18 Pro 256 GB'))

    def test_storage_and_radio_aliases_do_not_create_duplicate_model_pages(self):
        self.assertTrue(discovery.may_duplicate('Samsung Galaxy A56', 'Samsung Galaxy A56 5G'))
        self.assertFalse(discovery.may_duplicate('Samsung Galaxy A17 4G', 'Samsung Galaxy A17 5G'))
        self.assertFalse(discovery.may_duplicate('Apple iPhone 18 Pro', 'Apple iPhone 18 Pro Max'))

    def test_unknown_image_hosts_never_reach_existing_upload_transport(self):
        store = object.__new__(discovery.DiscoveryImageStore)
        for url in ['http://cdn-files.kimovil.com/a.webp', 'https://127.0.0.1/a.webp',
                    'https://user:pass@cdn-files.kimovil.com/a.webp', 'https://cdn-files.kimovil.com.evil.test/a.webp']:
            with self.assertRaises(ValueError):
                store.upload(url, 'samsung-galaxy-a56', 0)

    def test_daily_specification_reader_uses_only_normal_public_transport(self):
        reader = Mock()
        reader.read.return_value = '<h1>Real model</h1>'
        scraper = discovery.PublicSpecificationScraper(reader, image_store=Mock())
        self.assertEqual(scraper.get_via_flaresolverr('https://www.kimovil.com/en/where-to-buy-vivo-v60'), '<h1>Real model</h1>')
        reader.read.assert_called_once()
        self.assertIsNone(scraper.get_via_flaresolverr('https://localhost/'))
        reader.read.side_effect = ValueError('source_http_403')
        self.assertIsNone(scraper.get_via_flaresolverr('https://www.kimovil.com/en/where-to-buy-vivo-v60'))

    def test_manufacturer_names_ignore_navigation_and_related_product_schema(self):
        html = '<nav>Galaxy A56</nav><h1>Telefonlar</h1><script type="application/ld+json">' + json.dumps({
            '@type': 'Product', 'name': 'Galaxy A57', 'isRelatedTo': {'@type': 'Product', 'name': 'Galaxy A56'}}) + '</script>'
        self.assertEqual(discovery.product_names(html), ['Telefonlar', 'Galaxy A57'])

    def fixture(self, phones=(56,)):
        client = Mock(base_url='https://teknoskor.com')
        client.fetch_catalog.return_value = []
        client._json_body.side_effect = lambda response: response
        requests = []

        def request(method, url, **kwargs):
            requests.append((method, kwargs.get('json')))
            if method == 'GET':
                return {'contract': discovery.CONTRACT}
            payload = kwargs['json']
            if payload['mode'] == 'validate':
                return {'eligible': True, 'committed': False}
            return {'eligible': True, 'committed': True, 'outcome': 'created', 'slug': payload['candidate']['slug']}
        client._request_with_retry.side_effect = request
        reader = Mock()
        reader.read.side_effect = lambda url: listing(*phones) if url in discovery.LISTINGS.values() else f'<h1>Galaxy A{url.rstrip("/").rsplit("a", 1)[-1]}</h1>'
        specs = Mock()
        specs.search_product_on_kimovil.return_value = 'https://www.kimovil.com/en/where-to-buy-samsung-galaxy-a56'

        def scrape(url, **kwargs):
            kwargs['record_sink'].append({'attribute_key': 'ram_gb', 'value': 8})
            kwargs['image_sink'].append({'urls': ['https://cdn.teknoskor.com/products/model/image.webp']})
            return True
        specs.scrape_product_details.side_effect = scrape
        offers = [{'merchant_name': 'Hepsiburada', 'price': 20000}]
        return client, reader, specs, requests, offers

    def run_fixture(self, fixture, **kwargs):
        client, reader, specs, requests, offers = fixture
        with patch.object(discovery, 'direct_offers', return_value=offers):
            return discovery.run_discovery(client=client, reader=reader, specs=specs,
                price_scraper=Mock(), refresher=Mock(), **kwargs)

    def test_dry_run_validates_but_never_publishes(self):
        fixture = self.fixture()
        report = self.run_fixture(fixture)
        self.assertEqual(report['candidates'][0]['status'], 'ready_not_published')
        self.assertEqual([payload['mode'] for method, payload in fixture[3] if method == 'POST'], ['validate'])
        self.assertEqual(report['created'], 0)

    def test_daily_cap_has_no_minimum_or_forced_publication(self):
        fixture = self.fixture((56, 57, 58))
        report = self.run_fixture(fixture, publish=True)
        self.assertEqual(report['created'], 2)
        self.assertEqual(len(report['candidates']), 2)
        empty = self.fixture()
        empty[4].clear()
        report = self.run_fixture(empty, publish=True)
        self.assertEqual(report['created'], 0)
        self.assertEqual(report['candidates'][0]['reasons'], ['no_current_in_stock_exact_sku_pdp_offer'])
        self.assertFalse(any(method == 'POST' for method, payload in empty[3]))

    def test_existing_names_are_skipped_before_specification_or_image_writes(self):
        fixture = self.fixture()
        fixture[0].fetch_catalog.return_value = [CatalogProduct(1, 'Samsung Galaxy A56', 'older-slug', {})]
        report = self.run_fixture(fixture, publish=True)
        self.assertEqual(report['new_candidates'], 0)
        fixture[2].scrape_product_details.assert_not_called()

    def test_server_rejection_never_triggers_publish(self):
        fixture = self.fixture()
        original = fixture[0]._request_with_retry.side_effect

        def reject(method, url, **kwargs):
            if method == 'POST':
                raise SourceIngestionError('rejected', status_code=422,
                    response_body={'issues': ['specifications_not_verified'], 'committed': False})
            return original(method, url, **kwargs)
        fixture[0]._request_with_retry.side_effect = reject
        report = self.run_fixture(fixture, publish=True)
        self.assertEqual(report['created'], 0)
        self.assertEqual(report['candidates'][0]['reasons'], ['specifications_not_verified'])

    def test_rotation_gives_unchecked_candidates_priority(self):
        fixture = self.fixture((56, 57))
        with patch.object(discovery, 'MAX_ATTEMPTS', 1):
            report = self.run_fixture(fixture, previous={'attempted': {'samsung-galaxy-a56': discovery.utc_now()}})
        self.assertEqual(report['candidates'][0]['slug'], 'samsung-galaxy-a57')

    def test_manufacturer_failure_or_identity_conflict_leaves_no_published_placeholder(self):
        fixture = self.fixture()
        fixture[1].read.side_effect = lambda url: listing(56) if url in discovery.LISTINGS.values() else '<h1>Galaxy A57</h1>'
        report = self.run_fixture(fixture, publish=True)
        self.assertEqual(report['candidates'][0]['reasons'], ['manufacturer_identity_not_confirmed'])
        fixture[2].scrape_product_details.assert_not_called()

    def test_public_reader_does_not_follow_redirect_or_retry_forbidden_source(self):
        response = Mock(status_code=403)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        session = Mock()
        session.get.return_value = response
        reader = discovery.PublicReader(session=session)
        for _ in range(2):
            with self.assertRaises(ValueError):
                reader.read('https://www.vivo.com/tr/products')
        self.assertEqual(session.get.call_count, 1)
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
        self.assertNotIn('Authorization', session.get.call_args.kwargs['headers'])

    def test_direct_offer_stage_discards_unknown_stock_and_keeps_fetched_timestamp(self):
        price = Mock()
        async def hints(*args):
            return [{'url': 'https://www.hepsiburada.com/phone-p-HBCV0001'}]
        price.get_best_prices = hints
        refresher = Mock()
        refresher.refresh_existing_offer_targets.return_value = [
            {'merchant': 'Hepsiburada', 'price': 20000, 'url': 'https://www.hepsiburada.com/phone-p-HBCV0001',
             'source_title': 'Samsung Galaxy A56 8 GB RAM 256 GB', 'observed_at': '2026-10-19T01:00:00Z', 'availability': 'in_stock'},
            {'merchant': 'n11', 'price': 19000, 'observed_at': '2026-10-19T01:00:00Z', 'availability': 'unknown'}]
        rows = discovery.direct_offers({'name': 'Samsung Galaxy A56'}, {'ram_gb': 8, 'storage_gb': 256}, price, refresher)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['checked_at'], '2026-10-19T01:00:00Z')


if __name__ == '__main__':
    unittest.main()
