import logging
import os
import time
from urllib.parse import urljoin, urlparse

import requests


RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_INGESTION_BATCH_SIZE = 500
CATALOG_PAGE_SIZE = 500


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
                if body.get("scores_recalculated") is False:
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
                products.append(
                    {
                        "id": product_id,
                        "name": name,
                        "slug": slug,
                        "attributes": (
                            attributes if isinstance(attributes, dict) else {}
                        ),
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

    def ingest_offers(self, offers):
        if not offers:
            return {
                "accepted": 0,
                "stale_ignored": 0,
                "changed_paths": [],
                "committed": True,
            }

        totals = {
            "accepted": 0,
            "stale_ignored": 0,
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

            accepted = int(body.get("accepted", 0))
            stale_ignored = int(body.get("stale_ignored", 0))
            if accepted + stale_ignored != len(batch):
                raise IngestionClientError(
                    "Offer ingestion response count does not match submitted batch"
                )

            totals["accepted"] += accepted
            totals["stale_ignored"] += stale_ignored
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
