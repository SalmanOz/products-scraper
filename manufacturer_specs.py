"""Observed Turkish manufacturer facts only. Never infer scores or global SKUs."""
import re
import json
import unicodedata
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


def key(value):
    value = unicodedata.normalize('NFKD', value.replace('+', ' plus ')).casefold().replace('ı', 'i')
    return re.sub(r'[^a-z0-9]+', ' ', ''.join(c for c in value if not unicodedata.combining(c))).strip()


def model_key(value):
    return ''.join(word for word in key(value).split() if word not in {'vivo', 'galaxy', 'samsung', 'apple', 'xiaomi', 'poco', 'oppo'})


SPEC_PROFILES = {
    'apple': ({'apple.com', 'www.apple.com'}, r'/tr/iphone-[a-z0-9-]+'),
    'samsung': ({'samsung.com', 'www.samsung.com'}, r'/tr/smartphones/galaxy-[a-z0-9-]+(?:/[a-z0-9-]+)?'),
    'xiaomi': ({'mi.com', 'www.mi.com'}, r'/tr/product/(?:xiaomi|redmi|poco)-[a-z0-9-]+'),
    'poco': ({'mi.com', 'www.mi.com'}, r'/tr/product/poco-[a-z0-9-]+'),
    'oppo': ({'oppo.com', 'www.oppo.com'}, r'/tr/smartphones/series-[a-z0-9-]+/[a-z0-9-]+'),
}


def is_spec_url(candidate, raw):
    try:
        page, url = urlparse(candidate['evidenceUrl']), urlparse(raw)
        hosts, pattern = SPEC_PROFILES[candidate['brandSlug']]
        base = re.sub(r'/specs/?$', '', page.path).rstrip('/')
        return (page.scheme == url.scheme == 'https' and page.hostname in hosts and
                url.hostname == page.hostname and re.fullmatch(pattern, base) is not None and
                not any((u.username or u.password or u.port or u.query or u.fragment) for u in (page, url)) and
                url.path.rstrip('/') == base + '/specs')
    except (ValueError, KeyError, TypeError):
        return False


def manufacturer_spec_links(html, candidate):
    if candidate['brandSlug'] == 'vivo':
        return vivo_spec_links(html, candidate['evidenceUrl'])
    page = candidate['evidenceUrl']
    if is_spec_url(candidate, page):
        return [page]
    found = []
    for anchor in BeautifulSoup(html, 'html.parser').select('a[href]'):
        raw = urljoin(page, anchor['href'])
        if is_spec_url(candidate, raw) and raw not in found:
            found.append(raw)
    return found[:1]


def official_model_names(soup):
    # OPPO's current-product h1 omits radio; detail-name identifies the full model.
    selectors = 'h2.detail-name' if soup.select_one('h2.detail-name') else 'h1, .product-name-title'
    return [re.sub(r'\s*\(\s*CPH\d+\s*\)\s*$', '', tag.get_text(' ', strip=True), flags=re.I)
            for tag in soup.select(selectors)]


def _records(attributes, candidate, source_url, observed_at):
    return [{'product_slug': candidate['slug'], 'attribute_key': name, 'value': value,
             'value_origin': 'manufacturer', 'source_url': source_url, 'observed_at': observed_at,
             'is_estimate': False, 'confidence': 'high'} for name, value in attributes.items() if value]


