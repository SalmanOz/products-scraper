import os
import json
import unittest
from unittest.mock import Mock, patch

import requests

from main import KimovilScraper, SOURCE_PRODUCT_OVERRIDES
from source_ingestion import (
    CatalogProduct,
    ExistingProductNotFoundError,
    IngestionConfigurationError,
    SourceIngestionClient,
    SourceIngestionError,
    build_provenance_records,
    select_observed_physical_attributes,
)


SECRET = "s" * 32


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, requests.RequestException):
            raise response
        return response


class ProvenanceRecordTests(unittest.TestCase):
    def test_physical_specs_use_an_explicit_spec_database_origin(self):
        records = build_provenance_records(
            product_slug="phone-one",
            source_url="https://www.kimovil.com/en/phone-one",
            observed_at="2026-07-27T10:00:00Z",
            attributes={
                "battery_mah": 5000,
                "Technical sheet": {"Brand": "Example"},
                "camera_score": 8.2,
                "antutu_score": 900000,
                "partials": {"camera": 8.2, "hardware": 8.5},
                "gaming_performance": [{"game": "PUBG", "fps": 60}],
            },
        )

        self.assertEqual(
            [record["attribute_key"] for record in records],
            [
                "Technical sheet",
                "antutu_score",
                "battery_mah",
                "camera_score",
                "gaming_performance",
                "partials",
            ],
        )
        physical = {
            record["attribute_key"]: record
            for record in records
            if record["attribute_key"] in {
                "Technical sheet",
                "battery_mah",
            }
        }
        self.assertTrue(all(
            record["value_origin"] == "spec_database"
            and record["confidence"] == "medium"
            and record["is_estimate"] is False
            for record in physical.values()
        ))
        benchmarks = [
            record
            for record in records
            if record["attribute_key"] in {
                "antutu_score",
                "camera_score",
                "partials",
            }
        ]
        self.assertTrue(
            all(
                record["value_origin"] == "benchmark_source"
                and record["confidence"] == "medium"
                and record["is_estimate"] is False
                for record in benchmarks
            )
        )
        gaming = next(
            record for record in records
            if record["attribute_key"] == "gaming_performance"
        )
        self.assertEqual(gaming["value_origin"], "derived")
        self.assertTrue(gaming["is_estimate"])
        self.assertEqual(
            gaming["source_url"],
            "https://teknoskor.com/about#metodoloji",
        )

    def test_missing_and_zero_benchmarks_do_not_create_benchmark_records(self):
        records = build_provenance_records(
                product_slug="phone-one",
                source_url="https://www.kimovil.com/en/phone-one",
                observed_at="2026-07-27T10:00:00Z",
                attributes={
                    "antutu_score": 0,
                    "camera_score": 0,
                    "partials": {
                        "camera": 0,
                        "hardware": None,
                    },
                    "battery_mah": 5000,
                },
            )
        self.assertEqual(
            [record["attribute_key"] for record in records],
            ["battery_mah"],
        )
        self.assertEqual(records[0]["value_origin"], "spec_database")

    def test_physical_groups_require_matching_current_source_sections(self):
        selected = select_observed_physical_attributes(
            {
                "Performance & Hardware": {"Model": "Chip One"},
                "Batarya": {"Capacity": "5000 mAh"},
                "ram_gb": 8,
                "battery_mah": 5000,
                "quick_specs": {"ram": "8 GB"},
            },
            {"Hardware": {"Model": "Chip One"}},
        )
        self.assertEqual(
            selected,
            {
                "Performance & Hardware": {"Model": "Chip One"},
                "quick_specs": {"ram": "8 GB"},
                "ram_gb": 8,
            },
        )

    def test_partial_scores_drop_only_missing_children(self):
        records = build_provenance_records(
            product_slug="phone-one",
            source_url="https://www.kimovil.com/en/phone-one",
            observed_at="2026-07-27T10:00:00Z",
            attributes={
                "partials": {
                    "camera": 0,
                    "hardware": 7,
                    "battery": "unknown",
                },
            },
        )

        self.assertEqual(records[0]["value"], {"hardware": 7.0})


