"""Offline regression tests for fix_seo_data.py repair functions. No network/DB calls.

Run: python3 test_seo_data_repair.py
"""
from fix_seo_data import normalize_inches, fix_faq_number_format, fix_faq_grammar_mismatch

INCH_CASES = [
    # (raw, expected)
    (667, 6.67),
    (61, 6.1),
    (6.7, 6.7),
    (0, None),
    (-1, None),
]

NUMBER_FORMAT_CASES = [
    # (answer, expected_fixed)
    ("AnTuTu skoru 1,934,662 puandır.", "AnTuTu skoru 1.934.662 puandır."),
    ("Fiyatı 114.499 TL'dir.", "Fiyatı 114.499 TL'dir."),  # already Turkish format, unchanged
]

GRAMMAR_CASES = [
    # (question, answer, expected_answer, description)
    ("Apple iPhone 16 Pro fotoğraf kalitesi nasıl?", "Evet, düşük ışıkta profesyonel sonuçlar verir.",
     "Düşük ışıkta profesyonel sonuçlar verir.", "the exact reported bug case must be fixed"),
    ("Apple iPhone 16 Pro kamerası gece çekimi için iyi mi?", "Evet, düşük ışıkta profesyonel sonuçlar verir.",
     "Evet, düşük ışıkta profesyonel sonuçlar verir.", "legit yes/no pairing must be left untouched"),
    ("Pil ömrü ne kadar?", "Hayır, günlük kullanımda yetersiz kalabilir.",
     "Günlük kullanımda yetersiz kalabilir.", "Hayır-marker on a non-yes/no question must also be fixed"),
]


def test_normalize_inches():
    failures = []
    for raw, expected in INCH_CASES:
        result = normalize_inches(raw)
        if result != expected:
            failures.append(f'FAIL: normalize_inches({raw!r}) = {result!r} (expected {expected!r})')
    assert not failures, '\n' + '\n'.join(failures)


def test_fix_faq_number_format():
    failures = []
    for answer, expected in NUMBER_FORMAT_CASES:
        faq = [{"q": "x?", "a": answer}]
        fixed, _ = fix_faq_number_format(faq)
        if fixed[0]["a"] != expected:
            failures.append(f'FAIL: fix_faq_number_format({answer!r}) = {fixed[0]["a"]!r} (expected {expected!r})')
    assert not failures, '\n' + '\n'.join(failures)


def test_fix_faq_grammar_mismatch():
    failures = []
    for q, a, expected, desc in GRAMMAR_CASES:
        faq = [{"q": q, "a": a}]
        fixed, _ = fix_faq_grammar_mismatch(faq)
        if fixed[0]["a"] != expected:
            failures.append(f'FAIL [{desc}]: fix_faq_grammar_mismatch(q={q!r}, a={a!r}) = {fixed[0]["a"]!r} (expected {expected!r})')
    assert not failures, '\n' + '\n'.join(failures)

    # Non-list input must not crash
    fixed, changed = fix_faq_grammar_mismatch("not a list")
    assert changed is False and fixed == "not a list"


if __name__ == '__main__':
    test_normalize_inches()
    print(f'✅ All {len(INCH_CASES)} normalize_inches cases passed')
    test_fix_faq_number_format()
    print(f'✅ All {len(NUMBER_FORMAT_CASES)} fix_faq_number_format cases passed')
    test_fix_faq_grammar_mismatch()
    print(f'✅ All {len(GRAMMAR_CASES)} fix_faq_grammar_mismatch cases passed')
