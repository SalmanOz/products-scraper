#!/usr/bin/env python3
import os
import re
import json
import decimal
import argparse
import requests
import mysql.connector
from dotenv import load_dotenv
from llm_client import chat as llm_chat

# Load environment variables from .env if present
load_dotenv()

# ----------------------------------------------------------------------
# 1. Database Connections
# ----------------------------------------------------------------------
def get_db_connection():
    # Read environment variables with fallbacks
    host = os.getenv("DB_HOST", "127.0.0.1")
    port = int(os.getenv("DB_PORT", 3306))
    user = os.getenv("DB_USER", "root")
    password = os.getenv("DB_PASSWORD", "rootpassword")
    database = os.getenv("DB_NAME", "product_comparison")
    
    return mysql.connector.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        connection_timeout=10
    )

# ----------------------------------------------------------------------
# Topic archetypes.
#
# The generator used to know only 3 topics (performance/camera/battery), each
# built from a near-identical top-5 dataset — so every run produced the same
# phones under a slightly reworded "2026'nın en iyileri" headline. Each
# archetype below pulls a *different slice* of the catalog and carries its own
# editorial angle, and pick_topic() skips angles the blog covered recently.
#
# `keywords` are matched against recent post titles for the dedup guard.
# ----------------------------------------------------------------------
BASE_SELECT = """
    SELECT p.name, p.slug, p.base_price, b.name as brand_name,
           p.teknoskor_score, p.images,
           CAST(JSON_UNQUOTE(JSON_EXTRACT(p.attributes, '$.antutu_score')) AS SIGNED) as antutu_score,
           p.attributes
    FROM products p
    JOIN brands b ON p.brand_id = b.id
    WHERE p.status IN ('published', 'draft')
"""

TOPIC_ARCHETYPES = {
    "performance": {
        "keywords": ["performans", "antutu", "hız"],
        "angle": "Ham güç analizi: AnTuTu skorlarının günlük kullanımda ve oyunda gerçekte neye karşılık geldiğini anlat.",
    },
    "camera": {
        "keywords": ["kamera", "fotoğraf", "megapiksel"],
        "angle": "Kamera gerçekleri: megapiksel pazarlamasıyla gerçek fotoğraf kalitesi arasındaki farkı skorlarla göster.",
    },
    "battery": {
        "keywords": ["batarya", "şarj", "pil"],
        "angle": "Batarya ve şarj dengesi: mAh rakamının tek başına neden yetmediğini, işlemci verimliliğiyle birlikte değerlendir.",
    },
    "value": {
        "keywords": ["fiyat/performans", "fiyat performans", "değer"],
        "angle": "Puan/TL analizi: ödenen her 1.000 TL'nin karşılığını en çok veren modelleri, pahalıların neden kaybettiğini de açıklayarak sırala.",
    },
    "budget": {
        "keywords": ["bütçe", "ucuz", "20.000", "uygun fiyat"],
        "angle": "Dar bütçe rehberi: 20 bin TL altında neyin gerçekten alınabilir olduğunu, hangi fedakarlıkların kabul edilebilir olduğunu anlat.",
    },
    "flagship": {
        "keywords": ["amiral gemisi", "en pahalı", "premium"],
        "angle": "Amiral gemisi sorgulaması: 50 bin TL üstü telefonların hangileri parasını hak ediyor, hangileri marka vergisi ödetiyor?",
    },
    "gaming": {
        "keywords": ["oyun", "pubg", "fps"],
        "angle": "Oyuncu odaklı analiz: FPS verileri, ısınma ve dokunmatik tepkime üzerinden gerçek oyun deneyimini karşılaştır.",
    },
    "midrange": {
        "keywords": ["orta segment", "20-40", "dengeli"],
        "angle": "Orta segment savaşı: 20-40 bin TL bandında amiral gemisi deneyimine en çok yaklaşan modelleri karşılaştır.",
    },
    "brand_showdown": {
        "keywords": ["kapışma", "karşı karşıya", "marka savaşı"],
        "angle": "Marka kapışması: iki büyük markanın güncel kadrolarını segment segment karşılaştır, hangisi hangi bütçede kazanıyor söyle.",
    },
    "versus": {
        "keywords": [" vs ", "hangisi alınır", "düello"],
        "angle": "İkili düello: birbirine en yakın iki rakibi derinlemesine karşılaştır ve net bir kazanan ilan et.",
    },
}


