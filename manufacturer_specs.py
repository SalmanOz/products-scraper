"""Observed Turkish manufacturer facts only. Never infer scores or global SKUs."""
import re
import unicodedata
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


def key(value):
    value = unicodedata.normalize('NFKD', value.replace('+', ' plus ')).casefold().replace('ı', 'i')
    return re.sub(r'[^a-z0-9]+', ' ', ''.join(c for c in value if not unicodedata.combining(c))).strip()


def model_key(value):
    return ''.join(word for word in key(value).split() if word not in {'vivo', 'galaxy', 'samsung', 'apple', 'xiaomi', 'poco'})


def vivo_spec_links(html, page_url):
    """Use only links actually present on the exact official Turkish overview."""
    page = urlparse(page_url)
    if (page.scheme != 'https' or page.hostname not in {'www.vivo.com', 'vivo.com'} or page.query or page.fragment or
            page.username or page.password or page.port):
        return []
    match = re.fullmatch(r'/tr/products/([a-z0-9-]+)/?', page.path)
    if not match:
        return []
    found = []
    for anchor in BeautifulSoup(html, 'html.parser').select('a[href]'):
        url = urlparse(urljoin(page_url, anchor['href']))
        if (url.scheme in {'http', 'https'} and url.hostname == page.hostname and
                not url.username and not url.password and url.port is None and not url.query and not url.fragment and
                url.path.rstrip('/') == '/tr/products/param/' + match[1]):
            # The producer currently emits http links. Fetch HTTPS only.
            normalized = url._replace(scheme='https').geturl()
            if normalized not in found:
                found.append(normalized)
    return found


