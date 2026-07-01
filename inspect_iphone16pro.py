import logging
import json
from main import KimovilScraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def check_one():
    scraper = KimovilScraper()
    if not scraper.db or not scraper.db.is_connected():
        logging.error("❌ Could not connect to database.")
        return
        
    scraper.cursor.execute("SELECT id, name, slug, images FROM products WHERE slug = 'apple-iphone-16-pro'")
    p = scraper.cursor.fetchone()
    if p:
        print("\n--- SPECIFIC PRODUCT INSPECTION ---")
        print(f"ID: {p['id']} | Name: {p['name']} | Slug: {p['slug']}")
        raw_val = p['images']
        print(f"  Raw type: {type(raw_val)}")
        print(f"  Raw value: {repr(raw_val)}")
    else:
        print("\n❌ Product 'apple-iphone-16-pro' not found in database!")
        
    if scraper.db.is_connected():
        scraper.cursor.close()
        scraper.db.close()

if __name__ == "__main__":
    check_one()
