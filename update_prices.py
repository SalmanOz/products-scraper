
import logging
import asyncio
import json
import math
from datetime import datetime, timezone
from pathlib import Path
import re
from dotenv import load_dotenv
from urllib.parse import urlparse
from tr_price_scraper import TRPriceScraper
from indexnow import submit_urls
from offer_ingestion_client import OfferIngestionClient
from price_sanity import filter_price_outliers
from retailer_offer_refresh import ALLOWED_HOSTS, RetailerOfferRefresher, _safe_url
from reviewed_offer_targets import daily_offer_targets, PROTECTED_TWO_STORE_SLUGS

load_dotenv()

MIN_FULL_RUN_PRODUCT_COVERAGE = 0.50
INGESTION_PRODUCT_BATCH_SIZE = 10
MAX_OFFER_PRICE = 5_000_000
PURCHASABLE = {'in_stock', 'limited'}
MAX_SOURCE_OBSERVATION_AGE_SECONDS = 3 * 60 * 60

class PriceUpdater:
    def __init__(self, ingestion_client=None, price_scraper=None, offer_refresher=None):
        self.ingestion_client = (
            ingestion_client or OfferIngestionClient.from_env()
        )
        self.price_scraper = price_scraper or TRPriceScraper()
        self.offer_matcher = TRPriceScraper()
        self.offer_refresher = offer_refresher or RetailerOfferRefresher()
        if isinstance(self.price_scraper, TRPriceScraper):
            self.price_scraper.retailer_offer_refresher = self.offer_refresher
        self.last_report = None

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

    def live_offer_is_valid(self, product, offer, expected_specs):
        if not isinstance(offer, dict) or not _safe_url(offer.get('url')):
            return False
        host = _safe_url(offer['url']).hostname
        merchant = self.offer_matcher._standardize_merchant_name(str(offer.get('merchant', '')))
        expected_merchant = self.offer_matcher._standardize_merchant_name(ALLOWED_HOSTS[host])
        if not merchant or merchant.casefold() != expected_merchant.casefold():
            return False
        if not self.offer_matcher.has_observed_offer_evidence(product['name'], offer, expected_specs):
            return False
        if offer.get('availability') not in PURCHASABLE | {'out_of_stock', 'preorder'}:
            return False
        if isinstance(offer.get('price'), bool):
            return False
        try:
            price = float(offer['price'])
            observed = datetime.fromisoformat(offer['observed_at'].replace('Z', '+00:00'))
            now = datetime.fromisoformat(self.utc_timestamp().replace('Z', '+00:00'))
            age = (now - observed).total_seconds()
            return (math.isfinite(price) and 0 < price <= MAX_OFFER_PRICE
                    and offer.get('currency') == 'TRY'
                    and -60 <= age <= MAX_SOURCE_OBSERVATION_AGE_SECONDS)
        except (KeyError, TypeError, ValueError, AttributeError):
            return False

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
        purchasable = [o for o in offers if o.get('availability') in PURCHASABLE]
        clean = filter_price_outliers(purchasable)
        if len(clean) < len(purchasable):
            dropped = [o for o in purchasable if o not in clean]
            for d in dropped:
                logging.warning(f"  🚫 Outlier offer dropped: {d['merchant']} {d['price']} TL ({d['url'][:80]})")
            offers = [o for o in offers if o.get('availability') not in PURCHASABLE or o in clean]

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
                or offer.get('currency', 'TRY') != 'TRY'
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
                "availability": (
                    offer.get("availability") if offer.get("availability") in {
                        "in_stock", "out_of_stock", "preorder", "limited", "unknown",
                    } else "unknown"
                ),
                "source_url": source_url,
                # Commission links belong to the site owner, not price observations.
                "observed_at": observed_at,
                "checked_at": checked_at,
                "is_official": False,
            }
            source_title = offer.get("source_title")
            source_observed_at = offer.get('observed_at')
            if source_observed_at is not None:
                try:
                    actual_observation = datetime.fromisoformat(source_observed_at.replace('Z', '+00:00'))
                    if actual_observation.tzinfo is None:
                        raise ValueError('Missing observation timezone')
                except (AttributeError, TypeError, ValueError) as error:
                    raise RuntimeError(f"Invalid source observation timestamp for {product['slug']}") from error
                # An exact-URL cache hit retains its actual fetch time instead
                # of becoming a fresh observation again at each product.
                record['observed_at'] = actual_observation.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
                record['checked_at'] = record['observed_at']
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

    async def run_update(self, product_id=None, phase2_backfill=False, *, dry_run=False,
                         product_slugs=None, known_pdp_only=False, report_path=None):
        self.last_report = {'mode': 'audit' if dry_run else 'apply',
                            'known_pdp_only': known_pdp_only, 'products': [], 'issues': []}
        try:
            result = await self._run_update(product_id, phase2_backfill, dry_run=dry_run,
                                            product_slugs=product_slugs, known_pdp_only=known_pdp_only)
            self.last_report['status'] = 'completed'
            return result
        except Exception as error:
            self.last_report['status'] = 'failed'
            self.last_report['issues'].append(str(error))
            raise
        finally:
            if report_path:
                Path(report_path).write_text(json.dumps(self.last_report, ensure_ascii=False, indent=2) + '\n')

    async def _run_update(self, product_id=None, phase2_backfill=False, *, dry_run=False,
                          product_slugs=None, known_pdp_only=False):
        if product_id is not None and phase2_backfill:
            raise RuntimeError(
                "Phase 2 backfill must validate the full published catalog"
            )
        products = self.get_all_products()
        if product_slugs is not None:
            if (product_id is not None or phase2_backfill or not product_slugs
                    or len(set(product_slugs)) != len(product_slugs)
                    or any(not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', s) for s in product_slugs)):
                raise RuntimeError('Invalid or conflicting scoped product selection')
            available = {p['slug'] for p in products}
            if set(product_slugs) - available:
                raise RuntimeError('Requested product slug was not found in the catalog')
            products = [p for p in products if p['slug'] in product_slugs]
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
        self.last_report['ingestion'] = aggregate_result
        failures = []
        products_with_offers = 0
        merchant_counts = {}
        for processed_count, p in enumerate(products, start=1):
            name = p['name']
            expected_specs = self.get_expected_specs(p.get('attributes'))
            targets = daily_offer_targets(p, expected_specs)
            product_report = {'slug': p['slug'], 'targets': targets,
                              'purchasable_merchants': [], 'prepared_offers': [],
                              'rejected_live_observations': 0, 'discovered_destinations': 0}
            self.last_report['products'].append(product_report)

            logging.info(f"\n🔍 Updating prices for: {name}")
            try:
                refreshed = []
                if targets:
                    try:
                        refreshed = await asyncio.to_thread(
                            self.offer_refresher.refresh_existing_offer_targets,
                            name, targets, expected_specs,
                        )
                    except Exception as error:
                        # An unavailable old destination must not suppress
                        # ordinary discovery for the product.
                        logging.warning('  ⚠️ Retailer refresh unavailable (%s); trying discovery',
                                        type(error).__name__)
                    logging.info('  🔎 Existing retailer destinations: %s observed offer(s)', len(refreshed))
                # Reuse successful discovery work. A stored URL alone never
                # counts; every refreshed offer must pass the current matcher.
                accepted_refresh = [offer for offer in refreshed if self.live_offer_is_valid(p, offer, expected_specs)]
                product_report['rejected_live_observations'] += len(refreshed) - len(accepted_refresh)
                refreshed = accepted_refresh
                refreshed_sellers = {str(offer.get('merchant', '')).strip().casefold()
                                     for offer in refreshed if offer.get('availability') in PURCHASABLE}
                discovered = []
                if len(refreshed_sellers) < 4 and not known_pdp_only:
                    hints = await self.price_scraper.get_best_prices(name, expected_specs)
                    hints = hints if isinstance(hints, list) else []
                    discovery_targets = [{'source_url': row.get('url')} for row in hints
                                         if isinstance(row, dict) and _safe_url(row.get('url'))]
                    product_report['discovered_destinations'] = len(discovery_targets)
                    # Discovery cards are URL hints, not current merchant cash
                    # prices. A second live-PDP read (or same-run exact URL
                    # cache hit) must establish every discovered offer too.
                    discovered = await asyncio.to_thread(
                        self.offer_refresher.refresh_existing_offer_targets,
                        name, discovery_targets, expected_specs,
                    ) if discovery_targets else []
                    targets = targets + discovery_targets
                accepted_discovery = [offer for offer in discovered if self.live_offer_is_valid(p, offer, expected_specs)]
                product_report['rejected_live_observations'] += len(discovered) - len(accepted_discovery)
                discovered = accepted_discovery
                by_listing = {}
                # Direct retailer observations override a search-card copy of
                # the same URL, including a newly observed out-of-stock state.
                for offer in [*discovered, *refreshed]:
                    merchant = TRPriceScraper._standardize_merchant_name(str(offer.get('merchant', '')))
                    by_listing[(merchant.casefold(), offer.get('url'))] = {**offer, 'merchant': merchant}
                by_seller = {}
                for offer in by_listing.values():
                    merchant = offer['merchant'].casefold()
                    prior = by_seller.get(merchant)
                    is_available = offer.get('availability') in PURCHASABLE
                    prior_available = prior and prior.get('availability') in PURCHASABLE
                    if prior is None or (is_available and not prior_available) or (
                        is_available and prior_available and float(offer['price']) < float(prior['price'])
                    ):
                        by_seller[merchant] = offer
                offers = list(by_seller.values())
                if offers:
                    observed_at = self.utc_timestamp()
                    checked_at = self.utc_timestamp()
                    product_offers = self.prepare_product_offers(
                        p,
                        offers,
                        observed_at,
                        checked_at,
                    )
                    if not dry_run:
                        pending_offers.extend(product_offers)
                    if any(record['availability'] in PURCHASABLE for record in product_offers):
                        products_with_offers += 1
                    merchant_counts[p["slug"]] = len({
                        record["seller"].strip().casefold()
                        for record in product_offers
                        if record['availability'] in PURCHASABLE
                    })
                    product_report['purchasable_merchants'] = sorted({r['seller'] for r in product_offers if r['availability'] in PURCHASABLE})
                    product_report['prepared_offers'] = product_offers
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
                product_report['error'] = str(e)

            if hasattr(self.offer_refresher, 'target_report'):
                product_report['source_attempts'] = self.offer_refresher.target_report(name, targets, expected_specs)

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

        self.last_report['coverage'] = {
            'products': len(products), 'with_purchasable_offer': products_with_offers,
            'with_two_merchants': sum(c >= 2 for c in merchant_counts.values()),
            'without_two_merchants': [p['slug'] for p in products if merchant_counts.get(p['slug'], 0) < 2],
            'protected_pair_failures': [p['slug'] for p in products
                if p['slug'] in PROTECTED_TWO_STORE_SLUGS and merchant_counts.get(p['slug'], 0) < 2],
        }
        self.last_report['ingestion'] = aggregate_result
        if changed_paths and not dry_run:
            submit_urls(changed_paths + ["/", "/products"])

        logging.info("\n🏁 Price update completed!")
        run_issues = []
        if self.last_report['coverage']['protected_pair_failures']:
            run_issues.append('Protected budget models did not retain two live verified merchants: '
                              + ', '.join(self.last_report['coverage']['protected_pair_failures']))
        if aggregate_result["variant_rejected"]:
            run_issues.append(
                f"{aggregate_result['variant_rejected']} offer(s) rejected for "
                "identity/variant conflicts; valid committed updates retained"
            )
        if (
            product_id is None and product_slugs is None
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
    parser.add_argument('--dry-run', action='store_true', help='GET-only: no ingestion or IndexNow submission')
    parser.add_argument('--known-pdp-only', action='store_true', help='Skip discovery; inspect known retailer PDPs only')
    parser.add_argument('--slugs', help='Comma-separated exact published slugs for a scoped run')
    parser.add_argument('--report', default='daily-price-report.json')
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
            dry_run=arguments.dry_run,
            known_pdp_only=arguments.known_pdp_only,
            product_slugs=arguments.slugs.split(',') if arguments.slugs else None,
            report_path=arguments.report,
        )
    )
