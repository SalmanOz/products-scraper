
import logging
import asyncio
import json
import math
import os
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
from reviewed_offer_targets import daily_offer_targets, with_reviewed_radio, PROTECTED_TWO_STORE_SLUGS
from vatan_sitemap_source import VatanSitemapSource
from xiaomi_17t_preflight import PROFILES as XIAOMI_17T_PREFLIGHT_URLS
from price_sync_priority import prioritize_unpriced_products, prioritize_discoverable_products
from fold8_ultra_preflight import (PROFILE as FOLD8_ULTRA_PROFILE,
                                   URLS as FOLD8_ULTRA_PREFLIGHT_URLS,
                                   load_verified_offers as load_fold8_ultra_offers)
from mediamarkt_marketplace_source import (PROFILES as MEDIAMARKT_MARKETPLACE_PROFILES,
                                           ALL_PROFILES as ALL_MARKETPLACE_PROFILES,
                                           S24FE_PROFILES, S24FE_URL,
                                           A37_PROFILES, scoped_a37_specs,
                                           reviewed_marketplace_offer)
from mediamarkt_marketplace_preflight import (load_verified_offers as load_marketplace_offers,
                                              no_merchant_collision)
from mediamarkt_s24fe_preflight import load_verified_offer as load_s24fe_offer
from mediamarkt_a37_preflight import load_verified_offers as load_a37_offers

load_dotenv()

MIN_FULL_RUN_PRODUCT_COVERAGE = 0.50
INGESTION_PRODUCT_BATCH_SIZE = 10
MAX_OFFER_PRICE = 5_000_000
PURCHASABLE = {'in_stock', 'limited'}
MAX_SOURCE_OBSERVATION_AGE_SECONDS = 3 * 60 * 60

