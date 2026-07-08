import logging
import requests
import re
import json
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, urlparse
import time
import asyncio
from functools import partial

class TRPriceScraper:
    # After this many consecutive fetch failures a domain is skipped for the rest of the run
    DOMAIN_FAILURE_THRESHOLD = 3

    def __init__(self):
        self.flaresolverr_url = "http://localhost:8191/v1"
        self.domain_failures = {}
        self.dead_domains = set()
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
        self.proxies = self._fetch_proxies()
        logging.info(f"🌐 Loaded {len(self.proxies)} proxies for rotation")

    def _fetch_proxies(self):
        """Fetch and validate free proxies from public APIs."""
        proxy_urls = [
            "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=protocolipport&format=text&timeout=5000&protocol=http",
            "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt",
        ]
        raw_proxies = []
        for api_url in proxy_urls:
            try:
                resp = requests.get(api_url, timeout=10)
                if resp.status_code == 200:
                    for line in resp.text.strip().split('\n'):
                        line = line.strip()
                        if line and ':' in line:
                            # Normalize to ip:port format
                            clean = line.replace('http://', '').replace('https://', '').split('/')[0]
                            if re.match(r'^\d+\.\d+\.\d+\.\d+:\d+$', clean):
                                raw_proxies.append(clean)
            except Exception:
                continue
        
        if not raw_proxies:
            logging.warning("  ⚠️ No proxies fetched from any source")
            return []
        
        logging.info(f"  📋 Fetched {len(raw_proxies)} raw proxies, validating...")
        
        # Quick validation in parallel using thread pool to make it fast
        validated = []
        import random
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        random.shuffle(raw_proxies)
        test_proxies = raw_proxies[:50]  # Test a subset of 50
        
        def test_proxy(proxy_str):
            try:
                resp = requests.get(
                    "https://httpbin.org/ip", 
                    proxies={"http": f"http://{proxy_str}", "https": f"http://{proxy_str}"},
                    timeout=3
                )
                if resp.status_code == 200:
                    return proxy_str
            except Exception:
                pass
            return None

        logging.info(f"  ⚡ Validating {len(test_proxies)} proxies in parallel...")
        with ThreadPoolExecutor(max_workers=15) as executor:
            futures = {executor.submit(test_proxy, p): p for p in test_proxies}
            for fut in as_completed(futures):
                res = fut.result()
                if res:
                    validated.append(res)
                    if len(validated) >= 5:  # 5 working proxies is enough
                        break
        
        logging.info(f"  ✅ Validated {len(validated)} working proxies")
        return validated

    def _try_curl_cffi(self, url):
        """Fast TLS-impersonation fetch with optional proxy rotation. Returns (html, final_url) or None."""
        try:
            from curl_cffi import requests as cffi_requests
        except ImportError:
            logging.warning("  ⚠️ curl_cffi not installed, skipping TLS impersonation")
            return None
        
        headers = {"Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7"}
        
        # Try without proxy first (direct)
        for browser in ["chrome", "safari"]:
            try:
                resp = cffi_requests.get(url, impersonate=browser, timeout=15, headers=headers)
                logging.warning(f"  🔍 curl_cffi ({browser}): HTTP {resp.status_code} for {url} ({len(resp.text)} bytes)")
                if resp.status_code == 200 and len(resp.text) > 1000:
                    if 'Just a moment...' not in resp.text and 'cf-browser-verification' not in resp.text:
                        return resp.text, str(resp.url)
            except Exception as e:
                logging.warning(f"  ⚠️ curl_cffi ({browser}) error for {url}: {e}")
        
        # Try with proxies (cap attempts — free proxies rarely beat Cloudflare, don't burn minutes on them)
        for proxy_str in self.proxies[:3]:
            try:
                resp = cffi_requests.get(
                    url, impersonate="chrome", timeout=10, headers=headers,
                    proxies={"http": f"http://{proxy_str}", "https": f"http://{proxy_str}"}
                )
                if resp.status_code == 200 and len(resp.text) > 1000:
                    if 'Just a moment...' not in resp.text and 'cf-browser-verification' not in resp.text:
                        logging.info(f"  ✅ curl_cffi via proxy {proxy_str} succeeded for {url}")
                        return resp.text, str(resp.url)
            except Exception:
                continue
        
        return None

    def clean_price(self, price_str):
        if not price_str: return 0
        price_str = price_str.replace('TL', '').replace('₺', '').replace('–', '').strip()
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



    def _record_domain_result(self, domain, success):
        if success:
            self.domain_failures[domain] = 0
            return
        count = self.domain_failures.get(domain, 0) + 1
        self.domain_failures[domain] = count
        if count >= self.DOMAIN_FAILURE_THRESHOLD:
            self.dead_domains.add(domain)
            logging.warning(f"  ⛔ {domain} failed {count} times in a row — skipping it for the rest of this run")

    def get_via_flaresolverr(self, url, return_solution=False, max_retries=3):
        domain = urlparse(url).netloc
        if domain in self.dead_domains:
            return None

        # Fast path: TLS impersonation (no headless browser needed)
        result = self._try_curl_cffi(url)
        if result:
            html, final_url = result
            logging.info(f"  ⚡ curl_cffi succeeded for {url}")
            self._record_domain_result(domain, True)
            if return_solution:
                return {'response': html, 'url': final_url}
            return html

        # Slow path: FlareSolverr headless browser
        for attempt in range(1, max_retries + 1):
            try:
                payload = {
                    "cmd": "request.get",
                    "url": url,
                    "maxTimeout": 20000
                }
                response = requests.post(self.flaresolverr_url, json=payload, timeout=30)
                res_data = response.json()
                if res_data.get('status') == 'ok':
                    self._record_domain_result(domain, True)
                    if return_solution:
                        return res_data['solution']
                    return res_data['solution']['response']
                msg = res_data.get('message', '')
                # Detect IP bans — retrying won't help
                if 'banned' in msg.lower() or 'blocked' in msg.lower():
                    logging.warning(f"  🚫 IP banned/blocked for {url}, skipping retries")
                    self._record_domain_result(domain, False)
                    return None
                logging.warning(f"  ⚠️ FlareSolverr attempt {attempt}/{max_retries} failed for {url}: {msg[:120]}")
            except Exception as e:
                logging.warning(f"  ⚠️ FlareSolverr attempt {attempt}/{max_retries} exception for {url}: {e}")
            if attempt < max_retries:
                wait = 3
                logging.info(f"  ⏳ Retrying in {wait}s...")
                time.sleep(wait)
        logging.error(f"  ❌ FlareSolverr failed after {max_retries} attempts for {url}")
        self._record_domain_result(domain, False)
        return None

    def clean_search_query(self, product_name):
        clean = re.sub(r'\b(4G|5G)\b', '', product_name, flags=re.IGNORECASE)
        clean = clean.replace(' / ', ' ').replace('/', ' ')
        return clean.strip()

    def is_strict_match(self, product_name, item_title):
        # Standardize + to plus for suffix variation check (e.g. Pro+ vs Pro)
        name = product_name.lower().replace('+', 'plus')
        title = item_title.lower().replace('+', 'plus')
        
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
        if "akakce.com" in url or "/z/?" in url:
            try:
                parsed = urllib.parse.urlparse(url)
                query_params = urllib.parse.parse_qs(parsed.query)
                
                # Check every parameter for something that looks like a URL
                for key, values in query_params.items():
                    for val in values:
                        if (val.startswith('http') or val.startswith('www.')) and "akakce.com" not in val:
                            if val.startswith('www.'): val = 'https://' + val
                            # Success! Found a merchant URL in the parameters
                            url = val
                            break
                    else: continue
                    break
            except: pass
        
        # 2. Final cleanup: Remove tracking/affiliate params if it's a known store
        # But only if we successfully moved away from the aggregator domain
        if "akakce.com" not in url:
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

    def get_google_shopping_price(self, product_name):
        """Scrape Google Shopping Turkey for multi-merchant price comparison via FlareSolverr."""
        search_name = self.clean_search_query(product_name)
        url = f"https://www.google.com.tr/search?q={quote_plus(search_name)}+fiyat&tbm=shop&gl=tr&hl=tr"
        logging.info(f"🛒 Searching Google Shopping for: {search_name}")
        
        # Google Shopping is JS-rendered — curl_cffi returns empty shells.
        # Call FlareSolverr directly (Google doesn't use Cloudflare, so it should work).
        try:
            payload = {"cmd": "request.get", "url": url, "maxTimeout": 20000}
            response = requests.post(self.flaresolverr_url, json=payload, timeout=30)
            res_data = response.json()
            if res_data.get('status') == 'ok':
                html = res_data['solution']['response']
            else:
                logging.warning(f"  ⚠️ Google Shopping FlareSolverr: {res_data.get('message', 'unknown')}")
                return None
        except Exception as e:
            logging.warning(f"  ⚠️ Google Shopping FlareSolverr error: {e}")
            return None
        
        soup = BeautifulSoup(html, 'html.parser')
        results = []
        
        # Google Shopping product cards — multiple possible selectors as Google changes them
        card_selectors = [
            'g-inner-card',
            '.sh-dgr__content',
            '.sh-dlr__list-result', 
            '[data-docid]',
            '.sh-pr__product-results-grid .sh-pr__product-result',
        ]
        
        cards = []
        for sel in card_selectors:
            cards = soup.select(sel)
            if cards:
                logging.info(f"  📦 Found {len(cards)} cards with selector: {sel}")
                break
        
        for card in cards:
            try:
                # Target the main inner block if it's a g-inner-card with hover clones
                wrapper = card.select_one('.rRCm8') or card
                
                # Title
                title_el = wrapper.select_one('.gkQHve, h3, .tAxDx, [role="heading"], .EI11Pd')
                title = title_el.get_text().strip() if title_el else ""
                
                if title and not self.is_strict_match(product_name, title):
                    continue
                
                # Price
                price_el = wrapper.select_one('.lmQWe, .a8Pemb, .HRLxBb')
                price_text = price_el.get_text().strip() if price_el else ""
                price_val = self.clean_price(price_text)
                if price_val < 1000:
                    continue
                
                # Merchant
                merchant_el = wrapper.select_one('.WJMUdc, .aULzUe, .IuHnof, .E5ocAb')
                merchant = merchant_el.get_text().strip() if merchant_el else "Mağaza"
                merchant = re.sub(r'\.com(\.tr)?$', '', merchant).strip()
                
                # Link
                link_el = wrapper.select_one('a[href*="/shopping/"], a[href*="url?"]')
                if not link_el:
                    link_el = wrapper.select_one('a[href]') or card.select_one('a[href]')
                link = link_el.get('href', '') if link_el else ""
                if link and not link.startswith('http'):
                    link = "https://www.google.com.tr" + link
                
                results.append({
                    "merchant": merchant,
                    "price": price_val,
                    "url": link
                })
            except Exception:
                continue
        
        # Broader fallback: find ANY element with price in the page
        if not results:
            import re as re_mod
            body_text = soup.get_text()
            price_matches = re_mod.findall(r'([\d]+\.[\d]{3}(?:,[\d]+)?)\s*(?:₺|TL)', body_text)
            if price_matches:
                logging.info(f"  📦 Found {len(price_matches)} prices in body text (using fallback)")
                for pm in price_matches[:10]:
                    price_val = self.clean_price(pm)
                    if price_val >= 1000:
                        results.append({
                            "merchant": "Google Shopping",
                            "price": price_val,
                            "url": url
                        })
        
        if results:
            seen = {}
            for r in results:
                key = r['merchant']
                if key not in seen or r['price'] < seen[key]['price']:
                    seen[key] = r
            final = sorted(seen.values(), key=lambda x: x['price'])
            logging.info(f"  🛒 Google Shopping found {len(final)} merchants")
            return final
        
        logging.warning(f"  ⚠️ Google Shopping: No matching results for {product_name}")
        return None

    def get_epey_price(self, product_name):
        import urllib.parse
        search_name = self.clean_search_query(product_name)
        logging.info(f"🔍 Searching Epey for: {search_name} (Original: {product_name})")
        
        detail_soup = None
        
        # Strategy 1: Try direct product URL (lighter Cloudflare protection than search pages)
        slug = re.sub(r'[^a-z0-9]+', '-', search_name.lower()).strip('-')
        direct_url = f"https://www.epey.com/akilli-telefonlar/{slug}.html"
        logging.info(f"  📎 Trying direct Epey URL: {direct_url}")
        direct_html = self.get_via_flaresolverr(direct_url, max_retries=1)
        if direct_html:
            soup = BeautifulSoup(direct_html, 'html.parser')
            # Verify it's a real product page (has price links)
            if soup.select('a.git'):
                logging.info(f"  ✅ Direct URL worked for Epey!")
                detail_soup = soup
            else:
                logging.info(f"  ⚠️ Direct URL returned a page but no price data (likely 404/redirect)")
        
        # Strategy 2: Fall back to search page
        if not detail_soup:
            url = f"https://www.epey.com/ara/?ara={quote_plus(search_name)}"
            solution = self.get_via_flaresolverr(url, return_solution=True, max_retries=1)
            if not solution:
                logging.warning(f"  ⚠️ Epey: Both direct URL and search failed for {product_name}")
                return None
            
            html = solution['response']
            final_url = solution['url']
            soup = BeautifulSoup(html, 'html.parser')
            
            # If we were redirected directly to a product page
            if "ara" not in final_url and ".html" in final_url:
                logging.info(f"⚡ Redirected directly to Epey product page: {final_url}")
                detail_soup = soup
            else:
                # Search results page, find first match
                product_url = None
                for a in soup.find_all('a', href=True):
                    href = a['href']
                    if '/akilli-telefonlar/' in href and href.endswith('.html'):
                        title = a.get('title', '') or a.get_text().strip()
                        if title and self.is_strict_match(product_name, title):
                            product_url = href
                            if not product_url.startswith('http'): 
                                product_url = "https://www.epey.com" + product_url
                            break
                
                if not product_url: 
                    return None
                    
                logging.info(f"📄 Visiting Epey Detail: {product_url}")
                detail_html = self.get_via_flaresolverr(product_url)
                if not detail_html: return None
                detail_soup = BeautifulSoup(detail_html, 'html.parser')
            
        results = []
        git_links = detail_soup.select('a.git')
        
        for a in git_links:
            encoded_link = a.get('data-link', '')
            if not encoded_link: continue
            
            direct_url = urllib.parse.unquote(encoded_link)
            
            price_el = a.select_one('.urun_fiyat')
            price_text = price_el.get_text().strip() if price_el else ""
            
            price_val = 0
            if price_text:
                price_part = price_text.split(' ')[0].strip()
                price_str = price_part.replace('.', '').replace(',', '.')
                try:
                    price_val = float(price_str)
                except:
                    pass
                    
            if price_val == 0: continue
            
            title_attr = a.get('title', '')
            merchant = "Mağaza"
            if title_attr:
                merchant = title_attr.split(' ')[0].strip()
                
            results.append({
                "merchant": merchant,
                "price": price_val,
                "url": direct_url
            })
            
        if results:
            final_agg = []
            seen = set()
            for r in results:
                # Keep only lowest price per merchant
                key = f"{r['merchant']}"
                if key not in seen:
                    seen.add(key)
                    final_agg.append(r)
            final_agg.sort(key=lambda x: x['price'])
            return final_agg
            
        return None

    async def scrape_site_async(self, site_name, config, search_name, product_name, semaphore):
        async with semaphore:
            url = config['url'].format(query=quote_plus(search_name))
            loop = asyncio.get_running_loop()
            try:
                # Run the blocking network request in a thread pool executor.
                # Single FlareSolverr attempt: challenge timeouts almost never
                # succeed on retry from the same CI IP, and retries here
                # multiply across ~10 sites x every product.
                html = await loop.run_in_executor(
                    None, partial(self.get_via_flaresolverr, url, False, 1)
                )
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
        
        # 1. Try Google Shopping (no Cloudflare, most reliable from CI)
        results = self.get_google_shopping_price(product_name)
        if results:
            logging.info(f"  ✨ Found {len(results)} offers on Google Shopping")

        # 2. Try Epey.com as fallback
        if not results:
            logging.info(f"  🔄 Google Shopping found nothing, trying Epey...")
            results = self.get_epey_price(product_name)
            if results:
                logging.info(f"  ✨ Found {len(results)} offers on Epey")
            
        # 3. Try Akakçe as fallback aggregator
        if not results:
            logging.info(f"  🔄 Epey failed, trying Akakçe...")
            results = self.get_akakce_price(product_name)
            if results:
                logging.info(f"  ✨ Found {len(results)} offers on Akakçe")

        # 3. Specific sites fallback if aggregators failed (Our own direct scraper engine)
        if not results:
            search_name = self.clean_search_query(product_name)
            results = [] 
            logging.warning(f"  ⚠️ All aggregators found nothing. Falling back to multi-site direct search engine for {search_name}...")
            
            # Priority order: datacenter-friendly sites first, then others
            priority_sites = ['Hepsiburada', 'PttAVM', 'n11', 'Trendyol', 'Amazon TR', 'Vatan Bilgisayar', 'MediaMarkt', 'Pasaj', 'Pazarama', 'Gürgençler']
            sem = asyncio.Semaphore(3)
            tasks = []
            for site_name in priority_sites:
                config = self.site_configs.get(site_name)
                if config:
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
            # Standardize merchant names
            if 'hepsiburada' in m_name.lower(): m_name = 'Hepsiburada'
            elif 'trendyol' in m_name.lower(): m_name = 'Trendyol'
            elif 'amazon' in m_name.lower(): m_name = 'Amazon TR'
            elif 'n11' in m_name.lower(): m_name = 'n11'
            elif 'ptt' in m_name.lower(): m_name = 'PttAVM'
            elif 'pasaj' in m_name.lower(): m_name = 'Pasaj'
            elif 'pazarama' in m_name.lower(): m_name = 'Pazarama'
            elif 'vatan' in m_name.lower(): m_name = 'Vatan Bilgisayar'
            elif 'mediamarkt' in m_name.lower(): m_name = 'MediaMarkt'
            elif 'teknosa' in m_name.lower(): m_name = 'Teknosa'
            
            r['merchant'] = m_name
            if m_name not in merchant_best or r['price'] < merchant_best[m_name]['price']:
                merchant_best[m_name] = r
        
        unique_results = list(merchant_best.values())
        return sorted(unique_results, key=lambda x: x['price'])

if __name__ == "__main__":
    import asyncio
    scraper = TRPriceScraper()
    res = asyncio.run(scraper.get_best_prices("Samsung Galaxy S24"))
    logging.info(json.dumps(res, indent=2))
