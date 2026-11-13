from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase

from catalog_crawl import BudgetedGet, discover_inventory, import_inventory, normalize_route, retry_after
from catalog_inventory import InventoryError, compare_feed_offer, sitemap_hints, xml_root
from catalog_queue import CatalogQueue, DAY, DEFAULT_HTTP_BUDGET, LEASE_SECONDS, LeaseLost, SourceDeferred


HOST = 'www.n11.com'
GTIN = '8806097322344'
URL = f'https://{HOST}/urun/samsung-ls32fm700uuxuf-102483666'
ROUTE = {'url': URL, 'gtin': GTIN, 'code': 'ls32fm700uuxuf'}


class CrawlTests(TestCase):
    def setUp(self):
        self.work = TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.path = Path(self.work.name) / 'queue.sqlite'
        self.now = 1_800_000_000
        self.queue = self.open()
        self.addCleanup(self.queue.close)

    def open(self):
        return CatalogQueue(self.path, clock=lambda: self.now)

    def enqueue(self):
        self.queue.enqueue([normalize_route('monitor', ROUTE)])

    def advance(self, seconds):
        self.now += seconds

    def offer(self, **overrides):
        return dict({
            'url': URL, 'gtin13': GTIN, '_legal_name': 'sample company',
            'merchant': 'Sample store', 'price': 5000, 'currency': 'TRY',
            'condition': 'new', 'availability': 'in_stock',
            'observed_at': datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
        }, **overrides)

    def test_targets_strip_prices_and_conflicts_are_rejected(self):
        row = normalize_route('monitor', dict(ROUTE, price=10))
        self.assertEqual(row[2], ROUTE)
        self.assertEqual(self.queue.enqueue([row, row]), 1)
        with self.assertRaisesRegex(ValueError, 'Conflicting identity'):
            self.queue.enqueue([normalize_route('monitor', dict(ROUTE, code='other1234'))])
        self.assertEqual(self.queue.report()['categories'][0]['routes'], 1)

    def test_merchant_feeds_queue_only_unambiguous_catalog_gtins_without_prices(self):
        good = ROUTE['url']
        conflicting = good + '1'
        report = import_inventory(self.queue, xml_root(f'''<rss xmlns:g="http://base.google.com/ns/1.0">
          <channel>
            <item><link>{good}</link><g:gtin>{GTIN}</g:gtin><g:price>1 TRY</g:price></item>
            <item><link>{good}</link><g:gtin>{GTIN}</g:gtin></item>
            <item><link>{conflicting}</link><g:gtin>{GTIN}</g:gtin></item>
            <item><link>{conflicting}</link><g:gtin>4711081273981</g:gtin></item>
            <item><link>https://example.com/product</link><g:gtin>{GTIN}</g:gtin></item>
          </channel></rss>'''.encode()), 'google', HOST, 'monitor', [ROUTE])
        self.assertEqual((report['enqueued'], report['ambiguous_product_urls'], report['invalid_store_url']), (1, 1, 1))
        self.assertEqual(list(self.queue.fresh_offers('monitor')), [])
        lease = self.queue.acquire(HOST)
        self.assertEqual(self.queue.next_target(HOST, lease), ('monitor', ROUTE))
        self.queue.release(HOST, lease)
        report = import_inventory(self.queue, xml_root(f'''<products>
          <product><url>{good}</url><barcode>{GTIN}</barcode><price>2</price></product>
          <product><url>{conflicting}</url><barcode>8806097322340</barcode></product>
        </products>'''.encode()), 'akakce', HOST, 'monitor', [ROUTE])
        self.assertEqual((report['enqueued'], report['invalid_barcode']), (0, 1))

    def test_inventory_rejects_unsafe_xml_before_parsing(self):
        document = '<!DOCTYPE rss [<!ENTITY a "bad">]><rss>&a;</rss>'
        with self.assertRaisesRegex(InventoryError, 'inventory_unsafe_xml'):
            xml_root(document.encode())
        with self.assertRaises(InventoryError):
            xml_root(document.encode('utf-16'))

    def test_feed_commerce_is_compared_to_live_offer_without_persisting_claims(self):
        def item(url, price, stock='in_stock', sale=''):
            return f'<item><link>{url}</link><g:gtin>{GTIN}</g:gtin><g:price>{price}</g:price><g:availability>{stock}</g:availability>{sale}</item>'

        items = [item(URL, '5000.00 TRY'), item(URL + '1', '6000 TRY'), item(URL + '1', '5000 TRY'),
                 item(URL + '2', '5000 TRY', 'out of stock'),
                 item(URL + '3', '5000 TRY', sale='<g:sale_price>1 TRY</g:sale_price><g:sale_price_effective_date/>')]
        root = xml_root(('<rss xmlns:g="http://base.google.com/ns/1.0"><channel>' + ''.join(items) + '</channel></rss>').encode())
        commerce = {}
        import_inventory(self.queue, root, 'google', HOST, 'monitor', [ROUTE], commerce=commerce)
        self.assertEqual(compare_feed_offer(commerce[URL][1], self.offer()), 'matched_live_price_and_stock')
        self.assertEqual(compare_feed_offer(commerce[URL][1], self.offer(price=4999)), 'feed_price_differs')
        self.assertIsNone(commerce[URL + '1'])
        self.assertEqual(compare_feed_offer(commerce[URL + '2'][1], self.offer()), 'feed_stock_differs')
        self.assertEqual(compare_feed_offer(commerce[URL + '3'][1], self.offer()), 'feed_commerce_unproven')
        self.assertFalse(list(self.queue.fresh_offers('monitor')))

    def test_sitemaps_share_the_store_quota_and_keep_urls_on_host(self):
        base = f'https://{HOST}'
        index, products = (base + path for path in ('/index.xml', '/products.xml'))
        external = 'https://example.com/products.xml'
        data = {index: f'<sitemapindex>'
                       f'<sitemap><loc>{external}</loc></sitemap><sitemap><loc>{products}</loc></sitemap></sitemapindex>',
                products: f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{URL}</loc></url></urlset>'}
        calls = []

        def fetch(url, **kwargs):
            calls.append(url)
            self.assertFalse(kwargs['allow_redirects'])
            return SimpleNamespace(status_code=200, url=url, headers={}, close=lambda: None,
                                   iter_content=lambda **kw: [data[url].encode()])

        def consume(reader):
            hints, report = sitemap_hints(reader, [ROUTE], 5, roots=(index,))
            self.assertEqual(hints, [ROUTE])
            return report

        report = discover_inventory(self.queue, HOST, consume, get=fetch, sleep=self.advance)
        self.assertEqual(calls, [index, products])
        self.assertEqual(report['http_requests'], 2)
        self.assertEqual(self.queue.report()['stores'][0]['http_last_24h'], 2)

    def test_only_one_worker_can_lease_a_store_and_old_worker_is_fenced(self):
        self.enqueue()
        original = self.queue.acquire(HOST)
        _, job = self.queue.next_target(HOST, original)
        with closing(self.open()) as second:
            self.assertIsNone(second.acquire(HOST, discovery=True))
            self.advance(LEASE_SECONDS)
            current = second.acquire(HOST)
            self.assertEqual(second.next_target(HOST, current), ('monitor', ROUTE))
            with self.assertRaises(LeaseLost):
                self.queue.complete(HOST, original, job, 'verified', self.offer())
            second.complete(HOST, current, ROUTE, 'verified', self.offer())
            self.assertEqual(list(second.fresh_offers('monitor')), [self.offer()])

    def test_round_robin_prevents_category_starvation(self):
        self.enqueue()
        second = {**ROUTE, 'url': URL + '1'}
        laptop = {'url': URL + '2', 'gtin': '4711081273981', 'mpn': 'AB12345'}
        self.queue.enqueue([normalize_route('monitor', second), normalize_route('laptop', laptop)])
        lease = self.queue.acquire(HOST)
        for category in ('laptop', 'monitor', 'monitor'):
            actual, route = self.queue.next_target(HOST, lease)
            self.assertEqual(actual, category)
            self.queue.complete(HOST, lease, route, 'no_offer')
        self.assertIsNone(self.queue.next_target(HOST, lease))

    def test_focus_follows_expiring_offers_and_precedes_older_discovery(self):
        focus = {**ROUTE, 'url': URL + '1', 'gtin': '4711081273981'}
        background = {**ROUTE, 'url': URL + '2', 'gtin': '0198157072558'}
        self.queue.enqueue(normalize_route('monitor', row) for row in (ROUTE, background))
        lease = self.queue.acquire(HOST)
        self.assertEqual(self.queue.next_target(HOST, lease), ('monitor', ROUTE))
        self.queue.complete(HOST, lease, ROUTE, 'verified', self.offer())
        self.queue.release(HOST, lease)
        self.advance(DAY - 60 * 60)
        self.queue.enqueue([normalize_route('monitor', focus)])
        lease = self.queue.acquire(HOST)
        for expected in (ROUTE, focus, background):
            self.assertEqual(self.queue.next_target(HOST, lease, category='monitor', focus_gtins=(focus['gtin'],)),
                             ('monitor', expected))
            self.queue.complete(HOST, lease, expected, 'no_offer')

    def test_http_budget_survives_restart_and_applies_to_discovery(self):
        self.enqueue()
        lease = self.queue.acquire(HOST, discovery=True)
        for _ in range(DEFAULT_HTTP_BUDGET):
            self.assertEqual(self.queue.before_request(HOST, lease)[0], 'ready')
            self.advance(1)
        self.queue.release(HOST, lease)
        with closing(self.open()) as second:
            lease = second.acquire(HOST)
            self.assertEqual(second.before_request(HOST, lease)[0], 'http_budget_exhausted')
            self.advance(DAY - DEFAULT_HTTP_BUDGET)
            # Reacquire after the time jump just as a subsequent worker would.
            lease = second.acquire(HOST)
            self.assertEqual(second.before_request(HOST, lease)[0], 'ready')

    def test_shared_circuit_honors_retry_after_and_tracks_document_requests(self):
        self.enqueue()
        lease = self.queue.acquire(HOST)
        called = []
        def fetch(url, **kwargs):
            called.append((url, kwargs['allow_redirects']))
            return SimpleNamespace(status_code=429, headers={'Retry-After': '7200'})
        guarded = BudgetedGet(self.queue, HOST, lease, get=fetch, sleep=self.advance)
        guarded(URL)
        with self.assertRaisesRegex(SourceDeferred, 'http_429'):
            guarded(URL)
        self.assertEqual(called, [(URL, False)])
        self.queue.release(HOST, lease)
        with closing(self.open()) as second:
            store = second.report()['stores'][0]
            self.assertEqual(store['http_last_24h'], 1)
            self.assertEqual(store['blocked_until'], self.now + 7200)
            self.assertIsNone(second.acquire(HOST, discovery=True))

    def test_reduced_store_budget_waits_for_enough_existing_requests_to_expire(self):
        self.enqueue()
        lease = self.queue.acquire(HOST)
        start = self.now
        for _ in range(3):
            self.assertEqual(self.queue.before_request(HOST, lease)[0], 'ready')
            self.advance(1)
        self.queue.configure(HOST, 2, 1)
        self.assertEqual(self.queue.before_request(HOST, lease), ('http_budget_exhausted', start + DAY + 1))

    def test_verified_offer_expires_and_no_retry_can_extend_its_age(self):
        self.enqueue()
        lease = self.queue.acquire(HOST)
        self.queue.next_target(HOST, lease)
        with self.assertRaises(ValueError):
            self.queue.complete(HOST, lease, ROUTE, 'verified', self.offer(gtin13='4711081273981'))
        self.queue.complete(HOST, lease, ROUTE, 'verified', self.offer(price_kind='basket'))
        self.queue.release(HOST, lease)
        self.advance(DAY - 3600)
        lease = self.queue.acquire(HOST)
        self.queue.next_target(HOST, lease)
        self.queue.complete(HOST, lease, ROUTE, 'transport_error', keep=True)
        self.assertEqual(len(list(self.queue.fresh_offers('monitor'))), 1)
        self.advance(3600)
        self.assertEqual(list(self.queue.fresh_offers('monitor')), [])

    def test_due_verified_offer_is_renewed_before_unread_backlog(self):
        self.enqueue()
        lease = self.queue.acquire(HOST, category='monitor')
        self.queue.next_target(HOST, lease, category='monitor')
        self.queue.complete(HOST, lease, ROUTE, 'verified', self.offer())
        self.queue.release(HOST, lease)
        unread = normalize_route('monitor', {**ROUTE, 'url': URL + '9'})
        self.queue.enqueue([unread])
        self.advance(DAY - 3600)
        lease = self.queue.acquire(HOST, category='monitor')
        self.assertEqual(self.queue.next_target(HOST, lease, category='monitor'), ('monitor', ROUTE))

    def test_retry_after_accepts_http_date_and_ignores_invalid_header(self):
        self.assertEqual(retry_after('Tue, 14 Nov 2023 23:13:20 GMT', 1700000000, 60), 3600)
        self.assertEqual(retry_after('invalid', 1700000000, 60), 60)
