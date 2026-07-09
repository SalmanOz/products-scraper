import os
import re
import logging
import requests
import time
import mysql.connector
import boto3
from botocore.client import Config
from PIL import Image
from io import BytesIO
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Load env file
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

BRAND_DOMAINS = {
    'apple': 'apple.com',
    'samsung': 'samsung.com',
    'xiaomi': 'mi.com',
    'redmi': 'mi.com',
    'poco': 'po.co',
    'oneplus': 'oneplus.com',
    'oppo': 'oppo.com',
    'vivo': 'vivo.com',
    'realme': 'realme.com',
    'google': 'google.com',
    'huawei': 'huawei.com',
    'honor': 'hihonor.com',
    'nothing': 'nothing.tech',
    'motorola': 'motorola.com',
    'asus': 'asus.com',
    'sony': 'sony.com',
    'nokia': 'nokia.com',
    'tcl': 'tcl.com',
    'infinix': 'infinixmobility.com',
    'tecno': 'tecno-mobile.com',
    'zte': 'zte.com.cn'
}

WIKI_PAGES = {
    'apple': 'Apple Inc.',
    'samsung': 'Samsung Electronics',
    'xiaomi': 'Xiaomi',
    'redmi': 'Xiaomi',
    'poco': 'Poco (smartphone)',
    'oneplus': 'OnePlus',
    'oppo': 'Oppo',
    'vivo': 'Vivo (technology company)',
    'realme': 'Realme',
    'google': 'Google',
    'huawei': 'Huawei',
    'honor': 'Honor (brand)',
    'nothing': 'Nothing (technology company)',
    'motorola': 'Motorola Mobility',
    'asus': 'Asus',
    'sony': 'Sony',
    'nokia': 'Nokia',
    'tcl': 'TCL Technology',
    'infinix': 'Infinix Mobile',
    'tecno': 'Tecno Mobile',
    'zte': 'ZTE Corporation'
}

def get_wikipedia_logo(brand_name, slug):
    """
    Search Wikipedia for the brand and return the page image thumbnail URL.
    """
    try:
        # Rate limit prevention
        time.sleep(1.5)
        session = requests.Session()
        session.headers.update({"User-Agent": "TeknoskorBrandLogoScraper/1.0 (contact@teknoskor.com) Python-Requests"})
        search_url = "https://en.wikipedia.org/w/api.php"

        # Step 1: Resolve page title
        slug_lower = slug.lower()
        if slug_lower in WIKI_PAGES:
            page_title = WIKI_PAGES[slug_lower]
            logger.info(f"🎯 Deterministic Wikipedia page: '{page_title}' for slug '{slug}'")
        else:
            # Fallback to search
            search_params = {
                "action": "query",
                "list": "search",
                "srsearch": f"{brand_name} company logo",
                "format": "json"
            }
            res = session.get(search_url, params=search_params, timeout=10)
            res.raise_for_status()
            search_data = res.json()
            results = search_data.get("query", {}).get("search", [])
            if not results:
                # Retry search with simpler query
                search_params["srsearch"] = f"{brand_name} company"
                time.sleep(1.0)
                res = session.get(search_url, params=search_params, timeout=10)
                results = res.json().get("query", {}).get("search", [])

            if not results:
                return None
            page_title = results[0]["title"]
            logger.info(f"🔍 Found Wikipedia page: '{page_title}' for brand '{brand_name}'")

        # Step 2: Query the pageimage properties for the page title
        img_params = {
            "action": "query",
            "titles": page_title,
            "prop": "pageimages",
            "pithumbsize": 500,
            "format": "json",
            "redirects": 1
        }
        time.sleep(1.0)
        res = session.get(search_url, params=img_params, timeout=10)
        res.raise_for_status()
        img_data = res.json()
        pages = img_data.get("query", {}).get("pages", {})
        for page_id, page_info in pages.items():
            thumbnail = page_info.get("thumbnail", {})
            if thumbnail and "source" in thumbnail:
                logo_url = thumbnail["source"]
                return logo_url
    except Exception as e:
        logger.error(f"⚠️ Wikipedia logo search failed for {brand_name}: {e}")
    return None

