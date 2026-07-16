"""Post-generation quality gate for blog articles.

Runs after generation and before save. Two layers:

  1. Deterministic checks — free, offline, run every time:
     - AI-tell scan (clichés, meta-openers, banned title patterns)
     - Monotony metrics (uniform sentence lengths, repeated paragraph openers)
     - SEO structure (title/summary length, word count, H2s, internal links,
       heading hierarchy, image URL provenance)
     - FACT CHECK: every large number in the article (price, AnTuTu, mAh…)
       must trace back to the source dataset — the single strongest guard
       against hallucinated specs.
     - Title similarity vs recent posts (fuzzy dedup).

  2. LLM judge — one Gemini call scoring human-ness / factual consistency /
     SEO on a strict JSON rubric. Skipped gracefully if no API key.

The gate returns (passed, report). blog_manager retries generation once with
the failure report as feedback; if it still fails, the post is saved as a
DRAFT instead of published — quality failures never ship unreviewed.
"""

import difflib
import json
import os
import re

import requests

# ----------------------------------------------------------------------
# Deterministic layer
# ----------------------------------------------------------------------

AI_TELLS = [
    'devrimsel', 'şık tasarım', 'büyüleyici deneyim', 'sonuç olarak',
    'göz kamaştırıcı', 'özetlemek gerekirse', 'unutmamak gerekir ki',
    'derinlemesine dalış', 'ezber bozan', 'kendine hayran bırakıyor',
    'kullanıcı deneyimini üst seviyeye taşıyor', 'iddialı bir seçenek',
    'fark yaratıyor', 'hayatımızın vazgeçilmezi', 'adeta',
    'teknoloji dünyasında',
]

META_OPENERS = [
    r'^bu (makalede|yazıda|rehberde|incelemede)',
    r'^(bu içerikte|sizler için)',
]

BANNED_TITLE_PATTERNS = [
    r"^20\d\d'?[nı]?[iı]n en iyi",  # "2026'nın En İyi..." formula
]

# Physical-testing claims the site must never fabricate (E-E-A-T)
FABRICATED_EXPERIENCE = [
    'elimize aldık', 'testlerimizde', 'test ettiğimizde', 'kullandığımızda',
    'cebimizde taşıdık', 'masamızdan', 'denediğimizde',
]


def _tr_lower(text):
    """Turkish-safe lowercase: Python's 'İ'.lower() yields 'i' + a combining
    dot (U+0307), which silently breaks every substring match against plain
    'i' — strip the combining mark after lowering."""
    return text.lower().replace('̇', '')


def _strip_html(text):
    return re.sub(r'<[^>]+>', ' ', text)


def _sentences(text):
    plain = _strip_html(text)
    parts = re.split(r'(?<=[.!?])\s+', plain)
    return [p.strip() for p in parts if len(p.strip()) > 10]


def _turkish_number(tok):
    """Parse '1.932.053' / '12.133,33' / '42000' to float."""
    t = tok.replace('.', '').replace(',', '.')
    try:
        return float(t)
    except ValueError:
        return None


def collect_source_numbers(source_data):
    """Every numeric fact in the dataset the article is allowed to cite:
    prices, scores, and all numeric attribute values (AnTuTu, mAh, GB, W…)."""
    allowed = set()

    def walk(v):
        if isinstance(v, (int, float)) and v > 0:
            allowed.add(float(v))
        elif isinstance(v, str):
            for m in re.findall(r'\d[\d.,]*', v):
                n = _turkish_number(m)
                if n:
                    allowed.add(n)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    for item in source_data:
        walk(item)
    return allowed


def check_facts(content, source_data):
    """Flag numbers >= 1000 that don't match any source number within 5%.
    Small numbers (FPS caps, GB, percentages, years) are too ambiguous to
    attribute, and every hallucination class we care about — price, AnTuTu,
    mAh — lives above 1000."""
    allowed = collect_source_numbers(source_data)
    problems = []
    plain = _strip_html(content)
    for tok in re.findall(r'\d[\d.,]*\d|\d', plain):
        n = _turkish_number(tok)
        if n is None or n < 1000:
            continue
        if 2020 <= n <= 2030:  # years
            continue
        if not any(abs(n - a) <= a * 0.05 for a in allowed):
            problems.append(f"Kaynak veride olmayan sayı: {tok} (≈{n:.0f})")
    # de-dup, cap noise
    seen, unique = set(), []
    for p in problems:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique[:10]


