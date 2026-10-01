"""Trigger one reviewed TeknoSkor catalog-expansion batch over HTTPS."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from source_ingestion import SourceIngestionClient, SourceIngestionError


def load_reviewed_batch(batch_id: str) -> dict:
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", batch_id) or len(batch_id) > 160:
        raise ValueError("Invalid catalog batch ID")
    path = Path(__file__).parent / "catalog_batches" / f"{batch_id}.json"
    if not path.is_file():
        raise ValueError(f"Unknown reviewed catalog batch: {batch_id}")
    batch = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(batch, dict) or set(batch) != {"batch_id", "products"} or batch["batch_id"] != batch_id:
        raise ValueError("Catalog batch identity does not match its file")
    products = batch["products"]
    if not isinstance(products, list) or not 1 <= len(products) <= 20:
        raise ValueError("Catalog batch must contain 1-20 reviewed products")
    fields = {"name", "slug", "brandSlug", "evidenceUrl", "segment"}
    for product in products:
        if not isinstance(product, dict) or set(product) != fields or not all(
            isinstance(value, str) and value.strip() for value in product.values()
        ):
            raise ValueError("Invalid reviewed product fields")
    for key in ("name", "slug"):
        if len({product[key].strip().casefold() for product in products}) != len(products):
            raise ValueError(f"Duplicate product {key}")
    return batch


def expand_catalog(batch_id: str) -> dict:
    batch = load_reviewed_batch(batch_id)
    client = SourceIngestionClient.from_env()
    response = client._request_with_retry(
        "POST",
        f"{client.base_url}/api/ingestion/catalog-expansion",
        json=batch,
        authenticated=True,
    )
    body = client._json_body(response)
    if body.get("committed") is not True:
        raise SourceIngestionError(
            "Catalog expansion API did not confirm a committed transaction",
            status_code=response.status_code,
            response_body=body,
        )
    products = body.get("products")
    if not isinstance(products, list) or not products:
        raise SourceIngestionError(
            "Catalog expansion API returned no products",
            status_code=response.status_code,
            response_body=body,
        )
    expected_slugs = {product["slug"] for product in batch["products"]}
    returned_slugs = {product.get("slug") for product in products if isinstance(product, dict)}
    if body.get("batch_id") != batch_id or returned_slugs != expected_slugs or len(products) != len(expected_slugs):
        raise SourceIngestionError("Catalog expansion response does not match the reviewed batch")
    print(json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True))
    return body


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", required=True)
    args = parser.parse_args()
    expand_catalog(args.batch)
