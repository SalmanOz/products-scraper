"""Real Scrapling/Chromium smoke test against an intercepted synthetic page.

No retailer or production connection: every request is fulfilled locally or
aborted before network. This proves browser startup/rendering even when all
real candidate hosts later deny access to the runner.
"""
import unittest
from scrapling.fetchers import StealthyFetcher


class BrowserRuntimeTest(unittest.TestCase):
    def test_javascript_rendering_with_no_external_requests(self):
        target = 'https://scrapling-pilot.invalid/'
        seen = []
        def setup(page):
            def intercept(route):
                request = route.request
                seen.append((request.method, request.url))
                if request.method == 'GET' and request.url == target:
                    route.fulfill(status=200, content_type='text/html', body='''
                    <html><body><h1>Pilot fixture</h1><div id="price"></div>
                    <script>document.getElementById('price').textContent = '12345.67';</script>
                    </body></html>''')
                else:
                    route.abort()
            try:
                page.context.route('**/*', intercept)
            except Exception:
                page.context.close()
                raise
        response = StealthyFetcher.fetch(
            target, headless=True, solve_cloudflare=False, google_search=False,
            timeout=10000, retries=1, page_setup=setup,
            additional_args={'service_workers': 'block', 'accept_downloads': False},
            selector_config={'adaptive': False})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.css('#price::text').get(), '12345.67')
        self.assertIn(('GET', target), seen)


if __name__ == '__main__':
    unittest.main()