def fetch_topic_data(topic_type):
    """Returns (rows, extra_note) for the new archetypes; legacy topics fall
    through to fetch_phone_data. extra_note carries archetype-specific context
    the prompt needs (e.g. the /karsilastir URL for a versus duel)."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    rows, note = [], ""

    try:
        if topic_type == "value":
            cursor.execute(BASE_SELECT + """
                AND p.base_price > 0 AND p.teknoskor_score >= 70
                ORDER BY p.teknoskor_score / (p.base_price / 1000) DESC LIMIT 5
            """)
            rows = cursor.fetchall()
        elif topic_type == "budget":
            cursor.execute(BASE_SELECT + """
                AND p.base_price BETWEEN 1 AND 20000
                ORDER BY p.teknoskor_score DESC LIMIT 5
            """)
            rows = cursor.fetchall()
        elif topic_type == "flagship":
            cursor.execute(BASE_SELECT + """
                AND p.base_price >= 50000
                ORDER BY p.teknoskor_score DESC LIMIT 5
            """)
            rows = cursor.fetchall()
        elif topic_type == "gaming":
            cursor.execute(BASE_SELECT + """
                AND JSON_EXTRACT(p.attributes, '$.gaming_performance') IS NOT NULL
                ORDER BY antutu_score DESC LIMIT 5
            """)
            rows = cursor.fetchall()
        elif topic_type == "midrange":
            cursor.execute(BASE_SELECT + """
                AND p.base_price BETWEEN 20000 AND 40000
                ORDER BY p.teknoskor_score DESC LIMIT 5
            """)
            rows = cursor.fetchall()
        elif topic_type == "brand_showdown":
            cursor.execute("""
                SELECT b.id, b.name, COUNT(*) as cnt FROM products p
                JOIN brands b ON p.brand_id = b.id
                WHERE p.status IN ('published', 'draft') AND p.teknoskor_score > 0
                GROUP BY b.id, b.name ORDER BY cnt DESC LIMIT 2
            """)
            brands = cursor.fetchall()
            if len(brands) == 2:
                for br in brands:
                    cursor.execute(BASE_SELECT + """
                        AND p.brand_id = %s AND p.teknoskor_score > 0
                        ORDER BY p.teknoskor_score DESC LIMIT 3
                    """, (br["id"],))
                    rows.extend(cursor.fetchall())
                note = f"Bu bir marka kapışması yazısı: {brands[0]['name']} vs {brands[1]['name']}. Her markadan 3 model var; segment segment kıyasla."
        elif topic_type == "versus":
            # Closest matchup by relative price gap + score gap — the same
            # buyer-indecision heuristic as frontend versus.ts, so the duel
            # article always has a matching /karsilastir page to link to.
            cursor.execute(BASE_SELECT + " AND p.base_price > 0 AND p.teknoskor_score > 0")
            pool = cursor.fetchall()
            best, best_d = None, None
            for i in range(len(pool)):
                for j in range(i + 1, len(pool)):
                    a, b = pool[i], pool[j]
                    pa, pb = float(a["base_price"]), float(b["base_price"])
                    d = abs(pa - pb) / max(pa, pb) + abs(float(a["teknoskor_score"]) - float(b["teknoskor_score"])) / 100 * 0.5
                    if best_d is None or d < best_d:
                        best, best_d = (a, b), d
            if best:
                rows = list(best)
                pair = "-vs-".join(sorted([best[0]["slug"], best[1]["slug"]]))
                note = (f"Bu bir ikili düello yazısı: {best[0]['name']} vs {best[1]['name']}. "
                        f"Yazının içinde bir yerde şu karşılaştırma sayfasına doğal bir cümleyle link ver: "
                        f"[detaylı karşılaştırma tablosu](/karsilastir/{pair})")
    finally:
        cursor.close()
        conn.close()

    if rows:
        rows = _normalize_rows(rows)
    return rows, note


def fetch_recent_post_titles(limit=12):
    """Titles of the newest posts — the dedup guard reads these so the
    generator stops rewriting the same article. Fails soft: no table yet
    (fresh install) just means no dedup context."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT title FROM blog_posts WHERE lang = 'tr' ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        titles = [r["title"] for r in cursor.fetchall()]
        cursor.close()
        conn.close()
        return titles
    except Exception:
        return []


def pick_topic(recent_titles):
    """Random archetype, excluding any whose signature keywords appear in a
    recent title — cheap rotation without needing a topic column in the DB."""
    import random
    recent_blob = " ".join(recent_titles).lower()
    fresh = [t for t, cfg in TOPIC_ARCHETYPES.items()
             if not any(kw in recent_blob for kw in cfg["keywords"])]
    return random.choice(fresh if fresh else list(TOPIC_ARCHETYPES.keys()))


