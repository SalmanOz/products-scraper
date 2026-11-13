"""Check the new product routes, images and shop links on Teknoskor."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys

from bs4 import BeautifulSoup
import requests


BASE = 'https://teknoskor.com'
HEADERS = {'User-Agent': 'Mozilla/5.0', 'Cache-Control': 'no-cache'}
ROUTES = {
    'monitors': 'monitorler',
    'laptops': 'dizustu-bilgisayarlar',
    'earbuds': 'kablosuz-kulakliklar',
    'pc-parts': 'bilgisayar-parcalari',
}


def get(path):
    response = requests.get(BASE + path, headers=HEADERS, timeout=20)
    response.raise_for_status()
    return response


def check(product):
    media, slug = product['category'], product['slug']
    page = get(f'/{ROUTES[media]}/{slug}')
    image_name = slug.rsplit('-', 1)[-1] if media == 'pc-parts' else slug
    image = get(f'/media/{media}/{image_name}.webp')
    if not image.content.startswith(b'RIFF') or image.content[8:12] != b'WEBP':
        raise ValueError('A new product image is unavailable')
    doc = BeautifulSoup(page.content, 'html.parser')
    links = {a['href'] for a in doc.select('main a[href]')}
    if len(links.intersection(o['url'] for o in product['offers'])) < 2 or not doc.select_one('main h1'):
        raise ValueError('A new product is missing its detail or offers')


manifest = json.loads(Path(sys.argv[1]).read_bytes())
with ThreadPoolExecutor(max_workers=4) as pool:
    list(pool.map(check, manifest['products']))
print(f"Verified {len(manifest['products'])} new live product pages.")
