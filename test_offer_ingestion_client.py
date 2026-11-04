"""Offline contract tests for the TeknoSkor HTTPS offer ingestion client."""

import asyncio
from unittest.mock import patch

from offer_ingestion_client import (
    IngestionClientError,
    OfferIngestionClient,
)
from update_prices import PriceUpdater
from retailer_offer_refresh import ALLOWED_HOSTS
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
import requests


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


def test_transient_catalog_gateway_520_retries_instead_of_aborting_the_catalog_sweep():
    for body in ({'error': 'Temporary gateway error'}, ValueError('not JSON')):
        delays = []
        session = FakeSession([FakeResponse(520, body), FakeResponse(200, {'products': []})])
        client = OfferIngestionClient('https://teknoskor.com', TEST_SECRET,
                                      session=session, sleep=delays.append)
        assert client._request_json('GET', '/api/ingestion/catalog') == {'products': []}
        assert delays == [1]
        assert len(session.calls) == 2


def test_n11_collector_redirect_metadata_stays_bound_to_the_same_product_group():
    from priority_retailer_transport import PriorityRetailerTransport
    original = 'https://www.n11.com/urun/samsung-galaxy-a57-256-gb-12345678'
    canonical = original.replace('256-gb', '256gb')
    def body(url, status=200, redirect=None):
        return {'purpose': 'public_n11_document', 'writes': False, 'url': url,
                'source_status': status, 'observed_at': datetime.now(timezone.utc).isoformat(),
                'document': 'User-agent: *\nAllow: /\n' if url.endswith('robots.txt') else '<h1>Phone</h1>',
                'content_type': 'text/html', **({'redirect_url': redirect} if redirect is not None else {})}
    session = FakeSession([
        FakeResponse(200, body('https://www.n11.com/robots.txt')),
        FakeResponse(200, body(original, 301, canonical)),
        FakeResponse(200, body(canonical)),
    ])
    client = OfferIngestionClient('https://teknoskor.com', TEST_SECRET, session=session)
    transport = PriorityRetailerTransport(n11_collector=client.n11_document_get, sleep=lambda _: None)
    assert transport.read_document(original)[1:] == ('document_read', 200)
    assert transport.resolved_url(original) == canonical
    assert [call[2]['json']['url'] for call in session.calls] == [
        'https://www.n11.com/robots.txt', original, canonical]
    for destination, status in [(canonical.replace('12345678', '12345679'), 301),
        (canonical + '?magaza=other', 301), ('https://evil.example', 301), (canonical, 200),
        (canonical, 403), (123, 301)]:
        client = OfferIngestionClient('https://teknoskor.com', TEST_SECRET,
            session=FakeSession([FakeResponse(200, body(original, status, destination))]))
        try:
            client.n11_document_get(original)
        except requests.RequestException:
            pass
        else:
            raise AssertionError('Unbound canonical redirect accepted')


