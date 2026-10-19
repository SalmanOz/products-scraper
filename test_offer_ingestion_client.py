"""Offline contract tests for the TeknoSkor HTTPS offer ingestion client."""

import asyncio
from unittest.mock import patch

from offer_ingestion_client import (
    IngestionClientError,
    OfferIngestionClient,
)
from update_prices import PriceUpdater


TEST_SECRET = "s" * 32


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class OneOfferScraper:
    async def get_best_prices(self, product_name, expected_specs=None):
        return [
            {
                "merchant": "Teknosa",
                "price": 21999,
                "url": "https://www.teknosa.com/example-phone-p-123",
            }
        ]


class RecordingIngestionClient:
    def __init__(self):
        self.submissions = []

    def get_published_products(self):
        return [
            {
                "id": 42,
                "name": "Example Phone 8/256GB",
                "slug": "example-phone",
                "attributes": {"ram_gb": 8, "storage_gb": 256},
            }
        ]

    def ingest_offers(self, offers):
        self.submissions.append(offers)
        return {
            "accepted": len(offers),
            "stale_ignored": 0,
            "changed_paths": (
                [f"/product/{offers[0]['product_slug']}"]
                if offers
                else []
            ),
            "committed": True,
        }


async def no_delay(_seconds):
    return None


def sample_offer(index):
    return {
        "product_id": index + 1,
        "product_slug": f"phone-{index + 1}",
        "seller": "Teknosa",
        "price": 10000 + index,
        "currency": "TRY",
        "availability": "unknown",
        "source_url": f"https://www.teknosa.com/phone-{index + 1}",
        "affiliate_url": f"https://www.teknosa.com/phone-{index + 1}",
        "observed_at": "2026-07-27T10:00:00Z",
        "checked_at": "2026-07-27T10:00:00Z",
        "is_official": False,
    }


def test_catalog_is_read_without_mysql():
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "products": [
                        {
                            "id": 42,
                            "name": "Example Phone",
                            "slug": "example-phone",
                            "attributes": {"ram_gb": 8},
                            "offer_targets": [{"merchant_name": "n11", "source_url": "https://www.n11.com/urun/phone",
                                               "checked_at": "2026-01-01T00:00:00Z", "price": 1, "source_title": "Do not reuse"}],
                        }
                    ],
                    "hasMore": True,
                },
            ),
            FakeResponse(
                200,
                {
                    "products": [
                        {
                            "id": 43,
                            "name": "Second Phone",
                            "slug": "second-phone",
                            "attributes": None,
                        }
                    ],
                    "hasMore": False,
                },
            ),
        ]
    )
    client = OfferIngestionClient(
        "https://teknoskor.com",
        TEST_SECRET,
        session=session,
        sleep=lambda _seconds: None,
    )

    products = client.get_published_products()

    assert [product["id"] for product in products] == [42, 43]
    assert products[1]["attributes"] == {}
    assert [call[2]["params"]["page"] for call in session.calls] == ["1", "2"]
    assert all(call[0] == "GET" for call in session.calls)
    assert all(
        call[2]["headers"]["Authorization"] == f"Bearer {TEST_SECRET}"
        for call in session.calls
    )
    assert all(
        call[1] == "https://teknoskor.com/api/ingestion/catalog"
        for call in session.calls
    )
    assert all(call[2]['params']['include_offer_targets'] == '1' for call in session.calls)
    assert products[1]['offer_targets'] == []
    assert products[0]['offer_targets'] == [{"merchant_name": "n11", "source_url": "https://www.n11.com/urun/phone",
                                             "checked_at": "2026-01-01T00:00:00Z"}]


