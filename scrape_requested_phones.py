import logging
import time
import random
import sys
import os
from bs4 import BeautifulSoup
from main import KimovilScraper

DEVICES_TO_SCRAPE = [
    "Samsung Galaxy S25 FE",
    "Apple iPhone 17",
    "Apple iPhone 17 Pro",
    "Poco X8 Pro",
    "Redmi Note 15 Pro",
    "Apple iPhone 17 Pro Max",
    "Honor Magic8 Pro",
    "Honor 600",
    "Redmi 15C",
    "Samsung Galaxy A56 5G",
    "Honor 600 Pro",
    "Tecno Spark Slim 5G",
    "Xiaomi 15T Pro",
    "Samsung Galaxy S25",
    "Samsung Galaxy S26",
    "Apple iPhone 17e",
    "Xiaomi 15T",
    "Samsung Galaxy A17 5G",
    "Apple iPhone 16e",
    "Redmi Note 14 Pro"
]

def run_targeted_scrape():
    scraper = KimovilScraper()
    logging.info(f"🚀 Starting targeted scrape for {len(DEVICES_TO_SCRAPE)} missing devices...")
    
    success_count = 0
    fail_count = 0
    
    for device_name in DEVICES_TO_SCRAPE:
        logging.info(f"\n--- 📱 Processing: {device_name} ---")
        try:
            # Construct possible Kimovil URL slug
            slug = device_name.lower().replace(' ', '-').replace('+', '-plus')
            
            # Handle special cases
            if "iphone" in slug and "apple" not in slug: 
                slug = "apple-" + slug
            if "poco" in slug and "xiaomi" not in slug: 
                pass # POCO is a standalone brand on Kimovil
            if "redmi" in slug and "xiaomi" not in slug: 
                slug = "xiaomi-" + slug
            
            # Remove redundant 5G suffix if it's there
            slug = slug.replace('-5g', '')
            
            product_url = f"https://www.kimovil.com/en/where-to-buy-{slug}"
            logging.info(f"🔗 Guessed URL: {product_url}")
            
            success = scraper.scrape_product_details(product_url)
            
            if not success:
                # Try a fallback with search if direct guess fails
                logging.warning(f"⚠️ Guess failed. Trying search fallback for {device_name}...")
                search_url = f"{scraper.base_url}compare-smartphones/f_name.{device_name.replace(' ', '+')}"
                html = scraper.get_via_flaresolverr(search_url)
                if html:
                    soup = BeautifulSoup(html, 'html.parser')
                    link = soup.select_one('a.device-link') or soup.select_one('.device-list a')
                    if link:
                        product_url = link.get('href')
                        if not product_url.startswith('http'): 
                            product_url = "https://www.kimovil.com" + product_url
                        logging.info(f"🔎 Found via search: {product_url}")
                        success = scraper.scrape_product_details(product_url)

            if success:
                logging.info(f"✅ Successfully updated {device_name}")
                success_count += 1
            else:
                logging.error(f"❌ Could not find/update {device_name}")
                fail_count += 1
                
            # Random wait to avoid detection / Cloudflare ban
            wait_time = random.uniform(3.0, 6.0)
            logging.info(f"💤 Waiting {wait_time:.2f}s...")
            time.sleep(wait_time)
                
        except Exception as e:
            logging.error(f"❌ Error processing {device_name}: {str(e)}")
            fail_count += 1

    logging.info("\n🏁 Target scrape completed!")
    logging.info(f"📊 Summary: {success_count} succeeded, {fail_count} failed.")

if __name__ == "__main__":
    run_targeted_scrape()
