import os
import sys
import json
import logging
import argparse
import re
from dotenv import load_dotenv
import mysql.connector
import google.generativeai as genai
import typing_extensions as typing

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Load environment variables
load_dotenv()

# Setup database connection
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME", "product_comparison"),
        port=os.getenv("DB_PORT", 3306)
    )

# Define structured output schema for Gemini
class AIAnalysis(typing.TypedDict):
    verdict: str
    pros: list[str]
    cons: list[str]

def generate_ai_analysis(product_data, api_key):
    genai.configure(api_key=api_key)
    
    # Select target model (using the requested gemini-3.1-pro-preview model)
    model = genai.GenerativeModel(
        model_name="gemini-3.1-pro-preview",
        system_instruction=(
            "Sen Teknoskor.com teknoloji platformu için tarafsız, gerçekçi ve deneyimli bir mobil editörsün. "
            "Görevin, sana sunulan teknik özellikleri analiz ederek telefon için samimi, akıcı ve biraz da eleştirel bir 'uzman yorumu' (verdict) ve buna uygun artı/eksi (pros/cons) listesi oluşturmaktır.\n\n"
            "DİL VE YAZIM KURALLARI (ÇOK KRİTİK):\n"
            "1. Asla yapay zeka klişelerini ve abartılı kelimeleri kullanma. Şu kelimeler KESİNLİKLE YASAKTIR: "
            "'adeta', 'muazzam', 'harika', 'şüphesiz', 'canavar', 'ezber bozan', 'yeniden tanımlıyor', 'kusursuz', 'çığır açan', 'göz dolduruyor', 'olağanüstü', 'şık tasarımıyla', 'dikkat çekiyor'.\n"
            "2. Asla ünlem işareti (!) kullanma. Cümle sonları sadece nokta ile bitmelidir.\n"
            "3. Cümle uzunluklarını çeşitlendir. Kısa ve net cümleleri, bileşik ve bağlaçlı cümlelerle harmanla.\n"
            "4. Teknik terimleri Türkçe'ye zorlama, olduğu gibi İngilizce veya sektörel jargon olarak bırak:\n"
            "   - 'throttling' veya 'thermal throttling'\n"
            "   - 'multitasking'\n"
            "   - 'bloatware'\n"
            "   - 'always-on display'\n"
            "   - 'refresh rate' veya 'Hz' (Örn: 120Hz refresh rate)\n"
            "   - 'peak parlaklık' veya 'nits'\n"
            "   - 'OIS', 'dynamic range', 'chipset', 'benchmark', 'premium'\n"
            "5. Teknik tabloda yer almayan hiçbir sayısal özelliği (batarya mAh, şarj hızı W, ekran boyutu inç, kamera MP) uydurma veya ekleme. Sadece verilen sayıları kullan.\n"
            "6. Eleştirel ve nesnel ol. Cihazın zayıf yönlerini (örneğin plastik kasa, yavaş şarj, eski yonga seti) net bir şekilde belirt. Hedef kitleyi ve fiyat/performans dengesini değerlendir."
        )
    )
    
    prompt = (
        f"Aşağıdaki telefon verilerini analiz et ve bir inceleme oluştur:\n\n"
        f"Telefon Adı: {product_data['name']}\n"
        f"Marka: {product_data['brand']}\n"
        f"Teknoskor Puanı: {product_data['score']}/100\n"
        f"Teknik Özellikler: {json.dumps(product_data['specs'], ensure_ascii=False, indent=2)}\n\n"
        f"Lütfen sonucu pros, cons ve verdict alanlarını içeren JSON formatında döndür."
    )
    
    response = model.generate_content(
        prompt,
        generation_config=genai.GenerationConfig(
            response_mime_type="application/json",
            response_schema=AIAnalysis,
            temperature=0.7
        )
    )
    
    return json.loads(response.text)