def fetch_phone_data(topic_type="performance"):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    if topic_type == "camera":
        query = """
            SELECT p.name, p.slug, p.base_price, b.name as brand_name, 
                   p.teknoskor_score, p.images,
                   CAST(JSON_UNQUOTE(JSON_EXTRACT(p.attributes, '$.camera_score')) AS DECIMAL(3,1)) as camera_score,
                   p.attributes 
            FROM products p 
            JOIN brands b ON p.brand_id = b.id 
            WHERE p.status IN ('published', 'draft') AND JSON_EXTRACT(p.attributes, '$.camera_score') IS NOT NULL
            ORDER BY camera_score DESC 
            LIMIT 5
        """
    elif topic_type == "battery":
        query = """
            SELECT p.name, p.slug, p.base_price, b.name as brand_name, 
                   p.teknoskor_score, p.images,
                   CAST(JSON_UNQUOTE(JSON_EXTRACT(p.attributes, '$.battery_score')) AS DECIMAL(3,1)) as battery_score,
                   p.attributes 
            FROM products p 
            JOIN brands b ON p.brand_id = b.id 
            WHERE p.status IN ('published', 'draft') AND JSON_EXTRACT(p.attributes, '$.battery_score') IS NOT NULL
            ORDER BY battery_score DESC 
            LIMIT 5
        """
    else: # Default: performance
        query = """
            SELECT p.name, p.slug, p.base_price, b.name as brand_name, 
                   p.teknoskor_score, p.images,
                   CAST(JSON_UNQUOTE(JSON_EXTRACT(p.attributes, '$.antutu_score')) AS SIGNED) as antutu_score,
                   p.attributes 
            FROM products p 
            JOIN brands b ON p.brand_id = b.id 
            WHERE p.status IN ('published', 'draft') AND JSON_EXTRACT(p.attributes, '$.antutu_score') IS NOT NULL
            ORDER BY antutu_score DESC 
            LIMIT 5
        """
        
    cursor.execute(query)
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    return _normalize_rows(rows)


