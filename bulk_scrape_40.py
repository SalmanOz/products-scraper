
import logging
import asyncio
import sys
import os
from main import KimovilScraper

# The list of 40 specific devices requested by the user
DEVICES_TO_SCRAPE = [
    "iPhone 11", "iPhone 12", "iPhone 13", "iPhone 14", "iPhone 14 Pro Max", 
    "iPhone 15", "iPhone 15 Pro", "iPhone 15 Pro Max", "iPhone 16", "iPhone 16 Pro Max",
    "Samsung Galaxy S23 Ultra", "Samsung Galaxy S23 FE", "Samsung Galaxy S23", 
    "Samsung Galaxy S24 Ultra", "Samsung Galaxy S24", "Samsung Galaxy S24 FE", 
    "Samsung Galaxy S25 Ultra", "Samsung Galaxy A54 5G", "Samsung Galaxy A55 5G", 
    "Samsung Galaxy A34 5G", "Samsung Galaxy A35 5G", "Samsung Galaxy A15",
    "POCO X6 Pro", "POCO F5", "POCO F6 Pro", "POCO X5 Pro 5G",
    "Redmi Note 12 Pro 5G", "Redmi Note 13", "Redmi Note 13 Pro 4G", 
    "Redmi Note 13 Pro+ 5G", "Xiaomi 13T", "Xiaomi 14", "Xiaomi 14T Pro",
    "Honor 90", "Honor 200", "Tecno Spark 20 Pro", "Tecno Camon 20 Pro", 
    "Infinix Note 40", "Realme 11 Pro", "Vivo V30"
]

def run_bulk_scrape():
    scraper = KimovilScraper()
    logging.info(f"🚀 Starting bulk scrape for {len(DEVICES_TO_SCRAPE)} devices...")
    
    for device_name in DEVICES_TO_SCRAPE:
        logging.info(f"\n--- 📱 Processing: {device_name} ---")
        try:
            # Construct possible Kimovil URL slug
            slug = device_name.lower().replace(' ', '-').replace('+', '-plus')
            # Handle special cases
            if "iphone" in slug and "apple" not in slug: slug = "apple-" + slug
            if "poco" in slug and "xiaomi" not in slug: slug = slug # POCO is a standalone brand on Kimovil
            if "redmi" in slug and "xiaomi" not in slug: slug = "xiaomi-" + slug
            
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
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(html, 'html.parser')
                    link = soup.select_one('a.device-link') or soup.select_one('.device-list a')
                    if link:
                        product_url = link.get('href')
                        if not product_url.startswith('http'): product_url = "https://www.kimovil.com" + product_url
                        logging.info(f"🔎 Found via search: {product_url}")
                        success = scraper.scrape_product_details(product_url)

            if success:
                logging.info(f"✅ Successfully updated {device_name}")
            else:
                logging.error(f"❌ Could not find/update {device_name}")
                
        except Exception as e:
            logging.error(f"❌ Error processing {device_name}: {str(e)}")

    logging.info("\n🏁 Bulk scrape completed!")

if __name__ == "__main__":
    run_bulk_scrape()