def test_ingestion_batches_at_500_and_sends_bearer_token():
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "accepted": 500,
                    "stale_ignored": 0,
                    "changed_paths": ["/product/phone-1"],
                    "committed": True,
                },
            ),
            FakeResponse(
                200,
                {
                    "accepted": 1,
                    "stale_ignored": 0,
                    "changed_paths": ["/product/phone-501"],
                    "committed": True,
                },
            ),
        ]
    )
    client = OfferIngestionClient(
        "https://teknoskor.com",
        TEST_SECRET,
        session=session,
        sleep=lambda _seconds: None,
    )

    result = client.ingest_offers([sample_offer(i) for i in range(501)])

    assert result["accepted"] == 501
    assert result["changed_paths"] == [
        "/product/phone-1",
        "/product/phone-501",
    ]
    assert [len(call[2]["json"]["offers"]) for call in session.calls] == [
        500,
        1,
    ]
    assert all(
        call[2]["headers"]["Authorization"] == f"Bearer {TEST_SECRET}"
        for call in session.calls
    )


def test_committed_503_retries_until_scores_are_recalculated():
    session = FakeSession(
        [
            FakeResponse(
                503,
                {
                    "accepted": 1,
                    "stale_ignored": 0,
                    "changed_paths": [],
                    "scores_recalculated": False,
                    "committed": True,
                },
            ),
            FakeResponse(
                200,
                {
                    "accepted": 1,
                    "stale_ignored": 0,
                    "changed_paths": ["/product/phone-1"],
                    "scores_recalculated": True,
                    "committed": True,
                },
            ),
        ]
    )
    delays = []
    client = OfferIngestionClient(
        "https://teknoskor.com",
        TEST_SECRET,
        session=session,
        sleep=delays.append,
    )

    result = client.ingest_offers([sample_offer(0)])

    assert result["accepted"] == 1
    assert result["changed_paths"] == ["/product/phone-1"]
    assert len(session.calls) == 2
    assert session.calls[0][2]["json"] == session.calls[1][2]["json"]
    assert delays == [1]


def test_committed_503_fails_when_scores_never_recalculate():
    session = FakeSession(
        [
            FakeResponse(
                503,
                {
                    "accepted": 1,
                    "stale_ignored": 0,
                    "changed_paths": [],
                    "scores_recalculated": False,
                    "committed": True,
                },
            )
            for _ in range(3)
        ]
    )
    client = OfferIngestionClient(
        "https://teknoskor.com",
        TEST_SECRET,
        session=session,
        max_attempts=3,
        sleep=lambda _seconds: None,
    )

    try:
        client.ingest_offers([sample_offer(0)])
    except IngestionClientError as error:
        assert "was committed" in str(error)
        assert "failed after 3 attempt(s)" in str(error)
    else:
        raise AssertionError("Persistent score failure did not fail the run")

    assert len(session.calls) == 3


def test_uncommitted_error_fails_the_run():
    session = FakeSession(
        [
            FakeResponse(
                409,
                {"error": "Published product reference not found"},
            )
        ]
    )
    client = OfferIngestionClient(
        "https://teknoskor.com",
        TEST_SECRET,
        session=session,
        sleep=lambda _seconds: None,
    )

    try:
        client.ingest_offers([sample_offer(0)])
    except IngestionClientError as error:
        assert "HTTP 409" in str(error)
    else:
        raise AssertionError("Uncommitted ingestion error did not fail")


def test_retryable_uncommitted_error_is_retried():
    delays = []
    session = FakeSession(
        [
            FakeResponse(503, {"error": "Temporary failure"}),
            FakeResponse(
                200,
                {
                    "accepted": 1,
                    "stale_ignored": 0,
                    "changed_paths": [],
                    "committed": True,
                },
            ),
        ]
    )
    client = OfferIngestionClient(
        "https://teknoskor.com",
        TEST_SECRET,
        session=session,
        sleep=delays.append,
    )

    result = client.ingest_offers([sample_offer(0)])

    assert result["accepted"] == 1
    assert len(session.calls) == 2
    assert delays == [1]


def test_committed_count_mismatch_fails_the_run():
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "accepted": 0,
                    "stale_ignored": 0,
                    "changed_paths": [],
                    "committed": True,
                },
            )
        ]
    )
    client = OfferIngestionClient(
        "https://teknoskor.com",
        TEST_SECRET,
        session=session,
        sleep=lambda _seconds: None,
    )

    try:
        client.ingest_offers([sample_offer(0)])
    except IngestionClientError as error:
        assert "count does not match" in str(error)
    else:
        raise AssertionError("Committed count mismatch did not fail")


