"""Review or repair only hardware/camera mappings in an approved model batch."""

import argparse
import json
from pathlib import Path

from catalog_expansion import load_reviewed_batch
from main import KimovilScraper, SOURCE_PRODUCT_OVERRIDES
from source_ingestion import SourceIngestionError


REPAIR_GROUPS = ("Performance & Hardware", "Camera")


def repair_batch(batch, scraper, report_path, *, apply=False):
    client = scraper.get_ingestion_client()
    catalog = {product.slug: product for product in client.fetch_catalog(page_size=500)}
    targets = []
    for reviewed in batch["products"]:
        product = catalog.get(reviewed["slug"])
        if product is None or product.name != reviewed["name"]:
            raise SourceIngestionError(f"Reviewed identity missing or changed: {reviewed['slug']}")
        targets.append(product)

    changes, records = [], []
    for product in targets:
        staged = []
        override = SOURCE_PRODUCT_OVERRIDES.get(product.slug, {})
        url = override.get("url", f"https://www.kimovil.com/en/where-to-buy-{product.slug}")
        if not scraper.scrape_product_details(
            url, product_slug=product.slug,
            expected_name=override.get("expected_name", product.name),
            existing_attributes=product.attributes, existing_images=product.images,
            seed_physical_attributes=True, record_sink=staged,
        ):
            raise SourceIngestionError(f"Source could not be verified: {product.slug}; no repair submitted")
        selected = [record for record in staged if record["attribute_key"] in REPAIR_GROUPS]
        after = {record["attribute_key"]: record["value"] for record in selected}
        hardware = after.get("Performance & Hardware", {})
        camera = after.get("Camera", {})
        if (
            not all(hardware.get(key) for key in ("Model", "Processor Type", "Capacity"))
            or "Type" in hardware
            or not all(camera.get(key) for key in ("Resolution", "Selfie Resolution"))
        ):
            raise SourceIngestionError(f"Incomplete contextual specs: {product.slug}; no repair submitted")
        before = {group: product.attributes.get(group) for group in REPAIR_GROUPS}
        if before != after:
            changes.append({"slug": product.slug, "before": before, "after": after})
            records.extend(selected)

    report = {"batch_id": batch["batch_id"], "applied": False, "changes": changes}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not apply or not records:
        return report

    # Refuse to overwrite a group edited while the source pages were collected.
    current = {product.slug: product for product in client.fetch_catalog(page_size=500)}
    for change in changes:
        product = current.get(change["slug"])
        if product is None or any(
            product.attributes.get(group) != change["before"][group]
            for group in REPAIR_GROUPS
        ):
            raise SourceIngestionError(f"Concurrent spec change: {change['slug']}; no repair submitted")
    result = client.submit_sources(records, batch_size=500)
    if result["accepted"] != len(records) or result["stale_ignored"]:
        raise SourceIngestionError("Repair was not fully accepted; inspect the saved report")
    report["applied"] = True
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    refreshed = {product.slug: product for product in client.fetch_catalog(page_size=500)}
    for change in changes:
        product = refreshed.get(change["slug"])
        if product is None or product.data_quality_status != "verified" or any(
            product.attributes.get(group) != change["after"][group]
            for group in REPAIR_GROUPS
        ):
            raise SourceIngestionError(f"Post-repair verification failed: {change['slug']}")
    report["verified"] = True
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", required=True)
    parser.add_argument("--apply", action="store_true", help="Without this flag, report changes only")
    parser.add_argument("--report", type=Path, default=Path("spec-mapping-repair.json"))
    args = parser.parse_args()
    result = repair_batch(load_reviewed_batch(args.batch), KimovilScraper(), args.report, apply=args.apply)
    print(json.dumps({"batch_id": result["batch_id"], "changed_products": len(result["changes"]),
                      "applied": result["applied"], "verified": result.get("verified", False)}))
