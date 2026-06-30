import logging
import time
import random
from main import KimovilScraper

URLS = [
  "https://www.kimovil.com/en/where-to-buy-apple-iphone-11",
  "https://www.kimovil.com/en/where-to-buy-apple-iphone-12",
  "https://www.kimovil.com/en/where-to-buy-apple-iphone-13",
  "https://www.kimovil.com/en/where-to-buy-apple-iphone-14",
  "https://www.kimovil.com/en/where-to-buy-apple-iphone-14-pro-max",
  "https://www.kimovil.com/en/where-to-buy-apple-iphone-15",
  "https://www.kimovil.com/en/where-to-buy-apple-iphone-15-pro",
  "https://www.kimovil.com/en/where-to-buy-apple-iphone-15-pro-max",
  "https://www.kimovil.com/en/where-to-buy-apple-iphone-16",
  "https://www.kimovil.com/en/where-to-buy-apple-iphone-16-pro-max",
  "https://www.kimovil.com/en/where-to-buy-samsung-galaxy-s23-ultra",
  "https://www.kimovil.com/en/where-to-buy-samsung-galaxy-s23-fe",
  "https://www.kimovil.com/en/where-to-buy-samsung-galaxy-s23",
  "https://www.kimovil.com/en/where-to-buy-samsung-galaxy-s24-ultra",
  "https://www.kimovil.com/en/where-to-buy-samsung-galaxy-s24",
  "https://www.kimovil.com/en/where-to-buy-samsung-galaxy-s24-fe",
  "https://www.kimovil.com/en/where-to-buy-samsung-galaxy-s25-ultra",
  "https://www.kimovil.com/en/where-to-buy-samsung-galaxy-a54-5g",
  "https://www.kimovil.com/en/where-to-buy-samsung-galaxy-a55-5g",
  "https://www.kimovil.com/en/where-to-buy-samsung-galaxy-a34-5g",
  "https://www.kimovil.com/en/where-to-buy-samsung-galaxy-a35-5g",
  "https://www.kimovil.com/en/where-to-buy-samsung-galaxy-a15",
  "https://www.kimovil.com/en/where-to-buy-poco-x6-pro",
  "https://www.kimovil.com/en/where-to-buy-poco-f5",
  "https://www.kimovil.com/en/where-to-buy-poco-f6-pro",
  "https://www.kimovil.com/en/where-to-buy-poco-x5-pro-5g",
  "https://www.kimovil.com/en/where-to-buy-xiaomi-redmi-note-12-pro",
  "https://www.kimovil.com/en/where-to-buy-xiaomi-redmi-note-13-4g",
  "https://www.kimovil.com/en/where-to-buy-xiaomi-redmi-note-13-pro-4g",
  "https://www.kimovil.com/en/where-to-buy-xiaomi-redmi-note-13-pro-plus",
  "https://www.kimovil.com/en/where-to-buy-xiaomi-13t",
  "https://www.kimovil.com/en/where-to-buy-xiaomi-14",
  "https://www.kimovil.com/en/where-to-buy-xiaomi-14t-pro",
  "https://www.kimovil.com/en/where-to-buy-honor-90",
  "https://www.kimovil.com/en/where-to-buy-honor-200",
  "https://www.kimovil.com/en/where-to-buy-tecno-spark-20-pro",
  "https://www.kimovil.com/en/where-to-buy-tecno-camon-20-pro",
  "https://www.kimovil.com/en/where-to-buy-infinix-note-40",
  "https://www.kimovil.com/en/where-to-buy-realme-11-pro",
  "https://www.kimovil.com/en/where-to-buy-vivo-v30"
]

def run():
    logging.info(f"🚀 Starting scrape for {len(URLS)} phones...")
    scraper = KimovilScraper()
    
    try:
        for idx, url in enumerate(URLS):
            logging.info(f"\n[{idx+1}/{len(URLS)}] Processing: {url}")
            success = scraper.scrape_product_details(url)
            
            if success:
                logging.info(f"✅ Successfully scraped: {url}")
            else:
                logging.error(f"❌ Failed to scrape: {url}")
            
            # Cooldown to avoid detection
            wait_time = random.uniform(3, 7)
            logging.info(f"💤 Waiting {wait_time:.1f}s...")
            time.sleep(wait_time)
            
    except Exception as e:
        logging.error(f"❌ FATAL ERROR during execution: {str(e)}")
    finally:
        if scraper.db.is_connected():
            scraper.cursor.close()
            scraper.db.close()
            logging.info("\n💤 Database connection closed.")

if __name__ == "__main__":
    run()