def test_price_updater_builds_the_complete_offer_contract():
    ingestion_client = RecordingIngestionClient()
    updater = PriceUpdater(ingestion_client, OneOfferScraper())
    timestamps = iter(
        ["2026-07-27T10:00:00Z", "2026-07-27T10:00:01Z"]
    )
    updater.utc_timestamp = lambda: next(timestamps)

    with (
        patch("update_prices.asyncio.sleep", new=no_delay),
        patch("update_prices.submit_urls") as submit_urls,
    ):
        result = asyncio.run(updater.run_update())

    assert result["accepted"] == 1
    assert ingestion_client.submissions == [
        [
            {
                "product_id": 42,
                "product_slug": "example-phone",
                "seller": "Teknosa",
                "price": 21999.0,
                "currency": "TRY",
                "availability": "unknown",
                "source_url": "https://www.teknosa.com/example-phone-p-123",
                "observed_at": "2026-07-27T10:00:00Z",
                "checked_at": "2026-07-27T10:00:01Z",
                "is_official": False,
            }
        ]
    ]
    submit_urls.assert_called_once_with(
        ["/product/example-phone", "/", "/products"]
    )


def test_full_catalog_zero_offer_outage_fails():
    ingestion_client = RecordingIngestionClient()

    class EmptyOfferScraper:
        async def get_best_prices(self, product_name, expected_specs=None):
            return []

    updater = PriceUpdater(ingestion_client, EmptyOfferScraper())
    with (
        patch("update_prices.asyncio.sleep", new=no_delay),
        patch("update_prices.submit_urls"),
    ):
        try:
            asyncio.run(updater.run_update())
        except RuntimeError as error:
            assert "offer coverage 0.0%" in str(error)
        else:
            raise AssertionError("Full catalog zero-offer outage did not fail")

    assert ingestion_client.submissions == []


def test_partial_catalog_outage_commits_good_rows_but_fails_the_run():
    ingestion_client = RecordingIngestionClient()
    ingestion_client.get_published_products = lambda: [
        {
            "id": index,
            "name": f"Phone {index}",
            "slug": f"phone-{index}",
            "attributes": {},
        }
        for index in range(1, 11)
    ]

    class MostlyEmptyScraper:
        async def get_best_prices(self, product_name, expected_specs=None):
            return (
                [
                    {
                        "merchant": "Teknosa",
                        "price": 21999,
                        "url": "https://www.teknosa.com/phone-1",
                    }
                ]
                if product_name == "Phone 1"
                else []
            )

    updater = PriceUpdater(ingestion_client, MostlyEmptyScraper())
    with (
        patch("update_prices.asyncio.sleep", new=no_delay),
        patch("update_prices.submit_urls"),
    ):
        try:
            asyncio.run(updater.run_update())
        except RuntimeError as error:
            assert "offer coverage 10.0%" in str(error)
        else:
            raise AssertionError("Partial catalog outage did not fail")

    assert len(ingestion_client.submissions) == 1
    assert len(ingestion_client.submissions[0]) == 1


def test_long_run_commits_bounded_product_batches():
    ingestion_client = RecordingIngestionClient()
    ingestion_client.get_published_products = lambda: [
        {
            "id": index,
            "name": f"Phone {index}",
            "slug": f"phone-{index}",
            "attributes": {},
        }
        for index in range(1, 12)
    ]
    updater = PriceUpdater(ingestion_client, OneOfferScraper())

    with (
        patch("update_prices.asyncio.sleep", new=no_delay),
        patch("update_prices.submit_urls"),
    ):
        result = asyncio.run(updater.run_update())

    assert [len(batch) for batch in ingestion_client.submissions] == [10, 1]
    assert result["accepted"] == 11