def _normalize_rows(rows):
    """Parse attributes/images JSON strings and convert Decimals — shared by
    the legacy queries and the archetype fetchers."""
    for row in rows:
        if "attributes" in row and isinstance(row["attributes"], str):
            try:
                row["attributes"] = json.loads(row["attributes"])
            except:
                pass
        if "images" in row and isinstance(row["images"], str):
            try:
                row["images"] = json.loads(row["images"])
            except:
                pass

    def convert_decimals(obj):
        if isinstance(obj, decimal.Decimal):
            return float(obj)
        elif isinstance(obj, dict):
            return {k: convert_decimals(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [convert_decimals(v) for v in obj]
        return obj

    return convert_decimals(rows)

# ----------------------------------------------------------------------
# 2. Markdown to HTML Converter
# ----------------------------------------------------------------------
def markdown_to_html(md_text: str) -> str:
    html = md_text.replace('\r\n', '\n')
    
    # Convert blockquotes
    html = re.sub(r'^[ \t]*>\s+(.*?)$', r'<blockquote>\1</blockquote>', html, flags=re.MULTILINE)
    
    # Convert headings
    html = re.sub(r'^[ \t]*###\s+(.*?)$', r'<h3>\1</h3>', html, flags=re.MULTILINE)
    html = re.sub(r'^[ \t]*##\s+(.*?)$', r'<h2>\1</h2>', html, flags=re.MULTILINE)
    html = re.sub(r'^[ \t]*#\s+(.*?)$', r'<h1>\1</h1>', html, flags=re.MULTILINE)
    
    # Convert bold and italic
    html = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', html)
    html = re.sub(r'\*(.*?)\*', r'<em>\1</em>', html)
    
    # Convert Markdown Links to HTML anchors
    html = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', html)
    
    # Convert list items
    html = re.sub(r'^[ \t]*[\-\*]\s+(.*?)$', r'<li>\1</li>', html, flags=re.MULTILINE)
    
    # Wrap lists (line-by-line processing is 100% bulletproof)
    lines = html.split('\n')
    new_lines = []
    in_list = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('<li>') and stripped.endswith('</li>'):
            if not in_list:
                new_lines.append('<ul>')
                in_list = True
            new_lines.append(line)
        else:
            if in_list:
                new_lines.append('</ul>')
                in_list = False
            new_lines.append(line)
    if in_list:
        new_lines.append('</ul>')
    html = '\n'.join(new_lines)
    
    # Wrap paragraphs
    paragraphs = []
    for block in html.split('\n\n'):
        block = block.strip()
        if not block:
            continue
        if block.startswith('<h') or block.startswith('<u') or block.startswith('<o') or block.startswith('<l') or block.startswith('<b') or block.startswith('<i') or block.startswith('<p'):
            paragraphs.append(block)
        else:
            paragraphs.append(f"<p>{block}</p>")
            
    return "\n".join(paragraphs)

# ----------------------------------------------------------------------
# 3. Suffix-Aware Exact-Match Regex Internal Linking Engine
# ----------------------------------------------------------------------
def inject_internal_links(content: str, lang: str = "tr") -> str:
    lang_prefix = f"/{lang}" if lang and lang != "tr" else ""
    keyword_map = {
        "pubg performansı": f"{lang_prefix}/pubg-performansi-en-yuksek-telefonlar",
        "oyun telefonu": f"{lang_prefix}/pubg-performansi-en-yuksek-telefonlar",
        "oyun telefonları": f"{lang_prefix}/en-iyi-oyun-telefonlari",
        "kameralı telefon": f"{lang_prefix}/en-iyi-kamera-skorlu-telefonlar",
        "kamera performansı": f"{lang_prefix}/en-iyi-kamera-skorlu-telefonlar",
        "şarjı en uzun": f"{lang_prefix}/sarji-en-uzun-giden-fiyat-performans-telefonlari",
        "şarjı uzun": f"{lang_prefix}/sarji-en-uzun-giden-fiyat-performans-telefonlari",
        "pil ömrü": f"{lang_prefix}/sarji-en-uzun-giden-fiyat-performans-telefonlari",
        # Config-driven pillar pages (frontend/src/lib/pillars.ts)
        "antutu sıralaması": f"{lang_prefix}/antutu-siralamasi",
        "fiyat/performans oranı": f"{lang_prefix}/en-iyi-fiyat-performans-telefonlar",
        "fiyat performans telefonu": f"{lang_prefix}/en-iyi-fiyat-performans-telefonlar",
        "hızlı şarj": f"{lang_prefix}/en-hizli-sarj-olan-telefonlar",
        "suya dayanıklı": f"{lang_prefix}/en-dayanikli-telefonlar",
        "6000 mah": f"{lang_prefix}/6000-mah-uzeri-batarya-telefonlar",
    }
    
    # Split content by HTML anchor tags, regular HTML tags, and Markdown links
    parts = re.split(r'(?is)(<a[^>]*>.*?</a>|<[^>]+>|\[[^\]]+\]\([^)]+\))', content)
    
    replaced = set()
    
    for i in range(len(parts)):
        part = parts[i]
        # Skip HTML tags, anchor tags, and Markdown links
        if not part or part.startswith('<') or (part.startswith('[') and part.endswith(')')):
            continue
            
        for keyword, url in keyword_map.items():
            if keyword in replaced:
                continue
                
            # Suffix-aware regex pattern matching keyword and any appended Turkish letters
            pattern = re.compile(rf'\b({re.escape(keyword)})([a-zA-ZçğıöşüÇĞİÖŞÜ]*)\b', re.IGNORECASE)
            match = pattern.search(part)
            if match:
                # Replace only the first occurrence globally
                replacement = f'<a href="{url}">{match.group(1)}{match.group(2)}</a>'
                parts[i] = pattern.sub(replacement, part, count=1)
                replaced.add(keyword)
                part = parts[i]
                
    return "".join(parts)

# ----------------------------------------------------------------------
# 3. LLM Completion Invoker
# ----------------------------------------------------------------------
# Structure templates — one is picked per run so consecutive posts don't share
# the same skeleton. The old prompt forced "TOC + one section per phone" on
# every article, which is the single most recognizable AI-listicle fingerprint.
STRUCTURE_TEMPLATES = {
    "verdict_first": """YAPI: HÜKÜM ÖNCE. İlk paragrafta net kararını açıkla (hangi telefon[lar] kazandı ve neden), sonra kararın gerekçesini veriyle savun. İçindekiler listesi KULLANMA. Her telefona eşit yer AYIRMA — kazanan ve en yakın rakibi derin işle, kalanlara neden elendiklerini anlatan kısa birer paragraf yeter.""",
    "problem_led": """YAPI: SORUN ODAKLI. Gerçek bir okuyucu sorusuyla aç (örn. "Bütçen belli, kafan karışık: hangisi?") ve yazıyı o sorunun cevabı olarak kur. İçindekiler listesi KULLANMA. Senaryolar üzerinden ilerle (yoğun kullanan, oyuncu, fotoğrafçı gibi) ve her senaryoya net bir model öner.""",
    "myth_buster": """YAPI: EFSANE YIKICI. Yaygın bir satın alma inancını başa koy (örn. "daha çok mAh = daha uzun pil"), önce inancı adil biçimde anlat, sonra veriyle çürüt veya doğrula. Telefonları bu tez etrafında kanıt olarak kullan; klasik tek tek inceleme listesi yapma.""",
    "guide": """YAPI: REHBER. Uzun ve kapsamlı bir satın alma rehberi yaz. Girişten hemen sonra "İçindekiler" ver: Markdown link listesi (- [Bölüm](#bolum-id)) ve her H2'yi eşleşen id ile HTML olarak yaz: <h2 id="bolum-id">Bölüm</h2>. Telefonları mantıklı gruplara ayırarak işle, sonda net bir öneri tablosu ver.""",
}

# Voice notes — small register shifts between runs so the byline doesn't read
# like the same template every Monday.
VOICE_NOTES = [
    "Ses: tecrübeli ama yorgun editör — abartıya tahammülü yok, rakamı sever, gerektiğinde tek cümlelik paragrafla vurgu yapar.",
    "Ses: forumlarda yıllarını geçirmiş meraklı — okuyucuya 'sen' diye hitap eder, pazarlama dilini gördüğü yerde söyler.",
    "Ses: veri analisti — iddiaları ölçülebilir şeylere bağlar, emin olmadığı yerde 'veri bunu söylemiyor' demekten çekinmez.",
]


def generate_article_with_llm(topic_data, topic_type="performance", extra_note="", recent_titles=None):
    import random

    archetype = TOPIC_ARCHETYPES.get(topic_type, {})
    angle = archetype.get("angle", "")
    structure_key = "guide" if topic_type in ("budget", "midrange") else random.choice(list(STRUCTURE_TEMPLATES.keys()))
    structure = STRUCTURE_TEMPLATES[structure_key]
    voice = random.choice(VOICE_NOTES)

    recent_titles = recent_titles or []
    dedup_block = ""
    if recent_titles:
        titles_list = "\n".join(f"- {t}" for t in recent_titles)
        dedup_block = f"""
DAHA ÖNCE YAYINLANAN BAŞLIKLAR (bunlara benzeyen başlık ve açı ÜRETME — ne kelime kalıbı ne konu açısı tekrar etmeli):
{titles_list}
"""

    system_prompt = f"""Sen Teknoskor'un kıdemli donanım editörüsün. Sana verilen telefon verisiyle Türkçe, 1500-2000 kelimelik, derinlikli bir yazı yazacaksın.

EDİTORYAL AÇI: {angle}
{structure}
{voice}
{dedup_block}
İNSAN GİBİ YAZMA KURALLARI (en kritik bölüm):
1. YASAKLI KLİŞELER — bunları kullanırsan yazı reddedilir: 'devrimsel', 'şık tasarım', 'büyüleyici deneyim', 'sonuç olarak', 'göz kamaştırıcı', 'özetlemek gerekirse', 'unutmamak gerekir ki', 'derinlemesine dalış', 'adeta', 'ezber bozan', 'kendine hayran bırakıyor', 'kullanıcı deneyimini üst seviyeye taşıyor', 'teknoloji dünyasında', 'hayatımızın vazgeçilmezi', 'fark yaratıyor', 'iddialı bir seçenek'.
2. YASAKLI AÇILIŞLAR: "Bu makalede/yazıda ... inceleyeceğiz/ele alacağız" tarzı meta-açılış yapma. Doğrudan konuya, bir gözleme, bir rakama veya bir soruya gir.
3. CÜMLE RİTMİ: Cümle uzunluklarını bilinçli değiştir. Uzun bir analiz cümlesinin ardından kısa bir hüküm gelsin. Bazen tek cümlelik paragraf kullan. Her paragrafı aynı kalıpla ("X modeli ise...") başlatma.
4. SOMUTLUK: Soyut övgü yerine yaşanan senaryo yaz: sabah metrosunda navigasyon + Spotify, akşam PUBG oturumu, tatilde şarjsız geçen gün. Fiyatları TL olarak yaz ve pahalıysa "pahalı" de.
5. SİMETRİ YASAĞI: Her telefona eşit paragraf ayırmak zorunda değilsin (rehber yapısı hariç). Gerçek bir editör gibi önemliye çok, önemsize az yer ver.
6. DÜRÜSTLÜK: Her cihazın zayıf yönünü net söyle. Verinin desteklemediği iddia kurma; birinci elden test izlenimi UYDURMA — sen spesifikasyon ve benchmark verisiyle çalışıyorsun, "elimize aldık", "testlerimizde" gibi fiziksel test iması kesinlikle yasak. "Veriler gösteriyor", "skorlara göre" de.
7. RAKAM ANLAMLANDIRMA: Spec tekrarı yapma; rakamın hayattaki karşılığını yaz ("5500 mAh + verimli işlemci = sosyal medya ağırlıklı kullanımda akşama %30 pil").

BİÇİM:
- Paragraflar kısa (2-3 cümle), mobilde okunacak.
- Markdown H2/H3 alt başlıklar; kritik kıyaslarda **kalın**; karmaşık ödünleşimlerde madde işareti.
- BAŞLIK KURALI: "2026'nın En İyi..." kalıbını KULLANMA (bu kalıp zaten defalarca kullanıldı). Başlık merak uyandırsın veya net bir iddia taşısın; yıl geçebilir ama başa koyma.
- CİHAZ GÖRSELLERİ: Bir modeli işlediğin bölümde görselini şu etiketle göm (image_url alanı boşsa görsel koyma, URL uydurma):
  <img class="my-6 rounded-3xl max-h-[350px] object-contain mx-auto shadow-sm" src="IMAGE_URL" alt="Telefon Adı" />
- İç link: metinde doğal biçimde geçen kavramlar zaten otomatik linkleniyor; sen sadece akıcı yaz.

ÇIKTI SÖZLEŞMESİ — yanıtını TAM OLARAK bu üç etiketle yapılandır, [CONTENT] içine kısaltma/özet değil eksiksiz yazıyı koy:

[TITLE]
(başlık)

[SUMMARY]
(1-2 cümlelik özet — "bu makalede" deme)

[CONTENT]
(1500+ kelimelik tam yazı)
"""

    formatted_data = []
    for item in topic_data:
        image_url = None
        if "images" in item and item["images"]:
            if isinstance(item["images"], list) and len(item["images"]) > 0:
                image_url = item["images"][0]
            elif isinstance(item["images"], str):
                try:
                    imgs = json.loads(item["images"])
                    if imgs and len(imgs) > 0:
                        image_url = imgs[0]
                except:
                    pass
        
        phone_info = {
            "name": item.get("name"),
            "brand": item.get("brand_name"),
            "price": item.get("base_price"),
            "teknoskor_score": item.get("teknoskor_score"),
            "specs": item.get("attributes"),
            "image_url": image_url
        }
        formatted_data.append(phone_info)

    note_block = f"\n\nÖZEL TALİMAT: {extra_note}" if extra_note else ""
    prompt = f"{system_prompt}{note_block}\n\nVERİ:\n{json.dumps(formatted_data, indent=2, ensure_ascii=False)}"

    # Provider routing (Gemini -> NVIDIA fallback), retries and MAX_TOKENS
    # continuation all live in llm_client.chat
    full_text = llm_chat(prompt, temperature=0.7, max_tokens=8192)
    return clean_forbidden_phrases(full_text)


def clean_forbidden_phrases(text):
    """Algorithmic guardrail: strip the system prompt's banned AI-cliché phrases
    from generated article text in case the model ignores the instruction. Blog
    posts are generated on a cron schedule and default to status=published (see
    .github/workflows/generate-blog.yml), so unlike a manually-reviewed draft
    this has no human checkpoint — bulk_generate_verdicts.py has an equivalent
    fallback (clean_hallucinations) for the same reason.

    Only single-word/short-phrase bans get a direct substitution. The three
    sentence-opening transition phrases ("sonuç olarak" etc.) are removed along
    with a trailing comma, since leaving the comma behind reads as broken
    grammar; a possible lowercase first letter after removal is a minor
    cosmetic tradeoff against leaving a banned cliché in place.
    """
    word_replacements = {
        r'\bdevrimsel\b': 'gelişmiş',
        r'\bşık tasarım\b': 'tasarım',
        r'\bbüyüleyici deneyim\b': 'iyi bir deneyim',
        r'\bgöz kamaştırıcı\b': 'başarılı',
        r'\bderinlemesine dalış\b': 'detaylı inceleme',
        r'\bezber bozan\b': 'dikkat çeken',
        r'\bkendine hayran bırakıyor\b': 'öne çıkıyor',
        r'\bkullanıcı deneyimini üst seviyeye taşıyor\b': 'kullanımı belirgin iyileştiriyor',
        r'\biddialı bir seçenek\b': 'güçlü bir seçenek',
        r'\bfark yaratıyor\b': 'öne çıkıyor',
        r'\bhayatımızın vazgeçilmezi\b': 'vazgeçilmez',
    }
    transition_phrases = [
        r'\bsonuç olarak\b', r'\bözetlemek gerekirse\b', r'\bunutmamak gerekir ki\b',
        r'\badeta\b', r'\bteknoloji dünyasında\b',
    ]

    cleaned = text
    for pattern, replacement in word_replacements.items():
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)
    for pattern in transition_phrases:
        cleaned = re.sub(pattern + r',?\s*', '', cleaned, flags=re.IGNORECASE)
    # Collapse extra spaces left by empty replacements, but preserve newlines
    # so Markdown paragraph/heading structure isn't destroyed.
    cleaned = re.sub(r'[ \t]+', ' ', cleaned)
    cleaned = re.sub(r'[ \t]+\n', '\n', cleaned)
    return cleaned

# ----------------------------------------------------------------------
# 4. Turkish Slugify Utility
# ----------------------------------------------------------------------
def turkish_slugify(text: str) -> str:
    clean = text.lower()
    mapping = {
        'ş': 's', 'ı': 'i', 'ğ': 'g', 'ö': 'o', 'ü': 'u', 'ç': 'c',
        'Ş': 's', 'İ': 'i', 'Ğ': 'g', 'Ö': 'o', 'Ü': 'u', 'Ç': 'c'
    }
    for key, val in mapping.items():
        clean = clean.replace(key, val)
        
    clean = re.sub(r'[^a-z0-9 -]', '', clean)
    clean = re.sub(r'\s+', '-', clean)
    clean = re.sub(r'-+', '-', clean)
    return clean.strip('-')

# ----------------------------------------------------------------------
# 5. Database Save Operations
# ----------------------------------------------------------------------
def save_blog_post(title, slug, summary, content, image_url=None, status="draft", lang="tr"):
    conn = get_db_connection()
    cursor = conn.conn.cursor() if hasattr(conn, 'conn') else conn.cursor()
    
    # Self-healing database structure initialization
    create_table_query = """
    CREATE TABLE IF NOT EXISTS blog_posts (
      id INT AUTO_INCREMENT PRIMARY KEY,
      title VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
      slug VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
      summary TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL,
      content LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
      image_url VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL,
      status ENUM('draft', 'published') NOT NULL DEFAULT 'draft',
      lang VARCHAR(10) NOT NULL DEFAULT 'tr',
      views INT NOT NULL DEFAULT 0,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      UNIQUE KEY slug_lang (slug, lang),
      INDEX idx_lang_status_created (lang, status, created_at DESC)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
    """
    cursor.execute(create_table_query)
    conn.commit()
    
    # In case slug already exists in this language, append a unique modifier
    check_query = "SELECT COUNT(*) FROM blog_posts WHERE slug = %s AND lang = %s"
    
    cursor.execute(check_query, (slug, lang))
    count = cursor.fetchone()[0]
    if count > 0:
        import time
        slug = f"{slug}-{int(time.time())}"

    insert_query = """
        INSERT INTO blog_posts (title, slug, summary, content, image_url, status, lang) 
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """
    
    cursor.execute(insert_query, (title, slug, summary, content, image_url, status, lang))
    post_id = cursor.lastrowid
    conn.commit()
    
    cursor.close()
    conn.close()
    return post_id

# ----------------------------------------------------------------------
# 6. Main CLI Driver
# ----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Teknoskor Automated E-E-A-T Blog Generator")
    parser.add_argument("--topic", type=str, choices=list(TOPIC_ARCHETYPES.keys()) + ["random"], default="random",
                        help="The article archetype. 'random' rotates, skipping angles covered by recent post titles.")
    parser.add_argument("--status", type=str, choices=["draft", "published"], default="draft",
                        help="Status of the saved blog post in the database.")
    parser.add_argument("--save", action="store_true",
                        help="If set, save the generated blog post directly to the MySQL database.")
    
    args = parser.parse_args()
    
    recent_titles = fetch_recent_post_titles()

    topic = args.topic
    if topic == "random":
        topic = pick_topic(recent_titles)
        print(f"[*] Rotation picked archetype: '{topic}' (avoiding {len(recent_titles)} recent titles)")

    print(f"[*] Fetching comparison data for archetype: '{topic}'...")
    extra_note = ""
    if topic in ("performance", "camera", "battery"):
        data = fetch_phone_data(topic)
    else:
        data, extra_note = fetch_topic_data(topic)
    if not data:
        print("[!] No phone records found for this archetype. Aborting.")
        return

    # Generate → quality gate → (on failure) one retry with the failure report
    # fed back as an instruction → still failing forces DRAFT status, so a bad
    # article never auto-publishes (the cron workflow publishes by default).
    from blog_quality_check import run_quality_gate

    status = args.status
    content_with_links, title, summary = "", "", ""
    for attempt in (1, 2):
        note = extra_note
        if attempt == 2:
            note = (extra_note + "\n\nÖNCEKİ DENEME ŞU SEBEPLERLE REDDEDİLDİ — hepsini düzelterek YENİDEN yaz:\n" + report).strip()
            print("[!] Quality gate failed. Regenerating once with feedback...")

        print(f"[*] Compiling payload and calling LLM model API (attempt {attempt}/2)...")
        raw_response = generate_article_with_llm(data, topic, extra_note=note, recent_titles=recent_titles)
        title, summary, content = parse_article_response(raw_response, topic)

        print(f"[*] Converting Markdown to HTML...")
        html_content = markdown_to_html(content)
        print(f"[*] Injecting internal links into the article content...")
        content_with_links = inject_internal_links(html_content, lang="tr")

        print(f"[*] Running quality gate...")
        passed, report = run_quality_gate(title, summary, content_with_links, data, recent_titles)
        print(report + "\n")
        if passed:
            break
    else:
        if status == "published":
            status = "draft"
            print("[!] Both attempts failed the quality gate — saving as DRAFT for human review instead of publishing.")

    cover_image = None
    if data and len(data) > 0:
        first_phone = data[0]
        phone_images = first_phone.get("images")
        if phone_images:
            if isinstance(phone_images, str):
                try:
                    imgs = json.loads(phone_images)
                    if imgs and len(imgs) > 0:
                        cover_image = imgs[0]
                except:
                    pass
            elif isinstance(phone_images, list) and len(phone_images) > 0:
                cover_image = phone_images[0]

    slug = turkish_slugify(title)

    print("\n=======================================================")
    print(f"TITLE: {title}")
    print(f"SLUG: {slug}")
    print(f"SUMMARY: {summary}")
    print(f"COVER IMAGE: {cover_image}")
    print(f"STATUS: {status}")
    print(f"CONTENT LENGTH: {len(content_with_links)} chars")
    print("=======================================================\n")

    if args.save:
        print("[*] Saving article to database...")
        post_id = save_blog_post(title, slug, summary, content_with_links, cover_image, status, "tr")
        print(f"[+] Successfully saved! Post ID in database: {post_id}")
    else:
        print("[*] Dry run finished. Use '--save' to insert into MySQL.")


def parse_article_response(raw_response, topic):
    # Parse LLM response tags using robust normalization
    normalized_response = raw_response
    normalized_response = re.sub(r'\*\*\[?(TITLE|SUMMARY|CONTENT)\]?[:\*]*', r'[\1]', normalized_response, flags=re.IGNORECASE)
    normalized_response = re.sub(r'\[?(TITLE|SUMMARY|CONTENT)\]?:', r'[\1]', normalized_response, flags=re.IGNORECASE)
    
    title = ""
    summary = ""
    content = ""
    
    if "[TITLE]" in normalized_response:
        parts = normalized_response.split("[TITLE]")
        if len(parts) > 1:
            title = parts[1].split("[SUMMARY]")[0].split("[CONTENT]")[0].strip()
    if "[SUMMARY]" in normalized_response:
        parts = normalized_response.split("[SUMMARY]")
        if len(parts) > 1:
            summary = parts[1].split("[CONTENT]")[0].strip()
    if "[CONTENT]" in normalized_response:
        parts = normalized_response.split("[CONTENT]")
        if len(parts) > 1:
            content = parts[1].strip()

    # Fallback to smart bracket parser in case the LLM wrapped sections directly
    if not title or not content:
        brackets = re.findall(r'\[([^\]]+)\]', raw_response)
        if len(brackets) >= 2:
            title = brackets[0]
            summary = brackets[1]
            temp_content = raw_response
            temp_content = temp_content.replace(f"[{title}]", "", 1)
            temp_content = temp_content.replace(f"[{summary}]", "", 1)
            temp_content = re.sub(r'(?i)\[?CONTENT\]?[:\*]*', '', temp_content)
            content = temp_content.strip()

    # Clean formatting marks from parsed fields
    if title:
        title = title.strip('[]"\'* \t\n\r')
    if summary:
        summary = summary.strip('[]"\'* \t\n\r')

    # Ultimate fallback in case parsing still fails
    if not title or not content:
        print("[!] LLM output did not contain correct parsing tags. Saving raw text as fallback content.")
        title = f"{topic.capitalize()} Odaklı Akıllı Telefon İncelemesi (2026)"
        summary = "Otomatik üretilmiş teknik özellikler karşılaştırma rehberi."
        content = raw_response

    return title, summary, content


if __name__ == "__main__":
    main()