def parse_vivo_specifications(html, source_url, candidate, observed_at):
    url = urlparse(source_url)
    overview = urlparse(candidate['evidenceUrl'])
    model_path = overview.path.rstrip('/').rsplit('/', 1)[-1]
    if (candidate['brandSlug'] != 'vivo' or url.scheme != 'https' or
            url.hostname not in {'www.vivo.com', 'vivo.com'} or url.hostname != overview.hostname or
            url.username or url.password or url.port or url.query or url.fragment or
            url.path.rstrip('/') != '/tr/products/param/' + model_path):
        raise ValueError('manufacturer_spec_url_mismatch')
    soup = BeautifulSoup(html, 'html.parser')
    sections = {}
    for section in soup.select('li.parameter-item'):
        heading = section.select_one('h2.parameter-title')
        if not heading:
            continue
        for note in heading.select('sup'):
            note.decompose()
        title = key(heading.get_text(' ', strip=True))
        rows = sections.setdefault(title, {})
        conflicts = set()
        for row in section.select('li.attr-item'):
            label, value = row.select_one('.attr-item-name'), row.select_one('.attr-item-text')
            if not label or not value:
                continue
            name = label.get_text(' ', strip=True)
            text = value.get_text(' ', strip=True)
            if not text or text.casefold() in {'-', 'unknown', 'n/a'} or len(text) > 15000:
                continue
            if name in conflicts:
                continue
            if name in rows and rows[name] != text:
                rows.pop(name); conflicts.add(name); continue
            rows[name] = text
    # "Model" in the producer's own in-box specification is positive identity,
    # not a navigation link to a different phone or a URL guessed from a slug.
    model = sections.get('kutu icerigi', {}).get('Model')
    if not model or model_key(model) != model_key(candidate['name']):
        raise ValueError('manufacturer_spec_identity_not_confirmed')
    attributes = {}
    mapping = {
        'platform': ('Performance & Hardware', {'İşlemci': 'Model', 'CPU Çekirdek Sayısı': 'CPU Cores', 'CPU Saat Hızı': 'CPU Clock Speed'}),
        'depolama': ('Performance & Hardware', {'RAM Türü': 'RAM Type', 'ROM Türü': 'Storage Type'}),
        'pil': ('Batarya', {'Pil': 'Capacity', 'Şarj Gücü': 'Charging Power', 'Pil Türü': 'Type'}),
        'tasarim': ('Design & Materials', {'Boyutlar': 'Dimensions', 'Ağırlık': 'Weight'}),
        'ekran': ('Design & Materials', {'Boyut': 'Diagonal', 'Çözünürlük': 'Resolution', 'Tür': 'Screen Type', 'Yenileme Hızı': 'Refresh Rate'}),
        'ag': ('Connectivity', {}), 'baglanti': ('Connectivity', {}), 'kamera': ('Camera', {}),
    }
    for section, (group, labels) in mapping.items():
        for label, value in sections.get(section, {}).items():
            attributes.setdefault(group, {})[labels.get(label, label)] = value
    basic = sections.get('temel ozellikler', {})
    software = {label: value for label, value in basic.items() if label in {'Android Sürümü', 'İşletim Sistemi'}}
    if software:
        attributes['Software'] = software
    # Physical RAM+ROM only; "Genişletilebilir RAM" is never physical memory.
    raw_pair = sections.get('depolama', {}).get('RAM ve ROM', '')
    pair = re.fullmatch(r'\s*(\d+)\s*GB\s*\+\s*(\d+)\s*GB\s*', raw_pair, re.I)
    if pair:
        ram, storage = map(int, pair.groups())
        if 1 <= ram <= 64 and 16 <= storage <= 4096 and storage >= ram * 4:
            attributes.update(ram_gb=ram, storage_gb=storage)
            attributes.setdefault('Performance & Hardware', {}).update({'Memory RAM': f'{ram} GB', 'Capacity': f'{storage} GB'})
    battery = re.match(r'^\s*(\d{4,5})\s*mAh\b', sections.get('pil', {}).get('Pil', ''), re.I)
    if battery and 1500 <= int(battery[1]) <= 15000:
        attributes['battery_mah'] = int(battery[1])
    screen = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:inç|inch|[″"])', sections.get('ekran', {}).get('Boyut', ''), re.I)
    if screen and 4 <= float(screen[1].replace(',', '.')) <= 9:
        attributes['screen_size_inch'] = float(screen[1].replace(',', '.'))
    records = [{'product_slug': candidate['slug'], 'attribute_key': name, 'value': value,
                'value_origin': 'manufacturer', 'source_url': source_url, 'observed_at': observed_at,
                'is_estimate': False, 'confidence': 'high'} for name, value in attributes.items() if value]
    return records


def vivo_product_images(html):
    found = []
    for image in BeautifulSoup(html, 'html.parser').select('.parameter-color .color-image-list img[src]'):
        raw = image['src']
        url = urlparse(raw)
        if (url.scheme == 'https' and url.hostname == 'de-gdpr-exstatic-vivofs.vivo.com' and
                not url.port and not url.username and not url.password and not url.query and not url.fragment and
                re.search(r'\.(?:png|webp|jpe?g)$', url.path, re.I) and raw not in found):
            found.append(raw)
    return found[:3]


def fill_missing_records(existing, official):
    """Keep origins honest; never silently replace a whole source group or SKU."""
    by_key = {record['attribute_key']: record for record in existing}
    conflicts = []
    for record in official:
        name = record['attribute_key']
        if name in by_key:
            if name in {'ram_gb', 'storage_gb', 'battery_mah', 'screen_size_inch'} and by_key[name]['value'] != record['value']:
                conflicts.append(name)
            continue
        by_key[name] = record
    return list(by_key.values()), conflicts


def missing_critical_facts(records):
    values = {record['attribute_key']: record['value'] for record in records}
    missing = [name for name in ['ram_gb', 'storage_gb', 'battery_mah', 'screen_size_inch',
        'Performance & Hardware', 'Camera', 'Connectivity', 'Software', 'Design & Materials', 'Batarya'] if not values.get(name)]
    if not isinstance(values.get('Technical sheet'), dict) or not values['Technical sheet'].get('Release date'):
        missing.append('release_date')
    partials = values.get('partials', {})
    for score, partial in [('performance_score', 'hardware'), ('camera_score', 'camera'), ('battery_score', 'battery'), ('screen_score', 'design')]:
        if not values.get(score) and not (isinstance(partials, dict) and partials.get(partial)):
            missing.append(score)
    return missing
