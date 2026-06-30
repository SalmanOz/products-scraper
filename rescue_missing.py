
import logging
import asyncio
from main import KimovilScraper
import time

async def rescue_missing():
    scraper = KimovilScraper()
    
    # Target URLs found via Google search
    rescue_targets = [
        ("Samsung Galaxy S24 FE", "https://www.kimovil.com/en/where-to-buy-samsung-galaxy-s24fe"),
        ("Samsung Galaxy A15", "https://www.kimovil.com/en/where-to-buy-samsung-galaxy-a15-4g"),
        ("POCO X5 Pro 5G", "https://www.kimovil.com/en/where-to-buy-poco-x5-pro-5g"),
        ("Redmi Note 13", "https://www.kimovil.com/en/where-to-buy-xiaomi-redmi-note-13-4g"),
        ("Redmi Note 13 Pro 4G", "https://www.kimovil.com/en/where-to-buy-xiaomi-redmi-note-13-pro-4g"),
        ("Xiaomi 14T Pro", "https://www.kimovil.com/en/where-to-buy-xiaomi-14t-pro"),
        ("Honor 90", "https://www.kimovil.com/en/where-to-buy-honor-90"),
        ("Tecno Spark 20 Pro", "https://www.kimovil.com/en/where-to-buy-tecno-spark-20-pro"),
        ("Realme 11 Pro", "https://www.kimovil.com/en/where-to-buy-realme-11-pro"),
        ("Vivo V30", "https://www.kimovil.com/en/where-to-buy-vivo-v30")
    ]
    
    logging.info(f"🆘 Starting surgical rescue for {len(rescue_targets)} devices...")
    
    for name, url in rescue_targets:
        logging.info(f"\n🎯 Target Found: {name}")
        logging.info(f"🔗 URL: {url}")
        
        try:
            success = scraper.scrape_product_details(url)
            if success:
                logging.info(f"✅ Successfully rescued {name}")
            else:
                logging.error(f"❌ Failed to scrape {name}")
        except Exception as e:
            logging.error(f"❌ Error during rescue of {name}: {str(e)}")
        
        # Small delay
        time.sleep(2)

    logging.info("\n🏁 Surgical rescue operation completed!")

if __name__ == "__main__":
    asyncio.run(rescue_missing())
