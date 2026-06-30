import logging
import requests
from bs4 import BeautifulSoup
from tr_price_scraper import TRPriceScraper
import asyncio

async def debug_site(site_name, product_name):
    scraper = TRPriceScraper()
    url = scraper.sites[site_name].format(query=product_name.replace(' ', '+'))
    logging.info(f"URL: {url}")
    html = scraper.get_via_flaresolverr(url)
    if not html:
        logging.info("No HTML")
        return
    
    soup = BeautifulSoup(html, 'html.parser')
    if site_name == "Trendyol":
        items = soup.select('a.product-card')
        logging.info(f"Found {len(items)} items")
        for i, item in enumerate(items[:10]):
            title_el = item.select_one('h2.title') or item.select_one('.product-name')
            price_el = item.select_one('.price-value') or item.select_one('.single-price')
            title = title_el.get_text().strip() if title_el else "No Title"
            price = scraper.clean_price(price_el.get_text()) if price_el else 0
            logging.info(f"Item {i}: {title} | Price: {price}")
            logging.info(f"  Match: {scraper.is_strict_match(product_name, title)}")

if __name__ == "__main__":
    asyncio.run(debug_site("Trendyol", "iPhone 15 Pro"))