def deterministic_checks(title, summary, content, source_data, recent_titles=None):
    """Returns dict of {failures: [...], warnings: [...], metrics: {...}}."""
    failures, warnings = [], []
    plain = _strip_html(content)
    words = len(plain.split())
    sentences = _sentences(content)

    # --- AI tells ---
    low = _tr_lower(plain)
    hits = [t for t in AI_TELLS if t in low]
    if hits:
        failures.append(f"AI klişeleri geçiyor: {', '.join(hits)}")
    for pat in META_OPENERS:
        if re.search(pat, _tr_lower(plain.strip())):
            failures.append("Meta-açılış ('Bu makalede...') kullanılmış")
            break
    fab = [f for f in FABRICATED_EXPERIENCE if f in low]
    if fab:
        failures.append(f"Uydurma fiziksel test iması: {', '.join(fab)}")

    # --- monotony metrics ---
    if len(sentences) >= 10:
        lengths = [len(s.split()) for s in sentences]
        mean = sum(lengths) / len(lengths)
        var = sum((l - mean) ** 2 for l in lengths) / len(lengths)
        std = var ** 0.5
        if std < 4.0:
            warnings.append(f"Cümle uzunlukları fazla uniform (std={std:.1f}) — insan yazısında ritim değişir")
        openers = [s.split()[0].lower() for s in sentences if s.split()]
        for op in set(openers):
            share = openers.count(op) / len(openers)
            if share > 0.2 and openers.count(op) >= 4:
                warnings.append(f"Cümlelerin %{share*100:.0f}'i aynı kelimeyle başlıyor: '{op}'")

    # --- SEO structure ---
    if len(title) > 65:
        warnings.append(f"Başlık {len(title)} karakter — SERP'te kırpılır (hedef ≤60)")
    for pat in BANNED_TITLE_PATTERNS:
        if re.search(pat, _tr_lower(title)):
            failures.append("Yasaklı başlık kalıbı ('2026'nın En İyi...')")
    if not (60 <= len(summary) <= 200):
        warnings.append(f"Özet {len(summary)} karakter (hedef 60-200, meta description olarak kullanılıyor)")
    if words < 1000:
        failures.append(f"Yazı çok kısa: {words} kelime (hedef 1500+)")
    h2_count = len(re.findall(r'<h2[ >]|^##\s', content, re.MULTILINE))
    if h2_count < 3:
        warnings.append(f"Sadece {h2_count} H2 var — yapı zayıf")
    if re.search(r'<h1[ >]|^#\s', content, re.MULTILINE):
        warnings.append("İçerikte H1 var — sayfa şablonu zaten H1 basıyor, çift H1 olur")
    internal_links = len(re.findall(r'href=["\']/(?:product|karsilastir|blog|[a-z0-9-]+)', content))
    if internal_links < 2:
        warnings.append(f"Sadece {internal_links} iç link — en az 2-3 olmalı")

    # image provenance: only source-provided URLs
    allowed_imgs = set()
    for item in source_data:
        imgs = item.get("images") or []
        if isinstance(imgs, list):
            allowed_imgs.update(imgs)
    for src in re.findall(r'<img[^>]+src="([^"]+)"', content):
        if src not in allowed_imgs and 'cdn.teknoskor.com' not in src:
            failures.append(f"Kaynak veride olmayan görsel URL'si: {src[:80]}")

    # --- facts ---
    fact_problems = check_facts(content, source_data)
    if fact_problems:
        failures.append("Doğrulanamayan rakamlar: " + " | ".join(fact_problems))

    # --- title dedup ---
    for rt in (recent_titles or []):
        sim = difflib.SequenceMatcher(None, _tr_lower(title), _tr_lower(rt)).ratio()
        if sim > 0.6:
            failures.append(f"Başlık eski bir yazıya çok benziyor (%{sim*100:.0f}): '{rt}'")
            break

    return {"failures": failures, "warnings": warnings,
            "metrics": {"words": words, "sentences": len(sentences), "h2": h2_count,
                        "internal_links": internal_links}}


