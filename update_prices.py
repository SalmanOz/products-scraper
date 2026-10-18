
import logging
import asyncio
import json
import math
from datetime import datetime, timezone
from dotenv import load_dotenv
from urllib.parse import urlparse
from tr_price_scraper import TRPriceScraper
from indexnow import submit_urls
from offer_ingestion_client import OfferIngestionClient
from price_sanity import filter_price_outliers

load_dotenv()

MIN_FULL_RUN_PRODUCT_COVERAGE = 0.50
INGESTION_PRODUCT_BATCH_SIZE = 10
MAX_OFFER_PRICE = 5_000_000

class PriceUpdater:
    def __init__(self, ingestion_client=None, price_scraper=None):
        self.ingestion_client = (
            ingestion_client or OfferIngestionClient.from_env()
        )
        self.price_scraper = price_scraper or TRPriceScraper()

    def get_all_products(self):
        return self.ingestion_client.get_published_products()

    @staticmethod
    def get_expected_specs(attributes):
        if isinstance(attributes, str):
            try:
                attributes = json.loads(attributes)
            except (TypeError, ValueError):
                return {}
        if not isinstance(attributes, dict):
            return {}
        expected = {
            'ram_gb': attributes.get('ram_gb'),
            'storage_gb': attributes.get('storage_gb'),
        }
        connectivity = attributes.get('Connectivity')
        if isinstance(connectivity, dict):
            fifth_generation = connectivity.get('5G')
            import re
            values = fifth_generation if isinstance(fifth_generation, list) else [fifth_generation]
            for value in values:
                if value is True or (isinstance(value, str) and (
                    value.strip().casefold() in {'yes', 'true', 'var', 'supported', 'destekleniyor'} or
                    re.search(r'(?:^|[\s,;:/({])n\d{1,3}(?=$|[\s,;)/}])', value, re.I)
                )):
                    expected['network_generation'] = '5g'
                    break
                if value is False or (isinstance(value, str) and value.strip().casefold() in {
                    'no', 'false', 'yok', 'hayır', 'hayir', 'not supported', 'desteklenmiyor',
                }):
                    expected['network_generation'] = '4g'
        return expected

    @staticmethod
    def utc_timestamp():
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def prepare_product_offers(
        self,
        product,
        offers,
        observed_at,
        checked_at,
    ):
        if not offers:
            return []

        # Wrong-listing guard: a fuzzy-matched accessory/wrong variant surfaces
        # as a price far below the rest of the market and would poison the
        # current offer set.
        clean = filter_price_outliers(offers)
        if len(clean) < len(offers):
            dropped = [o for o in offers if o not in clean]
            for d in dropped:
                logging.warning(f"  🚫 Outlier offer dropped: {d['merchant']} {d['price']} TL ({d['url'][:80]})")
            offers = clean

        records = []
        for offer in offers:
            try:
                seller = str(offer["merchant"]).strip()
                price = float(offer["price"])
                source_url = str(offer["url"]).strip()
            except (KeyError, TypeError, ValueError) as error:
                raise RuntimeError(
                    f"Invalid extracted offer for {product['slug']}: {offer}"
                ) from error
            if (
                not seller
                or len(seller) > 100
                or not math.isfinite(price)
                or price <= 0
                or price > MAX_OFFER_PRICE
                or not source_url
                or len(source_url) > 1000
                or urlparse(source_url).scheme not in {"http", "https"}
                or not urlparse(source_url).netloc
            ):
                raise RuntimeError(
                    f"Invalid extracted offer for {product['slug']}: {offer}"
                )

            record = {
                "product_id": int(product["id"]),
                "product_slug": product["slug"],
                "seller": seller,
                "price": price,
                "currency": "TRY",
                # A successfully parsed listing is not proof of stock.
                "availability": "unknown",
                "source_url": source_url,
                # Commission links belong to the site owner, not price observations.
                "observed_at": observed_at,
                "checked_at": checked_at,
                "is_official": False,
            }
            source_title = offer.get("source_title")
            if isinstance(source_title, str) and source_title.strip():
                source_title = source_title.strip()
                # Match the endpoint's JavaScript string-length limit without
                # truncating away contradictory model/variant evidence.
                if len(source_title.encode("utf-16-le", errors="surrogatepass")) > 2000:
                    raise RuntimeError(
                        f"Observed source title exceeds 1000 characters for {product['slug']}"
                    )
                record["source_title"] = source_title
            records.append(record)
        return records

    @staticmethod
    def empty_ingestion_result():
        return {
            "accepted": 0,
            "stale_ignored": 0,
            "variant_rejected": 0,
            "variant_rejections": [],
            "changed_paths": [],
            "committed": True,
        }

    @staticmethod
    def merge_ingestion_result(aggregate, result):
        if result.get("committed") is not True:
            raise RuntimeError(
                "Offer ingestion response did not confirm a commit"
            )
        submitted_offset = (
            aggregate["accepted"] + aggregate["stale_ignored"]
            + aggregate["variant_rejected"]
        )
        aggregate["accepted"] += int(result.get("accepted", 0))
        aggregate["stale_ignored"] += int(
            result.get("stale_ignored", 0)
        )
        aggregate["variant_rejected"] += int(result.get("variant_rejected", 0))
        aggregate["variant_rejections"].extend(
            {**rejection, "index": submitted_offset + rejection["index"]}
            for rejection in result.get("variant_rejections", [])
        )
        aggregate["changed_paths"].extend(
            path
            for path in result.get("changed_paths", [])
            if isinstance(path, str) and path.startswith("/")
        )

    def flush_offers(self, pending_offers, aggregate):
        if not pending_offers:
            return
        result = self.ingestion_client.ingest_offers(
            list(pending_offers)
        )
        self.merge_ingestion_result(aggregate, result)
        logging.info(
            "  ✅ HTTPS ingestion committed: %s accepted, %s stale, %s variant rejected",
            result["accepted"],
            result["stale_ignored"],
            result.get("variant_rejected", 0),
        )
        for rejection in result.get("variant_rejections", []):
            logging.error(
                "  🚫 Variant rejection for %s (offer index %s within this flush): %s",
                rejection["product_slug"],
                rejection["index"],
                ", ".join(rejection["issues"]),
            )
        pending_offers.clear()

    async def run_update(self, product_id=None, phase2_backfill=False):
        if product_id is not None and phase2_backfill:
            raise RuntimeError(
                "Phase 2 backfill must validate the full published catalog"
            )
        products = self.get_all_products()
        if product_id is not None:
            try:
                target_id = int(product_id)
            except (TypeError, ValueError) as error:
                raise RuntimeError("Product id must be a positive integer") from error
            if target_id <= 0:
                raise RuntimeError("Product id must be a positive integer")
            products = [
                product
                for product in products
                if int(product["id"]) == target_id
            ]
            if not products:
                raise RuntimeError(
                    f"Published product {target_id} was not found in the catalog"
                )

        logging.info(f"🚀 Starting price update for {len(products)} products...")

        pending_offers = []
        aggregate_result = self.empty_ingestion_result()
        failures = []
        products_with_offers = 0
        merchant_counts = {}
        for processed_count, p in enumerate(products, start=1):
            name = p['name']
            expected_specs = self.get_expected_specs(p.get('attributes'))

            logging.info(f"\n🔍 Updating prices for: {name}")
            try:
                offers = await self.price_scraper.get_best_prices(name, expected_specs)
                if offers:
                    observed_at = self.utc_timestamp()
                    checked_at = self.utc_timestamp()
                    product_offers = self.prepare_product_offers(
                        p,
                        offers,
                        observed_at,
                        checked_at,
                    )
                    pending_offers.extend(product_offers)
                    if product_offers:
                        products_with_offers += 1
                    merchant_counts[p["slug"]] = len({
                        record["seller"].strip().casefold()
                        for record in product_offers
                    })
                    logging.info(
                        "  ✅ Prepared %s matched listing(s) for ingestion",
                        len(product_offers),
                    )
                else:
                    # A scrape miss is not proof that every retailer is out of
                    # stock. Preserve the last known-good offers/price so a
                    # temporary block on a GitHub runner cannot erase live data.
                    logging.warning(
                        f"  ⚠️ No verified offers found for {name}. "
                        "Keeping the last known price."
                    )
            except Exception as e:
                logging.error(f"  ❌ Error fetching prices for {name}: {str(e)}")
                failures.append(f"{p['slug']}: {e}")

            if processed_count % INGESTION_PRODUCT_BATCH_SIZE == 0:
                self.flush_offers(pending_offers, aggregate_result)

            # Small delay to avoid aggressive scraping
            await asyncio.sleep(1)

        self.flush_offers(pending_offers, aggregate_result)
        aggregate_result["changed_paths"] = list(
            dict.fromkeys(aggregate_result["changed_paths"])
        )
        changed_paths = aggregate_result["changed_paths"]
        coverage = (
            products_with_offers / len(products)
            if products
            else 0
        )
        logging.info(
            "  📊 Product offer coverage: %s/%s (%.1f%%)",
            products_with_offers,
            len(products),
            coverage * 100,
        )

        if changed_paths:
            submit_urls(changed_paths + ["/", "/products"])

        logging.info("\n🏁 Price update completed!")
        run_issues = []
        if aggregate_result["variant_rejected"]:
            run_issues.append(
                f"{aggregate_result['variant_rejected']} offer(s) rejected for "
                "identity/variant conflicts; valid committed updates retained"
            )
        if (
            product_id is None
            and products
            and coverage < MIN_FULL_RUN_PRODUCT_COVERAGE
        ):
            run_issues.append(
                "full-catalog offer coverage "
                f"{coverage:.1%} is below the required "
                f"{MIN_FULL_RUN_PRODUCT_COVERAGE:.0%}"
            )
        if phase2_backfill:
            insufficient_products = [
                product["slug"]
                for product in products
                if merchant_counts.get(product["slug"], 0) < 2
            ]
            if insufficient_products:
                run_issues.append(
                    "Phase 2 backfill requires at least two distinct "
                    "merchants for every published product; missing: "
                    + ", ".join(insufficient_products)
                )
        if failures:
            run_issues.append(
                f"{len(failures)} product scrape(s) failed: "
                + "; ".join(failures)
            )
        if run_issues:
            raise RuntimeError(
                " | ".join(run_issues)
            )
        return aggregate_result

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("product_id", nargs="?")
    parser.add_argument(
        "--phase2-backfill",
        action="store_true",
        help="Require two distinct merchants for every published product",
    )
    arguments = parser.parse_args()
    updater = PriceUpdater()
    asyncio.run(
        updater.run_update(
            arguments.product_id,
            phase2_backfill=arguments.phase2_backfill,
        )
    )
