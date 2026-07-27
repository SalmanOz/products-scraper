"""Authenticated HTTPS client for TeknoSkor product provenance ingestion."""

from __future__ import annotations

import logging
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

import requests


logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
BENCHMARK_ATTRIBUTE_KEYS = {
    "antutu_score",
    "battery_score",
    "camera_score",
    "partials",
    "performance_score",
    "screen_score",
}
ESTIMATED_ATTRIBUTE_KEYS = {"gaming_performance"}
DERIVATION_METHODOLOGY_URL = (
    "https://teknoskor.com/about#metodoloji"
)


class IngestionConfigurationError(ValueError):
    """Raised when the authenticated ingestion client is not configured."""


class SourceIngestionError(RuntimeError):
    """Raised when a source batch cannot be committed."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body or {}


class ExistingProductNotFoundError(SourceIngestionError):
    """The ingestion API deliberately refused an unknown product reference."""


@dataclass(frozen=True)
class CatalogProduct:
    id: int
    name: str
    slug: str
    attributes: dict[str, Any]


def utc_observation_time() -> str:
    """Return an ingestion-compatible UTC timestamp without local-time ambiguity."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00",
        "Z",
    )


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _contains_public_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in {
            "",
            "-",
            "--",
            "---",
            "bilinmiyor",
            "unknown",
            "n/a",
        }
    if isinstance(value, list):
        return any(_contains_public_value(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_public_value(item) for item in value.values())
    return True


def build_provenance_records(
    *,
    product_slug: str,
    source_url: str,
    attributes: dict[str, Any],
    observed_at: str,
) -> list[dict[str, Any]]:
    """Build honest, deterministic records for each public top-level claim.

    Kimovil is not a manufacturer or retailer. Its physical specifications
    therefore cannot satisfy the ingestion contract's origin enum and are not
    submitted. Only benchmark values reported by the benchmark aggregator and
    deterministic gaming estimates derived from those values are included.
    """

    if not product_slug:
        raise ValueError("product_slug is required")
    if not _is_http_url(source_url):
        raise ValueError("source_url must be an absolute HTTP(S) URL")
    if not observed_at:
        raise ValueError("observed_at is required")

    records: list[dict[str, Any]] = []
    for attribute_key in sorted(attributes):
        if (
            attribute_key not in BENCHMARK_ATTRIBUTE_KEYS
            and attribute_key not in ESTIMATED_ATTRIBUTE_KEYS
        ):
            continue
        value = attributes[attribute_key]
        if attribute_key == "partials":
            value = {
                key: float(child)
                for key, child in value.items()
                if (
                    isinstance(child, (int, float))
                    and not isinstance(child, bool)
                    and math.isfinite(float(child))
                    and float(child) > 0
                )
            } if isinstance(value, dict) else {}
        elif attribute_key in BENCHMARK_ATTRIBUTE_KEYS:
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or float(value) <= 0
            ):
                continue
        if not _contains_public_value(value):
            continue
        is_estimate = attribute_key in ESTIMATED_ATTRIBUTE_KEYS
        is_benchmark = attribute_key in BENCHMARK_ATTRIBUTE_KEYS
        value_origin = "benchmark_source" if is_benchmark else "derived"
        record_source_url = (
            DERIVATION_METHODOLOGY_URL
            if is_estimate
            else source_url
        )
        records.append(
            {
                "product_slug": product_slug,
                "attribute_key": attribute_key,
                "value_origin": value_origin,
                "source_url": record_source_url,
                "observed_at": observed_at,
                "is_estimate": is_estimate,
                "confidence": "medium",
                "value": value,
            }
        )
    if not records:
        raise ValueError("attributes contain no public values")
    return records


