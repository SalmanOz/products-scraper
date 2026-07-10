"""Offline regression test for bulk_generate_verdicts.clean_hallucinations(). No network/DB calls.

Requires the project's dependencies (google-genai, pydantic) — run via the repo venv:
    venv/bin/python3 test_verdicts.py
"""
from bulk_generate_verdicts import clean_hallucinations

# Every word/phrase banned in generate_ai_analysis()'s system_instruction must also be
# caught here as an algorithmic fallback in case the model ignores the instruction.
FORBIDDEN_WORDS = [
    'adeta', 'muazzam', 'harika', 'şüphesiz', 'canavar', 'ezber bozan',
    'yeniden tanımlıyor', 'kusursuz', 'çığır açan', 'göz dolduruyor',
    'olağanüstü', 'şık tasarımıyla', 'dikkat çekiyor',
]


def test_clean_hallucinations_strips_all_forbidden_words():
    failures = []
    for word in FORBIDDEN_WORDS:
        verdict = f'Bu telefon {word} bir performans sunuyor!'
        result = clean_hallucinations({'verdict': verdict}, {})
        cleaned = result['verdict']
        if word.lower() in cleaned.lower():
            failures.append(f'FAIL: {word!r} was not stripped -> {cleaned!r}')
        if '!' in cleaned:
            failures.append(f'FAIL: exclamation mark survived for {word!r} -> {cleaned!r}')
    assert not failures, '\n' + '\n'.join(failures)


if __name__ == '__main__':
    test_clean_hallucinations_strips_all_forbidden_words()
    print(f'✅ All {len(FORBIDDEN_WORDS)} forbidden-word cases passed')
