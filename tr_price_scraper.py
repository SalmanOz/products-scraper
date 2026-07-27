import logging
import requests
import re
import json
from bs4 import BeautifulSoup
from urllib.parse import quote, quote_plus, urlparse
import time
import asyncio
import unicodedata
from functools import partial

class TRPriceScraper:
    # After this many consecutive fetch failures a domain is skipped for the rest of the run
    DOMAIN_FAILURE_THRESHOLD = 3

    # Only offers from these known Turkish retailers are trusted. Google Shopping
    # lists countless gray-import/dropship sellers ("Wireless Source" etc.) whose
    # prices are unreliable and would corrupt base_price (min of all offers).
    TRUSTED_MERCHANTS = [
        'hepsiburada', 'trendyol', 'amazon', 'n11', 'pttavm', 'ptt avm',
        'mediamarkt', 'media markt', 'teknosa', 'vatan', 'pasaj', 'turkcell',
        'pazarama', 'gürgençler', 'gurgencler', 'idefix', 'vodafone',
        'türk telekom', 'turk telekom', 'apple', 'samsung', 'xiaomi', 'mi store',
        'd&r', 'boyner', 'a101', 'migros', 'carrefoursa', 'gittigidiyor', 'çiçeksepeti', 'ciceksepeti',
    ]

    # Brand-only names are accepted only in these explicit official forms.
    # Treating "apple" or "samsung" as a substring would also trust unrelated
    # marketplace sellers such as "Apple Sepeti" or "Samsung Cep Dünyası".
    OFFICIAL_BRAND_MERCHANT_NAMES = {
        'apple',
        'apple online store',
        'apple resmi mağazası',
        'apple store',
        'apple türkiye',
        'mi store',
        'mi store türkiye',
        'samsung',
        'samsung resmi mağazası',
        'samsung shop',
        'samsung store',
        'samsung türkiye',
        'xiaomi',
        'xiaomi resmi mağazası',
        'xiaomi store',
        'xiaomi türkiye',
    }
    BRAND_ONLY_MERCHANT_ALIASES = {'apple', 'samsung', 'xiaomi', 'mi store'}
    CANONICAL_MERCHANT_NAMES = {
        'hepsiburada': 'Hepsiburada',
        'trendyol': 'Trendyol',
        'amazon': 'Amazon TR',
        'amazon tr': 'Amazon TR',
        'n11': 'n11',
        'pttavm': 'PttAVM',
        'ptt avm': 'PttAVM',
        'mediamarkt': 'MediaMarkt',
        'media markt': 'MediaMarkt',
        'teknosa': 'Teknosa',
        'vatan': 'Vatan Bilgisayar',
        'vatan bilgisayar': 'Vatan Bilgisayar',
        'pasaj': 'Pasaj',
        'turkcell pasaj': 'Pasaj',
        'turkcell': 'Turkcell',
        'pazarama': 'Pazarama',
        'gurgencler': 'Gürgençler',
        'idefix': 'idefix',
        'vodafone': 'Vodafone',
        'turk telekom': 'Türk Telekom',
        'apple': 'Apple Store',
        'apple online store': 'Apple Store',
        'apple store': 'Apple Store',
        'apple turkiye': 'Apple Store',
        'samsung': 'Samsung',
        'samsung shop': 'Samsung',
        'samsung store': 'Samsung',
        'samsung turkiye': 'Samsung',
        'xiaomi': 'Xiaomi',
        'xiaomi store': 'Xiaomi',
        'xiaomi turkiye': 'Xiaomi',
        'mi store': 'Mi Store',
        'mi store turkiye': 'Mi Store',
        'd r': 'D&R',
        'boyner': 'Boyner',
        'a101': 'A101',
        'migros': 'Migros',
        'carrefoursa': 'CarrefourSA',
        'ciceksepeti': 'ÇiçekSepeti',
    }

    PHONE_BRAND_ALIASES = {
        'apple': {'apple', 'iphone'},
        'google': {'google', 'pixel'},
        'honor': {'honor'},
        'huawei': {'huawei'},
        'infinix': {'infinix'},
        'motorola': {'motorola'},
        'nothing': {'nothing'},
        'oneplus': {'oneplus'},
        'oppo': {'oppo'},
        'poco': {'poco'},
        'realme': {'realme'},
        'redmi': {'redmi'},
        'samsung': {'samsung', 'galaxy'},
        'tecno': {'tecno'},
        'vivo': {'vivo'},
        'xiaomi': {'xiaomi'},
    }

    @staticmethod
    def _normalize_words(value):
        folded = str(value or '').casefold().translate(
            str.maketrans({'ı': 'i'})
        )
        normalized = unicodedata.normalize('NFKD', folded)
        normalized = ''.join(
            char for char in normalized if not unicodedata.combining(char)
        )
        return ' '.join(re.findall(r'[a-z0-9]+', normalized))

    @classmethod
    def _canonical_merchant_name(cls, merchant):
        normalized = cls._normalize_words(merchant)
        suffixes = (
            ' resmi magazasi',
            ' magazasi',
            ' official store',
            ' com tr',
            ' com',
        )
        for suffix in suffixes:
            if normalized.endswith(suffix):
                normalized = normalized[:-len(suffix)].strip()
                break
        return cls.CANONICAL_MERCHANT_NAMES.get(normalized)

    @classmethod
    def _trusted_merchant_in_card(cls, card):
        """Read a retailer only from a standalone card element.

        Searching the whole card text for a trusted substring would turn a
        seller such as "Amazon Sahte" into "Amazon". Google cards normally
        render the merchant in its own span/div, so fail closed when that
        standalone identity is unavailable.
        """
        official_brand_names = {
            'Apple Store',
            'Samsung',
            'Xiaomi',
            'Mi Store',
        }
        for candidate in card.find_all(
            ['span', 'div', 'p', 'small'],
            recursive=True,
        ):
            canonical = cls._canonical_merchant_name(
                candidate.get_text(' ', strip=True)
            )
            if canonical and canonical not in official_brand_names:
                return canonical
        return None

    def is_trusted_merchant(self, merchant):
        return self._canonical_merchant_name(merchant) is not None

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
                "price": ".sale-price, .single-price, .price-section, .prc-box-dscntd, .p-card-price",
                "link": "a",
                "base_url": "https://www.trendyol.com"
            },
            "Amazon TR": {
                "url": "https://www.amazon.com.tr/s?k={query}",
                "container": "[data-component-type='s-search-result']",
                "title": "h2 span, h2",
                "price": ".a-price, .a-price-whole",
                "link": "a[href*='/dp/']",
                "base_url": "https://www.amazon.com.tr"
            },
            "Vatan Bilgisayar": {
                "url": "https://www.vatanbilgisayar.com/arama/{query}/",
                "container": ".product-list--item",
                "title": ".product-list__product-name",
                "price": ".product-list__price",
                "link": "a.product-list-link",
                "base_url": "https://www.vatanbilgisayar.com",
                "query_encoding": "path"
            },
            "n11": {
                "url": "https://www.n11.com/arama?q={query}",
                "container": "a.product-item, .product-item",
                "title": ".product-item-title, .product-name",
                "price": ".price-currency, .newPrice, .price",
                "link": "self",
                "base_url": "https://www.n11.com"
            },
            "PttAVM": {
                "url": "https://www.pttavm.com/arama?q={query}",
                "container": "article[class^='article__'], .product-list-card",
                "title": "h2[class^='name__'], .product-list-card__title",
                "price": "div[class^='price__'], .product-list-card__price-new",
                "link": "a[class^='card__'], a",
                "base_url": "https://www.pttavm.com"
            },
            "MediaMarkt": {
                "url": "https://www.mediamarkt.com.tr/tr/search.html?query={query}",
                "container": "[data-test='mms-product-card']",
                "title": "[data-test='product-title']",
                "price": "[data-test='mms-price'] span, [data-test='mms-price-display']",
                "link": "a",
                "base_url": "https://www.mediamarkt.com.tr"
            },
            "Pasaj": {
                "url": "https://www.turkcell.com.tr/pasaj/search?qx={query}",
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

    def _try_curl_cffi(self, url):
        """Fast TLS-impersonation fetch. Returns (html, final_url) or None."""
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

    @staticmethod
    def _spec_int(value):
        if value is None:
            return None
        match = re.search(r'\d+', str(value))
        return int(match.group(0)) if match else None

    @staticmethod
    def _extract_listing_capacities(item_title):
        """Return explicit RAM/storage values from a merchant title when present."""
        title = item_title.lower()

        pair = re.search(
            r'\b(\d{1,2})\s*(?:gb)?\s*/\s*(\d{2,4})\s*(gb|tb)\b',
            title,
        )
        if pair:
            storage = int(pair.group(2))
            if pair.group(3) == 'tb':
                storage *= 1024
            return int(pair.group(1)), storage

        ram = None
        ram_match = (
            re.search(r'\b(\d{1,2})\s*gb\s*(?:ram|bellek)\b', title)
            or re.search(r'\b(?:ram|bellek)\s*(\d{1,2})\s*gb\b', title)
        )
        if ram_match:
            ram = int(ram_match.group(1))

        capacities = []
        for amount, unit in re.findall(r'\b(\d{1,4})\s*(gb|tb)\b', title):
            value = int(amount) * (1024 if unit == 'tb' else 1)
            capacities.append(value)

        if ram is None:
            ram = next((value for value in capacities if value <= 24), None)
        storage = next((value for value in capacities if value >= 32), None)
        return ram, storage

    def is_strict_match(self, product_name, item_title, expected_specs=None):
        # Standardize + to plus for suffix variation check (e.g. Pro+ vs Pro)
        def normalize_product_text(value):
            value = str(value).replace('+', ' plus ')
            value = re.sub(r'\b([45])\s*g\b', r'\1g', value, flags=re.I)
            return self._normalize_words(value)

        name = normalize_product_text(product_name)
        title = normalize_product_text(item_title)
        
        # 1. Alphanumeric word extraction
        name_words = name.split()
        title_words = title.split()

        def detected_brands(words):
            word_set = set(words)
            return {
                brand
                for brand, aliases in self.PHONE_BRAND_ALIASES.items()
                if word_set & aliases
            }

        expected_brands = detected_brands(name_words)
        observed_brands = detected_brands(title_words)
        if expected_brands:
            if 'redmi' in expected_brands:
                required_brand = 'redmi'
                allowed_brands = {'redmi', 'xiaomi'}
            elif 'poco' in expected_brands:
                required_brand = 'poco'
                allowed_brands = {'poco', 'xiaomi'}
            else:
                required_brand = next(
                    brand
                    for brand in self.PHONE_BRAND_ALIASES
                    if brand in expected_brands
                )
                allowed_brands = {required_brand}
            if (
                required_brand not in observed_brands
                or observed_brands - allowed_brands
            ):
                return False

        def radio_variants(words):
            variants = set(words) & {'4g', '5g'}
            if 'lte' in words:
                variants.add('4g')
            return variants

        expected_radio = radio_variants(name_words)
        observed_radio = radio_variants(title_words)
        if (
            expected_radio
            and observed_radio
            and observed_radio != expected_radio
        ):
            return False
        
        # Brands & common words to exclude from the main match requirement
        brands = {
            alias
            for aliases in self.PHONE_BRAND_ALIASES.values()
            for alias in aliases
        }
        common = ['the', 'and', 'cep', 'telefonu', 'akilli', 'phone', 'smartphone', '4g', '5g', 'gb', 'ram', 'nfc', 'tb', 'rom', 'galaxy']
        
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
        normalized_bad_keywords = [
            self._normalize_words(keyword)
            for keyword in bad_keywords
        ]
        if (
            any(keyword in title for keyword in normalized_bad_keywords)
            and not any(keyword in name for keyword in normalized_bad_keywords)
        ):
            return False

        # 2.5. Category safety net: require SOME phone-brand marker or phone-category
        # word in the title. Without this, a title can pass step 1 purely by
        # coincidence — e.g. sunglasses SKU "MJ0439S-003-15T-58" contains "15T" as a
        # hyphen-delimited token and matches a "15T" search even though the title
        # never mentions a phone brand or "telefon" at all. Model-number tokens alone
        # are not sufficient proof this is the right product, let alone a phone.
        #
        # Deliberately checks for ANY known brand in the title, not only the
        # searched brand, because this helper is also used by maintenance scripts
        # that can supply a shortened product name.
        phone_category_words = ['telefon', 'akilli telefon', 'smartphone', 'cep telefonu', 'gsm']
        if not observed_brands and not any(c in title for c in phone_category_words):
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

        # 4. A product row represents a concrete RAM/storage variant. When a
        # merchant title states a different capacity, reject it instead of
        # attaching (for example) an 8/256 offer to a 4/128 product.
        expected_specs = expected_specs or {}
        expected_ram = self._spec_int(expected_specs.get('ram_gb'))
        expected_storage = self._spec_int(expected_specs.get('storage_gb'))
        listing_ram, listing_storage = self._extract_listing_capacities(item_title)
        if expected_ram and listing_ram and expected_ram != listing_ram:
            return False
        if expected_storage and listing_storage and expected_storage != listing_storage:
            return False

        return True

    @staticmethod
    def _build_search_url(config, search_name):
        if config.get('query_encoding') == 'path':
            encoded_query = quote(search_name, safe='')
        else:
            encoded_query = quote_plus(search_name)
        return config['url'].format(query=encoded_query)

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

    def get_akakce_price(self, product_name, expected_specs=None):
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
                if title_el and link_el and self.is_strict_match(
                    product_name, title_el.get_text(), expected_specs
                ):
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
        
        # Trust filter: Akakçe aggregates the same gray-import/dropship sellers Google
        # Shopping does (see TRUSTED_MERCHANTS), but unlike get_google_shopping_price
        # this path never checked is_trusted_merchant() — it only relied on
        # is_strict_match() for product matching, which says nothing about seller
        # reliability. That gap matters more now that get_best_prices() falls through
        # to Akakçe whenever Google Shopping's own coverage is thin.
        untrusted = [r for r in results if not self.is_trusted_merchant(r['merchant'])]
        for u in untrusted:
            logging.info(f"  🚷 Skipping untrusted Akakçe seller: {u['merchant']} ({u['price']} TL)")
        results = [r for r in results if self.is_trusted_merchant(r['merchant'])]

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

    GOOGLE_SHOPPING_DOMAIN = 'google-shopping'

    # Google-internal anchors that must never be stored as a merchant URL
    # (the old code grabbed the first <a> and shipped users to Google's
    # "how Shopping works" help page: support.google.com/googleshopping/answer/9128904)
    GOOGLE_INTERNAL_HOSTS = ('support.google.com', 'policies.google.com', 'accounts.google.com', 'myactivity.google.com')

    def _extract_merchant_link(self, card, merchant=None):
        """Return the best outbound merchant URL from a shopping card, or ''.

        Priority: direct external link matching the merchant > any external
        link > decoded /url? redirect > Google Shopping product page.
        Google help/policy/account anchors are always rejected.
        """
        import urllib.parse

        external, redirects, product_pages = [], [], []
        for a in card.find_all('a', href=True):
            href = a['href']
            if href.startswith('javascript:') or href.startswith('#'):
                continue
            if href.startswith('/url?') or 'google.com/url?' in href:
                # Decode Google redirect to the real destination
                parsed = urllib.parse.urlparse(href if href.startswith('http') else 'https://www.google.com' + href)
                qs = urllib.parse.parse_qs(parsed.query)
                for key in ('q', 'url', 'adurl'):
                    for val in qs.get(key, []):
                        if val.startswith('http') and 'google.com' not in urllib.parse.urlparse(val).netloc:
                            redirects.append(val)
                continue
            if href.startswith('http'):
                host = urllib.parse.urlparse(href).netloc.lower()
                if any(h in host for h in self.GOOGLE_INTERNAL_HOSTS):
                    continue
                if 'google.com' in host or 'google.com.tr' in host:
                    if '/shopping/product' in href:
                        product_pages.append(href)
                    continue
                external.append(href)
            elif href.startswith('/shopping/product'):
                product_pages.append('https://www.google.com' + href)

        candidates = redirects + external
        if merchant:
            m = merchant.lower().replace(' ', '')
            for url in candidates:
                host = urllib.parse.urlparse(url).netloc.lower().replace('-', '').replace(' ', '')
                if m[:6] in host:
                    return self.clean_merchant_url(url)
        if candidates:
            return self.clean_merchant_url(candidates[0])
        if product_pages:
            return product_pages[0]
        return ''

    def _parse_shopping_cards_generic(self, soup, product_name, expected_specs=None):
        """Selector-free card extraction: find the smallest DOM blocks that hold a
        matching product title, a TL price, and a known Turkish retailer name."""
        price_re = re.compile(r'\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?\s*(?:₺|TL)')
        offers = []
        for el in soup.find_all(['div', 'li', 'article']):
            text = el.get_text(' ', strip=True)
            if not (30 < len(text) < 400):
                continue
            price_m = price_re.search(text)
            if not price_m:
                continue
            # smallest block: skip if a child block also matches (we'll visit it too)
            if any(price_re.search(c.get_text(' ', strip=True) or '')
                   for c in el.find_all(['div', 'li', 'article'], recursive=False)):
                continue
            if not self.is_strict_match(product_name, text, expected_specs):
                continue
            # Anchor on retailer names only — phone brand names ('samsung', 'apple')
            # appear in every product title and would mislabel unknown sellers
            merchant = self._trusted_merchant_in_card(el)
            if not merchant:
                continue
            price_val = self.clean_price(price_m.group(0))
            if price_val < 1000:
                continue
            link = self._extract_merchant_link(el, merchant)
            if not link:
                parent_a = el.find_parent('a', href=True)
                if parent_a:
                    link = self._extract_merchant_link(parent_a.parent or parent_a, merchant)
            if not link:
                continue
            offers.append({"merchant": merchant, "price": price_val, "url": link})
        return offers

    def get_google_shopping_price(self, product_name, expected_specs=None):
        """Scrape Google Shopping Turkey for multi-merchant price comparison via FlareSolverr."""
        if self.GOOGLE_SHOPPING_DOMAIN in self.dead_domains:
            return None

        search_name = self.clean_search_query(product_name)
        # tbm=shop was retired by Google (redirects to udm=28) — use udm=28 directly
        url = f"https://www.google.com/search?q={quote_plus(search_name)}+fiyat&udm=28&gl=tr&hl=tr"
        logging.info(f"🛒 Searching Google Shopping for: {search_name}")

        # Google Shopping is JS-rendered — curl_cffi returns empty shells.
        # Call FlareSolverr directly (Google doesn't use Cloudflare, so it should work).
        try:
            payload = {"cmd": "request.get", "url": url, "maxTimeout": 20000}
            response = requests.post(self.flaresolverr_url, json=payload, timeout=30)
            res_data = response.json()
            if res_data.get('status') == 'ok':
                html = res_data['solution']['response']
                final_url = res_data['solution'].get('url', '')
                # Google serves /sorry/ (CAPTCHA) when rate-limited; keeping up the
                # per-product hammering only extends the ban — trip the breaker instead
                if '/sorry/' in final_url or 'recaptcha' in html[:5000].lower():
                    logging.warning("  🚫 Google Shopping rate-limited (CAPTCHA page)")
                    self._record_domain_result(self.GOOGLE_SHOPPING_DOMAIN, False)
                    return None
            else:
                logging.warning(f"  ⚠️ Google Shopping FlareSolverr: {res_data.get('message', 'unknown')}")
                self._record_domain_result(self.GOOGLE_SHOPPING_DOMAIN, False)
                return None
        except Exception as e:
            logging.warning(f"  ⚠️ Google Shopping FlareSolverr error: {e}")
            self._record_domain_result(self.GOOGLE_SHOPPING_DOMAIN, False)
            return None
        self._record_domain_result(self.GOOGLE_SHOPPING_DOMAIN, True)
        
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
                
                if title and not self.is_strict_match(product_name, title, expected_specs):
                    continue
                
                # Price
                price_el = wrapper.select_one('.lmQWe, .a8Pemb, .HRLxBb')
                price_text = price_el.get_text().strip() if price_el else ""
                price_val = self.clean_price(price_text)
                if price_val < 1000:
                    continue
                
                # Merchant — require a real, known Turkish retailer. Unknown sellers
                # (gray imports, dropshippers) are skipped entirely.
                merchant_el = wrapper.select_one('.WJMUdc, .aULzUe, .IuHnof, .E5ocAb')
                if not merchant_el:
                    continue
                merchant = merchant_el.get_text().strip()
                merchant = re.sub(r'\.com(\.tr)?$', '', merchant).strip()
                if not self.is_trusted_merchant(merchant):
                    logging.info(f"  🚷 Skipping untrusted Google Shopping seller: {merchant} ({price_val} TL)")
                    continue
                
                # Link — pick the real merchant URL, not Google help/ad-info anchors
                link = self._extract_merchant_link(card, merchant)
                if not link:
                    logging.info(f"  🔗 No usable merchant URL in card for {merchant}, skipping offer")
                    continue

                results.append({
                    "merchant": merchant,
                    "price": price_val,
                    "url": link
                })
            except Exception:
                continue
        
        # Fallback for markup drift: Google rotates its obfuscated class names, so
        # when no known card selector matches, scan compact DOM blocks that contain
        # a strict product-title match + a TL price + a *trusted merchant* name.
        # (The old fallback that scraped any bare price from body text was removed —
        # it produced fake offers with no title or merchant validation.)
        if not results:
            results = self._parse_shopping_cards_generic(soup, product_name, expected_specs)
            if results:
                logging.info(f"  📦 Generic card parser recovered {len(results)} offers (selector drift?)")
        
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

    def get_epey_price(self, product_name, expected_specs=None):
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
                        if title and self.is_strict_match(product_name, title, expected_specs):
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
            
        # Trust filter: same gap as Akakçe above — Epey's merchant list was never
        # checked against TRUSTED_MERCHANTS, only Google Shopping was.
        untrusted = [r for r in results if not self.is_trusted_merchant(r['merchant'])]
        for u in untrusted:
            logging.info(f"  🚷 Skipping untrusted Epey seller: {u['merchant']} ({u['price']} TL)")
        results = [r for r in results if self.is_trusted_merchant(r['merchant'])]

        if results:
            # Sort by price FIRST so "keep first occurrence per merchant" below
            # actually keeps the lowest price per merchant, not just whichever
            # listing happened to appear first in DOM order.
            results.sort(key=lambda x: x['price'])
            final_agg = []
            seen = set()
            for r in results:
                key = f"{r['merchant']}"
                if key not in seen:
                    seen.add(key)
                    final_agg.append(r)
            return final_agg

        return None

    def parse_site_offer(self, site_name, config, html, product_name, expected_specs=None):
        soup = BeautifulSoup(html, 'html.parser')
        offers = []
        items = soup.select(config['container'])
        for item in items:
            title_el = item.select_one(config['title'])
            price_el = item.select_one(config['price'])
            if config['link'] in ('self', '') or item.name == 'a':
                link_el = item
            else:
                link_el = item.select_one(config['link'])

            if not (title_el and price_el and link_el):
                continue

            title = title_el.get_text(' ', strip=True)
            price = self.clean_price(price_el.get_text(' ', strip=True))
            link = link_el.get('href', '')
            if link and not link.startswith('http'):
                link = config['base_url'] + link

            if (
                price > 5000
                and link
                and self.is_strict_match(product_name, title, expected_specs)
            ):
                offers.append({
                    "merchant": site_name,
                    "price": price,
                    "url": self.clean_merchant_url(link),
                })

        if offers:
            return min(offers, key=lambda offer: offer['price'])
        return None

    async def scrape_site_async(
        self,
        site_name,
        config,
        search_name,
        product_name,
        semaphore,
        expected_specs=None,
    ):
        async with semaphore:
            url = self._build_search_url(config, search_name)
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

                return self.parse_site_offer(
                    site_name, config, html, product_name, expected_specs
                )
            except Exception as exc:
                logging.warning(f"  ⚠️ {site_name} direct scrape failed: {exc}")
            return None

    @classmethod
    def _standardize_merchant_name(cls, m_name):
        return cls._canonical_merchant_name(m_name) or m_name

    async def get_best_prices(self, product_name, expected_specs=None):
        results = []
        # /about claims broad coverage (Hepsiburada, Trendyol, Amazon TR, n11 and more).
        # Stopping at the first source that returns anything left products with only
        # 1-2 offers whenever Google Shopping alone was thin — keep pulling from
        # additional aggregators until we hit a reasonable number of distinct merchants.
        TARGET_MERCHANT_COUNT = 4

        def merchant_count():
            return len({self._standardize_merchant_name(r['merchant']) for r in results})

        # 1. Try Google Shopping (no Cloudflare, most reliable from CI)
        gs_results = self.get_google_shopping_price(product_name, expected_specs)
        if gs_results:
            results.extend(gs_results)
            logging.info(f"  ✨ Found {len(gs_results)} offers on Google Shopping")

        # 2. Try Epey.com to add more merchants if coverage is still thin
        if merchant_count() < TARGET_MERCHANT_COUNT:
            logging.info(f"  🔄 {merchant_count()} merchant(s) so far, trying Epey for more coverage...")
            epey_results = self.get_epey_price(product_name, expected_specs)
            if epey_results:
                results.extend(epey_results)
                logging.info(f"  ✨ Found {len(epey_results)} offers on Epey")

        # 3. Try Akakçe to add more merchants if coverage is still thin
        if merchant_count() < TARGET_MERCHANT_COUNT:
            logging.info(f"  🔄 {merchant_count()} merchant(s) so far, trying Akakçe...")
            akakce_results = self.get_akakce_price(product_name, expected_specs)
            if akakce_results:
                results.extend(akakce_results)
                logging.info(f"  ✨ Found {len(akakce_results)} offers on Akakçe")

        # 4. Fill thin aggregator coverage from direct retailer searches. Keep this
        # list to sites that currently return useful HTML from GitHub-hosted runners;
        # blocked/broken searches only consume the workflow timeout.
        if merchant_count() < TARGET_MERCHANT_COUNT:
            search_name = self.clean_search_query(product_name)
            logging.info(
                f"  🔄 {merchant_count()} merchant(s) after aggregators, "
                f"trying direct retailer searches for {search_name}..."
            )

            priority_sites = [
                'Trendyol',
                'n11',
                'PttAVM',
                'MediaMarkt',
                'Pasaj',
                'Pazarama',
            ]
            sem = asyncio.Semaphore(4)
            tasks = []
            for site_name in priority_sites:
                config = self.site_configs.get(site_name)
                if config:
                    tasks.append(self.scrape_site_async(
                        site_name,
                        config,
                        search_name,
                        product_name,
                        sem,
                        expected_specs,
                    ))

            scraped_results = await asyncio.gather(*tasks)
            for r in scraped_results:
                if r:
                    results.append(r)
                    logging.info(f"  ✅ {r['merchant']}: {r['price']} ₺")

        # De-duplicate: ONLY ONE LOWEST PRICE PER MERCHANT
        merchant_best = {}
        for r in results:
            m_name = self._standardize_merchant_name(r['merchant'])
            r['merchant'] = m_name
            if m_name not in merchant_best or r['price'] < merchant_best[m_name]['price']:
                merchant_best[m_name] = r

        unique_results = list(merchant_best.values())

        # Outlier guard: an offer far below the median is almost always a wrong
        # variant (different storage, refurbished, gray import). Since base_price
        # is set to the MINIMUM offer, one bad listing corrupts the product price.
        if len(unique_results) >= 3:
            prices = sorted(r['price'] for r in unique_results)
            median = prices[len(prices) // 2]
            filtered = [r for r in unique_results if r['price'] >= median * 0.6]
            dropped = [r for r in unique_results if r['price'] < median * 0.6]
            for d in dropped:
                logging.warning(f"  📉 Dropping outlier offer: {d['merchant']} {d['price']} TL (median {median} TL)")
            unique_results = filtered

        return sorted(unique_results, key=lambda x: x['price'])

if __name__ == "__main__":
    import asyncio
    scraper = TRPriceScraper()
    res = asyncio.run(scraper.get_best_prices("Samsung Galaxy S24"))
    logging.info(json.dumps(res, indent=2))
