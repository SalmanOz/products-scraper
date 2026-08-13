import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

import os
import re
import sys
import time
import json
import requests
from dotenv import load_dotenv
from source_ingestion import (
    ExistingProductNotFoundError,
    IngestionConfigurationError,
    SourceIngestionClient,
    SourceIngestionError,
    build_provenance_records,
    select_observed_physical_attributes,
    utc_observation_time,
)
from bs4 import BeautifulSoup
import html as html_lib

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

class KimovilScraper:
    def __init__(self, ingestion_client=None):
        self.ingestion_client = ingestion_client
        self.base_url = os.getenv("KIMOVIL_BASE_URL", "https://www.kimovil.com/en/")
        self.flaresolverr_url = "http://localhost:8191/v1"

    def get_ingestion_client(self):
        if self.ingestion_client is None:
            self.ingestion_client = SourceIngestionClient.from_env()
        return self.ingestion_client

    def get_via_flaresolverr(self, url):
        logging.info(f"🚀 FlareSolverr: {url}")
        try:
            payload = {"cmd": "request.get", "url": url, "maxTimeout": 120000}
            response = requests.post(self.flaresolverr_url, json=payload, timeout=150)
            res_data = response.json()
            if res_data.get('status') == 'ok': return res_data['solution']['response']
            return None
        except: return None

    def extract_number(self, text):
        if not text: return 0
        t = str(text).replace('\t', '').replace('\n', '').strip()
        # Remove any text in parentheses first (like (v10))
        t = re.sub(r'\(.*?\)', '', t).strip()
        # Also remove common version patterns like v10, v9
        t = re.sub(r'v\d+', '', t, flags=re.IGNORECASE).strip()
        
        is_antutu = 'antutu' in t.lower()
        
        # Find all numeric sequences (handling dots and commas)
        # This will find things like "816.345" or "1,234,567"
        nums = re.findall(r'[\d\.,]+', t)
        if not nums: return 0
        
        # Take only the FIRST numeric sequence found
        raw_num = nums[0].strip('.,')
        
        # Remove separators
        clean_num = raw_num.replace('.', '').replace(',', '')
        
        if not clean_num: return 0
        
        try:
            val = int(float(clean_num))
            # Safeguard: If it's an Antutu score and it's suspiciously small (e.g. 816 instead of 816000)
            if is_antutu and 0 < val < 2000:
                logging.warning(f"⚠️ Suspiciously low Antutu score ({val}), multiplying by 1000")
                val *= 1000
            # Sanity check: Antutu scores shouldn't exceed 4 million currently
            if is_antutu and val > 4000000:
                logging.warning(f"⚠️ Suspiciously high Antutu score ({val}), taking only the first part")
                # Probably concatenated with something else, take only first 6-7 digits
                val = int(str(val)[:7]) if val > 9999999 else val
            return val
        except:
            return 0

    @staticmethod
    def is_product_name_match(expected, observed):
        """Fail closed when a source page appears to describe another variant."""

        def tokens(value):
            normalized = str(value).lower().replace("+", " plus ")
            return re.findall(r"[a-z0-9]+", normalized)

        expected_tokens = tokens(expected)
        observed_tokens = tokens(observed)
        if not expected_tokens or not observed_tokens:
            return False

        brands = {
            "apple",
            "google",
            "honor",
            "huawei",
            "infinix",
            "motorola",
            "nothing",
            "oneplus",
            "oppo",
            "poco",
            "realme",
            "redmi",
            "samsung",
            "tecno",
            "vivo",
            "xiaomi",
        }
        expected_brands = set(expected_tokens) & brands
        observed_brands = set(observed_tokens) & brands
        if expected_brands and not (
            expected_brands & observed_brands
        ):
            return False

        generic = {"phone", "smartphone", "galaxy"}
        meaningful_expected = [
            token
            for token in expected_tokens
            if token not in brands and token not in generic
        ]
        meaningful_observed = [
            token
            for token in observed_tokens
            if token not in brands and token not in generic
        ]
        # Joining handles harmless spacing differences such as iPhone16e vs
        # iPhone 16e while still rejecting extra model/variant qualifiers.
        return (
            bool(meaningful_expected) and
            "".join(meaningful_expected) == "".join(meaningful_observed)
        )

    def scrape_product_details(
        self,
        url,
        category_id=1,
        product_slug=None,
        expected_name=None,
        existing_attributes=None,
    ):
        try:
            # Kept for backward-compatible callers. Product category and all
            # publication decisions are owned by the website.
            _ = category_id
            html = self.get_via_flaresolverr(url)
            if not html:
                logging.error(f"❌ Error: FlareSolverr returned no HTML for {url}")
                return False
            if "Just a moment" in html:
                logging.error(f"❌ Error: Cloudflare blocked the request (Turnstile/Just a moment) for {url}")
                return False
            soup = BeautifulSoup(html, 'html.parser')
            device_ki = {}; device_compare = {}
            ki_meta = soup.find('meta', {'name': 'deviceki'})
            if ki_meta and ki_meta.get('content'):
                try: device_ki = json.loads(html_lib.unescape(ki_meta['content']))
                except: pass
            
            comp_meta = soup.find('meta', {'name': 'devicecompare'})
            if comp_meta and comp_meta.get('content'): 
                try: device_compare = json.loads(html_lib.unescape(comp_meta['content']))
                except: pass
            
            full_name = device_ki.get('name') or device_compare.get('name') or (soup.find('h1').get_text().strip() if soup.find('h1') else "Unknown Device")
            
            if "Compare smartphones" in full_name or "Unknown Device" == full_name or "Page not found" in full_name or not (device_ki or device_compare):
                logging.warning(f"⚠️ Skipping: {full_name} (Invalid or general comparison page)")
                return False
            if expected_name and not self.is_product_name_match(
                expected_name,
                full_name,
            ):
                logging.error(
                    "❌ Source identity mismatch: expected '%s', observed '%s'",
                    expected_name,
                    full_name,
                )
                return False

            logging.info(f"📦 Processing: {full_name}")

            raw_specs = {}; all_key_values = {}
            for section in soup.find_all('section', class_=re.compile(r'container-sheet-')):
                header = section.find(['h2', 'h3', 'h4'])
                if not header: continue
                title = header.get_text().strip()
                if " of " in title: title = title.split(' of ')[0].strip()
                raw_specs[title] = {}
                for table in section.find_all('table', class_='k-dltable'):
                    for tr in table.find_all('tr'):
                        th = tr.find(['th', 'td'], class_='label') or tr.find('th')
                        td_all = tr.find_all('td')
                        td = tr.find(['td'], class_='value') or (td_all[-1] if td_all else None)
                        if th and td:
                            key = th.get_text().strip()
                            val = td.get_text().replace('\n', ' ').replace('See more details', '').strip()
                            raw_specs[title][key] = val
                            all_key_values[key] = val

            partials = device_ki.get('partials', {})
            # Preserve the observed benchmark values exactly. The scoring API
            # owns TeknoSkor calculation; this scraper must not perturb inputs.
            partials = {
                key: float(value)
                for key, value in partials.items()
                if isinstance(value, (int, float))
            }

            def get_spec(s, k, f=None):
                for sect, specs in raw_specs.items():
                    if s.lower() in sect.lower():
                        for sk, sv in specs.items():
                            if k.lower() in sk.lower(): return sv
                            if f and f.lower() in sk.lower(): return sv
                return all_key_values.get(k, all_key_values.get(f, '---'))

            battery_v = get_spec('Battery', 'Capacity')
            antutu_v = next(
                (
                    value
                    for key, value in all_key_values.items()
                    if (
                        'antutu' in str(key).lower()
                        or 'antutu' in str(value).lower()
                    )
                ),
                '---',
            )
            nm_v = self.extract_number(get_spec('Hardware', 'Nanometers', 'nm'))

            attributes = {
                "antutu_score": int(self.extract_number(antutu_v)),
                "camera_score": partials.get('camera', 0), "performance_score": partials.get('hardware', 0),
                "battery_score": partials.get('battery', 0), "screen_score": partials.get('design', 0), "partials": partials
            }
            attributes.update(select_observed_physical_attributes(
                existing_attributes or {},
                raw_specs,
            ))

            def calc_gaming(antutu, bat, nm):
                if antutu <= 0 or bat <= 0:
                    return []
                games = {"PUBG Mobile": {"i": "🔫", "w": 1.0, "m": 120}, "Genshin Impact": {"i": "✨", "w": 2.2, "m": 60}, "CoD: Warzone": {"i": "🎖️", "w": 1.8, "m": 120}, "EA FC Mobile": {"i": "⚽", "w": 1.2, "m": 120}, "Mobile Legends": {"i": "⚔️", "w": 0.8, "m": 120}, "Roblox": {"i": "🧱", "w": 0.7, "m": 60}}
                res = []; nm = nm if nm > 0 else 6
                for name, c in games.items():
                    fps = c["m"] if antutu >= 1500000 else int(c["m"]*0.85) if antutu >= 1000000 else int(c["m"]*0.6) if antutu >= 700000 else int(c["m"]*0.4) if antutu >= 400000 else int(c["m"]*0.25)
                    play_time = round(bat / (c["w"]*800*(1+(nm-4)*0.1)), 1)
                    res.append({"game": name, "icon": c["i"], "fps": fps, "max_fps": c["m"], "hours": play_time, "tier": "Ultra" if fps >= c["m"]*0.9 else "Yüksek" if fps >= c["m"]*0.7 else "Orta"})
                return res
            gaming_performance = calc_gaming(
                attributes["antutu_score"],
                self.extract_number(battery_v),
                nm_v,
            )
            if gaming_performance:
                attributes["gaming_performance"] = gaming_performance
            target_slug = (
                product_slug
                or device_compare.get('slug')
                or re.sub(
                    r'[^a-z0-9]+',
                    '-',
                    full_name.lower(),
                ).strip('-')
            )
            records = build_provenance_records(
                product_slug=target_slug,
                source_url=url,
                attributes=attributes,
                observed_at=utc_observation_time(),
            )
            result = self.get_ingestion_client().submit_sources(records)
            logging.info(
                "✅ Source ingestion committed for %s: %s accepted, "
                "%s stale ignored",
                full_name,
                result["accepted"],
                result["stale_ignored"],
            )
            return True
        except ExistingProductNotFoundError as error:
            logging.error(
                "❌ Existing-product-only ingestion refused '%s': %s. "
                "Create/review the product in TeknoSkor first.",
                product_slug or url,
                error,
            )
            return False
        except (SourceIngestionError, ValueError) as error:
            logging.error(f"❌ Source ingestion error: {error}")
            return False
        except Exception as error:
            logging.exception(f"❌ Scrape error: {error}")
            return False

    def clean_phone_model_name(self, name):
        # 1. Split on the first occurrence of storage or RAM indicator, and discard everything after it.
        # This matches patterns like: 256GB, 256 GB, 128 GB, 8GB, 8 GB, 1TB, 1 TB, 8GB RAM, 8 RAM, etc.
        # We also match the word "ram" (case-insensitive) as a boundary.
        # Example: "Xiaomi Redmi 15 256GB 8GB Ram Siyah" -> Splits at "256GB" -> "Xiaomi Redmi 15"
        parts = re.split(r'\b\d+\s*(?:GB|TB|ram)\b|\b(?:gb|tb|ram)\b', name, flags=re.IGNORECASE)
        name = parts[0]
        
        # 2. Remove common Turkish/English color words and metadata at the end of the remaining string
        colors = ['siyah', 'beyaz', 'gri', 'mavi', 'sarı', 'yeşil', 'pembe', 'gümüş', 'turuncu', 'altın', 'mor', 'kırmızı', 'lacivert', 'kahverengi', 'titanyum', 'kozmik', 'sis', 'ada', 'çayı', 'lavanta', 'çöl']
        for color in colors:
            name = re.sub(rf'\b{color}\b', '', name, flags=re.IGNORECASE)
            
        # 3. Clean up extra whitespaces
        name = re.sub(r'\s+', ' ', name).strip()
        return name


    def search_product_on_kimovil(self, query):
        url = f"https://www.kimovil.com/_json/autocomplete_devicemodels_joined.json?device_type=0&name={requests.utils.quote(query)}"
        html = self.get_via_flaresolverr(url)
        if not html: return None
        
        try:
            if html.strip().startswith("<html"):
                soup = BeautifulSoup(html, 'html.parser')
                json_str = soup.get_text()
            else:
                json_str = html
            data = json.loads(json_str)
            results = data.get('results', [])
            if not results:
                return None

            # Extract important words from query for validation
            query_lower = query.lower().replace('+', 'plus')
            brands = ['apple', 'samsung', 'xiaomi', 'huawei', 'oppo', 'vivo', 'realme', 'poco', 'google', 'oneplus', 'honor', 'redmi']
            common = ['4g', '5g', 'gb', 'ram', 'nfc', 'tb', 'phone', 'smartphone', 'galaxy']
            query_words = re.findall(r'\w+', query_lower)
            important_words = [w for w in query_words if len(w) > 1 and w not in common and w not in brands]
            
            # Variation words that must match both directions
            variations = ['pro', 'max', 'plus', 'ultra', 'lite', 'fe', 'mini', 'se', 'note']
            query_variations = set(w for w in query_words if w in variations)

            for result in results:
                # Skip rumor/unannounced phones
                if result.get('is_rumor'):
                    continue
                result_name = (result.get('full_name') or result.get('alias') or '').lower().replace('+', 'plus')
                result_slug = result.get('url')
                if not result_slug or not result_name:
                    continue

                # Check all important query words exist in result name
                all_found = all(re.search(rf'\b{re.escape(w)}\b', result_name) for w in important_words)
                if not all_found:
                    continue

                # Check variation words match both directions
                result_words = re.findall(r'\w+', result_name)
                result_variations = set(w for w in result_words if w in variations)
                if query_variations != result_variations:
                    continue
                    
                logging.info(f"  ✅ Kimovil match: '{result.get('full_name')}' for query '{query}'")
                return f"https://www.kimovil.com/en/where-to-buy-{result_slug}"

            # No validated match found
            logging.warning(f"  ⚠️ Kimovil autocomplete returned {len(results)} results but none matched '{query}': {[r.get('full_name') for r in results[:3]]}")
            return None
        except Exception as e:
            logging.error(f"❌ Error parsing Kimovil autocomplete API: {e}")
            return None

    def scrape_existing_products(self):
        """Refresh provenance only for products approved in TeknoSkor.

        Product creation and publication are intentionally outside the scraper.
        The authenticated catalog is the complete allowlist for this run.
        """

        products = self.get_ingestion_client().fetch_catalog(page_size=500)
        if not products:
            raise SourceIngestionError(
                "Authenticated ingestion catalog returned no products",
            )

        succeeded = 0
        failed = []
        logging.info(
            "🚀 Starting existing-product-only provenance sync for %s products",
            len(products),
        )
        for index, product in enumerate(products, start=1):
            logging.info(
                "📱 [%s/%s] %s",
                index,
                len(products),
                product.name,
            )
            guessed_url = (
                "https://www.kimovil.com/en/where-to-buy-"
                f"{product.slug}"
            )
            success = self.scrape_product_details(
                guessed_url,
                product_slug=product.slug,
                expected_name=product.name,
                existing_attributes=product.attributes,
            )
            if not success:
                matched_url = self.search_product_on_kimovil(product.name)
                if matched_url and matched_url != guessed_url:
                    success = self.scrape_product_details(
                        matched_url,
                        product_slug=product.slug,
                        expected_name=product.name,
                        existing_attributes=product.attributes,
                    )
            if success:
                succeeded += 1
            else:
                failed.append(product.slug)
            if index < len(products):
                time.sleep(1)

        summary = {
            "catalog_products": len(products),
            "succeeded": succeeded,
            "failed": failed,
            "existing_product_only": True,
        }
        refreshed_products = self.get_ingestion_client().fetch_catalog(
            page_size=500,
        )
        readiness_available = all(
            product.data_quality_status is not None
            for product in refreshed_products
        )
        unverified = [
            product
            for product in refreshed_products
            if (
                product.data_quality_status != "verified"
                or not product.spec_verified_at
            )
        ] if readiness_available else []
        if readiness_available:
            summary["verified_products"] = (
                len(refreshed_products) - len(unverified)
            )
            summary["unverified"] = [
                product.slug for product in unverified
            ]
            for product in unverified:
                issue_keys = sorted({
                    str(issue.get("key") or issue.get("code") or "unknown")
                    for issue in product.data_quality_issues
                })
                logging.error(
                    "❌ Verification gate: %s remains %s; issues=%s",
                    product.slug,
                    product.data_quality_status,
                    ",".join(issue_keys) or "unknown",
                )
            failed = sorted(set(failed) | {
                product.slug for product in unverified
            })
            summary["failed"] = failed
        else:
            logging.warning(
                "Authenticated catalog does not expose verification state; "
                "deploy the matching TeknoSkor manifest contract.",
            )
        logging.info(
            "🏁 Provenance sync finished: %s succeeded, %s failed. "
            "No products were created or published.",
            succeeded,
            len(failed),
        )
        if failed:
            logging.error("Failed product slugs: %s", ", ".join(failed))
        return summary

    def scrape_latest_smartphones(self):
        """Backward-compatible alias; discovery/publication is no longer allowed."""

        logging.warning(
            "New-product auto-publication is disabled; syncing the authenticated "
            "existing-product catalog instead.",
        )
        return self.scrape_existing_products()

if __name__ == "__main__":
    try:
        scraper = KimovilScraper()
        run_summary = scraper.scrape_existing_products()
        if run_summary["failed"]:
            sys.exit(1)
    except (IngestionConfigurationError, SourceIngestionError) as error:
        logging.error(f"❌ FATAL: {error}")
        sys.exit(1)
    except Exception as error:
        logging.exception(f"❌ FATAL: {error}")
        sys.exit(1)