class SourceIngestionClient:
    """Small retrying client for the catalog and source-ingestion contracts."""

    def __init__(
        self,
        base_url: str,
        secret: str,
        *,
        session: requests.Session | None = None,
        max_attempts: int = 4,
        timeout_seconds: int = 60,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        normalized_base = base_url.strip().rstrip("/")
        parsed = urlparse(normalized_base)
        is_local_http = (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost"}
        )
        if not parsed.netloc or (
            parsed.scheme != "https" and not is_local_http
        ):
            raise IngestionConfigurationError(
                "TEKNOSKOR_INGESTION_URL must use HTTPS "
                "(HTTP is allowed only for localhost)",
            )
        if len(secret.strip()) < 32:
            raise IngestionConfigurationError(
                "SCRAPER_INGESTION_SECRET must contain at least 32 characters",
            )
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        self.base_url = normalized_base
        self.secret = secret.strip()
        self.session = session or requests.Session()
        self.max_attempts = max_attempts
        self.timeout_seconds = timeout_seconds
        self.sleep = sleep

    @classmethod
    def from_env(cls) -> "SourceIngestionClient":
        base_url = os.getenv("TEKNOSKOR_INGESTION_URL", "")
        secret = os.getenv("SCRAPER_INGESTION_SECRET", "")
        if not base_url:
            raise IngestionConfigurationError(
                "TEKNOSKOR_INGESTION_URL is required",
            )
        if not secret:
            raise IngestionConfigurationError(
                "SCRAPER_INGESTION_SECRET is required",
            )
        return cls(base_url, secret)

    @property
    def sources_url(self) -> str:
        return f"{self.base_url}/api/ingestion/sources"

    @property
    def catalog_url(self) -> str:
        return f"{self.base_url}/api/ingestion/catalog"

    def fetch_catalog(self, page_size: int = 100) -> list[CatalogProduct]:
        """Fetch all ingestion-eligible products without opening a DB connection."""

        products: list[CatalogProduct] = []
        seen_ids: set[int] = set()
        seen_slugs: set[str] = set()
        page = 1
        while True:
            response = self._request_with_retry(
                "GET",
                self.catalog_url,
                params={
                    "page": str(page),
                    "limit": str(page_size),
                },
                authenticated=True,
            )
            body = self._json_body(response)
            raw_products = body.get("products")
            if not isinstance(raw_products, list):
                raise SourceIngestionError(
                    "Catalog response does not contain a products list",
                    status_code=response.status_code,
                    response_body=body,
                )
            for index, raw in enumerate(raw_products):
                try:
                    product_id = int(raw["id"])
                    name = str(raw["name"]).strip()
                    slug = str(raw["slug"]).strip()
                    attributes = raw["attributes"]
                except (KeyError, TypeError, ValueError) as error:
                    raise SourceIngestionError(
                        f"Malformed catalog product at page {page}, "
                        f"index {index}",
                        status_code=response.status_code,
                        response_body=body,
                    ) from error
                if (
                    product_id <= 0
                    or not name
                    or not re.fullmatch(
                        r"[a-z0-9]+(?:-[a-z0-9]+)*",
                        slug,
                    )
                    or not isinstance(attributes, dict)
                ):
                    raise SourceIngestionError(
                        f"Malformed catalog product at page {page}, "
                        f"index {index}",
                        status_code=response.status_code,
                        response_body=body,
                    )
                if product_id in seen_ids or slug in seen_slugs:
                    raise SourceIngestionError(
                        f"Duplicate catalog product reference: "
                        f"id={product_id}, slug={slug}",
                        status_code=response.status_code,
                        response_body=body,
                    )
                seen_ids.add(product_id)
                seen_slugs.add(slug)
                products.append(
                    CatalogProduct(
                        product_id,
                        name,
                        slug,
                        attributes,
                    ),
                )

            has_more = body.get("hasMore") is True
            if not has_more:
                break
            page += 1

        return sorted(products, key=lambda product: product.slug)

    def submit_sources(
        self,
        records: Iterable[dict[str, Any]],
        *,
        batch_size: int = 500,
    ) -> dict[str, Any]:
        """Submit source records in stable chunks and aggregate API counters."""

        all_records = list(records)
        if not all_records:
            raise ValueError("at least one source record is required")
        if batch_size < 1 or batch_size > 1000:
            raise ValueError("batch_size must be between 1 and 1000")

        aggregate: dict[str, Any] = {
            "accepted": 0,
            "stale_ignored": 0,
            "affected_products": 0,
            "changed_products": 0,
            "changed_paths": [],
            "committed": True,
        }
        affected_paths: set[str] = set()
        for start in range(0, len(all_records), batch_size):
            batch = all_records[start : start + batch_size]
            response = self._request_with_retry(
                "POST",
                self.sources_url,
                json={"sources": batch},
                authenticated=True,
                retry_committed_503=True,
            )
            body = self._json_body(response)
            if body.get("committed") is not True:
                raise SourceIngestionError(
                    "Source API did not confirm a committed transaction",
                    status_code=response.status_code,
                    response_body=body,
                )
            accepted = int(body.get("accepted") or 0)
            stale_ignored = int(body.get("stale_ignored") or 0)
            if accepted + stale_ignored != len(batch):
                raise SourceIngestionError(
                    "Source API acknowledgement count does not match "
                    "the submitted batch",
                    status_code=response.status_code,
                    response_body=body,
                )
            aggregate["accepted"] += accepted
            aggregate["stale_ignored"] += stale_ignored
            aggregate["affected_products"] += int(
                body.get("affected_products") or 0
            )
            aggregate["changed_products"] += int(
                body.get("changed_products") or 0
            )
            changed_paths = body.get("changed_paths")
            if isinstance(changed_paths, list):
                affected_paths.update(
                    path
                    for path in changed_paths
                    if isinstance(path, str) and path.startswith("/")
                )

        aggregate["changed_paths"] = sorted(affected_paths)
        return aggregate

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        authenticated: bool,
        retry_committed_503: bool = False,
        **kwargs: Any,
    ) -> requests.Response:
        headers = dict(kwargs.pop("headers", {}))
        headers["Accept"] = "application/json"
        if authenticated:
            headers["Authorization"] = f"Bearer {self.secret}"

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    timeout=self.timeout_seconds,
                    **kwargs,
                )
            except requests.RequestException as error:
                last_error = error
                if attempt == self.max_attempts:
                    break
                self._wait_before_retry(attempt, str(error))
                continue

            body = self._json_body(response)
            if (
                retry_committed_503
                and response.status_code == 503
                and body.get("committed") is True
            ):
                if attempt < self.max_attempts:
                    self._wait_before_retry(
                        attempt,
                        "source committed but score recalculation failed",
                    )
                    continue
                raise SourceIngestionError(
                    "Source batch was committed, but score recalculation "
                    f"failed after {self.max_attempts} attempts",
                    status_code=response.status_code,
                    response_body=body,
                )
            if 200 <= response.status_code < 300:
                return response
            if (
                response.status_code in RETRYABLE_STATUS_CODES
                and attempt < self.max_attempts
            ):
                self._wait_before_retry(
                    attempt,
                    f"HTTP {response.status_code}",
                )
                continue

            error_message = str(body.get("error") or "request rejected")
            error_class = (
                ExistingProductNotFoundError
                if response.status_code == 409
                and "Product reference not found" in error_message
                else SourceIngestionError
            )
            raise error_class(
                f"Source ingestion failed: {error_message}",
                status_code=response.status_code,
                response_body=body,
            )

        raise SourceIngestionError(
            f"Source ingestion request failed after "
            f"{self.max_attempts} attempts: {last_error}",
        ) from last_error

    def _wait_before_retry(self, attempt: int, reason: str) -> None:
        delay = min(2 ** (attempt - 1), 8)
        logger.warning(
            "Ingestion request attempt %s/%s failed (%s); retrying in %ss",
            attempt,
            self.max_attempts,
            reason,
            delay,
        )
        self.sleep(delay)

    @staticmethod
    def _json_body(response: requests.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}