class ProductIdentityTests(unittest.TestCase):
    def test_category_scores_are_observed_finite_ten_point_values_not_totals_or_raw_benchmarks(self):
        scraper = KimovilScraper()
        scraper.get_via_flaresolverr = lambda _url: """<meta name='deviceki' content='{"name":"Phone One","ki":9.9,"partials":{"hardware":true,"camera":NaN,"battery":8.2,"design":82,"connectivity":-1}}'>"""
        records = []
        self.assertTrue(scraper.scrape_product_details('https://www.kimovil.com/en/where-to-buy-phone-one',
            product_slug='phone-one', expected_name='Phone One', record_sink=records))
        values = {r['attribute_key']:r['value'] for r in records}
        self.assertEqual(values['partials'], {'battery':8.2})
        self.assertEqual(values['battery_score'],8.2)
        for field in ['performance_score','camera_score','screen_score']:
            self.assertNotIn(field,values)

    def test_storage_capacity_converts_terabytes_to_gigabytes(self):
        scraper = KimovilScraper()

        self.assertEqual(scraper.extract_capacity_gb("1 TB UFS 4.1"), 1024)
        self.assertEqual(scraper.extract_capacity_gb("512 GB UFS 4.1"), 512)

    def test_pending_product_refreshes_a_previously_misparsed_capacity(self):
        scraper = KimovilScraper()
        scraper.get_via_flaresolverr = lambda _url: """
            <html><head>
              <meta name='deviceki'
                    content='{"name":"vivo X300 Ultra","partials":{}}'>
              <meta name='devicecompare'
                    content='{"name":"vivo X300 Ultra"}'>
            </head><body>
              <section class='container-sheet-hardware'>
                <h2>Performance &amp; Hardware of vivo X300 Ultra</h2>
                <table class='k-dltable'>
                  <tr><th class='label'>Capacity</th>
                      <td class='value'>1 TB</td></tr>
                </table>
              </section>
            </body></html>
        """
        staged = []

        self.assertTrue(scraper.scrape_product_details(
            "https://source.example/vivo-x300-ultra",
            product_slug="vivo-x300-ultra",
            expected_name="vivo X300 Ultra",
            existing_attributes={"storage_gb": 1},
            seed_physical_attributes=True,
            record_sink=staged,
        ))
        storage = next(
            record
            for record in staged
            if record["attribute_key"] == "storage_gb"
        )
        self.assertEqual(storage["value"], 1024)

    def test_reviewed_s25_fe_source_requires_exact_8_256_before_staging(self):
        override = SOURCE_PRODUCT_OVERRIDES['samsung-galaxy-s25-fe-256gb']
        self.assertEqual(override['expected_ram_gb'], 8)
        self.assertEqual(override['expected_storage_gb'], 256)
        scraper = KimovilScraper()
        template = """
            <html><head><meta name='deviceki' content='{"name":"Samsung Galaxy S25 FE","partials":{}}'>
            <meta name='devicecompare' content='{"name":"Samsung Galaxy S25 FE"}'></head><body>
            <section class='container-sheet-hardware'><h2>Performance &amp; Hardware</h2>
            <table class='k-dltable'>
            <tr><th class='label'>Memory RAM</th><td class='value'>8 GB</td></tr>
            <tr><th class='label'>Capacity</th><td class='value'>%s GB</td></tr>
            </table></section></body></html>
        """
        records = []
        scraper.get_via_flaresolverr = lambda _url: template % 128
        self.assertFalse(scraper.scrape_product_details(
            override['url'], product_slug='samsung-galaxy-s25-fe-256gb',
            expected_name=override['expected_name'], existing_attributes={},
            seed_physical_attributes=True, record_sink=records,
            expected_memory={'ram_gb': 8, 'storage_gb': 256},
        ))
        self.assertEqual(records, [])
        scraper.get_via_flaresolverr = lambda _url: template % 256
        self.assertTrue(scraper.scrape_product_details(
            override['url'], product_slug='samsung-galaxy-s25-fe-256gb',
            expected_name=override['expected_name'], existing_attributes={},
            seed_physical_attributes=True, record_sink=records,
            expected_memory={'ram_gb': 8, 'storage_gb': 256},
        ))
        self.assertEqual({r['attribute_key']: r['value'] for r in records
                          if r['attribute_key'] in {'ram_gb', 'storage_gb'}},
                         {'ram_gb': 8, 'storage_gb': 256})

    def test_catalog_aliases_keep_source_identity_checks_exact(self):
        self.assertEqual(
            SOURCE_PRODUCT_OVERRIDES["samsung-galaxy-a36-5g"],
            {
                "expected_name": "Samsung Galaxy A36",
                "url": (
                    "https://www.kimovil.com/en/"
                    "where-to-buy-samsung-galaxy-a36"
                ),
            },
        )
        self.assertEqual(
            SOURCE_PRODUCT_OVERRIDES["oppo-reno15-pro-5g"]["expected_name"],
            "Oppo Reno15 Pro",
        )

    def test_flaresolverr_reuses_a_named_browser_session(self):
        scraper = KimovilScraper()
        session_response = Mock()
        session_response.raise_for_status.return_value = None
        session_response.json.return_value = {"status": "ok"}
        page_response = Mock()
        page_response.raise_for_status.return_value = None
        page_response.json.return_value = {
            "status": "ok",
            "solution": {"response": "<html>phone</html>"},
        }

        with patch(
            "main.requests.post",
            side_effect=[session_response, page_response, page_response],
        ) as post:
            self.assertEqual(
                scraper.get_via_flaresolverr("https://source.example/one"),
                "<html>phone</html>",
            )
            self.assertEqual(
                scraper.get_via_flaresolverr("https://source.example/two"),
                "<html>phone</html>",
            )

        self.assertEqual(post.call_count, 3)
        self.assertEqual(
            post.call_args_list[0].kwargs["json"],
            {
                "cmd": "sessions.create",
                "session": "teknoskor-kimovil",
            },
        )
        self.assertEqual(
            post.call_args_list[1].kwargs["json"]["session"],
            "teknoskor-kimovil",
        )
        self.assertEqual(
            post.call_args_list[2].kwargs["json"]["session"],
            "teknoskor-kimovil",
        )

    def test_autocomplete_skips_wrong_variant_before_exact_model(self):
        scraper = KimovilScraper()
        scraper.get_via_flaresolverr = lambda _url: """
            {"results":[
                {"full_name":"Honor 200 Smart","url":"honor-200-smart"},
                {"full_name":"Honor 200","url":"honor-200-5g"}
            ]}
        """

        self.assertEqual(
            scraper.search_product_on_kimovil("Honor 200"),
            "https://www.kimovil.com/en/where-to-buy-honor-200-5g",
        )

    def test_exact_model_and_variant_match(self):
        self.assertTrue(
            KimovilScraper.is_product_name_match(
                "Samsung Galaxy A16 4G",
                "Samsung Galaxy A16 4G",
            )
        )
        self.assertTrue(
            KimovilScraper.is_product_name_match(
                "Apple iPhone 15 Pro Max",
                "Apple iPhone 15 Pro Max",
            )
        )
        self.assertTrue(
            KimovilScraper.is_product_name_match(
                "Apple iPhone16e",
                "Apple iPhone 16e",
            )
        )

    def test_wrong_brand_or_variant_is_rejected(self):
        self.assertFalse(
            KimovilScraper.is_product_name_match(
                "Samsung Galaxy A16 4G",
                "Samsung Galaxy A16 5G",
            )
        )
        self.assertFalse(
            KimovilScraper.is_product_name_match(
                "Xiaomi Redmi Note 13 Pro",
                "Xiaomi Redmi Note 13",
            )
        )
        self.assertFalse(
            KimovilScraper.is_product_name_match(
                "Samsung Galaxy A16",
                "Xiaomi Redmi A16",
            )
        )
        self.assertFalse(
            KimovilScraper.is_product_name_match(
                "Xiaomi 15T",
                "15T",
            )
        )
        self.assertFalse(
            KimovilScraper.is_product_name_match(
                "Google Pixel 9 Pro",
                "Google Pixel 9 Pro XL",
            )
        )

    def test_scrape_submits_only_provenance_contract_fields(self):
        class CapturingClient:
            def __init__(self):
                self.records = []

            def submit_sources(self, records):
                self.records = records
                return {"accepted": len(records), "stale_ignored": 0}

        client = CapturingClient()
        scraper = KimovilScraper(ingestion_client=client)
        scraper.get_via_flaresolverr = lambda _url: """
            <html><head>
              <meta name='deviceki'
                    content='{"name":"Phone One","partials":{"camera":8.1,"hardware":8.4,"battery":8.0,"design":7.9}}'>
              <meta name='devicecompare'
                    content='{"name":"Phone One","slug":"phone-one"}'>
            </head><body>
              <section class='container-sheet-hardware'>
                <h2>Hardware of Phone One</h2>
                <table class='k-dltable'>
                  <tr><th class='label'>Score</th>
                      <td class='value'>900.000 • Antutu v10</td></tr>
                  <tr><th class='label'>Nanometers</th>
                      <td class='value'>4 nm</td></tr>
                </table>
              </section>
              <section class='container-sheet-battery'>
                <h2>Battery of Phone One</h2>
                <table class='k-dltable'>
                  <tr><th class='label'>Capacity</th>
                      <td class='value'>5000 mAh</td></tr>
                </table>
              </section>
            </body></html>
        """

        succeeded = scraper.scrape_product_details(
            "https://www.kimovil.com/en/where-to-buy-phone-one",
            product_slug="phone-one",
            expected_name="Phone One",
            existing_attributes={
                "Performance & Hardware": {"Model": "Chip One"},
                "Batarya": {"Capacity": "5000 mAh"},
                "battery_mah": 5000,
                "quick_specs": {"battery": "5000 mAh"},
            },
        )

        self.assertTrue(succeeded)
        keys = {record["attribute_key"] for record in client.records}
        self.assertEqual(
            keys,
            {
                "Batarya",
                "Performance & Hardware",
                "antutu_score",
                "battery_mah",
                "battery_score",
                "camera_score",
                "gaming_performance",
                "partials",
                "performance_score",
                "quick_specs",
                "screen_score",
            },
        )
        self.assertTrue(
            all(record["product_slug"] == "phone-one"
                for record in client.records)
        )

    def test_missing_benchmark_does_not_call_ingestion(self):
        class RejectUnexpectedCall:
            def submit_sources(self, _records):
                raise AssertionError("ingestion must not be called")

        scraper = KimovilScraper(ingestion_client=RejectUnexpectedCall())
        scraper.get_via_flaresolverr = lambda _url: """
            <html><head>
              <meta name='deviceki'
                    content='{"name":"Phone One","partials":{}}'>
              <meta name='devicecompare'
                    content='{"name":"Phone One","slug":"phone-one"}'>
            </head><body></body></html>
        """

        self.assertFalse(
            scraper.scrape_product_details(
                "https://www.kimovil.com/en/where-to-buy-phone-one",
                product_slug="phone-one",
                expected_name="Phone One",
            )
        )

    def test_deferred_mode_stages_records_without_per_product_submission(self):
        class RejectUnexpectedCall:
            def submit_sources(self, _records):
                raise AssertionError("per-product ingestion must not be called")

        scraper = KimovilScraper(ingestion_client=RejectUnexpectedCall())
        scraper.get_via_flaresolverr = lambda _url: """
            <html><head>
              <meta name='deviceki'
                    content='{"name":"Phone One","partials":{"camera":8.1,"hardware":8.4,"battery":8.0,"design":7.9}}'>
              <meta name='devicecompare'
                    content='{"name":"Phone One","slug":"phone-one"}'>
            </head><body>
              <section class='container-sheet-hardware'>
                <h2>Hardware of Phone One</h2>
                <table class='k-dltable'>
                  <tr><th class='label'>Score</th>
                      <td class='value'>900.000 • Antutu v10</td></tr>
                </table>
              </section>
            </body></html>
        """
        staged = []

        succeeded = scraper.scrape_product_details(
            "https://www.kimovil.com/en/where-to-buy-phone-one",
            product_slug="phone-one",
            expected_name="Phone One",
            existing_attributes={
                "Performance & Hardware": {"Model": "Chip One"},
                "ram_gb": 8,
            },
            record_sink=staged,
        )

        self.assertTrue(succeeded)
        self.assertGreater(len(staged), 0)
        self.assertTrue(all(
            record["product_slug"] == "phone-one"
            for record in staged
        ))

    def test_run_fails_closed_when_committed_sources_leave_product_pending(self):
        class ReadinessClient:
            def __init__(self):
                self.fetches = 0

            def ensure_schema(self):
                return {"ready": True, "migrated": False}

            def fetch_catalog(self, page_size=100):
                _ = page_size
                self.fetches += 1
                status = None if self.fetches == 1 else "pending"
                return [
                    CatalogProduct(
                        1,
                        "Phone One",
                        "phone-one",
                        {"battery_mah": 5000},
                        status,
                        ({
                            "key": "screen_size_inch",
                            "code": "missing_value",
                        },) if status else (),
                        None,
                    ),
                ]

        scraper = KimovilScraper(ingestion_client=ReadinessClient())
        scraper.scrape_product_details = lambda *args, **kwargs: True
        summary = scraper.scrape_existing_products()

        self.assertEqual(summary["verified_products"], 0)
        self.assertEqual(summary["failed"], ["phone-one"])

    def test_pending_only_sync_skips_already_verified_products(self):
        class ReadinessClient:
            def __init__(self):
                self.fetches = 0

            def ensure_schema(self):
                return {"ready": True, "migrated": False}

            def fetch_catalog(self, page_size=100):
                _ = page_size
                self.fetches += 1
                pending_status = (
                    "pending" if self.fetches == 1 else "verified"
                )
                pending_verified_at = (
                    None if self.fetches == 1 else "2026-08-13T07:00:00Z"
                )
                return [
                    CatalogProduct(
                        1,
                        "Ready Phone",
                        "ready-phone",
                        {},
                        "verified",
                        (),
                        "2026-08-13T06:00:00Z",
                    ),
                    CatalogProduct(
                        2,
                        "Pending Phone",
                        "pending-phone",
                        {},
                        pending_status,
                        (),
                        pending_verified_at,
                    ),
                ]

        scraper = KimovilScraper(ingestion_client=ReadinessClient())
        scraped = []
        scraper.scrape_product_details = lambda *args, **kwargs: (
            scraped.append(kwargs["product_slug"]) or True
        )

        summary = scraper.scrape_existing_products(pending_only=True)

        self.assertEqual(scraped, ["pending-phone"])
        self.assertEqual(summary["catalog_products"], 2)
        self.assertEqual(summary["attempted_products"], 1)
        self.assertEqual(summary["skipped_verified_products"], 1)
        self.assertEqual(summary["verified_products"], 2)
        self.assertEqual(summary["failed"], [])

    def test_diagnostic_limit_scrapes_only_the_requested_catalog_prefix(self):
        class ReadinessClient:
            def ensure_schema(self):
                return {"ready": True, "migrated": False}

            def fetch_catalog(self, page_size=100):
                _ = page_size
                return [
                    CatalogProduct(
                        index,
                        f"Phone {index}",
                        f"phone-{index}",
                        {},
                        "pending",
                        (),
                        None,
                    )
                    for index in range(1, 4)
                ]

        scraper = KimovilScraper(ingestion_client=ReadinessClient())
        scraped = []
        scraper.scrape_product_details = lambda *args, **kwargs: (
            scraped.append(kwargs["product_slug"]) or True
        )

        summary = scraper.scrape_existing_products(max_products=1)

        self.assertEqual(scraped, ["phone-1"])
        self.assertEqual(summary["catalog_products"], 3)
        self.assertEqual(summary["attempted_products"], 1)