# ----------------------------------------------------------------------
# LLM judge layer
# ----------------------------------------------------------------------

JUDGE_PROMPT = """Sen acımasız bir yayın editörüsün. Aşağıdaki Türkçe blog yazısını üç eksende değerlendir ve SADECE geçerli JSON döndür.

Değerlendirme eksenleri:
1. human_score (0-10): İnsan mı yazmış gibi? AI kalıpları, mekanik simetri, ruhsuz geçişler puan düşürür. 7 altı = yayınlanamaz.
2. factual_score (0-10): Yazıdaki iddialar SADECE verilen kaynak veriyle tutarlı mı? Veride olmayan spesifik iddia (rakam, özellik, kıyas sonucu) gördüysen puanı düşür ve listele. Fiziksel test iması ("elimize aldık" vb.) otomatik 4 altı.
3. seo_score (0-10): Başlık niyeti karşılıyor mu, yapı taranabilir mi, giriş değer vaat ediyor mu, içerik sorulan soruyu gerçekten cevaplıyor mu?

JSON şeması:
{"human_score": n, "factual_score": n, "seo_score": n, "problems": ["somut sorun 1", ...], "verdict": "publish" | "revise"}

verdict kuralı: üç skor da >= 7 ise "publish", değilse "revise".

KAYNAK VERİ:
%%DATA%%

BAŞLIK: %%TITLE%%
ÖZET: %%SUMMARY%%

YAZI:
%%CONTENT%%
"""


def llm_judge(title, summary, content, source_data):
    """Returns judge dict or None when unavailable (no key / API error) —
    the deterministic layer alone still gates in that case."""
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return None

    prompt = (JUDGE_PROMPT
              .replace("%%DATA%%", json.dumps(source_data, ensure_ascii=False, default=str)[:8000])
              .replace("%%TITLE%%", title)
              .replace("%%SUMMARY%%", summary)
              .replace("%%CONTENT%%", _strip_html(content)[:16000]))

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 2048,
                             "responseMimeType": "application/json"},
    }
    for model in ["gemini-2.5-flash", "gemini-2.0-flash"]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        try:
            resp = requests.post(url, json=payload, timeout=60)
            if resp.status_code != 200:
                continue
            text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
            verdict = json.loads(text)
            if all(k in verdict for k in ("human_score", "factual_score", "seo_score", "verdict")):
                return verdict
        except Exception:
            continue
    return None


# ----------------------------------------------------------------------
# Gate
# ----------------------------------------------------------------------

def run_quality_gate(title, summary, content, source_data, recent_titles=None):
    """Returns (passed: bool, report: str). Passed requires zero deterministic
    failures AND (judge unavailable OR judge says publish)."""
    det = deterministic_checks(title, summary, content, source_data, recent_titles)
    judge = llm_judge(title, summary, content, source_data)

    lines = [f"Kelime: {det['metrics']['words']}, H2: {det['metrics']['h2']}, iç link: {det['metrics']['internal_links']}"]
    for f in det["failures"]:
        lines.append(f"❌ {f}")
    for w in det["warnings"]:
        lines.append(f"⚠️ {w}")

    judge_ok = True
    if judge:
        lines.append(f"Hakem: insan={judge['human_score']}/10 doğruluk={judge['factual_score']}/10 seo={judge['seo_score']}/10 → {judge['verdict']}")
        for p in judge.get("problems", [])[:8]:
            lines.append(f"🔎 {p}")
        judge_ok = judge.get("verdict") == "publish"
    else:
        lines.append("Hakem: atlandı (API yok/erişilemedi) — sadece deterministik kontroller")

    passed = not det["failures"] and judge_ok
    lines.insert(0, "✅ KALİTE KAPISI: GEÇTİ" if passed else "🛑 KALİTE KAPISI: KALDI")
    return passed, "\n".join(lines)