STATIC_LOGOS = {
    'apple': 'https://upload.wikimedia.org/wikipedia/commons/thumb/f/fa/Apple_logo_black.svg/500px-Apple_logo_black.svg.png',
    'samsung': 'https://upload.wikimedia.org/wikipedia/commons/thumb/a/a7/Samsung_logo.svg/500px-Samsung_logo.svg.png',
    'xiaomi': 'https://upload.wikimedia.org/wikipedia/commons/thumb/a/ae/Xiaomi_logo_%282021-%29.svg/500px-Xiaomi_logo_%282021-%29.svg.png',
    'redmi': 'https://upload.wikimedia.org/wikipedia/commons/thumb/a/ae/Xiaomi_logo_%282021-%29.svg/500px-Xiaomi_logo_%282021-%29.svg.png',
    'poco': 'https://upload.wikimedia.org/wikipedia/commons/thumb/0/0d/Poco_Logo_Transparent.png/500px-Poco_Logo_Transparent.png',
    'oneplus': 'https://upload.wikimedia.org/wikipedia/commons/5/52/Logo_entreprise_OnePlus.png',
    'oppo': 'https://upload.wikimedia.org/wikipedia/commons/thumb/e/e8/OPPO_logo.svg/500px-OPPO_logo.svg.png',
    'vivo': 'https://upload.wikimedia.org/wikipedia/commons/thumb/e/e5/Vivo_logo.svg/500px-Vivo_logo.svg.png',
    'realme': 'https://upload.wikimedia.org/wikipedia/commons/thumb/a/a2/Realme_logo.svg/500px-Realme_logo.svg.png',
    'google': 'https://upload.wikimedia.org/wikipedia/commons/thumb/2/2f/Google_2015_logo.svg/500px-Google_2015_logo.svg.png',
    'huawei': 'https://upload.wikimedia.org/wikipedia/commons/thumb/0/00/Huawei_Logo.svg/500px-Huawei_Logo.svg.png',
    'honor': 'https://upload.wikimedia.org/wikipedia/commons/thumb/1/1d/Honor_Logo.svg/500px-Honor_Logo.svg.png',
    'nothing': 'https://upload.wikimedia.org/wikipedia/commons/thumb/5/57/Nothing_Logo.svg/500px-Nothing_Logo.svg.png',
    'motorola': 'https://upload.wikimedia.org/wikipedia/commons/thumb/4/4f/Motorola_logo.svg/500px-Motorola_logo.svg.png',
    'asus': 'https://upload.wikimedia.org/wikipedia/commons/thumb/d/de/Asus_Logo.svg/500px-Asus_Logo.svg.png',
    'sony': 'https://upload.wikimedia.org/wikipedia/commons/thumb/c/ca/Sony_logo.svg/500px-Sony_logo.svg.png',
    'nokia': 'https://upload.wikimedia.org/wikipedia/commons/thumb/0/02/Nokia_Logo.svg/500px-Nokia_Logo.svg.png',
    'tcl': 'https://upload.wikimedia.org/wikipedia/commons/thumb/a/a2/TCL_logo.svg/500px-TCL_logo.svg.png',
    'infinix': 'https://upload.wikimedia.org/wikipedia/commons/thumb/e/e0/Infinix_logo.svg/500px-Infinix_logo.svg.png',
    'tecno': 'https://upload.wikimedia.org/wikipedia/commons/thumb/5/5e/Tecno_Mobile_logo.svg/500px-Tecno_Mobile_logo.svg.png',
    'zte': 'https://upload.wikimedia.org/wikipedia/commons/thumb/d/d9/ZTE_Logo.svg/500px-ZTE_Logo.svg.png',
    'nubia': 'https://upload.wikimedia.org/wikipedia/commons/thumb/f/f6/Nubia_logo.svg/500px-Nubia_logo.svg.png'
}

def fetch_logo_image(brand_name, slug):
    """
    Tries to retrieve the logo from static mappings, Clearbit, or Wikipedia.
    Returns (logo_url, image_bytes) if found, otherwise (None, None).
    """
    session = requests.Session()
    headers = {"User-Agent": "TeknoskorBrandLogoScraper/1.0 (contact@teknoskor.com) Python-Requests"}

    # 1. Static high-quality mapping (Pre-verified)
    slug_lower = slug.lower()
    if slug_lower in STATIC_LOGOS:
        static_url = STATIC_LOGOS[slug_lower]
        try:
            logger.info(f"🌐 Fetching verified static logo: {static_url}")
            # Rate limit protection for Wikimedia Commons
            time.sleep(1.5)
            res = session.get(static_url, headers=headers, timeout=8)
            if res.status_code == 200 and len(res.content) > 100:
                logger.info(f"✅ Static logo fetched for {brand_name}")
                return static_url, res.content
        except Exception as e:
            logger.warning(f"Failed to fetch static logo from {static_url}: {e}")

    # 2. Clearbit Logo API
    domain = BRAND_DOMAINS.get(slug_lower) or f"{slug_lower.replace(' ', '')}.com"
    clearbit_url = f"https://logo.clearbit.com/{domain}"
    try:
        logger.info(f"🌐 Trying Clearbit: {clearbit_url}")
        res = session.get(clearbit_url, headers=headers, timeout=8)
        if res.status_code == 200 and len(res.content) > 100:
            logger.info(f"✅ Clearbit match for {brand_name} ({domain})")
            return clearbit_url, res.content
    except Exception as e:
        logger.warning(f"Clearbit failed for {brand_name}: {e}")

    # 3. Wikipedia fallback
    wiki_logo_url = get_wikipedia_logo(brand_name, slug)
    if wiki_logo_url:
        try:
            logger.info(f"🌐 Fetching Wikipedia logo: {wiki_logo_url}")
            res = session.get(wiki_logo_url, headers=headers, timeout=8)
            if res.status_code == 200 and len(res.content) > 100:
                logger.info(f"✅ Wikipedia logo fetched for {brand_name}")
                return wiki_logo_url, res.content
        except Exception as e:
            logger.warning(f"Failed to fetch Wikipedia logo from {wiki_logo_url}: {e}")

    return None, None