class SourceIngestionClientTests(unittest.TestCase):
    def make_client(self, responses, **kwargs):
        session = FakeSession(responses)
        client = SourceIngestionClient(
            "https://teknoskor.example",
            SECRET,
            session=session,
            sleep=lambda _seconds: None,
            **kwargs,
        )
        return client, session

    def test_configuration_requires_https_and_strong_secret(self):
        with self.assertRaises(IngestionConfigurationError):
            SourceIngestionClient("http://teknoskor.example", SECRET)
        with self.assertRaises(IngestionConfigurationError):
            SourceIngestionClient("https://teknoskor.example", "short")
        SourceIngestionClient("http://localhost:3000", SECRET)

    def test_schema_preflight_is_authenticated_and_fail_closed(self):
        client, session = self.make_client(
            [
                FakeResponse(
                    200,
                    {
                        "ready": True,
                        "required_origin": "spec_database",
                        "column_present": True,
                        "migrated": True,
                    },
                )
            ]
        )

        result = client.ensure_schema()

        self.assertTrue(result["ready"])
        self.assertEqual(session.calls[0][0], "POST")
        self.assertEqual(
            session.calls[0][1],
            "https://teknoskor.example/api/ingestion/schema",
        )
        self.assertEqual(
            session.calls[0][2]["json"],
            {"action": "ensure_spec_database_origin"},
        )
        self.assertEqual(
            session.calls[0][2]["headers"]["Authorization"],
            f"Bearer {SECRET}",
        )

    def test_readiness_audit_parses_the_strict_inventory_contract(self):
        client, session = self.make_client(
            [
                FakeResponse(
                    200,
                    {
                        "controlled_pairs": 395,
                        "controlled_decisions": 395,
                        "strict_controlled_indexable_pairs": 395,
                        "strict_excluded_controlled_pairs": 0,
                        "controlled_products": 78,
                        "strict_eligible_controlled_products": 78,
                        "strict_ineligible_controlled_products": 0,
                        "strict_gates": {
                            "verified_products": True,
                            "comparison_approvals": True,
                            "substantive_comparison_reasons": True,
                        },
                    },
                )
            ]
        )

        result = client.fetch_readiness()

        self.assertEqual(result["strict_controlled_indexable_pairs"], 395)
        self.assertEqual(
            session.calls[0][1],
            "https://teknoskor.example/api/ingestion/readiness",
        )

    def test_price_index_audit_parses_a_not_ready_report_without_failing(self):
        client, session = self.make_client(
            [
                FakeResponse(
                    200,
                    {
                        "ready": False,
                        "counts": {
                            "comparableProducts": 0,
                            "currentProducts": 67,
                            "historyProducts": 0,
                            "observationDays": 2,
                            "outlierRows": 0,
                            "usableHistoryRows": 88,
                        },
                        "gates": {
                            "comparableBasket": False,
                            "currentCatalog": True,
                            "historyDepth": False,
                            "observationPeriod": False,
                            "outliersBounded": True,
                        },
                        "monthlyCoverage": [],
                        "reasonCodes": ["gate_comparable_basket_failed"],
                    },
                )
            ]
        )

        result = client.fetch_price_index_readiness()

        self.assertFalse(result["ready"])
        self.assertEqual(
            session.calls[0][1],
            "https://teknoskor.example/api/ingestion/price-index-readiness",
        )

    def test_from_env_requires_both_values(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                IngestionConfigurationError,
                "TEKNOSKOR_INGESTION_URL",
            ):
                SourceIngestionClient.from_env()

    def test_authenticated_catalog_is_paginated_and_strict(self):
        client, session = self.make_client(
            [
                FakeResponse(
                    200,
                    {
                        "products": [
                            {
                                "id": 2,
                                "name": "Phone B",
                                "slug": "phone-b",
                                "attributes": {},
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
                                "id": 1,
                                "name": "Phone A",
                                "slug": "phone-a",
                                "attributes": {},
                                "data_quality_status": "verified",
                                "data_quality_issues": [],
                                "spec_verified_at": "2026-08-13T06:00:00Z",
                            }
                        ],
                        "hasMore": False,
                    },
                ),
            ]
        )

        products = client.fetch_catalog(page_size=1)

        self.assertEqual(
            [product.slug for product in products],
            ["phone-a", "phone-b"],
        )
        self.assertEqual(products[0].data_quality_status, "verified")
        self.assertEqual(
            products[0].spec_verified_at,
            "2026-08-13T06:00:00Z",
        )
        self.assertIsNone(products[1].data_quality_status)
        self.assertEqual(
            session.calls[0][2]["headers"]["Authorization"],
            f"Bearer {SECRET}",
        )
        self.assertEqual(session.calls[1][2]["params"]["page"], "2")

    def test_malformed_or_duplicate_catalog_entries_fail_closed(self):
        malformed, _ = self.make_client(
            [
                FakeResponse(
                    200,
                    {
                        "products": [
                            {
                                "id": 1,
                                "name": "Phone",
                                "slug": "Bad Slug",
                                "attributes": {},
                            }
                        ],
                        "hasMore": False,
                    },
                )
            ]
        )
        with self.assertRaisesRegex(
            SourceIngestionError,
            "Malformed catalog product",
        ):
            malformed.fetch_catalog()

        duplicate, _ = self.make_client(
            [
                FakeResponse(
                    200,
                    {
                        "products": [
                            {
                                "id": 1,
                                "name": "Phone A",
                                "slug": "phone-a",
                                "attributes": {},
                            },
                            {
                                "id": 1,
                                "name": "Phone B",
                                "slug": "phone-b",
                                "attributes": {},
                            },
                        ],
                        "hasMore": False,
                    },
                )
            ]
        )
        with self.assertRaisesRegex(
            SourceIngestionError,
            "Duplicate catalog product",
        ):
            duplicate.fetch_catalog()

    def test_retry_then_commit_preserves_authenticated_payload(self):
        client, session = self.make_client(
            [
                FakeResponse(500, {"error": "temporary"}),
                FakeResponse(
                    200,
                    {
                        "accepted": 1,
                        "stale_ignored": 0,
                        "affected_products": 1,
                        "changed_products": 1,
                        "changed_paths": ["/product/phone-one"],
                        "committed": True,
                    },
                ),
            ]
        )
        record = {
            "product_slug": "phone-one",
            "attribute_key": "antutu_score",
            "value_origin": "benchmark_source",
            "source_url": "https://www.kimovil.com/en/phone-one",
            "observed_at": "2026-07-27T10:00:00Z",
            "is_estimate": False,
            "confidence": "medium",
            "value": 900000,
        }

        result = client.submit_sources([record])

        self.assertEqual(result["accepted"], 1)
        self.assertEqual(len(session.calls), 2)
        for _method, _url, kwargs in session.calls:
            self.assertEqual(kwargs["json"], {"sources": [record]})
            self.assertEqual(
                kwargs["headers"]["Authorization"],
                f"Bearer {SECRET}",
            )

    def test_committed_503_retries_same_idempotent_payload(self):
        committed_failure = {
            "accepted": 1,
            "stale_ignored": 0,
            "affected_products": 1,
            "changed_products": 1,
            "changed_paths": [],
            "scores_recalculated": False,
            "committed": True,
        }
        client, session = self.make_client(
            [
                FakeResponse(503, committed_failure),
                FakeResponse(
                    200,
                    {
                        "accepted": 1,
                        "stale_ignored": 0,
                        "affected_products": 1,
                        "changed_products": 0,
                        "changed_paths": [],
                        "scores_recalculated": True,
                        "committed": True,
                    },
                ),
            ]
        )
        record = {"record": 1}

        result = client.submit_sources([record])

        self.assertEqual(result["accepted"], 1)
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(
            all(
                kwargs["json"] == {"sources": [record]}
                for _method, _url, kwargs in session.calls
            )
        )

    def test_persistent_committed_503_fails_workflow_explicitly(self):
        committed_failure = FakeResponse(
            503,
            {
                "accepted": 1,
                "stale_ignored": 0,
                "affected_products": 1,
                "changed_products": 0,
                "changed_paths": [],
                "scores_recalculated": False,
                "committed": True,
            },
        )
        client, session = self.make_client(
            [committed_failure, committed_failure, committed_failure],
            max_attempts=3,
        )

        with self.assertRaisesRegex(
            SourceIngestionError,
            "was committed, but score recalculation failed after 3 attempts",
        ) as raised:
            client.submit_sources([{"record": 1}])

        self.assertEqual(raised.exception.status_code, 503)
        self.assertTrue(raised.exception.response_body["committed"])
        self.assertEqual(len(session.calls), 3)

    def test_partial_success_acknowledgement_fails_closed(self):
        client, _session = self.make_client(
            [
                FakeResponse(
                    200,
                    {
                        "accepted": 0,
                        "stale_ignored": 0,
                        "committed": True,
                    },
                )
            ]
        )

        with self.assertRaisesRegex(
            SourceIngestionError,
            "acknowledgement count",
        ):
            client.submit_sources([{"record": 1}])

    def test_unknown_product_has_specific_error(self):
        client, _session = self.make_client(
            [
                FakeResponse(
                    409,
                    {
                        "error": (
                            "Product reference not found or id/slug mismatch"
                        ),
                        "index": 0,
                    },
                )
            ]
        )

        with self.assertRaises(ExistingProductNotFoundError):
            client.submit_sources([{"record": 1}])