def test_phase2_backfill_commits_rows_but_requires_two_merchants():
    ingestion_client = RecordingIngestionClient()
    updater = PriceUpdater(ingestion_client, OneOfferScraper())

    with (
        patch("update_prices.asyncio.sleep", new=no_delay),
        patch("update_prices.submit_urls"),
    ):
        try:
            asyncio.run(updater.run_update(phase2_backfill=True))
        except RuntimeError as error:
            assert "at least two distinct merchants" in str(error)
            assert "example-phone" in str(error)
        else:
            raise AssertionError("One-merchant Phase 2 backfill passed")

    assert len(ingestion_client.submissions) == 1
    assert len(ingestion_client.submissions[0]) == 1


def test_invalid_offer_is_rejected_before_batch_submission():
    ingestion_client = RecordingIngestionClient()

    class InvalidOfferScraper:
        async def get_best_prices(self, product_name, expected_specs=None):
            return [
                {
                    "merchant": "Fake Store",
                    "price": 21999,
                    "url": "javascript:alert(1)",
                }
            ]

    updater = PriceUpdater(ingestion_client, InvalidOfferScraper())
    with (
        patch("update_prices.asyncio.sleep", new=no_delay),
        patch("update_prices.submit_urls"),
    ):
        try:
            asyncio.run(updater.run_update())
        except RuntimeError as error:
            assert "Invalid extracted offer for example-phone" in str(error)
        else:
            raise AssertionError("Invalid source URL reached ingestion")

    assert ingestion_client.submissions == []


def test_observed_title_is_forwarded_and_blank_titles_are_not_synthesized():
    updater = PriceUpdater(RecordingIngestionClient(), OneOfferScraper())
    product = {"id": 42, "name": "Expected catalog phone", "slug": "example-phone"}
    offer = {"merchant": "Teknosa", "price": 21999,
             "url": "https://www.teknosa.com/phone"}
    observed = "SAMSUNG Galaxy A16 8GB / 256GB - Türkiye Garantili"
    for value in [observed, "", "  ", None, 123]:
        record = updater.prepare_product_offers(
            product, [{**offer, "source_title": value}],
            "2026-07-27T10:00:00Z", "2026-07-27T10:00:01Z",
        )[0]
        if value == observed:
            assert record["source_title"] == observed
        else:
            assert "source_title" not in record
        assert record["observed_at"] == "2026-07-27T10:00:00Z"
        assert record["checked_at"] == "2026-07-27T10:00:01Z"
        assert "affiliate_url" not in record

    payload = {**sample_offer(0), "source_title": observed}
    session = FakeSession([FakeResponse(200, {
        "accepted": 1, "stale_ignored": 0, "committed": True,
    })])
    client = OfferIngestionClient("https://teknoskor.com", TEST_SECRET, session=session)
    result = client.ingest_offers([payload])
    assert session.calls[0][2]["json"]["offers"][0]["source_title"] == observed
    assert result["variant_rejected"] == 0
    assert result["variant_rejections"] == []


def test_source_title_limit_matches_endpoint_without_truncation():
    updater = PriceUpdater(RecordingIngestionClient(), OneOfferScraper())
    product = {"id": 42, "slug": "example-phone"}
    offer = {"merchant": "Teknosa", "price": 21999,
             "url": "https://www.teknosa.com/phone"}
    for title in ["a" * 1000, "📱" * 500]:
        records = updater.prepare_product_offers(product, [{**offer, "source_title": title}], "t1", "t2")
        assert records[0]["source_title"] == title
    for title in ["a" * 1001, "📱" * 501]:
        try:
            updater.prepare_product_offers(product, [{**offer, "source_title": title}], "t1", "t2")
        except RuntimeError as error:
            assert "exceeds 1000 characters" in str(error)
        else:
            raise AssertionError("Overlong source title was silently forwarded or truncated")


