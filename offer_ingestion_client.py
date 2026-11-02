import logging
import math
import os
import re
import time
from urllib.parse import parse_qsl, urljoin, urlparse

import requests


RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_INGESTION_BATCH_SIZE = 500
CATALOG_PAGE_SIZE = 500
COMPLETE_TARGET_PAGE_SIZE = 12
MAX_COMPLETE_TARGET_PAGES = 5


class IngestionClientError(RuntimeError):
    pass


class OfferIngestionClient:
    def __init__(
        self,
        base_url,
        secret,
        session=None,
        timeout=(10, 120),
        max_attempts=4,
        sleep=time.sleep,
    ):
        normalized_base_url = str(base_url or "").strip().rstrip("/")
        parsed = urlparse(normalized_base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise IngestionClientError(
                "TEKNOSKOR_INGESTION_URL must be an https:// base URL"
            )

        normalized_secret = str(secret or "").strip()
        if len(normalized_secret) < 32:
            raise IngestionClientError(
                "SCRAPER_INGESTION_SECRET must contain at least 32 characters"
            )

        self.base_url = normalized_base_url
        self.secret = normalized_secret
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_attempts = max(1, int(max_attempts))
        self.sleep = sleep

    @classmethod
    def from_env(cls):
        return cls(
            os.getenv("TEKNOSKOR_INGESTION_URL"),
            os.getenv("SCRAPER_INGESTION_SECRET"),
        )

    def _url(self, path):
        return urljoin(f"{self.base_url}/", path.lstrip("/"))

    @staticmethod
    def _json_body(response):
        try:
            return response.json()
        except (TypeError, ValueError) as error:
            raise IngestionClientError(
                f"HTTP {response.status_code} returned invalid JSON"
            ) from error

    def _request_json(self, method, path, **kwargs):
        url = self._url(path)
        last_error = None
        scoring_retry_pending = False

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    timeout=self.timeout,
                    **kwargs,
                )
            except requests.RequestException as error:
                last_error = error
                if attempt == self.max_attempts:
                    break
                delay = 2 ** (attempt - 1)
                logging.warning(
                    "  ⚠️ HTTPS request failed (%s/%s): %s; retrying in %ss",
                    attempt,
                    self.max_attempts,
                    error,
                    delay,
                )
                self.sleep(delay)
                continue

            try:
                body = self._json_body(response)
            except IngestionClientError as error:
                last_error = error
                if (
                    response.status_code not in RETRYABLE_STATUS_CODES
                    or attempt == self.max_attempts
                ):
                    raise
                delay = 2 ** (attempt - 1)
                logging.warning(
                    "  ⚠️ HTTPS endpoint returned invalid JSON with HTTP %s "
                    "(%s/%s); retrying in %ss",
                    response.status_code,
                    attempt,
                    self.max_attempts,
                    delay,
                )
                self.sleep(delay)
                continue

            if (
                method.upper() == "POST"
                and isinstance(body, dict)
                and body.get("committed") is True
            ):
                # The rows are durable, but price/performance lists remain
                # stale until the score recalculation succeeds. Replaying the
                # exact observation is idempotent and gives that follow-up
                # work another bounded chance to complete.
                if body.get("scores_recalculated") is False or (
                    scoring_retry_pending and body.get("scores_recalculated") is not True
                ):
                    scoring_retry_pending = True
                    last_error = IngestionClientError(
                        "Offer batch was committed, but score recalculation "
                        f"failed with HTTP {response.status_code}"
                    )
                    if (
                        response.status_code in RETRYABLE_STATUS_CODES
                        and attempt < self.max_attempts
                    ):
                        delay = 2 ** (attempt - 1)
                        logging.warning(
                            "  ⚠️ Offer batch committed, but score "
                            "recalculation failed (%s/%s); retrying the "
                            "idempotent batch in %ss",
                            attempt,
                            self.max_attempts,
                            delay,
                        )
                        self.sleep(delay)
                        continue
                    raise IngestionClientError(
                        "Offer batch was committed, but score recalculation "
                        f"failed after {attempt} attempt(s)"
                    )
                return body

            if 200 <= response.status_code < 300:
                return body

            message = body.get("error") if isinstance(body, dict) else None
            last_error = IngestionClientError(
                f"HTTP {response.status_code} from {path}: "
                f"{message or 'request failed'}"
            )
            if (
                response.status_code not in RETRYABLE_STATUS_CODES
                or attempt == self.max_attempts
            ):
                raise last_error

            delay = 2 ** (attempt - 1)
            logging.warning(
                "  ⚠️ HTTPS endpoint returned HTTP %s (%s/%s); "
                "retrying in %ss",
                response.status_code,
                attempt,
                self.max_attempts,
                delay,
            )
            self.sleep(delay)

        raise IngestionClientError(
            f"HTTPS request to {path} failed after "
            f"{self.max_attempts} attempts: {last_error}"
        )

    def get_published_products(self):
        products = []
        seen_ids = set()
        page = 1
        headers = {
            "Authorization": f"Bearer {self.secret}",
            "Accept": "application/json",
        }

        while True:
            body = self._request_json(
                "GET",
                "/api/ingestion/catalog",
                headers=headers,
                params={
                    "page": str(page),
                    "limit": str(CATALOG_PAGE_SIZE),
                    "include_offer_targets": "1",
                },
            )
            if not isinstance(body, dict) or not isinstance(
                body.get("products"), list
            ):
                raise IngestionClientError(
                    "Catalog endpoint returned an unexpected response"
                )

            for raw_product in body["products"]:
                try:
                    product_id = int(raw_product["id"])
                except (KeyError, TypeError, ValueError) as error:
                    raise IngestionClientError(
                        "Catalog product is missing a valid id"
                    ) from error

                name = str(raw_product.get("name") or "").strip()
                slug = str(raw_product.get("slug") or "").strip()
                if product_id <= 0 or not name or not slug:
                    raise IngestionClientError(
                        f"Catalog product {product_id} is missing name or slug"
                    )
                if product_id in seen_ids:
                    raise IngestionClientError(
                        f"Catalog endpoint returned duplicate product id {product_id}"
                    )

                attributes = raw_product.get("attributes")
                targets = raw_product.get("offer_targets")
                truncated = raw_product.get("offer_targets_truncated")
                if ("offer_targets_truncated" in raw_product and
                        type(truncated) is not bool):
                    raise IngestionClientError(
                        f"Catalog product {product_id} has invalid target completeness"
                    )
                if truncated is not None and not isinstance(targets, list):
                    raise IngestionClientError(
                        f"Catalog product {product_id} has invalid offer targets"
                    )
                targets = targets if isinstance(targets, list) else []
                # Refresh hints, not reusable prices or SKU proof. The fetcher
                # validates each destination again before making a request.
                offer_targets = [
                    {key: target.get(key) for key in (
                        "merchant_name", "source_url", "checked_at",
                    )}
                    | ({"seller_name": target["seller_name"]}
                       if "seller_name" in target else {})
                    for target in targets[:12]
                    if isinstance(target, dict)
                    and isinstance(target.get("source_url"), str)
                    and len(target["source_url"]) <= 1000
                ]
                # Filtering or the local cap can only make completeness worse.
                if len(offer_targets) != len(targets):
                    truncated = True
                products.append(
                    {
                        "id": product_id,
                        "name": name,
                        "slug": slug,
                        "attributes": (
                            attributes if isinstance(attributes, dict) else {}
                        ),
                        "offer_targets": offer_targets,
                        "offer_targets_truncated": truncated,
                    }
                )
                seen_ids.add(product_id)

            has_more = body.get("hasMore") is True
            if not has_more:
                break
            page += 1

        if not products:
            raise IngestionClientError(
                "Catalog endpoint returned no published products"
            )
        return products

    def get_complete_offer_targets(self, product_id):
        """Read every safe target for one product with bounded stable pages.

        This is only extra catalog evidence. A missing or changing page, a
        discarded URL or a duplicate target fails closed; no price is read
        from these records.
        """
        if type(product_id) is not int or product_id <= 0:
            raise IngestionClientError('Complete target product id is invalid')
        headers = {'Authorization': f'Bearer {self.secret}',
                   'Accept': 'application/json'}
        first_identity = None
        first_metadata = None
        gathered = []
        seen_urls = set()
        for page in range(1, MAX_COMPLETE_TARGET_PAGES + 1):
            body = self._request_json(
                'GET', '/api/ingestion/catalog', headers=headers,
                params={'include_offer_targets': '1',
                        'offer_target_product_id': str(product_id),
                        'offer_target_page': str(page)},
            )
            rows = body.get('products') if isinstance(body, dict) else None
            if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
                raise IngestionClientError('Complete target product is unavailable')
            product = rows[0]
            identity = (product.get('id'), product.get('slug'), product.get('name'))
            if (type(identity[0]) is not int or identity[0] != product_id
                    or not isinstance(identity[1], str) or not identity[1]
                    or not isinstance(identity[2], str) or not identity[2]):
                raise IngestionClientError('Complete target product identity changed')
            if first_identity is None:
                first_identity = identity
            elif identity != first_identity:
                raise IngestionClientError('Complete target product identity changed')
            total = product.get('offer_targets_total')
            invalid = product.get('offer_targets_invalid_count')
            duplicate = product.get('offer_targets_duplicate_count')
            response_page = product.get('offer_targets_page')
            snapshot = product.get('offer_targets_snapshot')
            truncated = product.get('offer_targets_truncated')
            if (type(total) is not int or not 0 <= total <=
                    COMPLETE_TARGET_PAGE_SIZE * MAX_COMPLETE_TARGET_PAGES
                    or type(invalid) is not int or invalid < 0
                    or type(duplicate) is not int or duplicate < 0
                    or type(response_page) is not int or response_page != page
                    or type(truncated) is not bool
                    or not isinstance(snapshot, str)
                    or not re.fullmatch(r'[0-9a-f]{64}', snapshot)):
                raise IngestionClientError('Complete target page metadata is invalid')
            if invalid:
                raise IngestionClientError(
                    f'Complete target source has invalid rows '
                    f'(total={total}, invalid={invalid}, duplicate={duplicate})')
            if duplicate:
                raise IngestionClientError(
                    f'Complete target source has duplicate rows '
                    f'(total={total}, invalid={invalid}, duplicate={duplicate})')
            if truncated is not (page * COMPLETE_TARGET_PAGE_SIZE < total):
                raise IngestionClientError('Complete target page completeness is invalid')
            metadata = (total, invalid, duplicate, snapshot)
            if first_metadata is None:
                first_metadata = metadata
            elif metadata != first_metadata:
                raise IngestionClientError('Complete target snapshot changed')
            targets = product.get('offer_targets')
            expected_length = min(COMPLETE_TARGET_PAGE_SIZE,
                                  max(0, total - len(gathered)))
            if not isinstance(targets, list) or len(targets) != expected_length:
                raise IngestionClientError('Complete target page is missing rows')
            for target in targets:
                if not isinstance(target, dict):
                    raise IngestionClientError('Complete target row is invalid')
                url = target.get('source_url')
                merchant = target.get('merchant_name')
                seller = target.get('seller_name') if 'seller_name' in target else merchant
                try:
                    parsed = urlparse(url) if isinstance(url, str) else None
                    valid_url = bool(parsed and parsed.scheme == 'https' and
                                     parsed.hostname and not parsed.port and
                                     not parsed.username and not parsed.password and
                                     parsed.path not in ('', '/') and
                                     not parsed.fragment and
                                     not any(re.search(
                                         r'(?:token|auth|password|secret|signature|session|email|api.?key|access.?key|credential|add.?to.?cart|wc.?ajax|nonce|pao.?key|pao.?edit)',
                                         key, re.I)
                                         for key, _ in parse_qsl(parsed.query,
                                                                max_num_fields=50)))
                except ValueError:
                    valid_url = False
                if (not isinstance(url, str) or len(url) > 1000 or
                        not valid_url or
                        not isinstance(merchant, str) or not merchant.strip() or
                        not isinstance(seller, str) or not seller.strip() or
                        target.get('checked_at') is not None and
                        not isinstance(target.get('checked_at'), str)):
                    raise IngestionClientError('Complete target row is invalid')
                if url in seen_urls:
                    raise IngestionClientError('Complete target URL repeated across pages')
                seen_urls.add(url)
                gathered.append({key: target.get(key) for key in
                                 ('merchant_name', 'source_url', 'checked_at')}
                                | ({'seller_name': target['seller_name']}
                                   if 'seller_name' in target else {}))
            if len(gathered) == total:
                return {'id': first_identity[0], 'slug': first_identity[1],
                        'name': first_identity[2], 'offer_targets': gathered,
                        'offer_targets_truncated': False,
                        'offer_targets_total': total,
                        'offer_targets_snapshot': snapshot}
        raise IngestionClientError('Complete target page limit exceeded')

    def get_public_price_priority(self):
        """Read a bounded public catalog snapshot only to order crawl work.

        The returned values are not offers and must never be ingested as prices.
        An absent/mismatched row remains unknown rather than being treated as
        unpriced. Full sync can fall back to its original order on API errors.
        """
        rows = self._request_json(
            "GET", "/api/products", params={"limit": "1000"},
            headers={"Accept": "application/json"},
        )
        if not isinstance(rows, list) or len(rows) > 1000:
            raise IngestionClientError("Public price priority snapshot is invalid")
        status = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                product_id = int(row['id'])
                slug = row['slug']
                if (product_id <= 0 or product_id in status
                        or not isinstance(slug, str) or not slug
                        or 'fresh_lowest_price' not in row
                        or 'fresh_offer_count' not in row):
                    continue
                price = float(row['fresh_lowest_price'] or 0)
                offer_count = int(row['fresh_offer_count'] or 0)
                if not math.isfinite(price) or price < 0 or offer_count < 0:
                    continue
                status[product_id] = (slug, price > 0 and offer_count > 0)
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        return status

    def get_offer_identity_readiness(self):
        """Authenticated, current commercial-offer identity readback."""
        body = self._request_json(
            "GET", "/api/ingestion/price-index-readiness",
            headers={"Authorization": f"Bearer {self.secret}",
                     "Accept": "application/json"},
        )
        identity = body.get('offerIdentity') if isinstance(body, dict) else None
        if (not isinstance(identity, dict)
                or identity.get('evidenceVersion') != 'product-offer-match-v1'
                or identity.get('mode') != 'shadow_audit_only'
                or not isinstance(identity.get('productCoverage'), list)):
            raise IngestionClientError('Offer identity readiness response is invalid')
        return identity

    def ingest_offers(self, offers):
        if not offers:
            return {
                "accepted": 0,
                "stale_ignored": 0,
                "variant_rejected": 0,
                "variant_rejections": [],
                "changed_paths": [],
                "committed": True,
            }

        totals = {
            "accepted": 0,
            "stale_ignored": 0,
            "variant_rejected": 0,
            "variant_rejections": [],
            "changed_paths": [],
            "committed": True,
        }
        headers = {
            "Authorization": f"Bearer {self.secret}",
            "Content-Type": "application/json",
        }

        for start in range(0, len(offers), MAX_INGESTION_BATCH_SIZE):
            batch = offers[start : start + MAX_INGESTION_BATCH_SIZE]
            body = self._request_json(
                "POST",
                "/api/ingestion/offers",
                headers=headers,
                json={"offers": batch},
            )
            if not isinstance(body, dict) or body.get("committed") is not True:
                raise IngestionClientError(
                    "Offer ingestion response did not confirm a commit"
                )

            try:
                accepted = int(body.get("accepted", 0))
                stale_ignored = int(body.get("stale_ignored", 0))
                variant_rejected = int(body.get("variant_rejected", 0))
            except (TypeError, ValueError) as error:
                raise IngestionClientError(
                    "Offer ingestion response has invalid counters"
                ) from error
            if (
                min(accepted, stale_ignored, variant_rejected) < 0
                or accepted + stale_ignored + variant_rejected != len(batch)
            ):
                raise IngestionClientError(
                    "Offer ingestion response count does not match submitted batch"
                )
            rejections = body.get("variant_rejections", [])
            if not isinstance(rejections, list) or len(rejections) != variant_rejected:
                raise IngestionClientError(
                    "Offer ingestion variant rejection details do not match count"
                )
            seen_indices = set()
            for rejection in rejections:
                index = rejection.get("index") if isinstance(rejection, dict) else None
                if (
                    not isinstance(index, int) or isinstance(index, bool)
                    or index < 0 or index >= len(batch) or index in seen_indices
                    or not isinstance(rejection.get("product_slug"), str)
                    or not rejection["product_slug"].strip()
                    or not isinstance(rejection.get("issues"), list)
                    or not rejection["issues"]
                    or any(not isinstance(issue, str) or not issue.strip()
                           for issue in rejection["issues"])
                ):
                    raise IngestionClientError(
                        "Offer ingestion returned malformed variant rejection details"
                    )
                seen_indices.add(index)
                # Endpoint indices are local to each HTTP batch. Report indices
                # relative to the complete offer list supplied to this call.
                totals["variant_rejections"].append({
                    **rejection, "index": start + index,
                })

            totals["accepted"] += accepted
            totals["stale_ignored"] += stale_ignored
            totals["variant_rejected"] += variant_rejected
            changed_paths = body.get("changed_paths")
            if isinstance(changed_paths, list):
                totals["changed_paths"].extend(
                    path
                    for path in changed_paths
                    if isinstance(path, str) and path.startswith("/")
                )

        totals["changed_paths"] = list(
            dict.fromkeys(totals["changed_paths"])
        )
        return totals

    def reconcile_scores(self):
        """Recompute from existing server evidence; never refresh offer dates."""
        body = self._request_json(
            "POST", "/api/ingestion/scoring/reconcile",
            headers={"Authorization": f"Bearer {self.secret}",
                     "Content-Type": "application/json"},
            json={},
        )
        if not isinstance(body, dict) or body.get("scores_recalculated") is not True:
            raise IngestionClientError("Score reconciliation did not confirm success")
        counters = [body.get(key) for key in ("total", "scored", "skipped")]
        if (any(type(value) is not int or value < 0 for value in counters)
                or counters[0] != counters[1] + counters[2]):
            raise IngestionClientError("Score reconciliation returned invalid counters")
        return body
