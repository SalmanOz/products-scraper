
import logging
import asyncio
from tr_price_scraper import TRPriceScraper

async def test_cimri():
    scraper = TRPriceScraper()
    product_name = "iPhone 15"
    logging.info(f"🚀 Testing Cimri specifically for: {product_name}")
    results = scraper.get_cimri_price(product_name)
    if results:
        logging.info(f"✅ Found {len(results)} offers on Cimri:")
        for r in results[:10]: # Show first 10
            logging.info(f"  - Merchant: {r['merchant']}")
            logging.info(f"    Price: {r['price']} TL")
            logging.info(f"    URL: {r['url'][:60]}...")
    else:
        logging.error("❌ No results found on Cimri.")

if __name__ == "__main__":
    asyncio.run(test_cimri())
