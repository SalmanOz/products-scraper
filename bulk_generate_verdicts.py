import os
import sys
import json
import logging
import argparse
import re
from dotenv import load_dotenv
import mysql.connector
from google import genai
from google.genai import types
from pydantic import BaseModel

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

# Global list to keep track of models that are rate-limited or have 0 quota limits
DISABLED_MODELS = set()

# Define structured output schema for Gemini using Pydantic
class AIAnalysis(BaseModel):
    verdict: str
    pros: list[str]
    cons: list[str]

def generate_ai_analysis(product_data, api_key):
    global DISABLED_MODELS
    client = genai.Client(api_key=api_key)
    
    system_instruction = (
        "You are an objective, realistic, and highly experienced mobile technology editor writing for Teknoskor.com. "
        "Your task is to analyze the provided phone specifications and generate an honest, slightly critical, and engaging review along with short, punchy pros/cons.\n\n"
        "CRITICAL WRITING RULES:\n"
        "1. Output language MUST be Turkish.\n"
        "2. Absolutely ban AI clichés and hype words in Turkish. Do NOT use: "
        "'adeta', 'muazzam', 'harika', 'şüphesiz', 'canavar', 'ezber bozan', 'yeniden tanımlıyor', 'kusursuz', 'çığır açan', 'göz dolduruyor', 'olağanüstü', 'şık tasarımıyla', 'dikkat çekiyor'.\n"
        "3. Do NOT use exclamation marks (!). Use periods (.) to maintain a neutral, professional editorial tone.\n"
        "4. Vary sentence lengths. Mix short, punchy sentences with compound ones.\n"
        "5. Keep common tech terms in their original English form or standard local tech jargon (do NOT translate them un-naturally):\n"
        "   - Use: 'throttling' or 'thermal throttling'\n"
        "   - Use: 'multitasking'\n"
        "   - Use: 'bloatware'\n"
        "   - Use: 'always-on display'\n"
        "   - Use: 'refresh rate' or 'Hz' (e.g., '120Hz ekran' or '120Hz refresh rate')\n"
        "   - Use: 'peak parlaklık' or 'nits' (e.g., '2000 nits peak parlaklık')\n"
        "   - Use: 'OIS', 'dynamic range', 'chipset', 'benchmark', 'premium'\n"
        "6. Do NOT hallucinate or invent specifications not present in the provided tech sheet. Only comment on numbers provided.\n"
        "7. Be objective and call out weaknesses (e.g., plastic frame, slow charging, outdated chipset). Analyze the target audience and value-for-money."
    )
    
    prompt = (
        f"Analyze the following phone specifications and generate a review:\n\n"
        f"Phone Name: {product_data['name']}\n"
        f"Brand: {product_data['brand']}\n"
        f"Teknoskor Score: {product_data['score']}/100\n"
        f"Specs: {json.dumps(product_data['specs'], ensure_ascii=False, indent=2)}\n\n"
        f"Please return the verdict, pros, and cons in Turkish as a structured JSON object."
    )
    
    models_to_try = [m for m in ["gemini-3.1-pro-preview", "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"] if m not in DISABLED_MODELS]
    last_exception = None
    
    for model_name in models_to_try:
        retries = 2
        for attempt in range(retries + 1):
            try:
                logging.info(f"🤖 Attempting with model: {model_name}...")
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        response_mime_type="application/json",
                        response_schema=AIAnalysis,
                        temperature=0.7
                    )
                )
                return json.loads(response.text)
            except Exception as e:
                err_msg = str(e)
                # If it's a transient server error (503 or 500) and we have retries left, wait and retry the same model
                if ("503" in err_msg or "500" in err_msg or "unavailable" in err_msg.lower()) and attempt < retries:
                    logging.warning(f"⚠️ Model {model_name} returned transient error: {e}. Retrying same model (attempt {attempt + 1}/{retries})...")
                    import time
                    time.sleep(3)
                    continue
                
                last_exception = e
                logging.warning(f"⚠️ Model {model_name} failed or rate-limited: {e}")
                
                # Only disable if the model has a limit of 0 (not provisioned under free tier)
                if "limit: 0" in err_msg:
                    logging.info(f"🚫 Disabling model {model_name} for the remainder of this run (not provisioned).")
                    DISABLED_MODELS.add(model_name)
                    
                # Wait 2 seconds before fallback retry
                import time
                time.sleep(2)
                break
            
    if last_exception is None:
        raise Exception("All Gemini models are currently disabled or unavailable.")
    raise last_exception

def clean_hallucinations(analysis_json, product_specs):
    # Algorithmic guardrail to clean any forbidden words or uydurma details
    verdict = analysis_json.get('verdict', '')
    
    # Strip exclamation marks
    verdict = verdict.replace('!', '.')
    
    # Replace forbidden words. Keep this in sync with the banned-word list in
    # system_instruction above — the system prompt lists 13 words/phrases but this
    # algorithmic guardrail previously only covered 8 of them, so if the model
    # ignored the instruction the remaining 5 shipped straight into stored "honest"
    # analysis unfiltered (the exact hype language this pipeline exists to avoid).
    forbidden = {
        r'\badeta\b': '',
        r'\bmuazzam\b': 'oldukça başarılı',
        r'\bharika\b': 'iyi',
        r'\bşüphesiz\b': 'belirgin şekilde',
        r'\bcanavar\b': 'yüksek performanslı',
        r'\bkusursuz\b': 'başarılı',
        r'\bçığır açan\b': 'yenilikçi',
        r'\bgöz dolduruyor\b': 'iyi performans gösteriyor',
        r'\bezber bozan\b': 'farklı bir yaklaşım sunan',
        r'\byeniden tanımlıyor\b': 'geliştiriyor',
        r'\bolağanüstü\b': 'iyi',
        r'\bşık tasarımıyla\b': 'tasarımıyla',
        r'\bdikkat çekiyor\b': 'öne çıkıyor',
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
        
    logging.info("🔄 Fetching products from database...")
    try:
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
        cursor.close()
        conn.close()
    except Exception as db_err:
        logging.error(f"❌ Failed to fetch products from database: {db_err}")
        sys.exit(1)
    
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
            
            # Save back to database (Open new connection to prevent timeout)
            attrs['ai_analysis'] = analysis
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE products SET attributes = %s WHERE id = %s",
                (json.dumps(attrs, ensure_ascii=False), p['id'])
            )
            conn.commit()
            cursor.close()
            conn.close()
            
            success_count += 1
            logging.info(f"✅ Saved AI Verdict for: {p['name']}")
            # Wait 4 seconds to comply with Free Tier rate limits (15 RPM)
            import time
            time.sleep(4)
        except Exception as e:
            logging.error(f"❌ Failed to generate or save verdict for {p['name']}: {e}")
            import time
            # If it's a rate limit on the active models, sleep for 30 seconds to let the rate limit decay
            if "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e):
                logging.info("⏳ Rate limit reached. Sleeping for 30 seconds to recover...")
                time.sleep(30)
            else:
                time.sleep(10)
            
    logging.info(f"🏁 Finished migration. Successfully updated {success_count} products.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bulk generate expert verdicts using Gemini API.")
    parser.add_argument("--limit", type=int, default=None, help="Limit the number of products to process.")
    args = parser.parse_args()
    
    run_migration(limit=args.limit)
