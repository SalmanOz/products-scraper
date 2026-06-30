import logging
import asyncio
import json
from tr_price_scraper import TRPriceScraper

async def test_all_merchants(product_name):
    scraper = TRPriceScraper()
    logging.info(f"🚀 Testing all merchants for: {product_name}")
    
    results = {}
    
    for site_name, config in scraper.site_configs.items():
        logging.info(f"--- Testing {site_name} ---")
        try:
            # We bypass the 'get_best_prices' loop to test each site individually
            from urllib.parse import quote_plus
            import time
            from bs4 import BeautifulSoup
            
            url = config['url'].format(query=quote_plus(product_name))
            html = scraper.get_via_flaresolverr(url)
            
            if not html:
                logging.error(f"  ❌ {site_name}: No HTML response")
                results[site_name] = "FAILED (No HTML)"
                continue
                
            soup = BeautifulSoup(html, 'html.parser')
            items = soup.select(config['container'])
            logging.info(f"  Found {len(items)} containers")
            
            matches = []
            for item in items:
                title_el = item.select_one(config['title'])
                price_el = item.select_one(config['price'])
                
                if config['link'] == 'self' or config['link'] == '':
                    link_el = item if item.name == 'a' else item.find('a')
                else:
                    link_el = item.select_one(config['link'])
                
                if title_el and price_el:
                    title = title_el.get_text().strip()
                    price = scraper.clean_price(price_el.get_text())
                    
                    if scraper.is_strict_match(product_name, title):
                        matches.append({"title": title, "price": price})
            
            if matches:
                best = min(matches, key=lambda x: x['price'] if x['price'] > 0 else float('inf'))
                logging.info(f"  ✅ {site_name}: {best['price']} TL ({best['title']})")
                results[site_name] = f"SUCCESS ({best['price']} TL)"
            else:
                logging.info(f"  ℹ️ {site_name}: No valid match found in {len(items)} items")
                results[site_name] = "FAILED (No Match)"
                
        except Exception as e:
            logging.error(f"  ❌ {site_name} Error: {str(e)}")
            results[site_name] = f"ERROR ({str(e)})"
            
    logging.info("\n" + "="*30)
    logging.info("SUMMARY")
    logging.info("="*30)
    for site, status in results.items():
        logging.info(f"{site:20}: {status}")

if __name__ == "__main__":
    import sys
    product = sys.argv[1] if len(sys.argv) > 1 else "Samsung Galaxy S24"
    asyncio.run(test_all_merchants(product))
