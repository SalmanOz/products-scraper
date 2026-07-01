import logging
import json
from main import KimovilScraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def debug_images():
    scraper = KimovilScraper()
    if not scraper.db or not scraper.db.is_connected():
        logging.error("❌ Could not connect to database.")
        return
        
    scraper.cursor.execute("SELECT id, name, slug, images FROM products LIMIT 10")
    products = scraper.cursor.fetchall()
    
    print("\n--- DATABASE RAW IMAGES FIELD INSPECTION ---")
    for p in products:
        print(f"ID: {p['id']} | Name: {p['name']} | Slug: {p['slug']}")
        raw_val = p['images']
        print(f"  Raw type: {type(raw_val)}")
        print(f"  Raw value: {repr(raw_val)}")
        try:
            if isinstance(raw_val, str):
                parsed = json.loads(raw_val)
            else:
                parsed = raw_val
            print(f"  Parsed type: {type(parsed)}")
            print(f"  Parsed value: {parsed}")
        except Exception as e:
            print(f"  ❌ Parse Error: {e}")
        print("-" * 60)
        
    if scraper.db.is_connected():
        scraper.cursor.close()
        scraper.db.close()

if __name__ == "__main__":
    debug_images();
