import copy
import json
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace

from main import KimovilScraper
from repair_spec_mapping import repair_batch, REPAIR_GROUPS
from source_ingestion import CatalogProduct, SourceIngestionError
from test_spec_sections import SOURCE_HTML


class RepairClient:
    def __init__(self):
        self.products = [CatalogProduct(
            1, "Phone One", "phone-one",
            {"Performance & Hardware": {"Model": "Chip One", "Type": "NVMe"},
             "Camera": {"Resolution": "18 Mpx"}, "battery_mah": 5000,
             "quick_specs": {"cpu": "Chip One"}, "camera_score": 8.5},
            "verified", (), "2026-10-01T00:00:00Z", ("https://cdn.teknoskor.com/one.webp",),
        )]
        self.calls = []
        self.fetches = 0
        self.concurrent_change = False

    def fetch_catalog(self, page_size):
        self.fetches += 1
        if self.concurrent_change and self.fetches == 2:
            self.products[0].attributes["Camera"]["Resolution"] = "50 Mpx"
        return copy.deepcopy(self.products)

    def submit_sources(self, records, batch_size):
        self.calls.append(records)
        for record in records:
            product = next(p for p in self.products if p.slug == record["product_slug"])
            product.attributes[record["attribute_key"]] = record["value"]
        return {"accepted": len(records), "stale_ignored": 0}


class RepairMappingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.report_path = Path(self.temp.name) / "report.json"
        self.client = RepairClient()
        self.scraper = KimovilScraper(ingestion_client=self.client)
        self.scraper.get_via_flaresolverr = lambda _url: SOURCE_HTML
        self.batch = {"batch_id": "reviewed", "products": [{"slug": "phone-one", "name": "Phone One"}]}

    def test_dry_run_saves_before_after_without_writing_sources(self):
        report = repair_batch(self.batch, self.scraper, self.report_path)
        self.assertFalse(report["applied"])
        self.assertEqual(self.client.calls, [])
        self.assertEqual(report["changes"][0]["after"]["Camera"]["Resolution"], "48 Mpx")
        self.assertEqual(json.loads(self.report_path.read_text()), report)

    def test_apply_updates_only_two_groups_and_is_idempotent(self):
        before = copy.deepcopy(self.client.products[0])
        report = repair_batch(self.batch, self.scraper, self.report_path, apply=True)
        self.assertTrue(report["applied"])
        self.assertTrue(report["verified"])
        self.assertEqual({r["attribute_key"] for r in self.client.calls[0]}, set(REPAIR_GROUPS))
        product = self.client.products[0]
        self.assertEqual(product.images, before.images)
        for key in before.attributes.keys() - set(REPAIR_GROUPS):
            self.assertEqual(product.attributes[key], before.attributes[key])
        self.assertEqual(report["changes"][0]["before"]["Camera"]["Resolution"], "18 Mpx")
        again = repair_batch(self.batch, self.scraper, self.report_path, apply=True)
        self.assertEqual(again["changes"], [])
        self.assertEqual(len(self.client.calls), 1)

    def test_unknown_or_renamed_product_is_rejected(self):
        self.batch["products"][0]["name"] = "Phone One Pro"
        with self.assertRaises(SourceIngestionError):
            repair_batch(self.batch, self.scraper, self.report_path, apply=True)
        self.assertEqual(self.client.calls, [])

    def test_source_identity_mismatch_is_rejected(self):
        self.scraper.get_via_flaresolverr = lambda _url: SOURCE_HTML.replace('"Phone One"', '"Phone Two"')
        with self.assertRaises(SourceIngestionError):
            repair_batch(self.batch, self.scraper, self.report_path, apply=True)
        self.assertEqual(self.client.calls, [])

    def test_incomplete_source_cannot_replace_existing_groups(self):
        self.scraper.get_via_flaresolverr = lambda _url: SOURCE_HTML.replace("Selfie", "Unidentified")
        with self.assertRaises(SourceIngestionError):
            repair_batch(self.batch, self.scraper, self.report_path, apply=True)
        self.assertEqual(self.client.calls, [])

    def test_unknown_storage_technology_is_omitted_not_invented(self):
        self.scraper.get_via_flaresolverr = lambda _url: SOURCE_HTML.replace("NVMe", "--")
        report = repair_batch(self.batch, self.scraper, self.report_path, apply=True)
        self.assertTrue(report["verified"])
        self.assertNotIn("Storage Type", self.client.products[0].attributes["Performance & Hardware"])

    def test_later_invalid_product_prevents_partial_batch_submission(self):
        self.batch["products"].append({"slug": "phone-two", "name": "Phone Two"})
        self.client.products.append(replace(self.client.products[0], id=2, slug="phone-two", name="Phone Two"))
        with self.assertRaises(SourceIngestionError):
            repair_batch(self.batch, self.scraper, self.report_path, apply=True)
        self.assertEqual(self.client.calls, [])

    def test_concurrent_edit_aborts_before_submission_and_keeps_backup(self):
        self.client.concurrent_change = True
        with self.assertRaises(SourceIngestionError):
            repair_batch(self.batch, self.scraper, self.report_path, apply=True)
        self.assertEqual(self.client.calls, [])
        self.assertTrue(self.report_path.is_file())


if __name__ == "__main__":
    unittest.main()
