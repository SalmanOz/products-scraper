import unittest
from unittest.mock import patch

from catalog_expansion import expand_catalog, load_reviewed_batch
from source_ingestion import SourceIngestionError


class FakeResponse:
    status_code = 200


class FakeClient:
    base_url = "https://teknoskor.com"

    def __init__(self):
        self.calls = []
        self.body = {}

    def _request_with_retry(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        batch = kwargs["json"]
        self.body = {
            "committed": True,
            "batch_id": batch["batch_id"],
            "products": [{"slug": product["slug"], "outcome": "created"} for product in batch["products"]],
        }
        return FakeResponse()

    def _json_body(self, _response):
        return self.body


class CatalogExpansionTests(unittest.TestCase):
    def test_reviewed_file_is_sent_with_authentication(self):
        client = FakeClient()
        with patch("catalog_expansion.SourceIngestionClient.from_env", return_value=client):
            result = expand_catalog("tr-2026-10-new-v1")
        self.assertTrue(result["committed"])
        self.assertEqual(client.calls[0][2]["json"], load_reviewed_batch("tr-2026-10-new-v1"))
        self.assertTrue(client.calls[0][2]["authenticated"])

    def test_unknown_or_path_traversal_batches_never_reach_api(self):
        with patch("catalog_expansion.SourceIngestionClient.from_env") as client:
            for batch_id in ("unknown", "../secret", "/tmp/secret", "none", "a" * 161):
                with self.assertRaises(ValueError):
                    expand_catalog(batch_id)
            client.assert_not_called()

    def test_legacy_and_new_lists_have_disjoint_identities(self):
        old = load_reviewed_batch("tr-2026-08-popular-new-v1")["products"]
        new = load_reviewed_batch("tr-2026-10-new-v1")["products"]
        self.assertEqual(len(old), 15)
        self.assertEqual(len(new), 6)
        self.assertFalse({p["slug"] for p in old} & {p["slug"] for p in new})

    def test_incomplete_or_wrong_response_cannot_report_success(self):
        invalid_bodies = [
            {"committed": False},
            {"committed": True, "products": []},
            {"committed": True, "batch_id": "wrong", "products": [{"slug": "wrong"}]},
        ]
        for body in invalid_bodies:
            client = FakeClient()
            with patch("catalog_expansion.SourceIngestionClient.from_env", return_value=client), \
                 patch.object(client, "_json_body", return_value=body), \
                 self.assertRaises(SourceIngestionError):
                expand_catalog("tr-2026-10-new-v1")


if __name__ == "__main__":
    unittest.main()
