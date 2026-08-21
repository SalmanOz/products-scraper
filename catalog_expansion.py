"""Trigger one reviewed TeknoSkor catalog-expansion batch over HTTPS."""

from __future__ import annotations

import argparse
import json

from source_ingestion import SourceIngestionClient, SourceIngestionError


def expand_catalog(batch_id: str) -> dict:
    client = SourceIngestionClient.from_env()
    response = client._request_with_retry(
        "POST",
        f"{client.base_url}/api/ingestion/catalog-expansion",
        json={"batch_id": batch_id},
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
    print(json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True))
    return body


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", required=True)
    args = parser.parse_args()
    expand_catalog(args.batch)
