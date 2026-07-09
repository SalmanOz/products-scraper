import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

import os
import re
import time
import json
import asyncio
import mysql.connector
import random
import requests
import nest_asyncio
import boto3
from botocore.client import Config
from dotenv import load_dotenv
from spec_mapper import map_specs_to_turkish
from tr_price_scraper import TRPriceScraper
from gsmarena_scraper import GSMArenaScraper
from bs4 import BeautifulSoup
from PIL import Image
from io import BytesIO
import html as html_lib

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))
nest_asyncio.apply()

class KimovilScraper:
    def __init__(self):
        self.db = None
        self.cursor = None
        self.ensure_connection()
        self.update_prices = False # Default to False: follow the new decoupled architecture
        self.base_url = os.getenv("KIMOVIL_BASE_URL", "https://www.kimovil.com/en/")
        self.flaresolverr_url = "http://localhost:8191/v1"
        self.r2_enabled = bool(os.getenv("R2_ACCESS_KEY_ID")) and bool(os.getenv("R2_ACCOUNT_ID"))
        if self.r2_enabled:
            logging.info("☁️ R2 enabled.")
            self.s3 = boto3.client('s3', endpoint_url=f"https://{os.getenv('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com", aws_access_key_id=os.getenv("R2_ACCESS_KEY_ID"), aws_secret_access_key=os.getenv("R2_SECRET_ACCESS_KEY"), config=Config(signature_version='s3v4'), region_name='auto')
            self.bucket_name = os.getenv("R2_BUCKET_NAME")
            pub_dom = os.getenv("R2_PUBLIC_DOMAIN", "").rstrip('/')
            if pub_dom and not pub_dom.startswith(('http://', 'https://')):
                pub_dom = 'https://' + pub_dom
            self.public_domain = pub_dom

    def ensure_connection(self, max_retries=5):
        try:
            if self.db and self.db.is_connected():
                self.db.ping(reconnect=True, attempts=3, delay=2)
                self.cursor = self.db.cursor(dictionary=True, buffered=True)
                return
        except Exception:
            pass

        logging.info("  🔄 Connecting/Reconnecting to MySQL database...")
        try:
            if self.db:
                self.db.close()
        except Exception:
            pass

        for attempt in range(1, max_retries + 1):
            try:
                self.db = mysql.connector.connect(
                    host=os.getenv("DB_HOST"),
                    user=os.getenv("DB_USER"),
                    password=os.getenv("DB_PASSWORD"),
                    database=os.getenv("DB_NAME"),
                    port=int(os.getenv("DB_PORT", 3306)),
                    buffered=True,
                    connection_timeout=30
                )
                self.cursor = self.db.cursor(dictionary=True, buffered=True)
                logging.info(f"  ✅ DB connected (attempt {attempt}/{max_retries})")
                return
            except Exception as e:
                logging.warning(f"  ⚠️ DB connection attempt {attempt}/{max_retries} failed: {e}")
                if attempt < max_retries:
                    wait = 10 * attempt
                    logging.info(f"  ⏳ Retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    logging.error(f"  ❌ All {max_retries} DB connection attempts failed.")
                    raise

    def upload_image_to_r2(self, source_url, destination_path):
        if not self.r2_enabled: 
            logging.warning("⚠️ R2 is not enabled, skipping image upload.")
            return source_url
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            img_res = requests.get(source_url, headers=headers, timeout=15)
            if img_res.status_code != 200: 
                logging.error(f"❌ Failed to download source image {source_url}, status code: {img_res.status_code}")
                return source_url
            img = Image.open(BytesIO(img_res.content))
            if img.mode in ("RGBA", "P"): img = img.convert("RGBA")
            else: img = img.convert("RGB")
            buffer = BytesIO()
            img.save(buffer, format="WEBP", quality=80, optimize=True)
            buffer.seek(0)
            webp_path = os.path.splitext(destination_path)[0] + ".webp"
            self.s3.put_object(Bucket=self.bucket_name, Key=webp_path, Body=buffer.getvalue(), ContentType='image/webp')
            final_url = f"{self.public_domain}/{webp_path}"
            logging.info(f"☁️ Successfully uploaded image to R2: {final_url}")
            return final_url
        except Exception as e: 
            logging.error(f"❌ R2 Upload Exception for {source_url} to {destination_path}: {str(e)}")
            return source_url

    def execute_with_retry(self, query, params=None, retries=3):
        self.ensure_connection()
        for i in range(retries):
            try:
                self.cursor.execute(query, params)
                self.db.commit()
                return True
            except mysql.connector.errors.InternalError as e:
                if e.errno in [1213, 1205]: # Deadlock or Lock Wait Timeout
                    logging.warning(f"⚠️ DB Lock detected ({e.errno}), retrying ({i+1}/{retries})...")
                    time.sleep(2)
                    self.ensure_connection()
                    continue
                raise e
            except Exception as e:
                logging.error(f"❌ DB Error: {str(e)}")
                try:
                    self.db.rollback()
                except Exception:
                    pass
                return False
        return False

    def get_via_flaresolverr(self, url):
        logging.info(f"🚀 FlareSolverr: {url}")
        try:
            payload = {"cmd": "request.get", "url": url, "maxTimeout": 120000}
            response = requests.post(self.flaresolverr_url, json=payload, timeout=150)
            res_data = response.json()
            if res_data.get('status') == 'ok': return res_data['solution']['response']
            return None
        except: return None

    def extract_number(self, text):
        if not text: return 0
        t = str(text).replace('\t', '').replace('\n', '').strip()
        # Remove any text in parentheses first (like (v10))
        t = re.sub(r'\(.*?\)', '', t).strip()
        # Also remove common version patterns like v10, v9
        t = re.sub(r'v\d+', '', t, flags=re.IGNORECASE).strip()
        
        is_antutu = 'antutu' in t.lower()
        
        # Find all numeric sequences (handling dots and commas)
        # This will find things like "816.345" or "1,234,567"
        nums = re.findall(r'[\d\.,]+', t)
        if not nums: return 0
        
        # Take only the FIRST numeric sequence found
        raw_num = nums[0].strip('.,')
        
        # Remove separators
        clean_num = raw_num.replace('.', '').replace(',', '')
        
        if not clean_num: return 0
        
        try:
            val = int(float(clean_num))
            # Safeguard: If it's an Antutu score and it's suspiciously small (e.g. 816 instead of 816000)
            if is_antutu and 0 < val < 2000:
                logging.warning(f"⚠️ Suspiciously low Antutu score ({val}), multiplying by 1000")
                val *= 1000
            # Sanity check: Antutu scores shouldn't exceed 4 million currently
            if is_antutu and val > 4000000:
                logging.warning(f"⚠️ Suspiciously high Antutu score ({val}), taking only the first part")
                # Probably concatenated with something else, take only first 6-7 digits
                val = int(str(val)[:7]) if val > 9999999 else val
            return val
        except:
            return 0

    def extract_decimal(self, text):
        """Like extract_number but keeps the decimal point — for values such as
        screen size where "6.67" must not collapse to 667."""
        if not text: return 0.0
        t = str(text).replace('\t', '').replace('\n', '').strip()
        t = re.sub(r'\(.*?\)', '', t).strip()
        m = re.search(r'\d+(?:[\.,]\d+)?', t)
        if not m: return 0.0
        try:
            return float(m.group(0).replace(',', '.'))
        except:
            return 0.0

    def scrape_product_details(self, url, category_id=1):
        try:
            html = self.get_via_flaresolverr(url)
            if not html:
                logging.error(f"❌ Error: FlareSolverr returned no HTML for {url}")
                return False
            if "Just a moment" in html:
                logging.error(f"❌ Error: Cloudflare blocked the request (Turnstile/Just a moment) for {url}")
                return False
            soup = BeautifulSoup(html, 'html.parser')
            device_ki = {}; device_compare = {}
            ki_meta = soup.find('meta', {'name': 'deviceki'})
            device_ki = {}; device_compare = {}
            ki_meta = soup.find('meta', {'name': 'deviceki'})
            if ki_meta and ki_meta.get('content'): 
                try: device_ki = json.loads(html_lib.unescape(ki_meta['content']))
                except: pass
            
            comp_meta = soup.find('meta', {'name': 'devicecompare'})
            if comp_meta and comp_meta.get('content'): 
                try: device_compare = json.loads(html_lib.unescape(comp_meta['content']))
                except: pass
            
            full_name = device_ki.get('name') or device_compare.get('name') or (soup.find('h1').get_text().strip() if soup.find('h1') else "Unknown Device")
            
            if "Compare smartphones" in full_name or "Unknown Device" == full_name or "Page not found" in full_name or not (device_ki or device_compare):
                logging.warning(f"⚠️ Skipping: {full_name} (Invalid or general comparison page)")
                return False

            brand_name = device_compare.get('brand_name') or (full_name.split(' ')[0] if ' ' in full_name else full_name)
            logging.info(f"📦 Processing: {full_name}")

            raw_specs = {}; all_key_values = {}
            for section in soup.find_all('section', class_=re.compile(r'container-sheet-')):
                header = section.find(['h2', 'h3', 'h4'])
                if not header: continue
                title = header.get_text().strip()
                if " of " in title: title = title.split(' of ')[0].strip()
                raw_specs[title] = {}
                for table in section.find_all('table', class_='k-dltable'):
                    for tr in table.find_all('tr'):
                        th = tr.find(['th', 'td'], class_='label') or tr.find('th')
                        td_all = tr.find_all('td')
                        td = tr.find(['td'], class_='value') or (td_all[-1] if td_all else None)
                        if th and td:
                            key = th.get_text().strip()
                            val = td.get_text().replace('\n', ' ').replace('See more details', '').strip()
                            raw_specs[title][key] = val
                            all_key_values[key] = val

            partials = device_ki.get('partials', {})
            ki_score = device_ki.get('ki', 0)
            def randomize_score(score):
                if not score or not isinstance(score, (int, float)): return score
                return max(0.1, min(10.0, round(float(score) + random.uniform(-0.2, 0.2), 1)))
            if partials: partials = {k: randomize_score(v) for k, v in partials.items()}
            ki_score = randomize_score(ki_score)

            def get_spec(s, k, f=None):
                for sect, specs in raw_specs.items():
                    if s.lower() in sect.lower():
                        for sk, sv in specs.items():
                            if k.lower() in sk.lower(): return sv
                            if f and f.lower() in sk.lower(): return sv
                return all_key_values.get(k, all_key_values.get(f, '---'))

            ram_v = get_spec('Hardware', 'RAM'); storage_v = get_spec('Hardware', 'Capacity', 'Storage')
            cpu_v = get_spec('Hardware', 'Processor', 'Model'); screen_v = get_spec('Screen', 'Diagonal', 'Size')
            battery_v = get_spec('Battery', 'Capacity'); antutu_v = get_spec('Hardware', 'Score')
            nm_v = self.extract_number(get_spec('Hardware', 'Nanometers', 'nm'))

            attributes = {
                **map_specs_to_turkish(raw_specs),
                "quick_specs": {"ram": ram_v, "storage": storage_v, "cpu": cpu_v, "screen": screen_v, "battery": battery_v, "camera_main": all_key_values.get('Main', '---')},
                "ram_gb": int(self.extract_number(ram_v)), "storage_gb": int(self.extract_number(storage_v)),
                "battery_mah": int(self.extract_number(battery_v)), "screen_size_inch": self.extract_decimal(screen_v),
                "kiscore": float(ki_score), "antutu_score": int(self.extract_number(antutu_v)),
                "camera_score": partials.get('camera', 0), "performance_score": partials.get('hardware', 0),
                "battery_score": partials.get('battery', 0), "screen_score": partials.get('design', 0), "partials": partials
            }

            def calc_gaming(antutu, bat, nm):
                games = {"PUBG Mobile": {"i": "🔫", "w": 1.0, "m": 120}, "Genshin Impact": {"i": "✨", "w": 2.2, "m": 60}, "CoD: Warzone": {"i": "🎖️", "w": 1.8, "m": 120}, "EA FC Mobile": {"i": "⚽", "w": 1.2, "m": 120}, "Mobile Legends": {"i": "⚔️", "w": 0.8, "m": 120}, "Roblox": {"i": "🧱", "w": 0.7, "m": 60}}
                res = []; nm = nm if nm > 0 else 6
                for name, c in games.items():
                    fps = c["m"] if antutu >= 1500000 else int(c["m"]*0.85) if antutu >= 1000000 else int(c["m"]*0.6) if antutu >= 700000 else int(c["m"]*0.4) if antutu >= 400000 else int(c["m"]*0.25)
                    play_time = round(bat / (c["w"]*800*(1+(nm-4)*0.1)), 1)
                    res.append({"game": name, "icon": c["i"], "fps": fps, "max_fps": c["m"], "hours": play_time, "tier": "Ultra" if fps >= c["m"]*0.9 else "Yüksek" if fps >= c["m"]*0.7 else "Orta"})
                return res
            attributes["gaming_performance"] = calc_gaming(attributes["antutu_score"], attributes["battery_mah"], nm_v)

            def gen_faq(name, attr, parts):
                # Only generate FAQ claims backed by real data — never fall back to
                # invented defaults (a missing refresh rate is NOT 60Hz)
                pool = []
                antutu = attr.get("antutu_score", 0); bat = attr.get("battery_mah", 0); cam = parts.get("camera", 0)
                hz = attr.get("screen_refresh_rate"); ch = attr.get("charging_speed_w")
                if antutu > 0:
                    antutu_tr = f"{antutu:,}".replace(",", ".")  # Turkish thousands separator
                    pool.append({"q": random.choice([f"{name} oyun performansı nasıl?", f"{name} oyunlarda kasar mı?"]), "a": random.choice([f"{name}, {antutu_tr} AnTuTu skoruyla " + ("tüm oyunları en yüksek ayarlarda akıcı çalıştırır." if antutu > 1200000 else "orta-yüksek ayarlarda dengeli deneyim sunar." if antutu > 700000 else "temel oyunlar için uygundur.")])})
                if bat > 0:
                    pool.append({"q": random.choice([f"{name} bataryası ne kadar gider?", f"{name} şarjı çabuk biter mi?"]), "a": random.choice([f"{bat} mAh kapasitesiyle " + ("normal kullanımda 1.5-2 gün pil ömrü sunar." if bat >= 5000 else "günlük standart kullanımı karşılar.")])})
                if cam > 0:
                    pool.append({"q": random.choice([f"{name} kamerası gece çekimi için iyi mi?", f"{name} fotoğraf kalitesi nasıl?"]), "a": [("Evet, düşük ışıkta profesyonel sonuçlar verir." if cam >= 8.5 else "Gün ışığında başarılı olsa da gece çekimlerinde kumlanma yapabilir.")][0]})
                if hz:
                    pool.append({"q": random.choice([f"{name} ekranı kaç Hz?", f"{name} ekran akıcılığı nasıl?"]), "a": [f"{hz}Hz yenileme hızıyla " + ("ipeksi bir akıcılık sunar." if hz >= 120 else "standart bir akıcılık sunar.")][0]})
                if ch:
                    pool.append({"q": random.choice([f"{name} hızlı şarj oluyor mu?", f"{name} kaç Watt destekliyor?"]), "a": [f"{ch}W desteğiyle " + ("ultra hızlı şarj imkanı sağlar." if ch >= 67 else "makul sürelerde dolum sağlar.")][0]})
                random.shuffle(pool)
                return pool[:5]
            attributes["faq"] = gen_faq(full_name, attributes, partials)

            # --- Image Handling: Kimovil Gallery Only ---
            def to_hi(u):
                if not u: return u
                if u.startswith('//'): u = 'https:' + u
                # Kimovil high-res images usually end with _big.jpg or _large.jpg
                u = re.sub(r'_(x_search|x_small|detail|medium|small|thumb|default)\.', '_big.', u)
                return u
            
            raw_urls = []
            
            # 1. Get Kimovil Main Image from metadata
            main_i = device_compare.get('image')
            forbidden = ['all-colors', 'colors', 'group', 'combo', 'rendering', 'variants', 'social']
            
            if main_i and not any(k in main_i.lower() for k in forbidden): 
                raw_urls.append(to_hi(main_i))
            
            # 2. Get Kimovil Gallery Images
            # We look for all possible gallery images
            gallery_selectors = [
                '.item-gallery img', 
                '.kigallery img', 
                '.device-main-image img',
                '#device-images img',
                '.image-gallery-container img'
            ]
            for selector in gallery_selectors:
                for i_t in soup.select(selector):
                    s = i_t.get('data-src') or i_t.get('src') or i_t.get('data-lazy-src')
                    if s and '/en/' not in s: # Avoid links that are not images
                        f_i = to_hi(s)
                        if f_i not in raw_urls and not any(k in f_i.lower() for k in forbidden): 
                            raw_urls.append(f_i)
            
            # De-duplicate and filter
            processed_urls = []
            for u in raw_urls:
                u_lower = u.lower()
                # Must be an image and not a generic icon/spinner
                if any(ext in u_lower for ext in ['.jpg', '.jpeg', '.png', '.webp']) and \
                   not any(x in u_lower for x in ['spinner', 'loading', 'icon', 'logo', 'avatar', 'pixel']):
                    processed_urls.append(u)
            
            raw_urls = processed_urls
            # Prioritize 'big' or 'large' images
            raw_urls.sort(key=lambda x: 1 if ('_big.' in x.lower() or '_large.' in x.lower()) else 2)
            
            # Finalize images - deduplicate and limit
            seen = set()
            raw_urls = [u for u in raw_urls if not (u in seen or seen.add(u))][:10]
            
            logging.info(f"🖼️ Found {len(raw_urls)} unique images for {full_name}")

            images = []
            slug = device_compare.get('slug') or re.sub(r'[^a-z0-9]+', '-', full_name.lower()).strip('-')
            for idx, i_u in enumerate(raw_urls):
                if self.r2_enabled: 
                    # Use slug in filename for better SEO/R2 organization
                    filename = f"{slug}-{idx}.jpg"
                    images.append(self.upload_image_to_r2(i_u, f"products/{slug}/{filename}"))
                else: 
                    images.append(i_u)
            if not images and main_i: images = [to_hi(main_i)]
            
            if partials:
                v_s = [v for v in partials.values() if isinstance(v, (int, float)) and v > 0]
                if v_s: teknoskor = int(round((sum(v_s)/len(v_s))*10))
                else: teknoskor = int(round(ki_score*10))
            else: teknoskor = int(round(ki_score*10))

            # Generate AI Verdict using Gemini Pro if API key is configured
            gemini_api_key = os.getenv("GEMINI_API_KEY")
            if gemini_api_key:
                try:
                    logging.info(f"✨ Generating AI Verdict for new product: {full_name}")
                    specs = {
                        "antutu_score": attributes.get("antutu_score"),
                        "ram_gb": attributes.get("ram_gb"),
                        "storage_gb": attributes.get("storage_gb"),
                        "battery_mah": attributes.get("battery_mah"),
                        "screen_size_inch": attributes.get("screen_size_inch"),
                        "screen_refresh_rate": attributes.get("screen_refresh_rate"),
                        "charging_speed_w": attributes.get("charging_speed_w"),
                        "camera_score": attributes.get("camera_score"),
                        "gaming_performance": attributes.get("gaming_performance"),
                        "Technical sheet": attributes.get("Technical sheet")
                    }
                    specs = {k: v for k, v in specs.items() if v is not None}
                    product_data = {
                        "name": full_name,
                        "brand": brand_name,
                        "score": teknoskor,
                        "specs": specs
                    }
                    from bulk_generate_verdicts import generate_ai_analysis, clean_hallucinations
                    analysis = generate_ai_analysis(product_data, gemini_api_key)
                    analysis = clean_hallucinations(analysis, attributes)
                    attributes['ai_analysis'] = analysis
                    logging.info(f"✅ Generated AI Verdict successfully.")
                except Exception as ex:
                    logging.error(f"⚠️ Failed to generate AI Verdict for {full_name} during scrape: {ex}")

            # Database operations with retry
            self.execute_with_retry("SELECT id FROM brands WHERE name LIKE %s LIMIT 1", (f"%{brand_name}%",))
            br = self.cursor.fetchone(); b_id = br['id'] if br else 0
            if b_id == 0:
                self.execute_with_retry("INSERT INTO brands (name, slug) VALUES (%s, %s)", (brand_name, brand_name.lower()))
                b_id = self.cursor.lastrowid
            
            self.execute_with_retry("""
                INSERT INTO products (name, slug, brand_id, category_id, base_price, images, attributes, teknoskor_score, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE attributes=VALUES(attributes), images=VALUES(images), teknoskor_score=VALUES(teknoskor_score), status='published', updated_at=CURRENT_TIMESTAMP
            """, (full_name, slug, b_id, category_id, 0, json.dumps(images), json.dumps(attributes), teknoskor, 'published'))
            
            # --- Price separation: prices are now handled by update_prices.py ---
            
            logging.info(f"✅ Success: {full_name}")
            return True
                
            logging.info(f"✅ Success: {full_name}")
            return True
        except Exception as e: logging.error(f"❌ Error: {str(e)}"); return False

    def get_product_id_by_slug(self, slug):
        try:
            self.ensure_connection()
            # 1. Try exact slug match
            self.cursor.execute("SELECT id FROM products WHERE slug = %s", (slug,))
            r = self.cursor.fetchone()
            if r: return r['id']
            
            # 2. Try suffix-agnostic match (stripping/appending -4g or -5g)
            base_slug = re.sub(r'-(?:4g|5g)$', '', slug)
            if base_slug != slug:
                self.cursor.execute("SELECT id FROM products WHERE slug = %s", (base_slug,))
                r = self.cursor.fetchone()
                if r: return r['id']
            else:
                self.cursor.execute("SELECT id FROM products WHERE slug IN (%s, %s)", (f"{slug}-4g", f"{slug}-5g"))
                r = self.cursor.fetchone()
                if r: return r['id']
                
            return None
        except: 
            return None

    def clean_phone_model_name(self, name):
        # 1. Split on the first occurrence of storage or RAM indicator, and discard everything after it.
        # This matches patterns like: 256GB, 256 GB, 128 GB, 8GB, 8 GB, 1TB, 1 TB, 8GB RAM, 8 RAM, etc.
        # We also match the word "ram" (case-insensitive) as a boundary.
        # Example: "Xiaomi Redmi 15 256GB 8GB Ram Siyah" -> Splits at "256GB" -> "Xiaomi Redmi 15"
        parts = re.split(r'\b\d+\s*(?:GB|TB|ram)\b|\b(?:gb|tb|ram)\b', name, flags=re.IGNORECASE)
        name = parts[0]
        
        # 2. Remove common Turkish/English color words and metadata at the end of the remaining string
        colors = ['siyah', 'beyaz', 'gri', 'mavi', 'sarı', 'yeşil', 'pembe', 'gümüş', 'turuncu', 'altın', 'mor', 'kırmızı', 'lacivert', 'kahverengi', 'titanyum', 'kozmik', 'sis', 'ada', 'çayı', 'lavanta', 'çöl']
        for color in colors:
            name = re.sub(rf'\b{color}\b', '', name, flags=re.IGNORECASE)
            
        # 3. Clean up extra whitespaces
        name = re.sub(r'\s+', ' ', name).strip()
        return name


    def search_product_on_kimovil(self, query):
        url = f"https://www.kimovil.com/_json/autocomplete_devicemodels_joined.json?device_type=0&name={requests.utils.quote(query)}"
        html = self.get_via_flaresolverr(url)
        if not html: return None
        
        try:
            if html.strip().startswith("<html"):
                soup = BeautifulSoup(html, 'html.parser')
                json_str = soup.get_text()
            else:
                json_str = html
            data = json.loads(json_str)
            results = data.get('results', [])
            if not results:
                return None

            # Extract important words from query for validation
            query_lower = query.lower().replace('+', 'plus')
            brands = ['apple', 'samsung', 'xiaomi', 'huawei', 'oppo', 'vivo', 'realme', 'poco', 'google', 'oneplus', 'honor', 'redmi']
            common = ['4g', '5g', 'gb', 'ram', 'nfc', 'tb', 'phone', 'smartphone', 'galaxy']
            query_words = re.findall(r'\w+', query_lower)
            important_words = [w for w in query_words if len(w) > 1 and w not in common and w not in brands]
            
            # Variation words that must match both directions
            variations = ['pro', 'max', 'plus', 'ultra', 'lite', 'fe', 'mini', 'se', 'note']
            query_variations = set(w for w in query_words if w in variations)

            for result in results:
                # Skip rumor/unannounced phones
                if result.get('is_rumor'):
                    continue
                result_name = (result.get('full_name') or result.get('alias') or '').lower().replace('+', 'plus')
                result_slug = result.get('url')
                if not result_slug or not result_name:
                    continue

                # Check all important query words exist in result name
                all_found = all(re.search(rf'\b{re.escape(w)}\b', result_name) for w in important_words)
                if not all_found:
                    continue

                # Check variation words match both directions
                result_words = re.findall(r'\w+', result_name)
                result_variations = set(w for w in result_words if w in variations)
                if query_variations != result_variations:
                    continue
                    
                logging.info(f"  ✅ Kimovil match: '{result.get('full_name')}' for query '{query}'")
                return f"https://www.kimovil.com/en/where-to-buy-{result_slug}"

            # No validated match found
            logging.warning(f"  ⚠️ Kimovil autocomplete returned {len(results)} results but none matched '{query}': {[r.get('full_name') for r in results[:3]]}")
            return None
        except Exception as e:
            logging.error(f"❌ Error parsing Kimovil autocomplete API: {e}")
            return None

    def scrape_latest_smartphones(self):
        new_added = 0
        max_new_products = 10
        
        logging.info(f"🚀 Starting daily sync to find and insert exactly {max_new_products} popular products in Turkey...")
        
        # Fetch latest releases directly from Kimovil
        k_page = 1
        while new_added < max_new_products and k_page <= 5:
            k_url = f"{self.base_url}compare-smartphones"
            if k_page > 1:
                k_url = f"{self.base_url}compare-smartphones/page/{k_page}"
                
            logging.info(f"📄 Fetching Kimovil latest releases page {k_page}: {k_url}")
            html = self.get_via_flaresolverr(k_url)
            if not html:
                break
            soup = BeautifulSoup(html, 'html.parser')
            urls = []
            for a in soup.find_all('a', href=re.compile(r'where-to-buy')):
                u = a.get('href')
                if u: urls.append(u if u.startswith('http') else f"https://www.kimovil.com{u}")
            urls = list(dict.fromkeys(urls))

            if not urls:
                break

            for u in urls:
                if new_added >= max_new_products:
                    break
                raw_slug = u.split('/')[-1]
                slug = raw_slug.replace('where-to-buy-', '')
                if self.get_product_id_by_slug(slug):
                    continue

                logging.info(f"✨ Scraping new latest release: {slug}")
                success = self.scrape_product_details(u)
                if success:
                    new_added += 1
                    logging.info(f"📈 Added latest product ({new_added}/{max_new_products}): {slug}")
                    time.sleep(5)
            k_page += 1
                
        logging.info(f"✅ Finished daily sync. Added {new_added} new popular/latest products to the database.")

if __name__ == "__main__":
    try:
        scraper = KimovilScraper()
        scraper.scrape_latest_smartphones()
        if scraper.db.is_connected(): scraper.cursor.close(); scraper.db.close(); logging.info("💤 DB closed.")
    except Exception as e: logging.error(f"❌ FATAL: {str(e)}")
