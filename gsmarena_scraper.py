import logging
import requests
from bs4 import BeautifulSoup
import re
import time

class GSMArenaScraper:
    def __init__(self):
        self.flaresolverr_url = "http://localhost:8191/v1"

    def get_html(self, url):
        payload = {"cmd": "request.get", "url": url, "maxTimeout": 60000}
        try:
            res = requests.post(self.flaresolverr_url, json=payload, timeout=90)
            data = res.json()
            if data.get('status') == 'ok':
                return data['solution']['response']
            return None
        except Exception as e:
            logging.error(f"  ⚠️ GSMArena Error: {str(e)}")
            return None

    def get_images(self, product_name):
        logging.info(f"🎨 Searching GSMArena images for: {product_name}")
        # 1. Search for product
        search_url = f"https://www.gsmarena.com/results.php3?sQuickSearch=yes&sName={product_name.replace(' ', '+')}"
        html = self.get_html(search_url)
        if not html: return []

        soup = BeautifulSoup(html, 'html.parser')
        makers = soup.select('.makers li a')
        if not makers: 
            logging.info(f"  ℹ️ GSMArena: No product found for {product_name}")
            return []

        # Find best match (compare names to avoid wrong models)
        best_match = None
        for maker in makers:
            title = maker.get_text().strip().lower()
            if product_name.lower() in title or title in product_name.lower():
                best_match = maker
                break
        
        if not best_match:
            best_match = makers[0] # Default to first

        target_link = best_match.get('href')
        
        # 2. Extract Main BigPic directly from search if possible (high quality render)
        images = []
        main_img_el = best_match.select_one('img')
        if main_img_el:
            main_src = main_img_el.get('src')
            if main_src:
                # GSMArena search thumbnails are actually high-res if you change the path
                # e.g. https://fdn2.gsmarena.com/vv/bigpic/samsung-galaxy-s24.jpg
                images.append(main_src)

        # 2. Go to pictures page for gallery
        gallery_images = []
        try:
            parts = target_link.split('-')
            last_part = parts[-1] 
            base_link = '-'.join(parts[:-1])
            pic_link = f"{base_link}-pictures-{last_part}"
            
            full_pic_url = f"https://www.gsmarena.com/{pic_link}"
            pic_html = self.get_html(full_pic_url)
            
            if pic_html:
                pic_soup = BeautifulSoup(pic_html, 'html.parser')
                gallery_imgs = pic_soup.select('#pictures-list img')
                for img in gallery_imgs:
                    src = img.get('src')
                    if src and src.startswith('http') and '.jpg' in src and 'blob:' not in src:
                        gallery_images.append(src)
        except:
            pass

        # 3. Prioritize: prefer gallery images ending in -1.jpg, -2.jpg (usually single device renders)
        final_images = []
        forbidden_keywords = ['all-colors', 'colors', 'group', 'combo', 'rendering', 'official']

        # Filter gallery images to remove obvious group shots
        clean_gallery = [img for img in gallery_images if not any(k in img.lower() for k in forbidden_keywords)]
        
        # Look for -1.jpg (front/back high res) first - this is the best standard
        for img in clean_gallery:
            if '-1.jpg' in img:
                final_images.append(img)
        
        # Then -2.jpg
        for img in clean_gallery:
            if '-2.jpg' in img and img not in final_images:
                final_images.append(img)
        
        # Then the rest of clean gallery
        for img in clean_gallery:
            if img not in final_images:
                final_images.append(img)
        
        # If we still have nothing, or want to check bigpic
        main_img_el = best_match.select_one('img')
        if main_img_el:
            main_src = main_img_el.get('src')
            if main_src and not any(k in main_src.lower() for k in forbidden_keywords):
                if main_src not in final_images:
                    final_images.append(main_src)
        
        return final_images[:5]

if __name__ == "__main__":
    scraper = GSMArenaScraper()
    logging.info(scraper.get_images("Samsung Galaxy S24"))
