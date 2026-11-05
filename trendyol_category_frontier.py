"""Continue only observed Trendyol phone-category routes, never saved quotes."""
from pathlib import Path
import re

from hepsiburada_category_frontier import (catalog_fingerprint, MAX_AGE_SECONDS, MAX_BYTES,
                                         load_frontier as _load, save_frontier as _save)

VERSION = 'public-ty-category-frontier-v1'
STATE_PATH = Path('.discovery-state/priority-ty-category.json')


def category_url(value):
    from trendyol_catalog_discovery import category_page_number
    return (isinstance(value, str) and len(value) <= 150
            and not re.search(r'[\x00-\x20\\]', value)
            and category_page_number(value) is not None)


def load_frontier(path, products, *, now=None):
    return _load(path, products, now=now, version=VERSION, url_validator=category_url)


def save_frontier(path, result, *, now=None):
    return _save(path, result, now=now, version=VERSION, url_validator=category_url,
                 report_prefix='trendyol')