def test_dedicated_n11_collector_keeps_authentication_on_our_host_and_binds_fresh_source_response():
    from priority_retailer_transport import PriorityRetailerTransport
    target = 'https://www.n11.com/urun/samsung-galaxy-a57-256-gb-12345678'
    def body(url, document, status=200):
        return {'purpose': 'public_n11_document', 'writes': False, 'url': url,
                'source_status': status, 'observed_at': datetime.now(timezone.utc).isoformat(),
                'document': document, 'content_type': 'text/html'}
    session = FakeSession([
        FakeResponse(200, body('https://www.n11.com/robots.txt', 'User-agent: *\nAllow: /\n')),
        FakeResponse(200, body(target, 'denied', 403)),
    ])
    client = OfferIngestionClient('https://teknoskor.com', TEST_SECRET, session=session)
    def forbidden(*args, **kwargs):
        raise AssertionError('No direct or alternate browser request after a dedicated collector denial')
    transport = PriorityRetailerTransport(get=forbidden, post=forbidden, sleep=lambda _: None,
                                         n11_collector=client.n11_document_get)
    assert transport.read_document(target)[1:] == ('http_403', 403)
    assert transport.read_document(target + '1')[1] == 'source_circuit_open'
    assert len(session.calls) == 2
    for method, url, options in session.calls:
        assert method == 'POST'
        assert url == 'https://teknoskor.com/api/ingestion/n11-document'
        assert options['headers']['Authorization'] == 'Bearer ' + TEST_SECRET
        assert set(options['json']) == {'url'}
    for change in (
        {'url': 'https://attacker.example/'}, {'source_status': True}, {'writes': True},
        {'observed_at': (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()},
        {'purpose': 'other'},
    ):
        session = FakeSession([FakeResponse(200, {**body(target, '<h1>Phone</h1>'), **change})])
        client = OfferIngestionClient('https://teknoskor.com', TEST_SECRET, session=session)
        try:
            client.n11_document_get(target)
        except requests.RequestException:
            pass
        else:
            raise AssertionError('Unbound collector response accepted')



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
                "name": "Samsung Example Phone",
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


class VerifiedRetailerFixture:
    """Separate live-PDP evidence; discovery prices alone are no longer enough."""
    def refresh_existing_offer_targets(self, name, targets, expected_specs):
        return [{
            'merchant': ALLOWED_HOSTS[urlparse(t['source_url']).hostname],
            'price': 21999, 'url': t['source_url'], 'currency': 'TRY',
            'source_title': f'{name if name.startswith("Samsung") else "Samsung " + name} 8GB RAM 256GB',
            'availability': 'in_stock', 'observed_at': '2026-07-27T09:58:00Z',
        } for t in targets]


def fixture_updater(client, scraper):
    updater = PriceUpdater(client, scraper, VerifiedRetailerFixture())
    updater.utc_timestamp = lambda: '2026-07-27T10:00:01Z'
    return updater


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
                            "technical_source_urls": ['https://www.kimovil.com/en/where-to-buy-example-phone',
                                'https://evil.example/phone',
                                'https://www.kimovil.com/en/where-to-buy-example-phone?token=private'],
                            "offer_targets_truncated": False,
                            "offer_targets": [{"merchant_name": "n11", "source_url": "https://www.n11.com/urun/phone",
                                               "seller_name": "N11 Store",
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
    assert products[0]['technical_source_urls'] == ['https://www.kimovil.com/en/where-to-buy-example-phone']
    assert 'technical_source_urls' not in products[1]
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
    assert products[0]['offer_targets_truncated'] is False
    assert products[1]['offer_targets_truncated'] is None
    assert products[0]['offer_targets'] == [{"merchant_name": "n11", "source_url": "https://www.n11.com/urun/phone",
                                             "seller_name": "N11 Store",
                                             "checked_at": "2026-01-01T00:00:00Z"}]


def test_catalog_target_completeness_must_be_boolean_and_survive_local_filtering():
    def read_product(targets, flag):
        session = FakeSession([FakeResponse(200, {
            'products': [{'id': 42, 'name': 'Example Phone',
                          'slug': 'example-phone', 'attributes': {},
                          'offer_targets': targets,
                          'offer_targets_truncated': flag}],
            'hasMore': False,
        })])
        client = OfferIngestionClient('https://teknoskor.com', TEST_SECRET,
                                      session=session, sleep=lambda _seconds: None)
        return client.get_published_products()[0]

    for invalid in (None, 0, 'false'):
        try:
            read_product([], invalid)
        except IngestionClientError as error:
            assert 'target completeness' in str(error)
        else:
            assert False, f'invalid completeness {invalid!r} was accepted'
    targets = [{'merchant_name': 'MediaMarkt',
                'source_url': f'https://www.mediamarkt.com.tr/tr/product/_phone-{i}.html'}
               for i in range(13)]
    product = read_product(targets, False)
    assert len(product['offer_targets']) == 12
    assert product['offer_targets_truncated'] is True


def test_complete_target_pages_require_stable_bounded_identity_and_no_discards():
    def target(index):
        return {'merchant_name': 'MediaMarkt', 'seller_name': 'Other Seller',
                'source_url': f'https://www.mediamarkt.com.tr/tr/product/_phone-{index}.html',
                'checked_at': '2026-11-02T17:00:00Z'}

    def page_response(index, targets, *, snapshot='a' * 64, invalid=0,
                      duplicate=0, total=14):
        return FakeResponse(200, {'products': [{
            'id': 238, 'slug': 'xiaomi-redmi-note-15-pro',
            'name': 'Xiaomi Redmi Note 15 Pro', 'offer_targets': targets,
            'offer_targets_total': total,
            'offer_targets_invalid_count': invalid,
            'offer_targets_duplicate_count': duplicate,
            'offer_targets_page': index,
            'offer_targets_snapshot': snapshot,
            'offer_targets_truncated': bool(invalid or duplicate or index * 12 < total),
        }]})

    def read(responses):
        session = FakeSession(responses)
        client = OfferIngestionClient('https://teknoskor.com', TEST_SECRET,
                                      session=session, sleep=lambda _seconds: None)
        return client.get_complete_offer_targets(238), session.calls

    complete, calls = read([page_response(1, [target(i) for i in range(12)]),
                            page_response(2, [target(12), target(13)])])
    assert len(complete['offer_targets']) == 14
    assert complete['offer_targets_truncated'] is False
    assert complete['offer_targets_total'] == 14
    assert [call[2]['params']['offer_target_page'] for call in calls] == ['1', '2']
    assert all(call[2]['params']['offer_target_product_id'] == '238' for call in calls)
    assert all(call[2]['headers']['Authorization'] == f'Bearer {TEST_SECRET}'
               for call in calls)
    failures = [
        ([page_response(1, [target(i) for i in range(12)], invalid=1)],
         'invalid rows (total=14, invalid=1, duplicate=0)'),
        ([page_response(1, [target(i) for i in range(12)]),
          page_response(2, [target(12), target(13)], snapshot='b' * 64)],
         'snapshot changed'),
        ([page_response(1, [target(i) for i in range(12)]),
          page_response(2, [target(0), target(13)])],
         'repeated'),
        ([page_response(1, [target(i) for i in range(12)], total=61)],
         'metadata is invalid'),
    ]
    for responses, reason in failures:
        try:
            read(responses)
        except IngestionClientError as error:
            assert reason in str(error), (reason, str(error))
        else:
            assert False, f'Incomplete target pages accepted: {reason}'


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


def test_strict_two_offer_batch_sends_opt_in_and_requires_atomic_ack():
    session = FakeSession([FakeResponse(200, {
        "accepted": 2, "stale_ignored": 0, "variant_rejected": 0,
        "variant_rejections": [], "changed_paths": ["/product/phone-1"],
        "committed": True, "require_all_accepted": True,
    })])
    client = OfferIngestionClient(
        "https://teknoskor.com", TEST_SECRET, session=session,
        sleep=lambda _seconds: None,
    )
    result = client.ingest_offers(
        [sample_offer(0), sample_offer(1)], require_all_accepted=True,
    )
    assert result["accepted"] == 2
    assert len(session.calls) == 1
    assert session.calls[0][2]["json"]["require_all_accepted"] is True


def test_strict_batch_rejects_missing_atomic_ack_or_partial_acceptance():
    for response in (
        {"accepted": 2, "stale_ignored": 0, "variant_rejected": 0,
         "variant_rejections": [], "committed": True},
        {"accepted": 1, "stale_ignored": 0, "variant_rejected": 1,
         "variant_rejections": [{"index": 1, "product_slug": "phone-2",
                                 "issues": ["storage_gb_mismatch"]}],
         "committed": True, "require_all_accepted": True},
    ):
        session = FakeSession([FakeResponse(200, response)])
        client = OfferIngestionClient(
            "https://teknoskor.com", TEST_SECRET, session=session,
            sleep=lambda _seconds: None,
        )
        try:
            client.ingest_offers(
                [sample_offer(0), sample_offer(1)], require_all_accepted=True,
            )
        except IngestionClientError:
            pass
        else:
            raise AssertionError("Strict batch accepted an unsafe response")


def test_strict_batch_refuses_to_span_multiple_requests():
    session = FakeSession([])
    client = OfferIngestionClient(
        "https://teknoskor.com", TEST_SECRET, session=session,
        sleep=lambda _seconds: None,
    )
    try:
        client.ingest_offers(
            [sample_offer(i) for i in range(501)],
            require_all_accepted=True,
        )
    except IngestionClientError as error:
        assert "multiple HTTP batches" in str(error)
    else:
        raise AssertionError("Strict batch was split across requests")
    assert session.calls == []


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
    updater = fixture_updater(ingestion_client, OneOfferScraper())

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
                "availability": "in_stock",
                "source_url": "https://www.teknosa.com/example-phone-p-123",
                "observed_at": "2026-07-27T09:58:00Z",
                "checked_at": "2026-07-27T09:58:00Z",
                "source_title": "Samsung Example Phone 8GB RAM 256GB",
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

    updater = fixture_updater(ingestion_client, EmptyOfferScraper())
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
            "attributes": {"ram_gb": 8, "storage_gb": 256},
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

    updater = fixture_updater(ingestion_client, MostlyEmptyScraper())
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
            "attributes": {"ram_gb": 8, "storage_gb": 256},
        }
        for index in range(1, 12)
    ]
    updater = fixture_updater(ingestion_client, OneOfferScraper())

    with (
        patch("update_prices.asyncio.sleep", new=no_delay),
        patch("update_prices.submit_urls"),
    ):
        result = asyncio.run(updater.run_update())

    assert [len(batch) for batch in ingestion_client.submissions] == [10, 1]
    assert result["accepted"] == 11


def test_phase2_backfill_commits_rows_but_requires_two_merchants():
    ingestion_client = RecordingIngestionClient()
    updater = fixture_updater(ingestion_client, OneOfferScraper())

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

    updater = fixture_updater(ingestion_client, InvalidOfferScraper())
    with (
        patch("update_prices.asyncio.sleep", new=no_delay),
        patch("update_prices.submit_urls"),
    ):
        try:
            asyncio.run(updater.run_update())
        except RuntimeError as error:
            assert "offer coverage 0.0%" in str(error)
        else:
            raise AssertionError("Invalid source URL reached ingestion")

    assert ingestion_client.submissions == []


def test_observed_title_is_forwarded_and_blank_titles_are_not_synthesized():
    updater = fixture_updater(RecordingIngestionClient(), OneOfferScraper())
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
    updater = fixture_updater(RecordingIngestionClient(), OneOfferScraper())
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
    updater = fixture_updater(client, TwoOfferScraper())
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
    updater = fixture_updater(RecordingIngestionClient(), OneOfferScraper())
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
    test_catalog_target_completeness_must_be_boolean_and_survive_local_filtering()
    test_complete_target_pages_require_stable_bounded_identity_and_no_discards()
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

def test_our_collector_gateway_failure_keeps_bounded_cooldown_instead_of_permanent_denial():
    target = 'https://www.n11.com/urun/samsung-galaxy-a57-256-gb-12345678'
    session = FakeSession([FakeResponse(503, {'error': 'Temporary source error'})])
    client = OfferIngestionClient('https://teknoskor.com', TEST_SECRET, session=session, max_attempts=1)
    response = client.n11_document_get(target)
    assert response.status_code == 503
    assert response.url == target
    assert b''.join(response.iter_content()) == b''
    assert len(session.calls) == 1

from unittest import TestCase


class TechnicalSourceHintContractTests(TestCase):
    def test_catalog_keeps_only_bounded_public_provenance_urls_and_no_stored_offer_values(self):
        test_catalog_is_read_without_mysql()
        for hints in ['https://www.kimovil.com/en/where-to-buy-phone', [None, {}],
                      ['https://www.kimovil.com/en/where-to-buy-phone'] * 5]:
            client = OfferIngestionClient('https://teknoskor.com', TEST_SECRET, session=FakeSession([
                FakeResponse(200, {'products': [{'id': 1, 'name': 'Phone', 'slug': 'phone',
                    'technical_source_urls': hints}], 'hasMore': False})]))
            self.assertNotIn('technical_source_urls', client.get_published_products()[0])


class N11CollectorBoundaryTests(TestCase):
    def test_same_product_redirect_metadata(self):
        test_n11_collector_redirect_metadata_stays_bound_to_the_same_product_group()

    def test_credentials_and_denial_boundary(self):
        test_dedicated_n11_collector_keeps_authentication_on_our_host_and_binds_fresh_source_response()

    def test_our_gateway_failure_is_not_a_retailer_denial(self):
        test_our_collector_gateway_failure_keeps_bounded_cooldown_instead_of_permanent_denial()


class CompleteTargetInspectionTests(TestCase):
    @staticmethod
    def pages():
        from offer_ingestion_client import TARGET_INVALID_REASONS
        reasons = {key: int(key == 'unsupported_host') for key in TARGET_INVALID_REASONS}
        targets = [{'merchant_name': 'n11', 'seller_name': f'Other Seller {i}',
                    'source_url': f'https://www.n11.com/urun/samsung-galaxy-s26-128-gb-{10000000+i}',
                    'checked_at': '2026-11-03T10:00:00Z'} for i in range(14)]
        common = {'id': 238, 'name': 'Samsung Galaxy S26', 'slug': 'samsung-galaxy-s26',
                  'attributes': {'ram_gb': 12, 'storage_gb': 128}, 'offer_targets_total': 14,
                  'offer_targets_invalid_count': 1, 'offer_targets_duplicate_count': 1,
                  'offer_targets_invalid_reasons': reasons, 'offer_targets_snapshot': 'a' * 64,
                  'offer_targets_truncated': True}
        return [{'products': [{**common, 'offer_targets_page': page,
                    'offer_targets': targets[(page-1)*12:page*12]}]} for page in (1, 2)]

    def test_all_diagnostic_pages_are_bound_before_returning_the_complete_target_set(self):
        session = FakeSession([FakeResponse(200, body) for body in self.pages()])
        client = OfferIngestionClient('https://teknoskor.com', TEST_SECRET, session=session)
        result = client.get_complete_offer_target_inspection(238)
        self.assertEqual(len(result['offer_targets']), 14)
        self.assertEqual(result['offer_targets_invalid_count'], 1)
        self.assertEqual(result['offer_targets_duplicate_count'], 1)
        self.assertTrue(result['offer_targets_truncated'])  # Raw proof is still mandatory.
        self.assertEqual([call[2]['params']['offer_target_page'] for call in session.calls], ['1', '2'])

    def test_legacy_scoped_inspection_keeps_its_strict_single_page_contract(self):
        session = FakeSession([FakeResponse(200, self.pages()[0])])
        client = OfferIngestionClient('https://teknoskor.com', TEST_SECRET, session=session)
        with self.assertRaises(IngestionClientError):
            client.get_offer_target_inspection(238)
        self.assertEqual(len(session.calls), 1)

    def test_changed_missing_repeated_or_incomplete_second_page_cannot_clear_the_guard(self):
        for kind in ['snapshot', 'identity', 'attributes', 'total', 'duplicate_url', 'missing_row', 'reject_counts', 'truncated']:
            with self.subTest(kind=kind):
                bodies = self.pages()
                row = bodies[1]['products'][0]
                if kind == 'snapshot': row['offer_targets_snapshot'] = 'b' * 64
                elif kind == 'identity': row['slug'] = 'other-phone'
                elif kind == 'attributes': row['attributes'] = {'ram_gb': 8, 'storage_gb': 128}
                elif kind == 'total': row['offer_targets_total'] = 13
                elif kind == 'duplicate_url': row['offer_targets'][0] = bodies[0]['products'][0]['offer_targets'][0]
                elif kind == 'missing_row': row['offer_targets'].pop()
                elif kind == 'reject_counts': row['offer_targets_invalid_count'] = 2
                else: row['offer_targets_truncated'] = False
                client = OfferIngestionClient('https://teknoskor.com', TEST_SECRET,
                    session=FakeSession([FakeResponse(200, body) for body in bodies]))
                with self.assertRaises(IngestionClientError):
                    client.get_complete_offer_target_inspection(238)

class ListingIdentityContractTests(TestCase):
    @staticmethod
    def accepted():
        return {'accepted': 1, 'stale_ignored': 0, 'variant_rejected': 0,
                'variant_rejections': [], 'committed': True, 'require_all_accepted': True,
                'offer_identity_contract': 'seller-source-url-v1'}

    def test_only_the_strict_listing_route_and_its_identity_acknowledgment_are_used(self):
        session = FakeSession([FakeResponse(200, self.accepted())])
        client = OfferIngestionClient('https://teknoskor.com', TEST_SECRET, session=session)
        result = client.ingest_listing_offers([sample_offer(0)])
        self.assertEqual(result['accepted'], 1)
        self.assertEqual(result['offer_identity_contract'], 'seller-source-url-v1')
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][1], 'https://teknoskor.com/api/ingestion/listing-offers')
        self.assertIs(session.calls[0][2]['json']['require_all_accepted'], True)

    def test_old_deployment_or_missing_identity_ack_cannot_fall_back_to_a_merchant_only_write(self):
        for status, body in [(404, {'error': 'Not found'}),
                             (200, {**self.accepted(), 'offer_identity_contract': None})]:
            session = FakeSession([FakeResponse(status, body)])
            client = OfferIngestionClient('https://teknoskor.com', TEST_SECRET, session=session)
            with self.assertRaises(IngestionClientError):
                client.ingest_listing_offers([sample_offer(0)])
            self.assertEqual(len(session.calls), 1)
            self.assertTrue(session.calls[0][1].endswith('/api/ingestion/listing-offers'))
        session = FakeSession([])
        client = OfferIngestionClient('https://teknoskor.com', TEST_SECRET, session=session)
        with self.assertRaises(IngestionClientError):
            client.ingest_listing_offers([sample_offer(0)], require_all_accepted=False)
        self.assertEqual(session.calls, [])

    def test_daily_updater_selects_the_strict_writer_when_supported_and_never_uses_legacy_upsert(self):
        class NewClient:
            def __init__(self): self.calls = []
            def ingest_listing_offers(self, records, *, require_all_accepted):
                self.calls.append((records, require_all_accepted))
                return {'accepted': len(records), 'stale_ignored': 0, 'variant_rejected': 0,
                        'variant_rejections': [], 'changed_paths': [], 'committed': True,
                        'offer_identity_contract': 'seller-source-url-v1'}
            def ingest_offers(self, *args, **kwargs):
                raise AssertionError('Legacy writer must not receive URL-scoped records')
        client = NewClient()
        updater = PriceUpdater(ingestion_client=client)
        self.assertTrue(updater.listing_scoped)
        total = updater.empty_ingestion_result()
        updater.flush_offers([sample_offer(0)], total)
        self.assertEqual(total['accepted'], 1)
        self.assertIs(client.calls[0][1], True)