def test_mixed_variant_rejections_aggregate_across_http_chunks():
    session = FakeSession([
        FakeResponse(200, {"accepted": 1, "stale_ignored": 0, "variant_rejected": 1,
            "variant_rejections": [{"index": 1, "product_slug": "phone-2",
                "issues": ["storage_mismatch"], "expected": {"storage_gb": 128},
                "observed": {"storage_gb": 256}}],
            "changed_paths": ["/product/phone-1"], "committed": True}),
        FakeResponse(200, {"accepted": 0, "stale_ignored": 1, "variant_rejected": 1,
            "variant_rejections": [{"index": 0, "product_slug": "phone-3",
                "issues": ["ram_mismatch"]}], "changed_paths": [], "committed": True}),
        FakeResponse(200, {"accepted": 1, "stale_ignored": 0,
            "changed_paths": ["/product/phone-1", "/product/phone-5"], "committed": True}),
    ])
    client = OfferIngestionClient("https://teknoskor.com", TEST_SECRET, session=session)
    with patch("offer_ingestion_client.MAX_INGESTION_BATCH_SIZE", 2):
        result = client.ingest_offers([sample_offer(i) for i in range(5)])
    assert (result["accepted"], result["stale_ignored"], result["variant_rejected"]) == (2, 1, 2)
    assert [item["index"] for item in result["variant_rejections"]] == [1, 2]
    assert result["variant_rejections"][0]["expected"] == {"storage_gb": 128}
    assert result["variant_rejections"][0]["observed"] == {"storage_gb": 256}
    assert result["changed_paths"] == ["/product/phone-1", "/product/phone-5"]


def test_bad_variant_acknowledgements_fail_without_retrying_committed_rows():
    rejection = {"index": 0, "product_slug": "phone-1", "issues": ["storage_mismatch"]}
    cases = [
        {"accepted": 1, "variant_rejected": 1, "variant_rejections": [rejection]},
        {"accepted": 0, "variant_rejected": 1, "variant_rejections": []},
        {"accepted": 0, "variant_rejected": 1,
         "variant_rejections": [{**rejection, "index": 1}]},
        {"accepted": 2, "variant_rejected": -1, "variant_rejections": []},
    ]
    for body in cases:
        session = FakeSession([FakeResponse(200, {**body, "stale_ignored": 0, "committed": True})])
        client = OfferIngestionClient("https://teknoskor.com", TEST_SECRET, session=session)
        try:
            client.ingest_offers([sample_offer(0)])
        except IngestionClientError:
            pass
        else:
            raise AssertionError("Malformed variant acknowledgement passed")
        assert len(session.calls) == 1


def test_all_conflicting_rows_are_a_successfully_acknowledged_rejection_batch():
    session = FakeSession([FakeResponse(200, {
        "accepted": 0, "stale_ignored": 0, "variant_rejected": 1,
        "variant_rejections": [{"index": 0, "product_slug": "phone-1",
                                "issues": ["storage_mismatch"]}],
        "changed_paths": [], "committed": True, "scores_recalculated": None,
    })])
    client = OfferIngestionClient("https://teknoskor.com", TEST_SECRET, session=session)
    result = client.ingest_offers([sample_offer(0)])
    assert result["accepted"] == 0
    assert result["variant_rejected"] == 1
    assert result["changed_paths"] == []
    assert len(session.calls) == 1


def test_updater_rejection_indices_track_the_full_submitted_offer_sequence():
    aggregate = PriceUpdater.empty_ingestion_result()
    PriceUpdater.merge_ingestion_result(aggregate, {
        "accepted": 2, "stale_ignored": 1, "committed": True,
    })
    result = {"accepted": 1, "stale_ignored": 0, "variant_rejected": 1,
              "variant_rejections": [{"index": 1, "product_slug": "phone-5",
                                      "issues": ["storage_mismatch"]}],
              "committed": True}
    PriceUpdater.merge_ingestion_result(aggregate, result)
    assert aggregate["accepted"] == 3
    assert aggregate["variant_rejected"] == 1
    assert aggregate["variant_rejections"][0]["index"] == 4
    assert result["variant_rejections"][0]["index"] == 1


