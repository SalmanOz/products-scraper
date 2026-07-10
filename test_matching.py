"""Offline regression tests for TRPriceScraper and GSMArenaScraper matching/merchant
logic. No network calls.

Run: python3 test_matching.py
"""
from tr_price_scraper import TRPriceScraper
from gsmarena_scraper import GSMArenaScraper

GSMARENA_CASES = [
    # (product_name, title, expected, description)
    ('iPhone 16 Pro', 'Apple iPhone 16 Pro', True, 'exact match'),
    ('iPhone 16 Pro', 'Apple iPhone 16 Pro Max', False,
     'must not match a listing with an extra variant qualifier (Pro Max != Pro)'),
    ('iPhone 16 Pro Max', 'Apple iPhone 16 Pro Max', True, 'matching variant'),
    ('Samsung Galaxy A16', 'Samsung Galaxy A16', True, 'exact match, no variant'),
    ('Samsung Galaxy A16', 'Samsung Galaxy A16 5G', True,
     '5G suffix is not a variant qualifier, should still match'),
    ('Xiaomi 15T', 'Xiaomi 15T Pro', False, 'must not match wrong variant (Pro)'),
]

MERCHANT_CASES = [
    # (raw_name, expected_standardized)
    ('Hepsiburada.com', 'Hepsiburada'),
    ('HEPSIBURADA Mağazası', 'Hepsiburada'),
    ('Trendyol', 'Trendyol'),
    ('Amazon.com.tr', 'Amazon TR'),
    ('n11.com', 'n11'),
    ('Pazarama', 'Pazarama'),
    ('Bilinmeyen Mağaza', 'Bilinmeyen Mağaza'),  # unknown merchant passes through unchanged
]

TRUST_CASES = [
    # (merchant_name, expected_trusted) — gray-import/dropship sellers must be rejected
    ('Hepsiburada', True),
    ('Trendyol.com', True),
    ('Pazarama', True),
    ('Wireless Source', False),
    ('CepMarketim Outlet', False),
    ('AliExpress Reseller', False),
]

CASES = [
    # (product_name, item_title, expected, description)
    ('Xiaomi 15T', 'Maui Jim MJ0439S-003-15T-58 Polarize Erkek Güneş Gözlüğü', False,
     'sunglasses SKU coincidentally containing "15T" must not match'),
    ('Xiaomi 15T', 'Xiaomi 15T 256 GB 12 GB RAM Akıllı Telefon', True,
     'legit Xiaomi 15T listing'),
    ('Xiaomi 15T', 'Xiaomi 15T Pro 512 GB Akıllı Telefon', False,
     'wrong variant (Pro) must still be rejected'),
    ('Apple iPhone 16 Pro', 'Apple iPhone 16 Pro 256GB Cep Telefonu', True,
     'legit iPhone 16 Pro'),
    ('Apple iPhone 16 Pro', 'Apple iPhone 16 Pro Kılıf Silikon Kapak', False,
     'accessory (kılıf) must still be rejected'),
    ('Samsung Galaxy A16 4G', 'Samsung Galaxy A16 4G 128GB Akıllı Telefon', True,
     'legit Galaxy A16'),
    ('Samsung Galaxy A16 4G', 'Samsung Galaxy A16 4G Smartphone', True,
     'legit, uses generic smartphone word instead of Turkish'),
    # update_prices.py strips brand prefixes before searching ("Xiaomi 15T" -> "15T"),
    # so is_strict_match() must reject/accept correctly even with no brand in `name`.
    ('15T', 'Maui Jim MJ0439S-003-15T-58 Polarize Erkek Güneş Gözlüğü', False,
     'production repro: brand-stripped search name must still reject sunglasses'),
    ('15T', 'Xiaomi 15T 256 GB 12 GB RAM Akıllı Telefon', True,
     'production repro: brand-stripped search name must still match legit listing'),
    ('16 Pro', 'Apple iPhone 16 Pro 256GB Cep Telefonu', True,
     'production repro: brand-stripped iPhone search'),
]


def test_is_strict_match():
    scraper = TRPriceScraper.__new__(TRPriceScraper)  # skip __init__ (no network/proxy fetch)
    failures = []
    for name, title, expected, desc in CASES:
        result = scraper.is_strict_match(name, title)
        if result != expected:
            failures.append(f'FAIL [{desc}]: is_strict_match({name!r}, {title!r}) = {result} (expected {expected})')
    assert not failures, '\n' + '\n'.join(failures)


def test_standardize_merchant_name():
    failures = []
    for raw, expected in MERCHANT_CASES:
        result = TRPriceScraper._standardize_merchant_name(raw)
        if result != expected:
            failures.append(f'FAIL: _standardize_merchant_name({raw!r}) = {result!r} (expected {expected!r})')
    assert not failures, '\n' + '\n'.join(failures)


def test_is_trusted_merchant():
    scraper = TRPriceScraper.__new__(TRPriceScraper)  # skip __init__ (no network/proxy fetch)
    failures = []
    for merchant, expected in TRUST_CASES:
        result = scraper.is_trusted_merchant(merchant)
        if result != expected:
            failures.append(f'FAIL: is_trusted_merchant({merchant!r}) = {result} (expected {expected})')
    assert not failures, '\n' + '\n'.join(failures)


def test_gsmarena_is_title_match():
    failures = []
    for name, title, expected, desc in GSMARENA_CASES:
        result = GSMArenaScraper.is_title_match(name, title)
        if result != expected:
            failures.append(f'FAIL [{desc}]: is_title_match({name!r}, {title!r}) = {result} (expected {expected})')
    assert not failures, '\n' + '\n'.join(failures)


if __name__ == '__main__':
    test_is_strict_match()
    print(f'✅ All {len(CASES)} is_strict_match cases passed')
    test_standardize_merchant_name()
    print(f'✅ All {len(MERCHANT_CASES)} _standardize_merchant_name cases passed')
    test_is_trusted_merchant()
    print(f'✅ All {len(TRUST_CASES)} is_trusted_merchant cases passed')
    test_gsmarena_is_title_match()
    print(f'✅ All {len(GSMARENA_CASES)} GSMArenaScraper.is_title_match cases passed')