def _scalars(attributes, memory='', battery='', screen=''):
    # Xiaomi writes "12+256 GB"; the complete memory field must still contain
    # exactly one pair. Lists, extended RAM and descriptive suffixes are held.
    pair = re.fullmatch(r'\s*(\d+)\s*(?:GB)?\s*\+\s*(\d+)\s*GB\s*', memory, re.I)
    if pair:
        ram, storage = map(int, pair.groups())
        if 1 <= ram <= 64 and 16 <= storage <= 4096 and storage >= ram * 4:
            attributes.update(ram_gb=ram, storage_gb=storage)
    cell = re.match(r'^\s*(\d{4,5})\s*mAh\b', battery, re.I)
    if cell and 1500 <= int(cell[1]) <= 15000:
        attributes['battery_mah'] = int(cell[1])
    diagonal = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:inç|inch|[″"])', screen, re.I)
    if diagonal and 4 <= float(diagonal[1].replace(',', '.')) <= 9:
        attributes['screen_size_inch'] = float(diagonal[1].replace(',', '.'))


def _xiaomi_sections(soup, candidate):
    # The Turkish specs use labelled adjacent blocks. Require an exact model
    # identity, not a recommended model, and never flatten the whole document.
    names = official_model_names(soup)
    if soup.title and soup.select_one('link[rel="canonical"][href]'):
        names.append(re.split(r'\s+(?:Özellikler|Özellikleri|Teknik Özellikler)\b',
                              soup.title.get_text(' ', strip=True), flags=re.I)[0])
    if not any(model_key(name) == model_key(candidate['name']) for name in names):
        return {}
    labels = {'ram ve depolama': ('Performance & Hardware', 'Memory options'),
              'boyutlar': ('Design & Materials', 'Dimensions'), 'islemci': ('Performance & Hardware', 'Model'),
              'ekran': ('Design & Materials', 'Diagonal'), 'arka kamera': ('Camera', 'Rear Camera'),
              'on kamera': ('Camera', 'Selfie Camera'), 'pil ve sarj': ('Batarya', 'Capacity'),
              'ag ve baglanti': ('Connectivity', 'Network'), 'isletim sistemi': ('Software', 'OS')}
    attrs, seen, conflicts = {}, {}, set()
    for tag in soup.select('h2, h3, h4, div, p, span'):
        if tag.find_parent(['nav', 'header', 'footer', 'script']):
            continue
        label = key(tag.get_text(' ', strip=True))
        if label not in labels or tag.find(['h2', 'h3', 'h4', 'div', 'p', 'span']):
            continue
        # At most one wrapper, only a sibling; no global find-next borrowing.
        value = tag.find_next_sibling()
        if value is None and tag.parent and key(tag.parent.get_text(' ', strip=True)) == label:
            value = tag.parent.find_next_sibling()
        text = value.get_text(' ', strip=True) if value else ''
        if not text or len(text) > 15000 or key(text) in labels:
            continue
        if label in seen and seen[label] != text:
            conflicts.add(label)
        seen[label] = text
    for label, text in seen.items():
        if label not in conflicts:
            group, field = labels[label]
            attrs.setdefault(group, {})[field] = text
    _scalars(attrs, attrs.get('Performance & Hardware', {}).get('Memory options', ''),
             attrs.get('Batarya', {}).get('Capacity', ''), attrs.get('Design & Materials', {}).get('Diagonal', ''))
    return attrs


def parse_manufacturer_specifications(html, source_url, candidate, observed_at):
    if candidate['brandSlug'] == 'vivo':
        return parse_vivo_specifications(html, source_url, candidate, observed_at)
    if not is_spec_url(candidate, source_url):
        raise ValueError('manufacturer_spec_url_mismatch')
    soup = BeautifulSoup(html, 'html.parser')
    canonical = soup.select_one('link[rel="canonical"][href]')
    if canonical and urljoin(source_url, canonical['href']).rstrip('/') != source_url.rstrip('/'):
        raise ValueError('manufacturer_spec_canonical_mismatch')
    brand = candidate['brandSlug']
    attributes = {}
    if brand == 'apple':
        # Establish all column identities from actual capacity headings first.
        model_names = [tag.get_text(' ', strip=True) for tag in soup.select('.techspecs-column strong.techspecs-small-heading')]
        names = official_model_names(soup) + model_names
        if not any(model_key(name) == model_key(candidate['name']) for name in names):
            raise ValueError('manufacturer_spec_identity_not_confirmed')
        groups = {'kapasite': 'Performance & Hardware', 'cip': 'Performance & Hardware',
                  'ekran': 'Design & Materials', 'boyut ve agirlik': 'Design & Materials',
                  'kamera': 'Camera', 'video kaydi': 'Camera', 'on kamera': 'Camera',
                  'hucresel ve kablosuz baglanti': 'Connectivity', 'konum': 'Connectivity',
                  'guc ve pil': 'Batarya', 'isletim sistemi': 'Software'}
        for row in soup.select('.techspecs-row'):
            heading = row.select_one('.techspecs-rowheader')
            if not heading:
                continue
            for note in heading.select('sup'):
                note.decompose()
            title = heading.get_text(' ', strip=True)
            group = groups.get(key(title))
            if not group:
                continue
            columns = row.select('.techspecs-column')
            selected = []
            for column in columns:
                model = column.select_one('strong.techspecs-small-heading')
                if model and model_key(model.get_text(' ', strip=True)) == model_key(candidate['name']):
                    selected.append(column)
            # Unnamed multiple columns cannot be safely aligned by position.
            if not selected and len(columns) == 1 and not columns[0].select_one('strong.techspecs-small-heading'):
                if len(set(model_names)) <= 1 or columns[0].get('aria-colspan') == '2':
                    selected = columns
            if len(selected) != 1:
                continue
            column = selected[0]
            for note in column.select('sup, .techspecs-caption, .techspecs-small-heading'):
                note.decompose()
            value = column.get_text(' ', strip=True)
            if value and len(value) < 15000:
                attributes.setdefault(group, {})[title] = value
                if key(title) == 'ekran':
                    _scalars(attributes, screen=value)
        # Apple does not publish physical RAM/mAh in these tables; never derive
        # them from video playback hours, storage options or the sibling model.
    elif brand == 'oppo':
        if not any(model_key(name) == model_key(candidate['name']) for name in official_model_names(soup)):
            raise ValueError('manufacturer_spec_identity_not_confirmed')
        groups = {
            'height': 'Design & Materials', 'width': 'Design & Materials', 'thickness': 'Design & Materials',
            'weight': 'Design & Materials', 'size': 'Design & Materials', 'resolution': 'Design & Materials',
            'refresh-rate': 'Design & Materials', 'panel': 'Design & Materials',
            'ram-and-rom-capacities': 'Performance & Hardware', 'ram-type': 'Performance & Hardware',
            'rom-specifications': 'Performance & Hardware', 'SoC': 'Performance & Hardware',
            'cpu': 'Performance & Hardware', 'GPU': 'Performance & Hardware', 'rear': 'Camera', 'front': 'Camera',
            'battery': 'Batarya', 'fast-charge': 'Batarya', 'frequency-band': 'Connectivity',
            'wlan': 'Connectivity', 'bluetooth-version': 'Connectivity', 'nfc': 'Connectivity',
            'sim-card-type': 'Connectivity', 'os': 'Software',
        }
        rows = {}
        for row in soup.select('.param-detail-item .item-right-list[data-labelkey]'):
            field = row.get('data-labelkey')
            value = row.select_one('.value')
            label = row.select_one('.key')
            if field not in groups or not value:
                continue
            text = value.get_text(' ', strip=True)
            if not text or len(text) > 15000:
                continue
            # Camera and Video repeat rear/front labels; retain their section.
            section = row.find_parent(class_='param-detail-item')
            section_title = section.select_one('.item-left') if section else None
            context = section_title.get_text(' ', strip=True) if section_title else ''
            name = ((context + ': ') if context else '') + (label.get_text(' ', strip=True) if label else field)
            attributes.setdefault(groups[field], {})[name] = text
            rows.setdefault(field, []).append(text)
        unique = lambda name: rows.get(name, [''])[0] if len(set(rows.get(name, []))) == 1 else ''
        _scalars(attributes, unique('ram-and-rom-capacities'), unique('battery'), unique('size'))
    else:
        # Xiaomi/POCO and Samsung: only exact, visible Product structured data.
        # Dynamic/blocked pages remain unavailable, never scrape a sibling SKU.
        from release_dates import normalize_release_date
        def visit(value):
            if isinstance(value, list):
                for child in value:
                    yield from visit(child)
            elif isinstance(value, dict):
                types = value.get('@type', [])
                if 'Product' in (types if isinstance(types, list) else [types]):
                    yield value
                yield from visit(value.get('@graph'))
        products = []
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                products.extend(p for p in visit(json.loads(script.get_text()))
                                if model_key(str(p.get('name', ''))) == model_key(candidate['name']))
            except (ValueError, TypeError):
                continue
        attributes = _xiaomi_sections(soup, candidate) if brand in {'xiaomi', 'poco'} else {}
        if not products and attributes:
            return _records(attributes, candidate, source_url, observed_at)
        if len(products) != 1:
            raise ValueError('manufacturer_structured_specs_unavailable')
        product = products[0]
        release = normalize_release_date(str(product.get('releaseDate', '')))
        if release:
            attributes['Technical sheet'] = {'Release date': release}
        groups = {'ram': ('Performance & Hardware', 'Memory RAM'), 'storage': ('Performance & Hardware', 'Capacity'),
                  'processor': ('Performance & Hardware', 'Model'), 'camera': ('Camera', 'Camera'),
                  'battery capacity': ('Batarya', 'Capacity'), 'screen size': ('Design & Materials', 'Diagonal'),
                  'operating system': ('Software', 'OS'), 'network': ('Connectivity', 'Network')}
        for prop in product.get('additionalProperty', []):
            if not isinstance(prop, dict) or key(str(prop.get('name', ''))) not in groups or not isinstance(prop.get('value'), str):
                continue
            group, label = groups[key(prop['name'])]
            attributes.setdefault(group, {})[label] = prop['value']
        hardware = attributes.get('Performance & Hardware', {})
        _scalars(attributes, hardware.get('Memory RAM', '') + ' + ' + hardware.get('Capacity', ''),
                 attributes.get('Batarya', {}).get('Capacity', ''), attributes.get('Design & Materials', {}).get('Diagonal', ''))
    if not attributes:
        raise ValueError('manufacturer_specs_unavailable')
    return _records(attributes, candidate, source_url, observed_at)


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
