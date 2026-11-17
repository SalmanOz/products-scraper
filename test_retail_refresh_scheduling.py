import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import AsyncMock, patch

import test_retail_html_catalog as fixtures
from retail_html_catalog import Host, TY, crawl, database, export, read_product


def args(**changes):
    return SimpleNamespace(**dict(dict(spacing=.5, host=[TY], kind=None, sources={}, refresh=True,
        verify_only=False, retry_unverified=False, retry_redirects=False, discovery_only=False,
        maximum=4, workers=1, stale_hours=6, time_budget_minutes=None), **changes))


class RetailRefreshSchedulingTest(TestCase):
    def test_circuit_opened_during_pacing_does_not_send_or_count_a_request(self):
        async def check():
            host = Host(.5)
            host.next_start = asyncio.get_running_loop().time() + 10
            async def stop_during_wait(_delay):
                host.stopped = True
            with patch('retail_html_catalog.asyncio.sleep', side_effect=stop_during_wait), \
                    patch.object(host, 'request') as request:
                self.assertEqual(await host.get(fixtures.URL), (None, b''))
                request.assert_not_called()
                self.assertEqual(host.rejections, {})
        asyncio.run(check())

    def test_circuit_deferred_page_does_not_advance_refresh_or_discovery_state(self):
        with TemporaryDirectory() as root:
            db = database(Path(root)/'queue.sqlite')
            db.execute('INSERT INTO queue(url,host,kind,title,status) VALUES (?,?,?,?,?)',
                       (fixtures.URL, TY, 'laptop', 'Laptop', 'pending'))
            db.execute('INSERT INTO catalog_targets VALUES (?,?,?)', (fixtures.URL, fixtures.GTIN, 'laptop'))
            with patch('retail_html_catalog.Host.get', new_callable=AsyncMock, return_value=(None, b'')):
                asyncio.run(crawl(args(), db))
                asyncio.run(crawl(args(refresh=False, discovery_only=True, sources={TY: {'laptop': ['/laptop']}},
                                      start_page=1, pages=1, rediscover_after=6), db))
            self.assertEqual(db.execute('SELECT count(*) FROM refresh_attempts').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT status,data,checked_at FROM queue').fetchone(), ('pending', None, None))
            self.assertEqual(db.execute('SELECT count(*) FROM pages').fetchone()[0], 0)
            db.close()

    def test_pickup_quota_prioritizes_expiring_prices_without_starving_published_unknowns(self):
        row = read_product(fixtures.RetailHtmlCatalogTest().listing(), fixtures.URL, 'laptop')
        row['checkedAt'] = (datetime.now(timezone.utc)-timedelta(hours=8)).isoformat()
        row['offer']['storeOnly'] = True
        with TemporaryDirectory() as root:
            db = database(Path(root)/'queue.sqlite')
            urls = [fixtures.URL+str(i) for i in range(6)]
            for i,url in enumerate(urls):
                data = None if i in (1,2,3) else json.dumps(row)
                db.execute('INSERT INTO queue(url,host,kind,title,status,data,checked_at) VALUES (?,?,?,?,?,?,?)',
                           (url, TY, 'laptop', 'Laptop', 'verified' if data else 'pending', data, '2020-01-01'))
                if i != 5:
                    db.execute('INSERT INTO catalog_targets VALUES (?,?,?)',(url,fixtures.GTIN,'laptop'))
            with patch('retail_html_catalog.Host.get',new_callable=AsyncMock,return_value=(200,b'html')) as get:
                with patch('retail_html_catalog.read_product',return_value=None):
                    asyncio.run(crawl(args(),db))
            self.assertEqual([call.args[0] for call in get.await_args_list],urls[:4])
            self.assertEqual(db.execute('SELECT count(*) FROM refresh_attempts').fetchone()[0],4)
            db.close()

    def test_pickup_requests_are_interleaved_so_partial_batches_also_advance_ordinary_targets(self):
        row = read_product(fixtures.RetailHtmlCatalogTest().listing(), fixtures.URL, 'laptop')
        row['checkedAt'] = (datetime.now(timezone.utc)-timedelta(hours=8)).isoformat()
        row['offer']['storeOnly'] = True
        with TemporaryDirectory() as root:
            db = database(Path(root)/'queue.sqlite')
            urls = [fixtures.URL+str(i) for i in range(8)]
            for i,url in enumerate(urls):
                data = json.dumps(row) if i in (0,4) else None
                db.execute('INSERT INTO queue(url,host,kind,title,status,data,checked_at) VALUES (?,?,?,?,?,?,?)',
                           (url,TY,'laptop','Laptop','verified' if data else 'pending',data,'2020-01-01'))
                db.execute('INSERT INTO catalog_targets VALUES (?,?,?)',(url,fixtures.GTIN,'laptop'))
            with patch('retail_html_catalog.Host.get',new_callable=AsyncMock,return_value=(200,b'html')) as get:
                with patch('retail_html_catalog.read_product',return_value=None):
                    asyncio.run(crawl(args(maximum=8),db))
            self.assertEqual([call.args[0] for call in get.await_args_list],urls)
            db.close()

    def test_unresolved_published_targets_rotate_by_attempt_before_unpublished_history(self):
        with TemporaryDirectory() as root:
            db = database(Path(root)/'queue.sqlite')
            urls = [fixtures.URL+str(i) for i in range(2)]
            for url in urls:
                db.execute('INSERT INTO queue(url,host,kind,title,status) VALUES (?,?,?,?,?)',
                           (url,TY,'laptop','Laptop','pending'))
                db.execute('INSERT INTO catalog_targets VALUES (?,?,?)',(url,fixtures.GTIN,'laptop'))
            with patch('retail_html_catalog.Host.get',new_callable=AsyncMock,return_value=(200,b'html')) as get:
                with patch('retail_html_catalog.read_product',return_value=None):
                    asyncio.run(crawl(args(maximum=1),db))
                    asyncio.run(crawl(args(maximum=1),db))
            self.assertEqual([call.args[0] for call in get.await_args_list],urls)
            db.close()

    def test_time_budget_retains_and_exports_completed_observation_without_inventing_attempts(self):
        row = read_product(fixtures.RetailHtmlCatalogTest().listing(), fixtures.URL, 'laptop')
        async def slower_page(url):
            await asyncio.sleep(.02)
            return 200,b'html'
        with TemporaryDirectory() as root:
            db = database(Path(root)/'queue.sqlite')
            urls = [fixtures.URL,fixtures.URL+'2']
            for url in urls:
                db.execute('INSERT INTO queue(url,host,kind,title,status) VALUES (?,?,?,?,?)',
                           (url,TY,'laptop','Laptop','pending'))
                db.execute('INSERT INTO catalog_targets VALUES (?,?,?)',(url,fixtures.GTIN,'laptop'))
            with patch('retail_html_catalog.Host.get',side_effect=slower_page) as get:
                with patch('retail_html_catalog.read_product',return_value=row):
                    asyncio.run(crawl(args(time_budget_minutes=.0001),db))
            self.assertEqual(get.await_count,1)
            self.assertEqual(db.execute('SELECT count(*) FROM refresh_attempts').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT status,data,checked_at FROM queue WHERE url=?',(urls[1],)).fetchone(),
                             ('pending',None,None))
            destination=Path(root)/'staged.json'
            export(db,destination)
            rows=json.loads(destination.read_text())
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['gtin'],fixtures.GTIN)
            self.assertEqual(rows[0]['checkedAt'],row['checkedAt'])
            self.assertEqual(rows[0]['offers'][0]['price'],row['offer']['price'])
            db.close()
