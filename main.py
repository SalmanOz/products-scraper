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
            self.public_domain = os.getenv("R2_PUBLIC_DOMAIN", "").rstrip('/')

    def ensure_connection(self):
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

        self.db = mysql.connector.connect(
            host=os.getenv("DB_HOST"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            database=os.getenv("DB_NAME"),
            port=int(os.getenv("DB_PORT", 3306)),
            buffered=True
        )
        self.cursor = self.db.cursor(dictionary=True, buffered=True)

    def upload_image_to_r2(self, source_url, destination_path):
        if not self.r2_enabled: return source_url
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            img_res = requests.get(source_url, headers=headers, timeout=15)
            if img_res.status_code != 200: return source_url
            img = Image.open(BytesIO(img_res.content))
            if img.mode in ("RGBA", "P"): img = img.convert("RGBA")
            else: img = img.convert("RGB")
            buffer = BytesIO()
            img.save(buffer, format="WEBP", quality=80, optimize=True)
            buffer.seek(0)
            webp_path = os.path.splitext(destination_path)[0] + ".webp"
            self.s3.put_object(Bucket=self.bucket_name, Key=webp_path, Body=buffer.getvalue(), ContentType='image/webp')
            return f"{self.public_domain}/{webp_path}"
        except: return source_url

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
            
            if "Page not found" in full_name or "Unknown Device" == full_name:
                logging.warning(f"⚠️ Skipping: {full_name} (Invalid page)")
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
                "battery_mah": int(self.extract_number(battery_v)), "screen_size_inch": float(self.extract_number(screen_v)),
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
                pool = []
                antutu = attr.get("antutu_score", 0); bat = attr.get("battery_mah", 0); cam = parts.get("camera", 0)
                hz = attr.get("screen_refresh_rate", 60); ch = attr.get("charging_speed_w", 18); sc = attr.get("kiscore", 0)
                pool.append({"q": random.choice([f"{name} oyun performansı nasıl?", f"{name} oyunlarda kasar mı?"]), "a": random.choice([f"{name}, {antutu:,} AnTuTu skoruyla " + ("tüm oyunları en yüksek ayarlarda akıcı çalıştırır." if antutu > 1200000 else "orta-yüksek ayarlarda dengeli deneyim sunar." if antutu > 700000 else "temel oyunlar için uygundur.")])})
                pool.append({"q": random.choice([f"{name} bataryası ne kadar gider?", f"{name} şarjı çabuk biter mi?"]), "a": random.choice([f"{bat} mAh kapasitesiyle " + ("normal kullanımda 1.5-2 gün pil ömrü sunar." if bat >= 5000 else "günlük standart kullanımı karşılar.")])})
                pool.append({"q": random.choice([f"{name} kamerası gece çekimi için iyi mi?", f"{name} fotoğraf kalitesi nasıl?"]), "a": [("Evet, düşük ışıkta profesyonel sonuçlar verir." if cam >= 8.5 else "Gün ışığında başarılı olsa da gece çekimlerinde kumlanma yapabilir.")][0]})
                pool.append({"q": random.choice([f"{name} ekranı kaç Hz?", f"{name} ekran akıcılığı nasıl?"]), "a": [f"{hz}Hz yenileme hızıyla " + ("ipeksi bir akıcılık sunar." if hz >= 120 else "standart bir akıcılık sunar.")][0]})
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
            self.cursor.execute("SELECT id FROM products WHERE slug = %s", (slug,))
            r = self.cursor.fetchone(); return r['id'] if r else None
        except: return None

    def scrape_latest_smartphones(self):
        logging.info("Fetching listing...")
        html = self.get_via_flaresolverr(f"{self.base_url}compare-smartphones")
        if not html: return
        soup = BeautifulSoup(html, 'html.parser')
        urls = []
        for a in soup.find_all('a', href=re.compile(r'where-to-buy')):
            u = a.get('href')
            if u: urls.append(u if u.startswith('http') else f"https://www.kimovil.com{u}")
        urls = list(dict.fromkeys(urls)); logging.info(f"Found {len(urls)} products.")
        for u in urls[:50]:
            self.scrape_product_details(u)
            time.sleep(3)

if __name__ == "__main__":
    try:
        scraper = KimovilScraper()
        scraper.scrape_latest_smartphones()
        if scraper.db.is_connected(): scraper.cursor.close(); scraper.db.close(); logging.info("💤 DB closed.")
    except Exception as e: logging.error(f"❌ FATAL: {str(e)}")
