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
        database=database
    )

def fetch_phone_data(topic_type="performance"):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    if topic_type == "camera":
        query = """
            SELECT p.name, p.slug, p.base_price, b.name as brand_name, 
                   p.teknoskor_score, 
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
                   p.teknoskor_score, 
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
                   p.teknoskor_score, 
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
    
    # Parse attributes if they are returned as string/JSON
    for row in rows:
        if "attributes" in row and isinstance(row["attributes"], str):
            try:
                row["attributes"] = json.loads(row["attributes"])
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
# 2. Suffix-Aware Exact-Match Regex Internal Linking Engine
# ----------------------------------------------------------------------
def inject_internal_links(content: str) -> str:
    keyword_map = {
        "pubg performansı en yüksek telefonlar": "https://teknoskor.com/pubg-performansi-en-yuksek-telefonlar",
        "en iyi kameralı telefonlar": "https://teknoskor.com/en-iyi-kamerali-telefonlar",
        "şarjı en uzun giden telefonlar": "https://teknoskor.com/sarji-en-uzun-giden-telefonlar"
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

    system_prompt = """You are a senior tech editor and hardware reviewer for Teknoskor. Write a deeply comprehensive, long-form blog post (Minimum 1000 words) in Turkish analyzing the provided phone data.

ANTI-SPAM & CRITICAL STYLE RULES:
1. BAN ALL AI CLICHÉS: Never use words like 'devrimsel', 'şık tasarım', 'büyüleyici deneyim', 'sonuç olarak', 'göz kamaştırıcı', 'özetlemek gerekirse', 'unutmamak gerekir ki', 'derinlemesine dalış'. If you use these, the post fails.
2. NO THIN CONTENT / NO REPEATING SPECS: Do not just list RAM and CPU numbers. Explain WHAT those numbers mean for the user in real life. (e.g., Instead of "It has 8GB RAM", write "With 8GB of RAM, this phone prevents background apps like Discord or Spotify from crashing while you are in a heavy PUBG gunfight").
3. CRITICAL HUMAN TONE: The tone must be blunt, objective, analytical, and data-driven. Highlight the WEAKNESSES of the phones as much as their strengths. True authority comes from honesty.
4. HIGH UX / MOBILE FORMATTING: 
   - Keep paragraphs short (maximum 2-3 sentences per paragraph) for extreme mobile readability.
   - Use clean Markdown subheadings (H2, H3).
   - Use bold text for critical data comparisons.
   - Use clean Markdown bullet points to break down complex technical trade-offs.

REQUIRED STRUCTURAL BLUEPRINT:
- You must return your response inside the following tag structure so the script can parse it cleanly:
[TITLE] Put a catchy, click-worthy, non-clickbait Turkish title with the current year (2026) here.
[SUMMARY] Put a 1-2 sentence meta description/summary of the post here.
[CONTENT] Put the article content here. Start with a hook addressing real pain points. Use H2/H3 Markdown subheadings. Make sure to structure it with:
  - Intro: Addressing pain points.
  - H2: Hardware & Performance Score Synthesis.
  - H2: Real-World Use Case Scenarios (Megapixel myths vs. sensor size, frame rates).
  - H2: The Teknoskor Score Breakdown.
  - Verdict: Definitive buying recommendation for different budgets.
"""

    prompt = f"{system_prompt}\n\nTOPIC TYPE: {topic_type}\n\nDATA:\n{json.dumps(topic_data, indent=2, ensure_ascii=False)}"

    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 4096
        }
    }

    import time
    models = ["gemini-2.5-flash", "gemini-1.5-flash"]
    response = None
    last_error_msg = ""

    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        retry_delay = 5
        for attempt in range(3):
            try:
                print(f"[*] Requesting generation from {model} (Attempt {attempt+1}/3)...")
                response = requests.post(url, headers=headers, json=payload)
                if response.status_code == 200:
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
                
        if response and response.status_code == 200:
            break

    if not response or response.status_code != 200:
        raise Exception(f"All Gemini model invocations failed. Last error details: {last_error_msg}")

    res_json = response.json()
    try:
        return res_json["candidates"][0]["content"]["parts"][0]["text"]
    except KeyError:
        raise Exception(f"Invalid Gemini API response structure: {json.dumps(res_json)}")

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
def save_blog_post(title, slug, summary, content, status="draft", lang="tr"):
    conn = get_db_connection()
    cursor = conn.conn.cursor() if hasattr(conn, 'conn') else conn.cursor()
    
    # We will use a placeholder generic thumbnail or leave it NULL
    image_url = None
    
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
    
    # Parse LLM response tags
    title = ""
    summary = ""
    content = ""
    
    if "[TITLE]" in raw_response:
        title = raw_response.split("[TITLE]")[1].split("[SUMMARY]")[0].strip()
    if "[SUMMARY]" in raw_response:
        summary = raw_response.split("[SUMMARY]")[1].split("[CONTENT]")[0].strip()
    if "[CONTENT]" in raw_response:
        content = raw_response.split("[CONTENT]")[1].strip()
        
    if not title or not content:
        # Fallback in case tags are missing/misplaced
        print("[!] LLM output did not contain correct parsing tags. Saving raw text as fallback content.")
        title = f"{args.topic.capitalize()} Odaklı Akıllı Telefon İncelemesi (2026)"
        summary = "Otomatik üretilmiş teknik özellikler karşılaştırma rehberi."
        content = raw_response
        
    print(f"[*] Injecting internal links into the article content...")
    content_with_links = inject_internal_links(content)
    
    slug = turkish_slugify(title)
    
    print("\n=======================================================")
    print(f"TITLE: {title}")
    print(f"SLUG: {slug}")
    print(f"SUMMARY: {summary}")
    print(f"CONTENT LENGTH: {len(content_with_links)} chars")
    print("=======================================================\n")
    
    if args.save:
        print("[*] Saving article to database...")
        post_id = save_blog_post(title, slug, summary, content_with_links, args.status, "tr")
        print(f"[+] Successfully saved! Post ID in database: {post_id}")
    else:
        print("[*] Dry run finished. Use '--save' to insert into MySQL.")

if __name__ == "__main__":
    main()
