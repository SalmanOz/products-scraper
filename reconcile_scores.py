"""Authenticated score-only maintenance. No retailer requests or offer writes."""
import json
from pathlib import Path

from offer_ingestion_client import OfferIngestionClient


def reconcile(client, report_path="score-reconciliation.json"):
    try:
        result = client.reconcile_scores()
    except Exception:
        # Do not serialize request details, credentials or exception messages.
        Path(report_path).write_text(json.dumps({"status": "failed"}) + "\n")
        raise
    Path(report_path).write_text(json.dumps({"status": "completed", **result}, indent=2) + "\n")
    print(f"Score reconciliation completed: {result['scored']}/{result['total']} scored; "
          f"{result['skipped']} remain ineligible. Offer timestamps were not renewed.")
    return result


if __name__ == "__main__":
    reconcile(OfferIngestionClient.from_env())
