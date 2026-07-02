#!/usr/bin/env python3
import os
import re
import json
import decimal
import argparse
import requests
import mysql.connector
from dotenv import load_dotenv

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
    
    # Parse attributes and images if they are returned as string/JSON
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

    rows = convert_decimals(rows)
                
    cursor.close()
    conn.close()
    return rows

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
def inject_internal_links(content: str) -> str:
    keyword_map = {
        "pubg performansı": "https://teknoskor.com/pubg-performansi-en-yuksek-telefonlar",
        "oyun telefonu": "https://teknoskor.com/pubg-performansi-en-yuksek-telefonlar",
        "kameralı telefon": "https://teknoskor.com/en-iyi-kamerali-telefonlar",
        "kamera performansı": "https://teknoskor.com/en-iyi-kamerali-telefonlar",
        "şarjı en uzun": "https://teknoskor.com/sarji-en-uzun-giden-telefonlar",
        "şarjı uzun": "https://teknoskor.com/sarji-en-uzun-giden-telefonlar",
        "pil ömrü": "https://teknoskor.com/sarji-en-uzun-giden-telefonlar"
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
def generate_article_with_llm(topic_data, topic_type="performance"):
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY or GOOGLE_API_KEY environment variable is missing.")

    system_prompt = """You are a senior tech editor and hardware reviewer for Teknoskor. Write an extremely detailed, high-depth review article (Minimum 1500 to 2000 words, at least 8 to 12 extensive paragraphs) in Turkish comparing the provided phone data. You must cover every phone in the dataset in its own dedicated section, detail its score, camera performance, gaming/Antutu stats, and battery capacity based on the parameters. Do not write short summaries or cut the text short; expand fully on user real-world value.

ANTI-SPAM & CRITICAL STYLE RULES:
1. BAN ALL AI CLICHÉS: Never use words like 'devrimsel', 'şık tasarım', 'büyüleyici deneyim', 'sonuç olarak', 'göz kamaştırıcı', 'özetlemek gerekirse', 'unutmamak gerekir ki', 'derinlemesine dalış'. If you use these, the post fails.
2. NO THIN CONTENT / NO REPEATING SPECS: Do not just list RAM and CPU numbers. Explain WHAT those numbers mean for the user in real life. (e.g., Instead of "It has 8GB RAM", write "With 8GB of RAM, this phone prevents background apps like Discord or Spotify from crashing while you are in a heavy PUBG gunfight").
3. CRITICAL HUMAN TONE: The tone must be blunt, objective, analytical, and data-driven. Highlight the WEAKNESSES of the phones as much as their strengths. True authority comes from honesty.
4. HIGH UX / MOBILE FORMATTING: 
   - Keep paragraphs short (maximum 2-3 sentences per paragraph) for extreme mobile readability.
   - Use clean Markdown subheadings (H2, H3).
   - Use bold text for critical data comparisons.
   - Use clean Markdown bullet points to break down complex technical trade-offs.

GOOGLE HELPFUL CONTENT & SEO REQUIREMENTS:
1. TABLE OF CONTENTS (İÇİNDEKİLER): Include a structured "İçindekiler" index at the very beginning of the post (immediately following the intro hook paragraph). Write it as a clean Markdown list of links linking to H2 headings. E.g.:
   - [Giriş](#giris)
   - [Donanım ve AnTuTu Analizi](#donanim-ve-antutu-analizi)
   ...
   You MUST write all corresponding H2 headings using the HTML syntax with matching IDs so clicking on the TOC link scrolls down correctly. E.g.:
   <h2 id="donanim-ve-antutu-analizi">Donanım ve AnTuTu Analizi</h2>
2. DEVICE IMAGES: In the sections of the article where you discuss/compare a specific phone model, embed its image using the HTML img tag exactly as shown below:
   <img class="my-6 rounded-3xl max-h-[350px] object-contain mx-auto shadow-sm" src="IMAGE_URL" alt="Phone Name" />
   Use the exact `image_url` provided in the database records. If a phone has no image_url, do not embed an image for it. Do not invent fake URLs.

REQUIRED STRUCTURAL BLUEPRINT:
You must structure your response EXACTLY like the following example. Do not use three dots or write a short summary placeholder in the CONTENT tag. Write a full, highly detailed 1500+ words article inside the [CONTENT] tag:

[TITLE]
2026'nın En İyi Kameralı Telefonları: Megapiksel Yalanı ve Gerçekler

[SUMMARY]
Bu makalede 2026 model amiral gemisi telefonların kamera sensör boyutlarını ve gerçek dünya fotoğraf performanslarını Teknoskor verileriyle analiz ediyoruz.

[CONTENT]
[Buraya en az 8-12 geniş paragraftan oluşan, her bir telefon modelini kendi alt başlığında detaylıca kıyaslayan, cihaz görsellerini img etiketleriyle barındıran ve tablo dizilimlerini içeren minimum 1500 kelimelik eksiksiz ve uzun makale içeriği gelecektir. Metni kesinlikle kısa kesmeyin veya yarım bırakmayın.]
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

    prompt = f"{system_prompt}\n\nTOPIC TYPE: {topic_type}\n\nDATA:\n{json.dumps(formatted_data, indent=2, ensure_ascii=False)}"
    
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}]
            }
        ],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 8192
        }
    }

    import time
    models = ["gemini-2.5-flash", "gemini-2.0-flash"]
    response = None
    last_error_msg = ""
    full_text = ""

    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        retry_delay = 5
        success = False
        
        for attempt in range(3):
            try:
                print(f"[*] Requesting generation from {model} (Attempt {attempt+1}/3)...")
                response = requests.post(url, headers=headers, json=payload, timeout=60)
                if response.status_code == 200:
                    res_json = response.json()
                    candidate = res_json["candidates"][0]
                    text_part = candidate["content"]["parts"][0]["text"]
                    finish_reason = candidate.get("finishReason", "STOP")
                    
                    full_text = text_part
                    
                    # Continue generation loop if response was truncated (MAX_TOKENS)
                    contents = list(payload["contents"])
                    continue_count = 0
                    
                    while finish_reason == "MAX_TOKENS" and continue_count < 3:
                        continue_count += 1
                        print(f"[!] Generation truncated (MAX_TOKENS). Continuing text generation (Attempt {continue_count}/3)...")
                        
                        contents.append({
                            "role": "model",
                            "parts": [{"text": text_part}]
                        })
                        contents.append({
                            "role": "user",
                            "parts": [{"text": "Yazın yarım kaldı. En son kaldığın cümleden/kelimeden itibaren makaleyi yazmaya devam et. Tamamen bitirene kadar devam et."}]
                        })
                        
                        continue_payload = {
                            "contents": contents,
                            "generationConfig": {
                                "temperature": 0.7,
                                "maxOutputTokens": 8192
                            }
                        }
                        
                        cont_response = requests.post(url, headers=headers, json=continue_payload, timeout=60)
                        if cont_response.status_code != 200:
                            print(f"[!] Continuation request failed with status {cont_response.status_code}: {cont_response.text}")
                            break
                            
                        cont_json = cont_response.json()
                        cont_candidate = cont_json["candidates"][0]
                        cont_text_part = cont_candidate["content"]["parts"][0]["text"]
                        
                        full_text += " " + cont_text_part.strip()
                        text_part = cont_text_part
                        finish_reason = cont_candidate.get("finishReason", "STOP")
                        
                    success = True
                    break
                elif response.status_code in [429, 503] and attempt < 2:
                    print(f"[!] API returned {response.status_code}. Retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    last_error_msg = f"{model} failed: {response.status_code} - {response.text}"
                    break
            except Exception as e:
                last_error_msg = f"{model} exception: {str(e)}"
                break
                
        if success:
            break

    if not success:
        raise Exception(f"All Gemini model invocations failed. Last error details: {last_error_msg}")

    return full_text

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
    parser.add_argument("--topic", type=str, choices=["performance", "camera", "battery", "random"], default="random",
                        help="The data source metric to focus the blog content on.")
    parser.add_argument("--status", type=str, choices=["draft", "published"], default="draft",
                        help="Status of the saved blog post in the database.")
    parser.add_argument("--save", action="store_true",
                        help="If set, save the generated blog post directly to the MySQL database.")
    
    args = parser.parse_args()
    
    topic = args.topic
    if topic == "random":
        import random
        topic = random.choice(["performance", "camera", "battery"])
        
    print(f"[*] Fetching comparison data for focus metric: '{topic}'...")
    data = fetch_phone_data(topic)
    if not data:
        print("[!] No phone records found in the database. Aborting.")
        return
        
    print(f"[*] Compiling payload and calling LLM model API...")
    raw_response = generate_article_with_llm(data, topic)
    
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

    print(f"[*] Converting Markdown to HTML...")
    html_content = markdown_to_html(content)

    print(f"[*] Injecting internal links into the article content...")
    content_with_links = inject_internal_links(html_content)
    
    slug = turkish_slugify(title)
    
    print("\n=======================================================")
    print(f"TITLE: {title}")
    print(f"SLUG: {slug}")
    print(f"SUMMARY: {summary}")
    print(f"COVER IMAGE: {cover_image}")
    print(f"CONTENT LENGTH: {len(content_with_links)} chars")
    print("=======================================================\n")
    
    if args.save:
        print("[*] Saving article to database...")
        post_id = save_blog_post(title, slug, summary, content_with_links, cover_image, args.status, "tr")
        print(f"[+] Successfully saved! Post ID in database: {post_id}")
    else:
        print("[*] Dry run finished. Use '--save' to insert into MySQL.")

if __name__ == "__main__":
    main()
