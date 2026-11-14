from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from offer_ingestion_client import PublicDocumentResponse
from retail_html_catalog import database
from retail_html_watch import read_watch, targets
import test_retail_html_catalog as fixtures

GTIN, IMAGE, URL = fixtures.GTIN, fixtures.IMAGE, fixtures.URL


class StockWatchTest(IsolatedAsyncioTestCase):
    async def test_only_exact_in_stock_listing_is_staged_on_retry(self):
        config = {GTIN: {'urls': [URL], 'kind': 'laptop', 'brand': 'Lenovo',
                         'model': '83K10016TR', 'photo': IMAGE}}
        fixture = fixtures.RetailHtmlCatalogTest()
        with TemporaryDirectory() as directory, closing(database(Path(directory) / 'queue.sqlite')) as db, \
                patch('retail_html_watch.served_image', return_value=True), \
                patch('retail_html_watch.Host.get') as request:
            request.return_value = 200, fixture.listing().replace(b'"inStock": true', b'"inStock": false')
            self.assertEqual((await read_watch(db, config))[0]['offers'], [])
            request.return_value = 200, fixture.listing(barcode='4711081273981', winner_barcode='4711081273981')
            self.assertEqual(await read_watch(db, config), [])
            request.return_value = 200, fixture.listing()
            proof, = await read_watch(db, config)
            self.assertEqual((proof['gtin'], proof['offers'][0]['price'], proof['image']), (GTIN, 31000, IMAGE))
            self.assertEqual(proof['checkedSources'].keys(), {URL})

    async def test_uses_scoped_site_reader_when_runner_is_denied(self):
        config = {GTIN: {'urls': [URL], 'kind': 'laptop', 'brand': 'Lenovo',
                         'model': '83K10016TR', 'photo': IMAGE}}
        fixture = fixtures.RetailHtmlCatalogTest()
        def collect(url):
            self.assertEqual(url, URL)
            return PublicDocumentResponse(url, 200, fixture.listing().decode(), 'text/html')
        with TemporaryDirectory() as directory, closing(database(Path(directory) / 'queue.sqlite')) as db, \
                patch('retail_html_watch.served_image', return_value=True), \
                patch('retail_html_watch.Host.get', return_value=(403, b'')):
            row, = await read_watch(db, config, collect)
            self.assertEqual(row['offers'][0]['price'], 31000)

    async def test_browser_rechecks_denied_listing_and_still_requires_its_barcode(self):
        config = {GTIN: {'urls': [URL], 'kind': 'laptop', 'brand': 'Lenovo',
                         'model': '83K10016TR', 'photo': IMAGE}}
        fixture = fixtures.RetailHtmlCatalogTest()
        wrong = fixture.listing(barcode='4711081273981', winner_barcode='4711081273981')
        browser = Mock(get=AsyncMock(side_effect=[('http_200', wrong), ('http_200', fixture.listing())]))
        site = Mock(return_value=PublicDocumentResponse(URL, 403, '', 'text/html'))
        with TemporaryDirectory() as directory, closing(database(Path(directory) / 'queue.sqlite')) as db, \
                patch('retail_html_watch.served_image', return_value=True), \
                patch('retail_html_watch.Host.get', return_value=(403, b'')):
            self.assertEqual(await read_watch(db, config, site, browser), [])
            proof, = await read_watch(db, config, site, browser)
            self.assertEqual((proof['gtin'], proof['offers'][0]['price']), (GTIN, 31000))

    def test_every_configured_store_url_is_an_exact_product_destination(self):
        self.assertEqual(set(targets()), {'0781387324506', '6937574855337', '8806091651983'})
