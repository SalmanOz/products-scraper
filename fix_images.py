import logging
import asyncio
from main import KimovilScraper
import time

FIX_LIST = [
    "https://www.kimovil.com/en/where-to-buy-samsung-galaxy-s24-ultra",
    "https://www.kimovil.com/en/where-to-buy-samsung-galaxy-s23",
    "https://www.kimovil.com/en/where-to-buy-samsung-galaxy-s23-fe",
    "https://www.kimovil.com/en/where-to-buy-samsung-galaxy-s23-ultra",
    "https://www.kimovil.com/en/where-to-buy-apple-iphone-15-pro",
    "https://www.kimovil.com/en/where-to-buy-apple-iphone-15-pro-max",
    "https://www.kimovil.com/en/where-to-buy-apple-iphone-14",
    "https://www.kimovil.com/en/where-to-buy-apple-iphone-14-pro-max",
    "https://www.kimovil.com/en/where-to-buy-apple-iphone-13",
    "https://www.kimovil.com/en/where-to-buy-apple-iphone-12",
    "https://www.kimovil.com/en/where-to-buy-apple-iphone-11"
]

def run_fix():
    scraper = KimovilScraper()
    logging.info(f"🛠️ Fixing images for {len(FIX_LIST)} models...")
    
    for url in FIX_LIST:
        logging.info(f"\n🔄 Updating: {url}")
        scraper.scrape_product_details(url)
        time.sleep(5)
    
    if scraper.db.is_connected():
        scraper.cursor.close()
        scraper.db.close()
    logging.info("\n✅ Fix completed!")

if __name__ == "__main__":
    run_fix()
