import logging
import json
from bs4 import BeautifulSoup
from tr_price_scraper import TRPriceScraper
from urllib.parse import quote_plus

def dump_cimri_json():
    scraper = TRPriceScraper()
    product_name = "iPhone 15"
    url = f"https://www.cimri.com/arama?q={quote_plus(product_name)}"
    html = scraper.get_via_flaresolverr(url)
    if not html: return
    
    soup = BeautifulSoup(html, 'html.parser')
    items = soup.select('article, .product-card, [class*="product-card"]')
    product_url = None
    for item in items:
        title_el = item.select_one('h3, [class*="title"]')
        link_el = item.select_one('a')
        if title_el and link_el and scraper.is_strict_match(product_name, title_el.get_text()):
            product_url = link_el.get('href', '')
            if not product_url.startswith('http'): product_url = "https://www.cimri.com" + product_url
            break
            
    if not product_url: return
    
    detail_html = scraper.get_via_flaresolverr(product_url)
    detail_soup = BeautifulSoup(detail_html, 'html.parser')
    data_script = detail_soup.find('script', id='__OCTOPUS_DATA__')
    if data_script:
        data = json.loads(data_script.string)
        # Just find the first offer and print its keys to see what we have
        def find_offers(obj):
            if isinstance(obj, dict):
                if 'offers' in obj and isinstance(obj['offers'], list) and len(obj['offers']) > 0:
                    logging.info(json.dumps(obj['offers'][0], indent=2))
                    return True
                for v in obj.values(): 
                    if find_offers(v): return True
            elif isinstance(obj, list):
                for item in obj: 
                    if find_offers(item): return True
            return False
            
        find_offers(data)

if __name__ == "__main__":
    dump_cimri_json()