class PriceUpdater:
    def __init__(self, ingestion_client=None, price_scraper=None, offer_refresher=None,
                 vatan_sitemap_source=None):
        self.ingestion_client = (
            ingestion_client or OfferIngestionClient.from_env()
        )
        self.price_scraper = price_scraper or TRPriceScraper()
        self.offer_matcher = TRPriceScraper()
        self.offer_refresher = offer_refresher or RetailerOfferRefresher()
        self.vatan_sitemap_source = vatan_sitemap_source or VatanSitemapSource()
        self.pazarama_source = getattr(self.offer_refresher, 'pazarama_source', None)
        self.mediamarkt_source = getattr(self.offer_refresher, 'mediamarkt_source', None)
        self.apple_store_source = getattr(self.offer_refresher, 'apple_store_source', None)
        self.turk_telekom_source = getattr(self.offer_refresher, 'turk_telekom_source', None)
        if isinstance(self.price_scraper, TRPriceScraper):
            self.price_scraper.retailer_offer_refresher = self.offer_refresher
        self.last_report = None
        self._raw_target_products = {}
        self._raw_target_attempts = set()

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

    def _raw_target_key(self, product, offer):
        merchant = self.offer_matcher._standardize_merchant_name(str(offer.get('merchant', '')))
        return (product.get('id'), product.get('slug'), product.get('name'),
                merchant.casefold(), offer.get('url'))

    def _collision_product(self, product, offer):
        cached = self._raw_target_products.get(self._raw_target_key(product, offer))
        if cached:
            try:
                age = (datetime.fromisoformat(self.utc_timestamp().replace('Z', '+00:00')) -
                       datetime.fromisoformat(cached['checked_at'].replace('Z', '+00:00'))).total_seconds()
                if -60 <= age <= 5 * 60:
                    return cached['product']
            except (KeyError, ValueError, TypeError):
                pass
        return product

    def _resolve_raw_target_proofs(self, product, offers, product_report):
        """Inspect every raw seller when only unsupported refresh hints were rejected."""
        for offer in offers:
            if not isinstance(offer, dict) or offer.get('platform') not in {'MediaMarkt', 'Hepsiburada', 'Trendyol', 'Teknosa'}:
                continue
            key = self._raw_target_key(product, offer)
            if key in self._raw_target_attempts:
                continue
            self._raw_target_attempts.add(key)
            seller = self.offer_matcher._standardize_merchant_name(str(offer.get('merchant', '')))
            record = {'seller': seller, 'url': offer.get('url'), 'status': 'unavailable'}
            product_report.setdefault('raw_target_proofs', []).append(record)
            try:
                raw = self.ingestion_client.get_raw_seller_collision_proof(
                    product['id'], seller, offer['url'])
                inspected = self.ingestion_client.get_offer_target_inspection(product['id'])
                reasons = raw.get('refresh_targets_invalid_reasons', {})
                total = raw.get('refresh_targets_total')
                invalid = raw.get('refresh_targets_invalid_count')
                if (raw.get('purpose') != 'read_only_raw_seller_collision_proof'
                        or raw.get('writes') is not False or raw.get('product_id') != product['id']
                        or raw.get('complete') is not True or raw.get('blocked') is not False
                        or raw.get('reasons') != [] or type(total) is not int or not 0 <= total <= 12
                        or raw.get('refresh_targets_duplicate_count') != 0
                        or type(invalid) is not int or invalid < 1
                        or invalid != reasons.get('unsupported_host', -1) + reasons.get('retailer_query_restricted', -1)
                        or raw.get('stored_rows_checked') != total + invalid
                        or total + invalid > 60
                        or not isinstance(raw.get('refresh_targets_snapshot'), str)
                        or not re.fullmatch(r'[0-9a-f]{64}', raw['refresh_targets_snapshot'])
                        or raw.get('unknown_seller_rows') != 0 or raw.get('invalid_url_rows') != 0
                        or raw.get('invalid_row_identity_rows') != 0
                        or raw.get('proposed_seller_other_url_rows') != 0):
                    raise ValueError('raw_seller_or_target_rejection')
                targets = inspected.get('offer_targets')
                if (any(inspected.get(field) != product.get(field) for field in ('id', 'slug', 'name', 'attributes'))
                        or inspected.get('offer_targets_total') != total
                        or inspected.get('offer_targets_invalid_count') != invalid
                        or inspected.get('offer_targets_duplicate_count') != 0
                        or inspected.get('offer_targets_truncated') is not True
                        or inspected.get('offer_targets_snapshot') != raw.get('refresh_targets_snapshot')
                        or inspected.get('offer_targets_invalid_reasons') != reasons
                        or not isinstance(targets, list) or len(targets) != total
                        or product.get('offer_targets') != targets
                        or product.get('offer_targets_truncated') is not True):
                    raise ValueError('raw_target_snapshot_or_catalog_changed')
                verified = {**product, 'offer_targets': targets, 'offer_targets_total': total,
                            'offer_targets_snapshot': inspected['offer_targets_snapshot'],
                            'offer_targets_truncated': False}
                if not no_merchant_collision(verified, {'seller_name': seller}, offer['url'],
                                             expected_host=urlparse(offer['url']).hostname):
                    raise ValueError('seller_collision_in_refresh_targets')
                self._raw_target_products[key] = {'product': verified, 'checked_at': self.utc_timestamp()}
                record.update(status='ready', raw_rows_checked=raw['stored_rows_checked'],
                              rejected_refresh_hints=invalid)
            except Exception as error:
                record['reason'] = (str(error) if isinstance(error, ValueError) and str(error) in {
                    'raw_seller_or_target_rejection', 'raw_target_snapshot_or_catalog_changed',
                    'seller_collision_in_refresh_targets'} else type(error).__name__)

    def hydrate_marketplace_target_proof(self, product, offers, product_report):
        """Resolve a truncated seller snapshot only after a live marketplace read."""
        if not any(isinstance(offer, dict) and offer.get('platform') in {'MediaMarkt', 'Hepsiburada', 'Trendyol', 'Teknosa'}
                   for offer in offers):
            return
        if 'complete_target_proof' in product_report:
            if product_report['complete_target_proof'].get('raw_check_needed'):
                self._resolve_raw_target_proofs(product, offers, product_report)
            return
        targets = product.get('offer_targets', [])
        if (product.get('offer_targets_truncated') is False or
                product.get('offer_targets_truncated') is not True and len(targets) < 12):
            return
        try:
            complete = self.ingestion_client.get_complete_offer_targets(product['id'])
            if (not isinstance(complete, dict) or
                    any(complete.get(key) != product.get(key) for key in ('id', 'slug', 'name'))):
                raise ValueError('Complete target catalog identity changed')
            # The ingestion client verifies every page, completeness and stable
            # snapshot; the usual collision guard still checks the full result.
            product.update({key: complete[key] for key in (
                'offer_targets', 'offer_targets_truncated', 'offer_targets_total',
                'offer_targets_snapshot')})
            product_report['complete_target_proof'] = {
                'status': 'ready', 'target_count': len(product['offer_targets'])}
        except Exception as error:
            message = str(error)
            safe_reason = (message if re.fullmatch(
                r'Complete target source has (?:invalid|duplicate) rows '
                r'\(total=\d+, invalid=\d+, duplicate=\d+\)', message)
                else type(error).__name__)
            product_report['complete_target_proof'] = {
                'status': 'unavailable', 'reason': safe_reason}
            if safe_reason.startswith('Complete target source has invalid rows '):
                product_report['complete_target_proof']['raw_check_needed'] = True
                self._resolve_raw_target_proofs(product, offers, product_report)

    def live_offer_is_valid(self, product, offer, expected_specs):
        if not isinstance(offer, dict) or not _safe_url(offer.get('url')):
            return False
        host = _safe_url(offer['url']).hostname
        if offer['url'] in A37_PROFILES:
            audit_file = os.environ.get('MEDIAMARKT_A37_VERIFIED_AUDIT_FILE')
            verified = load_a37_offers(audit_file) if audit_file else None
            if (not verified or offer['url'] not in verified
                    or offer != verified[offer['url']]):
                return False
        merchant = self.offer_matcher._standardize_merchant_name(str(offer.get('merchant', '')))
        if offer['url'] in ALL_MARKETPLACE_PROFILES:
            profile = ALL_MARKETPLACE_PROFILES[offer['url']]
            if (not reviewed_marketplace_offer(offer, product['name'], expected_specs, product)
                    or not no_merchant_collision(self._collision_product(product, offer), profile, offer['url'])):
                return False
        elif offer.get('platform') == 'Teknosa' and offer.get('selected_teknosa_variant_verified') is True:
            from teknosa_marketplace_source import catalog_offer_matches
            if (host != 'www.teknosa.com'
                    or not catalog_offer_matches(offer, product['name'], expected_specs)
                    or not no_merchant_collision(self._collision_product(product, offer),
                         {'seller_name': merchant}, offer['url'], expected_host=host)):
                return False
        elif offer.get('platform') == 'Trendyol':
            from trendyol_catalog_source import catalog_offer_matches
            if (host != 'www.trendyol.com'
                    or not catalog_offer_matches(offer, product['name'], expected_specs)
                    or not self.offer_matcher.has_observed_offer_evidence(product['name'], offer, expected_specs)
                    or not no_merchant_collision(self._collision_product(product, offer),
                         {'seller_name': merchant}, offer['url'], expected_host=host)):
                return False
        elif offer.get('platform') == 'Hepsiburada':
            from hepsiburada_catalog_source import catalog_offer_matches
            if (host != 'www.hepsiburada.com'
                    or not catalog_offer_matches(offer, product['name'], expected_specs)
                    or not self.offer_matcher.has_observed_offer_evidence(product['name'], offer, expected_specs)
                    or not no_merchant_collision(self._collision_product(product, offer),
                         {'seller_name': merchant}, offer['url'], expected_host=host)):
                return False
        elif offer.get('platform') == 'MediaMarkt':
            from mediamarkt_marketplace_audit import generic_marketplace_offer
            if (host != 'www.mediamarkt.com.tr'
                    or not generic_marketplace_offer(offer, product['name'], expected_specs)
                    or not no_merchant_collision(self._collision_product(product, offer), {'seller_name': merchant}, offer['url'])):
                return False
        else:
            expected_merchant = self.offer_matcher._standardize_merchant_name(ALLOWED_HOSTS[host])
            if not merchant or merchant.casefold() != expected_merchant.casefold():
                return False
            from pasaj_source import catalog_offer_matches
            identity_matches = (catalog_offer_matches(offer, product['name'], expected_specs)
                                if offer.get('selected_pasaj_variant_verified') is True else
                                self.offer_matcher.has_observed_offer_evidence(product['name'], offer, expected_specs))
            if not identity_matches:
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
        self._raw_target_products.clear()
        self._raw_target_attempts.clear()
        xiaomi_preflight_only = bool(os.environ.get('XIAOMI_17T_VERIFIED_AUDIT_FILE'))
        fold8_preflight_file = os.environ.get('FOLD8_ULTRA_VERIFIED_AUDIT_FILE')
        fold8_preflight_only = bool(fold8_preflight_file)
        marketplace_preflight_file = os.environ.get('MEDIAMARKT_MARKETPLACE_VERIFIED_AUDIT_FILE')
        marketplace_preflight_only = bool(marketplace_preflight_file)
        s24fe_preflight_file = os.environ.get('MEDIAMARKT_S24FE_VERIFIED_AUDIT_FILE')
        s24fe_preflight_only = bool(s24fe_preflight_file)
        a37_preflight_file = os.environ.get('MEDIAMARKT_A37_VERIFIED_AUDIT_FILE')
        a37_preflight_only = bool(a37_preflight_file)
        if sum((xiaomi_preflight_only, fold8_preflight_only,
                marketplace_preflight_only, s24fe_preflight_only,
                a37_preflight_only)) > 1:
            raise RuntimeError('Only one scoped preflight may be active')
        if a37_preflight_only:
            if (product_id is not None or phase2_backfill or not known_pdp_only
                    or product_slugs != ['samsung-galaxy-a37-5g']):
                raise RuntimeError('A37 preflight requires only its exact 6/128 SKU and known PDPs')
            if load_a37_offers(a37_preflight_file) is None:
                raise RuntimeError('A37 two-seller preflight invalid or stale')
        if s24fe_preflight_only:
            if (product_id is not None or phase2_backfill or not known_pdp_only
                    or product_slugs != [S24FE_PROFILES[S24FE_URL]['slug']]):
                raise RuntimeError('S24 FE preflight requires only its exact reviewed SKU and known PDP')
            if load_s24fe_offer(s24fe_preflight_file) is None:
                raise RuntimeError('S24 FE exact-GTIN preflight invalid or stale')
        if marketplace_preflight_only:
            reviewed_slugs = {profile['slug'] for profile in MEDIAMARKT_MARKETPLACE_PROFILES.values()}
            if (product_id is not None or phase2_backfill or not known_pdp_only
                    or product_slugs is None or len(product_slugs) != len(reviewed_slugs)
                    or set(product_slugs) != reviewed_slugs):
                raise RuntimeError('Marketplace preflight requires only two reviewed SKUs and known PDPs')
            if load_marketplace_offers(marketplace_preflight_file) is None:
                raise RuntimeError('Marketplace two-offer preflight invalid or stale')
        if fold8_preflight_only:
            if (product_id is not None or phase2_backfill or not known_pdp_only
                    or product_slugs != [FOLD8_ULTRA_PROFILE[1]]):
                raise RuntimeError('Fold8 preflight requires only the exact reviewed SKU and known PDPs')
            if load_fold8_ultra_offers(fold8_preflight_file) is None:
                raise RuntimeError('Fold8 two-retailer preflight invalid or stale')
        if xiaomi_preflight_only and (product_id is not None or phase2_backfill
                                      or not known_pdp_only or product_slugs is None
                                      or set(product_slugs) != {p[1] for p in XIAOMI_17T_PREFLIGHT_URLS.values()}):
            raise RuntimeError('Xiaomi preflight requires only the two reviewed slugs and known PDPs')
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
        if marketplace_preflight_only:
            by_slug = {p['slug']: p for p in products}
            for url, profile in MEDIAMARKT_MARKETPLACE_PROFILES.items():
                product = by_slug.get(profile['slug'])
                if (not product or product.get('id') != profile['id']
                        or product.get('name') != profile['name']
                        or not no_merchant_collision(product, profile, url)):
                    raise RuntimeError('Marketplace catalog identity or seller platform collision')
        if s24fe_preflight_only:
            profile = S24FE_PROFILES[S24FE_URL]
            product = next((p for p in products if p.get('slug') == profile['slug']), None)
            if (not product or product.get('id') != profile['id']
                    or product.get('name') != profile['name']
                    or not no_merchant_collision(product, profile, S24FE_URL)):
                raise RuntimeError('S24 FE catalog identity or seller platform collision')
        if a37_preflight_only:
            product = next((p for p in products if p.get('slug') == 'samsung-galaxy-a37-5g'), None)
            if (not product or product.get('id') != 278
                    or product.get('name') != 'Samsung Galaxy A37 5G'
                    or scoped_a37_specs(product, self.get_expected_specs(product.get('attributes'))) is None
                    or any(not no_merchant_collision(product, profile, url)
                           for url, profile in A37_PROFILES.items())):
                raise RuntimeError('A37 catalog identity or seller platform collision')
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

        # This model is owned by the paired same-run A37 workflow. An ordinary
        # full, stored-target or single-product job must never publish only one
        # of its two selected marketplace sellers.
        if not a37_preflight_only:
            if any(p.get('id') == 278 or p.get('slug') == 'samsung-galaxy-a37-5g'
                   for p in products) and (product_id is not None or product_slugs is not None):
                raise RuntimeError('A37 offers require the exact paired preflight')
            products = [p for p in products
                        if p.get('id') != 278 and p.get('slug') != 'samsung-galaxy-a37-5g']

        if product_id is None and product_slugs is None and not known_pdp_only and products:
            from mediamarkt_source import MediaMarktSource
            from vatan_reviewed_source import VatanReviewedSource
            from pasaj_source import PasajSource
            if isinstance(self.mediamarkt_source, MediaMarktSource):
                self.mediamarkt_source.configure_catalog_budget(len(products))
            vatan_reader = getattr(self.offer_refresher, 'vatan_reviewed_source', None)
            if isinstance(vatan_reader, VatanReviewedSource):
                vatan_reader.configure_catalog_budget(len(products))
            pasaj_reader = getattr(self.offer_refresher, 'pasaj_source', None)
            if isinstance(pasaj_reader, PasajSource):
                pasaj_reader.configure_catalog_budget(len(products))
            teknosa_reader = getattr(self.offer_refresher, 'teknosa_catalog_source', None)
            if teknosa_reader is not None:
                teknosa_reader.configure_catalog_budget(len(products))
            hb_reader = getattr(self.offer_refresher, 'hepsiburada_catalog_source', None)
            if hb_reader is not None:
                hb_reader.configure_catalog_budget(len(products))
            trendyol_reader = getattr(self.offer_refresher, 'trendyol_catalog_source', None)
            if trendyol_reader is not None:
                trendyol_reader.configure_catalog_budget(len(products))

        if (product_id is None and product_slugs is None and not known_pdp_only
                and hasattr(self.ingestion_client, 'get_public_price_priority')):
            try:
                status = self.ingestion_client.get_public_price_priority()
                products, counts = prioritize_unpriced_products(products, status)
                self.last_report['discovery_priority'] = {
                    'source': 'public_current_price_snapshot', **counts,
                }
                # The source-wide PDP budget must not be consumed by speculative
                # searches for imported/global models before locally sold phones.
                from mediamarkt_source import MediaMarktSource
                if isinstance(self.mediamarkt_source, MediaMarktSource):
                    discovered_slugs = set()
                    for item in products:
                        specs = with_reviewed_radio(item, self.get_expected_specs(item.get('attributes')))
                        candidates = await asyncio.to_thread(
                            self.mediamarkt_source.discover, item['name'], specs)
                        if candidates:
                            discovered_slugs.add(item['slug'])
                    if discovered_slugs:
                        products = prioritize_discoverable_products(products, status, discovered_slugs)
                        self.last_report['discovery_priority']['retailer_candidate_models'] = len(discovered_slugs)
                        self.last_report['discovery_priority']['order'] = 'unpriced_with_pdp_then_current_then_speculative'
            except Exception as error:
                # Priority is an efficiency hint; a temporary public API
                # outage must not stop the source-verified refresh itself.
                self.last_report['discovery_priority'] = {
                    'source': 'original_catalog_order',
                    'reason': type(error).__name__,
                }

        logging.info(f"🚀 Starting price update for {len(products)} products...")

        pending_offers = []
        aggregate_result = self.empty_ingestion_result()
        self.last_report['ingestion'] = aggregate_result
        failures = []
        products_with_offers = 0
        merchant_counts = {}
        for processed_count, p in enumerate(products, start=1):
            name = p['name']
            expected_specs = with_reviewed_radio(p, self.get_expected_specs(p.get('attributes')))
            targets = daily_offer_targets(p, expected_specs)
            if a37_preflight_only:
                expected_specs = scoped_a37_specs(p, expected_specs)
                if expected_specs is None:
                    raise RuntimeError('A37 exact catalog variant changed')
                targets = [{'merchant_name': 'MediaMarkt', 'source_url': url}
                           for url in A37_PROFILES]
            else:
                targets = [row for row in targets
                           if row['source_url'] not in A37_PROFILES]
            if xiaomi_preflight_only:
                reviewed_url = next(url for url, profile in XIAOMI_17T_PREFLIGHT_URLS.items()
                                    if profile[1] == p['slug'])
                targets = [row for row in targets if row['source_url'] == reviewed_url]
                if len(targets) != 1:
                    raise RuntimeError('Exact Xiaomi MediaMarkt reviewed target missing for ' + p['slug'])
            if fold8_preflight_only:
                targets = [row for row in targets if row['source_url'] in FOLD8_ULTRA_PREFLIGHT_URLS]
                if (len(targets) != 2
                        or {row['source_url'] for row in targets} != set(FOLD8_ULTRA_PREFLIGHT_URLS)):
                    raise RuntimeError('Both exact Fold8 reviewed targets are required')
            if marketplace_preflight_only:
                reviewed_url = next(url for url, profile in MEDIAMARKT_MARKETPLACE_PROFILES.items()
                                    if profile['slug'] == p['slug'])
                targets = [row for row in targets if row['source_url'] == reviewed_url]
                if len(targets) != 1:
                    raise RuntimeError('Exact marketplace reviewed target missing for ' + p['slug'])
            if s24fe_preflight_only:
                targets = [row for row in targets if row['source_url'] == S24FE_URL]
                if len(targets) != 1:
                    raise RuntimeError('Exact S24 FE marketplace target missing')
            if a37_preflight_only:
                targets = [row for row in targets if row['source_url'] in A37_PROFILES]
                if len(targets) != len(A37_PROFILES) or {row['source_url'] for row in targets} != set(A37_PROFILES):
                    raise RuntimeError('Both exact A37 marketplace targets are required')
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
                await asyncio.to_thread(self.hydrate_marketplace_target_proof,
                                        p, refreshed, product_report)
                accepted_refresh = [offer for offer in refreshed if self.live_offer_is_valid(p, offer, expected_specs)]
                product_report['rejected_live_observations'] += len(refreshed) - len(accepted_refresh)
                refreshed = accepted_refresh
                refreshed_sellers = {str(offer.get('merchant', '')).strip().casefold()
                                     for offer in refreshed if offer.get('availability') in PURCHASABLE}
                discovered = []
                if len(refreshed_sellers) < 4 and not known_pdp_only:
                    public_sources = getattr(self.offer_refresher, 'public_sources', None)
                    public_targets = []
                    try:
                        if public_sources:
                            public_targets = await asyncio.to_thread(public_sources.discover, name, expected_specs)
                    except Exception as error:
                        product_report['public_discovery_error'] = type(error).__name__
                        logging.warning('  ⚠️ Public catalog discovery unavailable (%s)', type(error).__name__)
                    vatan_targets = []
                    try:
                        vatan_targets = await asyncio.to_thread(
                            self.vatan_sitemap_source.discover, name, expected_specs)
                    except Exception as error:
                        product_report['vatan_sitemap_error'] = type(error).__name__
                        logging.warning('  ⚠️ Vatan sitemap discovery unavailable (%s)', type(error).__name__)
                    product_report['vatan_sitemap_candidates'] = len(vatan_targets)
                    mediamarkt_targets = []
                    try:
                        if self.mediamarkt_source:
                            mediamarkt_targets = await asyncio.to_thread(
                                self.mediamarkt_source.discover, name, expected_specs)
                    except Exception as error:
                        product_report['mediamarkt_discovery_error'] = type(error).__name__
                        logging.warning('  ⚠️ MediaMarkt sitemap discovery unavailable (%s)', type(error).__name__)
                    product_report['mediamarkt_candidates'] = len(mediamarkt_targets)
                    pazarama_targets = []
                    try:
                        if self.pazarama_source:
                            pazarama_targets = await asyncio.to_thread(
                                self.pazarama_source.discover, name, expected_specs)
                    except Exception as error:
                        product_report['pazarama_discovery_error'] = type(error).__name__
                        logging.warning('  ⚠️ Pazarama category discovery unavailable (%s)', type(error).__name__)
                    product_report['pazarama_candidates'] = len(pazarama_targets)
                    turk_telekom_targets = []
                    try:
                        if self.turk_telekom_source:
                            turk_telekom_targets = await asyncio.to_thread(
                                self.turk_telekom_source.discover, name, expected_specs)
                    except Exception as error:
                        product_report['turk_telekom_discovery_error'] = type(error).__name__
                        logging.warning('  ⚠️ Türk Telekom stock-card discovery unavailable (%s)',
                                        type(error).__name__)
                    product_report['turk_telekom_candidates'] = len(turk_telekom_targets)
                    try:
                        hints = await self.price_scraper.get_best_prices(name, expected_specs)
                    except Exception as error:
                        # A discovery outage must not discard already verified
                        # PDP refreshes or new public-source destinations.
                        hints = []
                        product_report['legacy_discovery_error'] = type(error).__name__
                        logging.warning('  ⚠️ Legacy discovery unavailable (%s)', type(error).__name__)
                    hints = hints if isinstance(hints, list) else []
                    discovery_targets = public_targets + vatan_targets + mediamarkt_targets + pazarama_targets + turk_telekom_targets + [{'source_url': row.get('url')} for row in hints
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
                    product_report['targets'] = targets
                await asyncio.to_thread(self.hydrate_marketplace_target_proof,
                                        p, discovered, product_report)
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
                if fold8_preflight_only and (
                    len(offers) != 2 or {offer.get('url') for offer in offers}
                        != set(FOLD8_ULTRA_PREFLIGHT_URLS)
                ):
                    raise RuntimeError('Both Fold8 preflight observations must survive matching')
                if marketplace_preflight_only and (
                    len(offers) != 1 or offers[0].get('url') not in MEDIAMARKT_MARKETPLACE_PROFILES
                    or MEDIAMARKT_MARKETPLACE_PROFILES[offers[0]['url']]['slug'] != p['slug']
                ):
                    raise RuntimeError('Exact marketplace preflight observation must survive matching')
                if s24fe_preflight_only and (
                    len(offers) != 1 or offers[0].get('url') != S24FE_URL
                ):
                    raise RuntimeError('S24 FE exact-GTIN preflight observation must survive matching')
                if a37_preflight_only and (
                    len(offers) != len(A37_PROFILES) or
                    {offer.get('url') for offer in offers} != set(A37_PROFILES)
                ):
                    raise RuntimeError('Both A37 preflight observations must survive matching')
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
            if hasattr(self.offer_refresher, 'public_sources'):
                self.last_report['public_source_discovery'] = self.offer_refresher.public_sources.report()
            vatan_browser = getattr(self.offer_refresher, 'vatan_reviewed_source', None)
            if vatan_browser is not None:
                self.last_report['vatan_reviewed_pdp'] = vatan_browser.report()
            self.last_report['vatan_sitemap_discovery'] = self.vatan_sitemap_source.report()
            if self.mediamarkt_source:
                self.last_report['mediamarkt_source'] = self.mediamarkt_source.report()
            if self.apple_store_source:
                self.last_report['apple_store_source'] = self.apple_store_source.report()
            if self.pazarama_source:
                self.last_report['pazarama_category_discovery'] = self.pazarama_source.report()
            if self.turk_telekom_source:
                self.last_report['turk_telekom_source'] = self.turk_telekom_source.report()

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
        if fold8_preflight_only and not dry_run and (
            aggregate_result['accepted'] != 2
            or aggregate_result['stale_ignored'] != 0
            or aggregate_result['variant_rejected'] != 0
            or aggregate_result['committed'] is not True
        ):
            raise RuntimeError('Fold8 scoped ingestion did not accept both exact offers')
        if marketplace_preflight_only and not dry_run and (
            aggregate_result['accepted'] != len(MEDIAMARKT_MARKETPLACE_PROFILES)
            or aggregate_result['stale_ignored'] != 0
            or aggregate_result['variant_rejected'] != 0
            or aggregate_result['committed'] is not True
        ):
            raise RuntimeError('Marketplace scoped ingestion did not accept both exact offers')
        if s24fe_preflight_only and not dry_run and (
            aggregate_result['accepted'] != 1
            or aggregate_result['stale_ignored'] != 0
            or aggregate_result['variant_rejected'] != 0
            or aggregate_result['committed'] is not True
        ):
            raise RuntimeError('S24 FE exact-GTIN ingestion did not accept the sole offer')
        if a37_preflight_only and not dry_run and (
            aggregate_result['accepted'] != len(A37_PROFILES)
            or aggregate_result['stale_ignored'] != 0
            or aggregate_result['variant_rejected'] != 0
            or aggregate_result['committed'] is not True
        ):
            raise RuntimeError('A37 scoped ingestion did not accept both exact offers')
        if changed_paths and not dry_run:
            submit_urls(changed_paths + ["/", "/products"])

        logging.info("\n🏁 Price update completed!")
        run_issues = []
        if product_id is None and product_slugs is None and self.last_report['coverage']['protected_pair_failures']:
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
