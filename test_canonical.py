import logging
import asyncio
from bs4 import BeautifulSoup
from tr_price_scraper import TRPriceScraper

def test_resolve_with_canonical():
    scraper = TRPriceScraper()
    # Cimri offer URL
    offer_url = "https://www.cimri.com/offer/1980150603"
    logging.info(f"Original URL: {offer_url}")
    
    # Let FlareSolverr follow the redirect and get the final HTML
    html = scraper.get_via_flaresolverr(offer_url)
    
    if html:
        logging.info("HTML length:", len(html))
        soup = BeautifulSoup(html, 'html.parser')
        
        # 1. Look for canonical URL
        canonical = soup.find('link', rel='canonical')
        if canonical and canonical.get('href'):
            logging.info("✅ Found Canonical URL:", canonical.get('href'))
            return
            
        # 2. Look for og:url
        og_url = soup.find('meta', property='og:url')
        if og_url and og_url.get('content'):
            logging.info("✅ Found OG URL:", og_url.get('content'))
            return
            
        logging.error("❌ Could not find canonical or og:url in HTML.")
    else:
        logging.error("❌ FlareSolverr returned no HTML.")

if __name__ == "__main__":
    test_resolve_with_canonical()