def main():
    # Database configuration
    db_host = os.getenv("DB_HOST", "127.0.0.1")
    db_user = os.getenv("DB_USER", "root")
    db_password = os.getenv("DB_PASSWORD", "rootpassword")
    db_name = os.getenv("DB_NAME", "product_comparison")
    db_port = int(os.getenv("DB_PORT", 3306))

    # S3 / R2 configuration
    r2_access_key = os.getenv("R2_ACCESS_KEY_ID")
    r2_account_id = os.getenv("R2_ACCOUNT_ID")
    r2_secret_key = os.getenv("R2_SECRET_ACCESS_KEY")
    r2_bucket = os.getenv("R2_BUCKET_NAME")
    r2_public_domain = os.getenv("R2_PUBLIC_DOMAIN", "").rstrip('/')

    r2_enabled = bool(r2_access_key) and bool(r2_account_id) and bool(r2_bucket)
    s3 = None

    if r2_enabled:
        logger.info("☁️ R2 Configured. Uploading logos to R2.")
        s3 = boto3.client(
            's3',
            endpoint_url=f"https://{r2_account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=r2_access_key,
            aws_secret_access_key=r2_secret_key,
            config=Config(signature_version='s3v4'),
            region_name='auto'
        )
    else:
        logger.warning("⚠️ R2 is not configured. Will only log detected urls and not upload.")

    try:
        db = mysql.connector.connect(
            host=db_host,
            user=db_user,
            password=db_password,
            database=db_name,
            port=db_port
        )
        cursor = db.cursor(dictionary=True)
    except Exception as e:
        logger.error(f"❌ Failed to connect to MySQL database: {e}")
        return

    # Select all brands with missing or non-uploaded logos
    cursor.execute("SELECT id, name, slug, logo_url FROM brands WHERE logo_url IS NULL OR logo_url = '' OR logo_url NOT LIKE '%brands/%'")
    brands = cursor.fetchall()
    logger.info(f"📊 Found {len(brands)} brands needing logo processing/updating.")

    updated_count = 0

    for brand in brands:
        brand_name = brand["name"]
        slug = brand["slug"]
        brand_id = brand["id"]

        logger.info(f"🚀 Processing brand: '{brand_name}' (slug: {slug})")
        logo_source_url, img_bytes = fetch_logo_image(brand_name, slug)

        if img_bytes:
            final_logo_url = logo_source_url
            if r2_enabled:
                try:
                    img = Image.open(BytesIO(img_bytes))
                    if img.mode in ("RGBA", "P"):
                        img = img.convert("RGBA")
                    else:
                        img = img.convert("RGB")
                    
                    buffer = BytesIO()
                    img.save(buffer, format="WEBP", quality=90, optimize=True)
                    buffer.seek(0)
                    
                    destination_key = f"brands/{slug}/logo.webp"
                    s3.put_object(
                        Bucket=r2_bucket,
                        Key=destination_key,
                        Body=buffer.getvalue(),
                        ContentType='image/webp'
                    )
                    final_logo_url = f"{r2_public_domain}/{destination_key}"
                    logger.info(f"☁️ Uploaded to R2: {final_logo_url}")
                except Exception as e:
                    logger.error(f"❌ R2 processing error for {brand_name}: {e}")
                    # Fallback to source url if R2 fails
            
            # Update database
            try:
                cursor.execute(
                    "UPDATE brands SET logo_url = %s WHERE id = %s",
                    (final_logo_url, brand_id)
                )
                db.commit()
                logger.info(f"✅ DB Updated for {brand_name} with logo URL: {final_logo_url}")
                updated_count += 1
            except Exception as e:
                logger.error(f"❌ Failed to update DB for {brand_name}: {e}")
        else:
            logger.warning(f"❌ Could not find a logo for brand: '{brand_name}'")

    cursor.close()
    db.close()
    logger.info(f"🎉 Complete. Successfully updated {updated_count} brands.")

if __name__ == "__main__":
    main()
