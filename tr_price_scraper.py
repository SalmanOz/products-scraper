import logging
import requests
import re
import json
from bs4 import BeautifulSoup
from urllib.parse import quote_plus
import time
import asyncio

class TRPriceScraper:
    def __init__(self):
        self.flaresolverr_url = "http://localhost:8191/v1"
        # Search URL templates and selectors for each site
        self.site_configs = {
            "Hepsiburada": {
                "url": "https://www.hepsiburada.com/ara?q={query}",
                "container": "li[class*='productListContent-item'], article[class*='productCard'], [data-test-id='product-card-container']",
                "title": "[data-test-id^='title-'], h3[data-test-id='product-card-name'], [class*='product-title']",
                "price": "[data-test-id^='final-price-'], [data-test-id='price-current-price'], .price-current-price",
                "link": "a[href*='/p/'], a[class*='productCardLink']",
                "base_url": "https://www.hepsiburada.com"
            },
            "Trendyol": {
                "url": "https://www.trendyol.com/sr?q={query}&wc=103498",
                "container": ".p-card-wrppr, .product-card",
                "title": ".prdct-desc-cntnr-name, .prdct-desc-cntnr-ttl, .product-name",
                "price": ".prc-box-dscntd, .p-card-price, .sale-price",
                "link": "a",
                "base_url": "https://www.trendyol.com"
            },
            "Amazon TR": {
                "url": "https://www.amazon.com.tr/s?k={query}",
                "container": "[data-component-type='s-search-result']",
                "title": "h2 a span",
                "price": ".a-price-whole",
                "link": "h2 a",
                "base_url": "https://www.amazon.com.tr"
            },
            "Vatan Bilgisayar": {
                "url": "https://www.vatanbilgisayar.com/arama/{query}/",
                "container": ".product-list--item",
                "title": ".product-list__product-name",
                "price": ".product-list__price",
                "link": "a.product-list-link",
                "base_url": "https://www.vatanbilgisayar.com"
            },
            "n11": {
                "url": "https://www.n11.com/arama?q={query}",
                "container": ".product-item",
                "title": ".product-name",
                "price": ".newPrice",
                "link": "a",
                "base_url": "https://www.n11.com"
            },
            "PttAVM": {
                "url": "https://www.pttavm.com/arama?q={query}",
                "container": ".product-list-card",
                "title": ".product-list-card__title",
                "price": ".product-list-card__price-new",
                "link": "a",
                "base_url": "https://www.pttavm.com"
            },
            "MediaMarkt": {
                "url": "https://www.mediamarkt.com.tr/tr/search.html?query={query}",
                "container": "[data-test='mms-product-card']",
                "title": "[data-test='product-title']",
                "price": "[data-test='mms-price-display']",
                "link": "a",
                "base_url": "https://www.mediamarkt.com.tr"
            },
            "Pasaj": {
                "url": "https://www.turkcell.com.tr/pasaj/arama?q={query}",
                "container": ".p-card, .m-p-pc-new",
                "title": ".p-card-title, .m-p-pc-new__title",
                "price": ".p-card-price, .m-p-pc-new__price",
                "link": "a",
                "base_url": "https://www.turkcell.com.tr"
            },
            "Pazarama": {
                "url": "https://www.pazarama.com/arama?q={query}",
                "container": "[data-testid='listing-product-card-grid'], .product-card",
                "title": ".product-name, h2, .p-card-title",
                "price": "div[class*='text-gray-600'], .product-card__price, .price",
                "link": "a",
                "base_url": "https://www.pazarama.com"
            },
            "Gürgençler": {
                "url": "https://www.gurgencler.com.tr/arama?q={query}",
                "container": ".product-item",
                "title": ".product-item-link",
                "price": ".price",
                "link": "a",
                "base_url": "https://www.gurgencler.com.tr"
            }
        }


    def clean_price(self, price_str):
        if not price_str: return 0
        # Remove TL, symbols, extra text
        price_str = price_str.replace('TL', '').replace('₺', '').replace('–', '').strip()
        
        # Heuristic for cases like "Sepette 12.345,00 TL" - we want the first/main price
        # Extract the numeric part (handling TR format dots and commas)
        match = re.search(r'(\d[\d.,]*)', price_str)
        if not match: return 0
        price_str = match.group(1)
        
        # TR format: 12.345,67 or 12345,67 or 12.345
        if ',' in price_str:
            # Thousands separator can be anything, but comma is decimal
            price_str = price_str.replace('.', '').replace(',', '.')
        else:
            # If there is a dot but no comma, it's likely a thousands separator (62.599)
            # unless it's specifically a decimal (e.g. 62599.00)
            if '.' in price_str:
                parts = price_str.split('.')
                # If there are 3 digits after the dot, it's a thousands separator
                if len(parts[-1]) == 3 or len(parts) > 2:
                    price_str = price_str.replace('.', '')
            
        try:
            val = float(price_str)
            return val
        except:
            return 0

    def get_via_flaresolverr(self, url, return_solution=False):
        try:
            payload = {
                "cmd": "request.get",
                "url": url,
                "maxTimeout": 60000
            }
            response = requests.post(self.flaresolverr_url, json=payload, timeout=90)
            res_data = response.json()
            if res_data.get('status') == 'ok':
                if return_solution:
                    return res_data['solution']
                return res_data['solution']['response']
            return None
        except:
            return None

    def clean_search_query(self, product_name):
        clean = re.sub(r'\b(4G|5G)\b', '', product_name, flags=re.IGNORECASE)
        clean = clean.replace(' / ', ' ').replace('/', ' ')
        return clean.strip()

    def is_strict_match(self, product_name, item_title):
        name = product_name.lower()
        title = item_title.lower()
        
        # 1. Alphanumeric word extraction
        name_words = re.findall(r'\w+', name)
        
        # Brands & common words to exclude from the main match requirement
        brands = ['apple', 'samsung', 'xiaomi', 'huawei', 'oppo', 'vivo', 'realme', 'poco', 'google', 'oneplus', 'honor']
        common = ['the', 'and', 'cep', 'telefonu', 'akıllı', 'phone', 'smartphone', '4g', '5g', 'gb', 'ram', 'nfc', 'tb', 'rom', 'galaxy']
        
        important_words = [w for w in name_words if len(w) > 1 and w not in common and w not in brands]
        
        # We check that every important word in the product name is present in the title as a WHOLE word
        for w in important_words:
            if not re.search(rf'\b{re.escape(w)}\b', title):
                return False
            
        # 2. Avoid accessories, refurbished, and non-phone items
        bad_keywords = [
            "kılıf", "case", "cam", "protector", "adaptör", "şarj", "kablo", "kulaklık", "earbuds", 
            "watch", "saat", "askı", "zincir", "koruyucu", "kapak", "film", "çanta", "stand", 
            "lens", "kordon", "askısı", "başlığı", "outlet", "teşhir", "yenilenmiş", "ikinci el",
            "revizyonlu", "refurbished", "kullanılmış", "tamirli", "b kalite", "a kalite", "c kalite",
            "traş", "köpüğü", "parfüm", "bakım", "kozmetik", "oyuncak", "lego", "puzzle", "kutu", 
            "boş", "aksesuar", "yedek parça", "pil", "batarya", "ekran", "parça", "uyumlu", "for", "için"
        ]
        if any(k in title for k in bad_keywords) and not any(k in name for k in bad_keywords):
            return False
            
        # 3. Handle Pro/Max/Ultra variations strictly
        variations = ["pro", "max", "plus", "ultra", "lite", "fe", "mini", "se"]
        for var in variations:
            if var in title and var not in name:
                if var == 'max' and 'max' not in name: return False
                if var == 'pro' and 'pro' not in name: return False
                if var not in name: return False
            if var in name and var not in title:
                return False
        
        return True

    def clean_merchant_url(self, url):
        if not url: return ""
        
        import urllib.parse
        
        # 1. Try to extract direct URL from parameters (Aggregators often use 'u', 'url', 'link' etc)
        if "akakce.com" in url or "cimri.com" in url or "/z/?" in url:
            try:
                parsed = urllib.parse.urlparse(url)
                query_params = urllib.parse.parse_qs(parsed.query)
                
                # Check every parameter for something that looks like a URL
                for key, values in query_params.items():
                    for val in values:
                        if (val.startswith('http') or val.startswith('www.')) and "akakce.com" not in val and "cimri.com" not in val:
                            if val.startswith('www.'): val = 'https://' + val
                            # Success! Found a merchant URL in the parameters
                            url = val
                            break
                    else: continue
                    break
            except: pass
        
        # 2. Final cleanup: Remove tracking/affiliate params if it's a known store
        # But only if we successfully moved away from the aggregator domain
        if "akakce.com" not in url and "cimri.com" not in url:
            # Common tracking params
            tracking_params = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'gclid', 'qbit']
            try:
                parsed = urllib.parse.urlparse(url)
                qs = urllib.parse.parse_qs(parsed.query)
                new_qs = {k: v for k, v in qs.items() if k.lower() not in tracking_params}
                
                clean_url = urllib.parse.urlunparse((
                    parsed.scheme, parsed.netloc, parsed.path, 
                    parsed.params, urllib.parse.urlencode(new_qs, doseq=True), 
                    parsed.fragment
                ))
                # If URL ends with ?, remove it
                return clean_url.rstrip('?')
            except:
                return url.split('?')[0].split('&')[0]
        
        return url

    def get_akakce_price(self, product_name):
        search_name = self.clean_search_query(product_name)
        logging.info(f"🔍 Searching Akakçe for: {search_name} (Original: {product_name})")
        url = f"https://www.akakce.com/arama/?q={quote_plus(search_name)}"
        solution = self.get_via_flaresolverr(url, return_solution=True)
        if not solution: return None
        
        html = solution['response']
        final_url = solution['url']
        soup = BeautifulSoup(html, 'html.parser')
        
        # If we were redirected directly to a product detail page (e.g. exact match on Akakçe)
        if "arama" not in final_url and ".html" in final_url:
            logging.info(f"⚡ Redirected directly to product detail page: {final_url}")
            detail_soup = soup
        else:
            # Get the first matching product link (Updated class check to match 'v-8' structure)
            items = soup.select('li.w, li.v-8, li[class*="v-8"]')
            product_url = None
            for item in items:
                title_el = item.select_one('h3, .pn_v8')
                link_el = item.select_one('a')
                if title_el and link_el and self.is_strict_match(product_name, title_el.get_text()):
                    product_url = link_el.get('href', '')
                    if not product_url.startswith('http'): product_url = "https://www.akakce.com" + product_url
                    break
            
            if not product_url: return None
            
            # Now visit the product detail page to get actual merchants
            logging.info(f"📄 Visiting Akakçe Detail: {product_url}")
            detail_html = self.get_via_flaresolverr(product_url)
            if not detail_html: return None
            detail_soup = BeautifulSoup(detail_html, 'html.parser')
            
        results = []
        
        # EXTRACT FROM JSON-LD (THE GOLD MINE) - Using get_text() instead of .string which returns None
        for script in detail_soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.get_text())
                
                def extract_offers(obj):
                    if not isinstance(obj, dict): return
                    
                    offers_data = obj.get('offers')
                    if isinstance(offers_data, dict):
                        nested_offers = offers_data.get('offers', [])
                        if not isinstance(nested_offers, list): nested_offers = [nested_offers]
                        
                        for off in nested_offers:
                            m_url = off.get('url', '')
                            m_price = off.get('price')
                            m_seller = off.get('seller', {}).get('name', 'Mağaza')
                            
                            if m_url and m_price:
                                clean_url = self.clean_merchant_url(m_url)
                                logging.info(f"    🔗 Found URL: {clean_url[:50]}... from {m_seller}")
                                results.append({
                                    "merchant": m_seller.split('/')[0].strip(),
                                    "price": float(m_price),
                                    "url": clean_url
                                })
                    
                    # Recursively look in variants
                    variants = obj.get('hasVariant', [])
                    if not isinstance(variants, list): variants = [variants]
                    for var in variants: extract_offers(var)

                extract_offers(data)
            except Exception as e: 
                logging.error(f"    ⚠️ JSON-LD Parse Error: {str(e)}")
                continue

        # Fallback to HTML parsing if JSON-LD extraction failed or yielded nothing
        if not results:
            for a in detail_soup.select('a[rel="nofollow"]'):
                container = a
                price = 0
                merchant_name = "Mağaza"
                for _ in range(4):
                    container = container.parent
                    if not container: break
                    txt = container.get_text()
                    if 'TL' in txt or '₺' in txt:
                        price = self.clean_price(txt)
                        if price > 5000:
                            img = container.select_one('img[alt]')
                            if img: merchant_name = img.get('alt').strip()
                            break
                if price > 5000:
                    link = a.get('href', '')
                    if not link.startswith('http'): link = "https://www.akakce.com" + link
                    results.append({
                        "merchant": merchant_name, 
                        "price": price, 
                        "url": self.clean_merchant_url(link)
                    })
        
        # Filter and De-duplicate
        if results:
            final_agg = []
            seen = set()
            for r in results:
                # Keep only strict matches for safety
                key = f"{r['merchant']}-{r['price']}"
                if key not in seen:
                    seen.add(key)
                    final_agg.append(r)
            return final_agg
        
        return None

    def get_cimri_price(self, product_name):
        search_name = self.clean_search_query(product_name)
        logging.info(f"🔍 Searching Cimri for: {search_name} (Original: {product_name})")
        url = f"https://www.cimri.com/arama?q={quote_plus(search_name)}"
        solution = self.get_via_flaresolverr(url, return_solution=True)
        if not solution: return None
        
        html = solution['response']
        final_url = solution['url']
        soup = BeautifulSoup(html, 'html.parser')
        
        # If we were redirected directly to a product detail page (exact match on Cimri)
        if "arama" not in final_url and ("/fiyatlar" in final_url or ",a" in final_url or "," in final_url):
            logging.info(f"⚡ Redirected directly to Cimri product page: {final_url}")
            detail_soup = soup
        else:
            items = soup.select('article, .product-card, [class*="product-card"]')
            product_url = None
            for item in items:
                title_el = item.select_one('h3, [class*="title"]')
                link_el = item.select_one('a')
                if title_el and link_el and self.is_strict_match(product_name, title_el.get_text()):
                    product_url = link_el.get('href', '')
                    if not product_url.startswith('http'): product_url = "https://www.cimri.com" + product_url
                    break
            
            if not product_url: return None
            
            logging.info(f"📄 Visiting Cimri Detail: {product_url}")
            detail_html = self.get_via_flaresolverr(product_url)
            if not detail_html: return None
            detail_soup = BeautifulSoup(detail_html, 'html.parser')
            
        # Parse offers from __OCTOPUS_DATA__ script
        data_script = detail_soup.find('script', id='__OCTOPUS_DATA__')
        if not data_script: return None
        
        try:
            data = json.loads(data_script.get_text())
            offers = []
            def find_offers(obj):
                if isinstance(obj, dict):
                    if 'offers' in obj and isinstance(obj['offers'], list):
                        offers.extend(obj['offers'])
                    for v in obj.values(): find_offers(v)
                elif isinstance(obj, list):
                    for item in obj: find_offers(item)
            find_offers(data)
            
            results = []
            for off in offers:
                merchant_name = off.get('merchant', {}).get('name', 'Mağaza')
                price = off.get('price')
                offer_id = off.get('id')
                if price and offer_id:
                    results.append({
                        "merchant": merchant_name,
                        "price": float(price),
                        "url": f"https://www.cimri.com/offer/{offer_id}"
                    })
            
            # Filter and De-duplicate
            if results:
                final_agg = []
                seen = set()
                for r in results:
                    key = f"{r['merchant']}-{r['price']}"
                    if key not in seen:
                        seen.add(key)
                        final_agg.append(r)
                final_agg.sort(key=lambda x: x['price'])
                return final_agg
            return None
        except Exception as e:
            logging.error(f"    ⚠️ Cimri JSON Parse Error: {str(e)}")
            return None

    async def scrape_site_async(self, site_name, config, search_name, product_name, semaphore):
        async with semaphore:
            url = config['url'].format(query=quote_plus(search_name))
            loop = asyncio.get_running_loop()
            try:
                # Run the blocking network request in a thread pool executor
                html = await loop.run_in_executor(None, self.get_via_flaresolverr, url)
                if not html: return None
                
                soup = BeautifulSoup(html, 'html.parser')
                offers = []
                items = soup.select(config['container'])
                for item in items:
                    title_el = item.select_one(config['title'])
                    price_el = item.select_one(config['price'])
                    link_el = item if item.name == 'a' else item.find('a') if config['link'] == 'self' else item.select_one(config['link'])
                    
                    if title_el and price_el and link_el:
                        title = title_el.get_text().strip()
                        price = self.clean_price(price_el.get_text())
                        link = link_el.get('href', '')
                        if not link.startswith('http'): link = config['base_url'] + link
                        
                        if price > 5000 and self.is_strict_match(product_name, title):
                            offers.append({"merchant": site_name, "price": price, "url": self.clean_merchant_url(link)})
                
                if offers:
                    return min(offers, key=lambda x: x['price'])
            except:
                pass
            return None

    async def get_best_prices(self, product_name):
        results = []
        
        # 1. Try Akakçe
        results = self.get_akakce_price(product_name)
        if results:
            logging.info(f"  ✨ Found {len(results)} offers on Akakçe")
            
        # 2. Specific sites fallback if Akakçe failed (Our own direct scraper engine)
        if not results:
            search_name = self.clean_search_query(product_name)
            results = [] 
            logging.warning(f"  ⚠️ Akakçe found nothing. Falling back to multi-site direct search engine for {search_name}...")
            
            # Safe concurrency of 3 tasks in parallel to prevent FlareSolverr crashes
            sem = asyncio.Semaphore(3)
            tasks = []
            for site_name, config in self.site_configs.items():
                tasks.append(self.scrape_site_async(site_name, config, search_name, product_name, sem))
            
            scraped_results = await asyncio.gather(*tasks)
            for r in scraped_results:
                if r:
                    results.append(r)
                    logging.info(f"  ✅ {r['merchant']}: {r['price']} ₺")

        # De-duplicate: ONLY ONE LOWEST PRICE PER MERCHANT
        merchant_best = {}
        for r in results:
            m_name = r['merchant']
            if m_name not in merchant_best or r['price'] < merchant_best[m_name]['price']:
                merchant_best[m_name] = r
        
        unique_results = list(merchant_best.values())
        return sorted(unique_results, key=lambda x: x['price'])

if __name__ == "__main__":
    import asyncio
    scraper = TRPriceScraper()
    res = asyncio.run(scraper.get_best_prices("Samsung Galaxy S24"))
    logging.info(json.dumps(res, indent=2))
