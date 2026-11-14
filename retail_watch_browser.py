"""Read watched product pages in a fresh Chromium context after HTTP denial."""

import asyncio

from playwright.async_api import Error, async_playwright

from retail_html_catalog import retail_listing_url


class StockWatchBrowser:
    def __init__(self, executable=None):
        self.executable = executable
        self.playwright = None
        self.browser = None
        self.launch_lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(3)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        try:
            if self.browser:
                await self.browser.close()
        finally:
            if self.playwright:
                await self.playwright.stop()

    async def get(self, url):
        if retail_listing_url(url, url) != url:
            return 'invalid_destination', b''
        async with self.slots:
            context = None
            try:
                async with self.launch_lock:
                    if not self.playwright:
                        self.playwright = await async_playwright().start()
                    if not self.browser:
                        self.browser = await self.playwright.chromium.launch(executable_path=self.executable)
                context = await self.browser.new_context(locale='tr-TR', service_workers='block',
                                                         accept_downloads=False)
                page = await context.new_page()

                async def limit_request(route):
                    request = route.request
                    if (request.is_navigation_request() and request.url != url
                            or request.method not in ('GET', 'HEAD')
                            or request.resource_type in ('font', 'media')):
                        await route.abort()
                    else:
                        await route.continue_()

                await page.route('**/*', limit_request)
                response = await page.goto(url, wait_until='domcontentloaded', timeout=18_000)
                if response is None or page.url != url or response.url != url:
                    return 'changed_destination', b''
                if response.status != 200:
                    return f'http_{response.status}', b''
                if 'html' not in response.headers.get('content-type', ''):
                    return 'not_html', b''
                await page.locator('h1, .urun-adi').first.wait_for(state='attached', timeout=4_000)
                body = await asyncio.wait_for(page.content(), timeout=5)
                raw = body.encode('utf-8')
                if len(raw) > 6_000_000:
                    return 'oversized', b''
                return 'http_200', raw
            except (Error, TimeoutError) as error:
                return type(error).__name__, b''
            finally:
                if context:
                    await context.close()
