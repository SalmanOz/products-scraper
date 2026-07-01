import logging
import json
import os
import re
from main import KimovilScraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def run_fix():
    scraper = KimovilScraper()
    if not scraper.db or not scraper.db.is_connected():
        logging.error("❌ Could not connect to database.")
        return
        
    logging.info("🔍 Fetching all products from database...")
    scraper.cursor.execute("SELECT id, name, slug, images FROM products")
    products = scraper.cursor.fetchall()
    logging.info(f"📋 Found {len(products)} products to check.")
    
    updated_count = 0
    
    for p in products:
        p_id = p['id']
        name = p['name']
        slug = p['slug']
        
        try:
            images = json.loads(p['images']) if p['images'] else []
        except Exception as e:
            logging.error(f"❌ Failed to parse images JSON for {name} (ID: {p_id}): {e}")
            continue
            
        if not isinstance(images, list):
            continue
            
        modified = False
        new_images = []
        
        for idx, img_url in enumerate(images):
            if not img_url:
                continue
                
            fixed_url = img_url.strip()
            
            # 1. Fix missing protocol (e.g. starts with pub-xxxx.r2.dev or cdn.site.com)
            if fixed_url and not fixed_url.startswith(('http://', 'https://', '//')):
                fixed_url = 'https://' + fixed_url
                modified = True
                logging.info(f"  🔧 Prepended https:// to image for {name}: {fixed_url}")
                
            # 2. Fix protocol relative URLs
            if fixed_url.startswith('//'):
                fixed_url = 'https:' + fixed_url
                modified = True
                logging.info(f"  🔧 Changed relative protocol image for {name}: {fixed_url}")
                
            # 3. If it's a Kimovil hotlink (not uploaded to R2)
            if 'kimovil.com' in fixed_url:
                logging.info(f"  🖼️ Found external Kimovil image for {name}. Re-uploading to R2...")
                if scraper.r2_enabled:
                    filename = f"{slug}-{idx}.jpg"
                    r2_url = scraper.upload_image_to_r2(fixed_url, f"products/{slug}/{filename}")
                    if r2_url != fixed_url:
                        fixed_url = r2_url
                        modified = True
                        logging.info(f"    ✅ Uploaded to R2: {fixed_url}")
                    else:
                        logging.warning(f"    ⚠️ Upload failed, keeping original URL: {fixed_url}")
                else:
                    logging.warning("    ⚠️ R2 not enabled, cannot upload external image.")
                    
            new_images.append(fixed_url)
            
        if modified:
            logging.info(f"💾 Saving updated images for {name} (ID: {p_id})")
            scraper.cursor.execute("UPDATE products SET images = %s WHERE id = %s", (json.dumps(new_images), p_id))
            scraper.db.commit()
            updated_count += 1
            
    if scraper.db.is_connected():
        scraper.cursor.close()
        scraper.db.close()
        
    logging.info(f"\n🎉 Image fix process completed! Updated {updated_count} products.")

if __name__ == "__main__":
    run_fix()
