"""Confirm the deployed product, store price and browser-loaded photo on Teknoskor."""

import argparse
import json
from pathlib import Path
from time import monotonic, sleep

from playwright.sync_api import Error, sync_playwright

from retail_catalog_publish import read
from retail_html_watch import targets


SITE = 'https://teknoskor.com'


def verify(page, row):
    gtin = row['gtin']
    url = f'{SITE}/elektronik/{row["kind"]}/{gtin}'
    response = page.goto(url, wait_until='domcontentloaded', timeout=15_000)
    if response is None or not response.ok or page.url.rstrip('/') != url:
        return 'detail page is unavailable'
    detail = page.locator(f'main[data-retail-gtin="{gtin}"]')
    if detail.count() != 1 or detail.locator('h1').inner_text() != row['name']:
        return 'product identity did not match'
    product = json.loads(detail.locator('script[type="application/ld+json"]').text_content())
    if product.get('gtin13') != gtin:
        return 'structured barcode did not match'
    schema = {(o['url'], o['price'], o['priceCurrency']) for o in product.get('offers', [])}
    offer = min(row['offers'], key=lambda o: o['price'])
    visible_price = page.evaluate(
        "(amount) => new Intl.NumberFormat('tr-TR', {style: 'currency', currency: 'TRY'}).format(amount)",
        offer['price'],
    )
    shops = detail.locator('section[aria-labelledby="shops"]')
    if ((offer['url'], offer['price'], 'TRY') not in schema
            or not any(a.get_attribute('href') == offer['url']
                       and visible_price in a.locator('..').inner_text() for a in shops.locator('a').all())):
        return 'visible price or retailer link did not match'
    photo = detail.locator('img')
    if photo.count() != 1 or not photo.evaluate(
        "(img) => Promise.race([img.decode().then(() => img.naturalWidth > 0, () => false),"
        "new Promise(resolve => setTimeout(() => resolve(false), 8000))])"
    ):
        return 'product photo did not load'
    print(f'Live verified: {url} | {visible_price} | {photo.get_attribute("src")}', flush=True)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('catalog', type=Path)
    parser.add_argument('--has-products', action='store_true')
    args = parser.parse_args()
    configured = targets()
    rows = {p['gtin']: p for p in read(args.catalog) if p['gtin'] in configured}
    if args.has_products:
        return 0 if rows else 1
    for gtin in sorted(configured.keys() - rows.keys()):
        print(f'{gtin}: still queued, no publishable stock and photo', flush=True)
    if not rows:
        return 0
    deadline = monotonic() + 600
    with sync_playwright() as browser:
        chromium = browser.chromium.launch()
        page = chromium.new_page()
        while rows:
            issues = {}
            for gtin, row in list(rows.items()):
                try:
                    issue = verify(page, row)
                except (Error, ValueError, KeyError, TypeError) as error:
                    issue = str(error).splitlines()[0]
                if issue is None:
                    del rows[gtin]
                else:
                    issues[gtin] = issue
            if not rows:
                return 0
            print(f'Awaiting live deployment: {issues}', flush=True)
            if monotonic() >= deadline:
                return 1
            sleep(20)


if __name__ == '__main__':
    raise SystemExit(main())
