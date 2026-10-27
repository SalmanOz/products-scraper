"""Offline score client/finalizer/workflow contracts; no secrets or live I/O."""
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from offer_ingestion_client import OfferIngestionClient, IngestionClientError
from reconcile_scores import reconcile


class Response:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        return self.body


SUCCESS = {"total": 4, "scored": 1, "skipped": 3, "scores_recalculated": True}


class ScoreReconciliationTests(unittest.TestCase):
    def setUp(self):
        forbidden = Mock(side_effect=AssertionError("Real network forbidden"))
        for p in (patch.object(socket.socket, "connect", forbidden),
                  patch.object(requests.sessions.Session, "request", forbidden)):
            p.start()
            self.addCleanup(p.stop)

    def client(self, responses):
        session = Mock()
        session.request.side_effect = responses
        client = OfferIngestionClient("https://isolated.example", "x" * 32,
                                      session=session, sleep=lambda _: None, max_attempts=2)
        return client, session

    def test_authenticated_empty_payload_does_not_claim_offer_freshness(self):
        client, session = self.client([Response(200, SUCCESS)])
        self.assertEqual(client.reconcile_scores(), SUCCESS)
        args, kwargs = session.request.call_args
        self.assertEqual(args, ("POST", "https://isolated.example/api/ingestion/scoring/reconcile"))
        self.assertEqual(kwargs["json"], {})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + "x" * 32)

    def test_retryable_score_failure_retries_and_requires_explicit_success(self):
        client, session = self.client([Response(503, {"scores_recalculated": False}), Response(200, SUCCESS)])
        self.assertEqual(client.reconcile_scores()["scored"], 1)
        self.assertEqual(session.request.call_count, 2)

    def test_invalid_auth_and_unconfirmed_or_inconsistent_responses_fail_closed(self):
        for status, body in [(401, {}), (404, {}), (200, {}),
                             (200, {**SUCCESS, "scores_recalculated": False}),
                             (200, {**SUCCESS, "total": 5}),
                             (200, {**SUCCESS, "scored": True}),
                             (200, {**SUCCESS, "skipped": -1})]:
            with self.subTest(status=status, body=body):
                client, session = self.client([Response(status, body)])
                with self.assertRaises(IngestionClientError):
                    client.reconcile_scores()
                self.assertEqual(session.request.call_count, 1)

    def test_offer_retry_cannot_turn_failed_scoring_into_null_success(self):
        client, _ = self.client([
            Response(503, {"committed": True, "scores_recalculated": False}),
            Response(200, {"committed": True, "scores_recalculated": None}),
        ])
        with self.assertRaises(IngestionClientError):
            client._request_json("POST", "/api/ingestion/offers", json={"offers": []})

    def test_zero_eligible_catalog_is_valid_maintenance_not_fabricated_coverage(self):
        result = {"total": 3, "scored": 0, "skipped": 3, "scores_recalculated": True}
        client, _ = self.client([Response(200, result)])
        self.assertEqual(client.reconcile_scores(), result)

    def test_finalizer_writes_result_or_failure_without_leaking_error_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "result.json"
            client = Mock()
            client.reconcile_scores.return_value = SUCCESS
            reconcile(client, report)
            self.assertEqual(json.loads(report.read_text())["status"], "completed")
            client.reconcile_scores.side_effect = RuntimeError("sensitive request detail")
            with self.assertRaises(RuntimeError):
                reconcile(client, report)
            self.assertEqual(json.loads(report.read_text()), {"status": "failed"})

    def test_daily_finalize_includes_failed_price_runs_and_excludes_readonly_modes(self):
        text = Path(__file__).with_name(".github").joinpath("workflows/scrape-prices.yml").read_text()
        finalizer = text.split("- name: Reconcile catalog scores after price refresh", 1)[1].split("- name:", 1)[0]
        self.assertIn("always() && !cancelled()", finalizer)
        for step in ["daily_prices", "expanded_prices"]:
            for outcome in ["success", "failure"]:
                self.assertIn(f"steps.{step}.outcome == '{outcome}'", finalizer)
        self.assertNotIn("outcome == 'skipped'", finalizer)
        self.assertIn("run: python reconcile_scores.py", finalizer)
        self.assertNotIn("continue-on-error", finalizer)
        # The IDs must remain attached to the actual price writers, not audits.
        self.assertIn("name: Run Price Update Scraper (TR Merchants)\n        id: daily_prices", text)
        self.assertIn("name: Discover Prices for Expanded Catalog\n        id: expanded_prices", text)

    def test_hourly_job_does_not_fetch_retailers_or_replace_pending_daily_jobs(self):
        text = Path(__file__).with_name(".github").joinpath("workflows/reconcile-scores.yml").read_text()
        self.assertIn("cron: '17 * * * *'", text)
        self.assertIn("group: teknoskor-score-reconciliation", text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn("run: python reconcile_scores.py", text)
        self.assertIn("python -m unittest -q test_score_reconciliation", text)
        self.assertNotIn("update_prices.py", text)
        self.assertNotIn("flaresolverr", text.lower())
        self.assertNotIn("continue-on-error", text)


if __name__ == "__main__":
    unittest.main()