class PhysicalRefreshTests(unittest.TestCase):
    def catalog_client(self):
        client = Mock()
        client.fetch_catalog.return_value = [CatalogProduct(
            1, 'Phone One', 'phone-one',
            {'ram_gb': 8, 'storage_gb': 128, 'battery_mah': 5000, 'screen_size_inch': 6.1},
            'verified', (), '2026-01-01T00:00:00Z', ('https://cdn.teknoskor.com/phone.webp',))]
        client.submit_sources.return_value = {'accepted': 6, 'stale_ignored': 0, 'affected_products': 1}
        return client

    def test_verified_phone_refresh_uses_current_fields_and_exact_memory(self):
        client = self.catalog_client()
        scraper = KimovilScraper(ingestion_client=client)
        scraper.get_via_flaresolverr = Mock(return_value="""
            <meta name='deviceki' content='{"name":"Phone One","partials":{}}'>
            <section class='container-sheet-hardware'><h2>Performance &amp; Hardware</h2>
            <table class='k-dltable'>
              <tr><th class='label'>Memory RAM</th><td class='value'>8 GB</td></tr>
              <tr><th class='label'>Capacity</th><td class='value'>128 GB</td></tr>
            </table></section>
            <section class='container-sheet-battery'><h2>Battery</h2>
            <table class='k-dltable'><tr><th class='label'>Capacity</th><td class='value'>6000 mAh</td></tr></table>
            </section>
        """)
        summary = scraper.scrape_existing_products(refresh_physical=True)
        self.assertEqual(summary['failed'], [])
        records = client.submit_sources.call_args.args[0]
        values = {record['attribute_key']: record['value'] for record in records}
        self.assertEqual(values['battery_mah'], 6000)
        self.assertEqual(values['ram_gb'], 8)
        self.assertEqual(values['storage_gb'], 128)
        self.assertNotIn('screen_size_inch', values)
        self.assertTrue(all(record['observed_at'] != '2026-01-01T00:00:00Z' for record in records))
        client.submit_sources.reset_mock()
        scraper.get_via_flaresolverr.return_value = scraper.get_via_flaresolverr.return_value.replace('128 GB', '256 GB')
        scraper.search_product_on_kimovil = Mock(return_value=None)
        summary = scraper.scrape_existing_products(refresh_physical=True)
        self.assertEqual(summary['failed'], ['phone-one'])
        client.submit_sources.assert_not_called()

    def test_failed_physical_read_does_not_submit_or_redate_existing_data(self):
        client = self.catalog_client()
        scraper = KimovilScraper(ingestion_client=client)
        scraper.get_via_flaresolverr = Mock(return_value=None)
        scraper.search_product_on_kimovil = Mock(return_value=None)
        summary = scraper.scrape_existing_products(refresh_physical=True)
        self.assertEqual(summary['failed'], ['phone-one'])
        client.submit_sources.assert_not_called()
        self.assertEqual(client.fetch_catalog.return_value[0].spec_verified_at, '2026-01-01T00:00:00Z')

    def test_catalog_variant_refresh_follows_observed_provider_url_and_source_timestamp(self):
        from automatic_memory_variants import TechnicalVariantReader
        from test_automatic_memory_variants import tech_document
        from dataclasses import replace
        model = 'Samsung Galaxy A54 5G'
        base = 'https://www.kimovil.com/en/where-to-buy-samsung-galaxy-a54-5g'
        target = base + 'global-8gb-256gb'
        client = self.catalog_client()
        variant = replace(client.fetch_catalog.return_value[0], name=model + ' 8GB 256GB',
                          slug='samsung-galaxy-a54-5g-8gb-256gb',
                          attributes={'ram_gb': 8, 'storage_gb': 256, 'Connectivity': {'5G': True}})
        client.fetch_catalog.return_value = [variant]
        source = Mock()
        source.read.side_effect = lambda url: (json.dumps({'results': [{
            'full_name': model, 'url': 'samsung-galaxy-a54-5g'}]}) if '_json/' in url else
            tech_document(base, storage=128, name=model) + '<div class="versions-list"><a href="' + target + '">8GB 256GB</a></div>'
            if url == base else tech_document(target, storage=256, name=model))
        reader = TechnicalVariantReader(source)
        scraper = KimovilScraper(ingestion_client=client, technical_reader=reader)
        scraper.get_via_flaresolverr = Mock(return_value=None)
        scraper.search_product_on_kimovil = Mock(return_value=None)
        observed = '2026-11-17T09:00:00Z'
        with patch('automatic_memory_variants.utc_observation_time', return_value=observed):
            result = scraper.scrape_existing_products(refresh_physical=True, only_slugs=[variant.slug])
        self.assertEqual(result['failed'], [])
        self.assertEqual(result['exact_variant_refreshes'], [{'slug': variant.slug, 'source_url': target, 'observed_at': observed}])
        records = client.submit_sources.call_args.args[0]
        self.assertTrue(all(record['source_url'] == target and record['observed_at'] == observed for record in records))
        values = {record['attribute_key']: record['value'] for record in records}
        self.assertEqual(values['storage_gb'], 256)
        self.assertNotIn('gaming_performance', values)
        scraper.get_via_flaresolverr.assert_not_called()
        scraper.search_product_on_kimovil.assert_not_called()
        self.assertEqual([call.args[0] for call in source.read.call_args_list][-2:], [base, target])
        # A freshly selected wrong capacity cannot refresh this product.
        source.read.side_effect = lambda url: (json.dumps({'results': [{
            'full_name': model, 'url': 'samsung-galaxy-a54-5g'}]}) if '_json/' in url else
            tech_document(base, storage=128, name=model))
        scraper.technical_reader = TechnicalVariantReader(source)
        client.submit_sources.reset_mock()
        result = scraper.scrape_existing_products(refresh_physical=True, only_slugs=[variant.slug])
        self.assertEqual(result['failed'], [variant.slug])
        client.submit_sources.assert_not_called()

    def test_retry_selection_never_refreshes_other_or_unknown_products(self):
        from dataclasses import replace
        client = self.catalog_client()
        other = replace(client.fetch_catalog.return_value[0], id=2, name='Phone Two', slug='phone-two')
        client.fetch_catalog.return_value.append(other)
        scraper = KimovilScraper(ingestion_client=client)
        scraper.scrape_product_details = Mock(return_value=True)
        result = scraper.scrape_existing_products(only_slugs=['phone-two'])
        self.assertEqual(result['attempted_products'], 1)
        self.assertEqual(scraper.scrape_product_details.call_args.kwargs['product_slug'], 'phone-two')
        scraper.scrape_product_details.reset_mock()
        with self.assertRaisesRegex(IngestionConfigurationError, 'Unknown selected product slugs'):
            scraper.scrape_existing_products(only_slugs=['unknown-phone'])
        scraper.scrape_product_details.assert_not_called()


if __name__ == "__main__":
    unittest.main()
