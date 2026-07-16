"""Offline regression test for blog_manager.clean_forbidden_phrases(). No network/DB calls.

Run: python3 test_blog_content.py
"""
from blog_manager import clean_forbidden_phrases

# Every phrase banned in generate_article_with_llm()'s system prompt must also be
# caught here as an algorithmic fallback in case the model ignores the instruction.
FORBIDDEN_PHRASES = [
    'devrimsel', 'şık tasarım', 'büyüleyici deneyim', 'sonuç olarak',
    'göz kamaştırıcı', 'özetlemek gerekirse', 'unutmamak gerekir ki',
    'derinlemesine dalış', 'ezber bozan', 'kendine hayran bırakıyor',
    'kullanıcı deneyimini üst seviyeye taşıyor', 'iddialı bir seçenek',
    'fark yaratıyor', 'hayatımızın vazgeçilmezi', 'adeta',
    'teknoloji dünyasında',
]


def test_clean_forbidden_phrases_strips_all_banned_phrases():
    failures = []
    for phrase in FORBIDDEN_PHRASES:
        text = f'Bu telefon {phrase} bir seçenek sunuyor.'
        result = clean_forbidden_phrases(text)
        if phrase.lower() in result.lower():
            failures.append(f'FAIL: {phrase!r} was not stripped -> {result!r}')
    assert not failures, '\n' + '\n'.join(failures)


def test_clean_forbidden_phrases_preserves_markdown_structure():
    sample = (
        '<h2 id="giris">Giriş</h2>\n'
        'Bu telefon devrimsel bir yaklaşım sunuyor.\n\n'
        'Sonuç olarak, iyi bir seçim.\n'
    )
    result = clean_forbidden_phrases(sample)
    assert '<h2 id="giris">Giriş</h2>' in result, 'heading structure destroyed'
    assert '\n\n' in result, 'paragraph break destroyed'
    assert 'devrimsel' not in result.lower()
    assert 'sonuç olarak' not in result.lower()


if __name__ == '__main__':
    test_clean_forbidden_phrases_strips_all_banned_phrases()
    print(f'✅ All {len(FORBIDDEN_PHRASES)} forbidden-phrase cases passed')
    test_clean_forbidden_phrases_preserves_markdown_structure()
    print('✅ Markdown structure preservation check passed')