def test_variant_rejection_marks_run_failure_after_retaining_good_updates():
    client = RecordingIngestionClient()
    def ingest_offers(offers):
        client.submissions.append(offers)
        return {"accepted": 1, "stale_ignored": 0, "variant_rejected": 1,
                "variant_rejections": [{"index": 1, "product_slug": "example-phone",
                                        "issues": ["storage_mismatch"]}],
                "changed_paths": ["/product/example-phone"], "committed": True}
    client.ingest_offers = ingest_offers
    class TwoOfferScraper:
        async def get_best_prices(self, product_name, expected_specs=None):
            return [{"merchant": merchant, "price": 21999,
                     "url": f"https://www.{merchant.lower()}.com/phone"}
                    for merchant in ["Teknosa", "Trendyol"]]
    updater = PriceUpdater(client, TwoOfferScraper())
    with (patch("update_prices.asyncio.sleep", new=no_delay),
          patch("update_prices.submit_urls") as submit_urls,
          patch("update_prices.logging.error") as log_error):
        try:
            asyncio.run(updater.run_update())
        except RuntimeError as error:
            assert "1 offer(s) rejected for identity/variant conflicts" in str(error)
            assert "valid committed updates retained" in str(error)
        else:
            raise AssertionError("Variant rejection did not mark the run as an issue")
    assert len(client.submissions) == 1
    assert len(client.submissions[0]) == 2
    submit_urls.assert_called_once_with(["/product/example-phone", "/", "/products"])
    assert log_error.call_args.args[1:] == ("example-phone", 1, "storage_mismatch")


def test_refreshed_stock_and_actual_fetch_time_are_not_rewritten_as_new_unknown_evidence():
    updater = PriceUpdater(RecordingIngestionClient(), OneOfferScraper())
    product = {'id': 42, 'slug': 'example-phone'}
    offer = {'merchant': 'idefix', 'price': 21999, 'url': 'https://www.idefix.com/phone',
             'source_title': 'Samsung Galaxy S25 12GB RAM 256GB', 'currency': 'TRY',
             'availability': 'out_of_stock', 'observed_at': '2026-10-18T09:00:00Z'}
    record = updater.prepare_product_offers(product, [offer], 'new-observation', 'new-check')[0]
    assert record['availability'] == 'out_of_stock'
    assert record['observed_at'] == record['checked_at'] == '2026-10-18T09:00:00Z'
    assert record['source_title'] == offer['source_title']
    for override in [{'currency': 'USD'}, {'observed_at': 'not-a-date'},
                     {'observed_at': '2026-10-18T09:00:00'}]:
        try:
            updater.prepare_product_offers(product, [{**offer, **override}], 'new', 'new')
        except RuntimeError:
            pass
        else:
            raise AssertionError(f'Invalid refreshed evidence accepted: {override}')


if __name__ == "__main__":
    test_refreshed_stock_and_actual_fetch_time_are_not_rewritten_as_new_unknown_evidence()
    test_catalog_is_read_without_mysql()
    test_ingestion_batches_at_500_and_sends_bearer_token()
    test_committed_503_retries_until_scores_are_recalculated()
    test_committed_503_fails_when_scores_never_recalculate()
    test_uncommitted_error_fails_the_run()
    test_retryable_uncommitted_error_is_retried()
    test_committed_count_mismatch_fails_the_run()
    test_price_updater_builds_the_complete_offer_contract()
    test_full_catalog_zero_offer_outage_fails()
    test_partial_catalog_outage_commits_good_rows_but_fails_the_run()
    test_long_run_commits_bounded_product_batches()
    test_phase2_backfill_commits_rows_but_requires_two_merchants()
    test_invalid_offer_is_rejected_before_batch_submission()
    test_observed_title_is_forwarded_and_blank_titles_are_not_synthesized()
    test_source_title_limit_matches_endpoint_without_truncation()
    test_mixed_variant_rejections_aggregate_across_http_chunks()
    test_bad_variant_acknowledgements_fail_without_retrying_committed_rows()
    test_all_conflicting_rows_are_a_successfully_acknowledged_rejection_batch()
    test_updater_rejection_indices_track_the_full_submitted_offer_sequence()
    test_variant_rejection_marks_run_failure_after_retaining_good_updates()
    print("✅ HTTPS offer ingestion contract tests passed")