def clean_hallucinations(analysis_json, product_specs):
    # Algorithmic guardrail to clean any forbidden words or uydurma details
    verdict = analysis_json.get('verdict', '')
    
    # Strip exclamation marks
    verdict = verdict.replace('!', '.')
    
    # Replace forbidden words
    forbidden = {
        r'\badeta\b': '',
        r'\bmuazzam\b': 'oldukça başarılı',
        r'\bharika\b': 'iyi',
        r'\bşüphesiz\b': 'belirgin şekilde',
        r'\bcanavar\b': 'yüksek performanslı',
        r'\bkusursuz\b': 'başarılı',
        r'\bçığır açan\b': 'yenilikçi',
        r'\bgöz dolduruyor\b': 'iyi performans gösteriyor'
    }
    
    for pattern, replacement in forbidden.items():
        verdict = re.sub(pattern, replacement, verdict, flags=re.IGNORECASE)
        
    analysis_json['verdict'] = re.sub(r'\s+', ' ', verdict).strip()
    return analysis_json

def run_migration(limit=None):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logging.error("❌ GEMINI_API_KEY environment variable is missing.")
        sys.exit(1)
        
    logging.info("🔄 Connecting to database...")
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    query = """
        SELECT p.id, p.name, p.slug, p.teknoskor_score, p.attributes, b.name as brand_name 
        FROM products p
        JOIN brands b ON p.brand_id = b.id
        WHERE p.status = 'published'
    """
    cursor.execute(query)
    all_products = cursor.fetchall()
    
    # Filter products missing ai_analysis
    target_products = []
    for p in all_products:
        attrs = json.loads(p['attributes'] or '{}')
        if 'ai_analysis' not in attrs:
            target_products.append(p)
            
    logging.info(f"📊 Found {len(all_products)} total products. {len(target_products)} are missing AI verdicts.")
    
    if limit:
        target_products = target_products[:limit]
        logging.info(f"⚙️ Limiting process to {limit} products.")
        
    success_count = 0
    for p in target_products:
        logging.info(f"✨ Generating AI Verdict for: {p['name']} (ID: {p['id']})")
        attrs = json.loads(p['attributes'] or '{}')
        
        # Prepare subset of specs for Gemini prompt
        specs = {
            "antutu_score": attrs.get("antutu_score"),
            "ram_gb": attrs.get("ram_gb"),
            "storage_gb": attrs.get("storage_gb"),
            "battery_mah": attrs.get("battery_mah"),
            "screen_size_inch": attrs.get("screen_size_inch"),
            "screen_refresh_rate": attrs.get("screen_refresh_rate"),
            "charging_speed_w": attrs.get("charging_speed_w"),
            "camera_score": attrs.get("camera_score"),
            "gaming_performance": attrs.get("gaming_performance"),
            "Technical sheet": attrs.get("Technical sheet")
        }
        # Filter out empty specs
        specs = {k: v for k, v in specs.items() if v is not None}
        
        product_data = {
            "name": p['name'],
            "brand": p['brand_name'],
            "score": p['teknoskor_score'],
            "specs": specs
        }
        
        try:
            analysis = generate_ai_analysis(product_data, api_key)
            analysis = clean_hallucinations(analysis, attrs)
            
            # Save back to database
            attrs['ai_analysis'] = analysis
            cursor.execute(
                "UPDATE products SET attributes = %s WHERE id = %s",
                (json.dumps(attrs, ensure_ascii=False), p['id'])
            )
            conn.commit()
            success_count += 1
            logging.info(f"✅ Saved AI Verdict for: {p['name']}")
        except Exception as e:
            logging.error(f"❌ Failed to generate verdict for {p['name']}: {e}")
            
    cursor.close()
    conn.close()
    logging.info(f"🏁 Finished migration. Successfully updated {success_count} products.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bulk generate expert verdicts using Gemini API.")
    parser.add_argument("--limit", type=int, default=None, help="Limit the number of products to process.")
    args = parser.parse_args()
    
    run_migration(limit=args.limit)
