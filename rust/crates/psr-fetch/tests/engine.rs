//! Wiremock tests for the `_fetch_target` generic walk. The Python suite
//! (test_retailer_offer_refresh.py) injects a fake fetch; here a real HTTP
//! stack runs against a local mock server, with the engine in permissive
//! mode (plain-HTTP localhost targets) while the walk logic stays identical.

use std::time::Duration;

use psr_fetch::{AttemptReason, FetchEngine};
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

fn engine() -> FetchEngine {
    let http = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(5))
        .timeout(Duration::from_secs(20))
        .build()
        .unwrap();
    FetchEngine::permissive_for_tests(http)
}

fn product_schema(url: &str) -> serde_json::Value {
    serde_json::json!({
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "Samsung Galaxy S25 256GB Akıllı Telefon",
        "url": url,
        "offers": {
            "@type": "Offer",
            "price": "29999.90",
            "priceCurrency": "TRY",
            "availability": "https://schema.org/InStock",
            "url": url,
        },
    })
}

fn page(schema: &serde_json::Value) -> String {
    format!(
        "<!doctype html><html><head><title>Shop</title></head><body><script type=\"application/ld+json\">{}</script></body></html>",
        serde_json::to_string(schema).unwrap()
    )
}

async fn mount_product_schema(server: &MockServer, page_path: &str, schema_url: &str) {
    Mock::given(method("GET"))
        .and(path(page_path))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_string(page(&product_schema(schema_url)))
                .append_header("Content-Type", "text/html"),
        )
        .mount(server)
        .await;
}

async fn mount_product(server: &MockServer, page_path: &str) {
    mount_product_schema(server, page_path, &format!("{}{}", server.uri(), page_path)).await;
}

#[tokio::test]
async fn valid_jsonld_page_yields_observed_exact_offer() {
    let server = MockServer::start().await;
    mount_product(&server, "/samsung-galaxy-s25-256gb-p-1").await;
    let engine = engine();
    let url = format!("{}/samsung-galaxy-s25-256gb-p-1", server.uri());
    let offer = engine.fetch_target(&url).await.expect("offer");
    assert_eq!(offer.price, 29999.90);
    assert_eq!(offer.currency, "TRY");
    assert_eq!(offer.availability, "in_stock");
    assert!(offer.observed_at.ends_with('Z'));
    let record = engine.diagnostics(&url).await.unwrap();
    assert_eq!(record.reason, AttemptReason::ObservedExactOffer.as_str());
    assert_eq!(record.http_status, Some(200));
    // Repeated fetch is served from the run cache without a second hit.
    assert_eq!(server.received_requests().await.unwrap().len(), 1);
    assert_eq!(engine.fetch_target(&url).await.unwrap().price, 29999.90);
    assert_eq!(server.received_requests().await.unwrap().len(), 1);
}

#[tokio::test]
async fn denied_host_blocks_for_the_rest_of_the_run() {
    let server = MockServer::start().await;
    let url = format!("{}/phone-p-1", server.uri());
    Mock::given(method("GET"))
        .respond_with(ResponseTemplate::new(403))
        .mount(&server)
        .await;
    let engine = engine();
    assert!(engine.fetch_target(&url).await.is_none());
    let record = engine.diagnostics(&url).await.unwrap();
    assert_eq!(record.reason, "access_denied");
    assert_eq!(record.http_status, Some(403));

    // A second URL on the same host hits the circuit before any request.
    let url2 = format!("{}/phone-p-2", server.uri());
    assert!(engine.fetch_target(&url2).await.is_none());
    assert_eq!(
        engine.diagnostics(&url2).await.unwrap().reason,
        "host_circuit_open"
    );
    assert_eq!(server.received_requests().await.unwrap().len(), 1);
}

#[tokio::test]
async fn rate_limit_blocks_host_with_reason() {
    let server = MockServer::start().await;
    let url = format!("{}/phone-p-1", server.uri());
    Mock::given(method("GET"))
        .respond_with(ResponseTemplate::new(429))
        .mount(&server)
        .await;
    let engine = engine();
    assert!(engine.fetch_target(&url).await.is_none());
    let record = engine.diagnostics(&url).await.unwrap();
    assert_eq!(record.reason, "rate_limited");
    assert_eq!(record.http_status, Some(429));
}

#[tokio::test]
async fn challenge_page_blocks_host() {
    let server = MockServer::start().await;
    let url = format!("{}/phone-p-1", server.uri());
    Mock::given(method("GET"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_string("<html><head><title>Just a moment...</title></head><body>Challenge</body></html>"),
        )
        .mount(&server)
        .await;
    let engine = engine();
    assert!(engine.fetch_target(&url).await.is_none());
    let record = engine.diagnostics(&url).await.unwrap();
    assert_eq!(record.reason, "challenge_page");
    assert_eq!(record.http_status, Some(200));

    let url2 = format!("{}/phone-p-2", server.uri());
    assert!(engine.fetch_target(&url2).await.is_none());
    assert_eq!(
        engine.diagnostics(&url2).await.unwrap().reason,
        "host_circuit_open"
    );
    assert_eq!(server.received_requests().await.unwrap().len(), 1);
}

#[tokio::test]
async fn repeated_server_errors_trip_transient_circuit() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .respond_with(ResponseTemplate::new(503))
        .mount(&server)
        .await;
    let engine = engine();
    let url = format!("{}/phone-p-1", server.uri());
    assert!(engine.fetch_target(&url).await.is_none());
    assert_eq!(
        engine.diagnostics(&url).await.unwrap().reason,
        "retailer_unavailable"
    );
    // Second failure opens the transient cooldown for the host.
    let url2 = format!("{}/phone-p-2", server.uri());
    engine.fetch_target(&url2).await;
    // Third URL on the same host hits the circuit before any request.
    let url3 = format!("{}/phone-p-3", server.uri());
    assert!(engine.fetch_target(&url3).await.is_none());
    assert_eq!(
        engine.diagnostics(&url3).await.unwrap().reason,
        "host_circuit_open"
    );
    // Only two requests reached the server.
    assert_eq!(server.received_requests().await.unwrap().len(), 2);
}

#[tokio::test]
async fn http_error_is_recorded_without_circuit() {
    let server = MockServer::start().await;
    let url = format!("{}/phone-p-1", server.uri());
    Mock::given(method("GET"))
        .respond_with(ResponseTemplate::new(404))
        .mount(&server)
        .await;
    let engine = engine();
    assert!(engine.fetch_target(&url).await.is_none());
    let record = engine.diagnostics(&url).await.unwrap();
    assert_eq!(record.reason, "http_error");
    assert_eq!(record.http_status, Some(404));
    // 404 is not a denial: the next URL still reaches the server.
    assert!(engine.fetch_target(&url).await.is_none()); // cached None
    assert_eq!(server.received_requests().await.unwrap().len(), 1);
}

#[tokio::test]
async fn oversized_body_fails_closed() {
    let server = MockServer::start().await;
    let url = format!("{}/phone-p-1", server.uri());
    let big = "x".repeat(psr_fetch::MAX_BODY_BYTES + 1);
    Mock::given(method("GET"))
        .respond_with(ResponseTemplate::new(200).set_body_string(big))
        .mount(&server)
        .await;
    let engine = engine();
    assert!(engine.fetch_target(&url).await.is_none());
    let record = engine.diagnostics(&url).await.unwrap();
    assert_eq!(record.reason, "invalid_or_oversized_response");
    assert_eq!(record.http_status, Some(200));
}

#[tokio::test]
async fn redirect_chain_preserves_variant_and_observations() {
    let server = MockServer::start().await;
    let target = format!("{}/final-p-1?sku=256gb", server.uri());
    mount_product_schema(&server, "/final-p-1", &target).await;
    Mock::given(method("GET"))
        .and(path("/hop-p-1"))
        .respond_with(ResponseTemplate::new(302).append_header("Location", &target))
        .mount(&server)
        .await;
    let engine = engine();
    let url = format!("{}/hop-p-1?sku=256gb", server.uri());
    let offer = engine.fetch_target(&url).await.expect("offer follows redirect");
    assert_eq!(offer.url, target);
    let record = engine.diagnostics(&url).await.unwrap();
    assert_eq!(record.reason, "observed_exact_offer");
}

#[tokio::test]
async fn redirect_cannot_discard_or_change_variant() {
    let server = MockServer::start().await;
    mount_product(&server, "/final-p-1").await;
    Mock::given(method("GET"))
        .and(path("/hop-p-1"))
        .respond_with(
            ResponseTemplate::new(302)
                .append_header("Location", format!("{}/final-p-1", server.uri())),
        )
        .mount(&server)
        .await;
    let engine = engine();
    let url = format!("{}/hop-p-1?sku=256gb", server.uri());
    assert!(engine.fetch_target(&url).await.is_none());
    let record = engine.diagnostics(&url).await.unwrap();
    assert_eq!(record.reason, "unsafe_or_changed_variant_redirect");
    assert_eq!(record.http_status, Some(302));
}

#[tokio::test]
async fn redirect_to_unsafe_host_is_rejected() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/hop-p-1"))
        .respond_with(
            ResponseTemplate::new(301).append_header("Location", "https://evil.example.com/steal"),
        )
        .mount(&server)
        .await;
    let engine = engine();
    let url = format!("{}/hop-p-1", server.uri());
    assert!(engine.fetch_target(&url).await.is_none());
    assert_eq!(
        engine.diagnostics(&url).await.unwrap().reason,
        "unsafe_or_changed_variant_redirect"
    );
}

#[tokio::test]
async fn redirect_walk_stops_at_four_hops() {
    let server = MockServer::start().await;
    for hop in 1..=5 {
        Mock::given(method("GET"))
            .and(path(&format!("/hop-{hop}")))
            .respond_with(
                ResponseTemplate::new(302).append_header(
                    "Location",
                    format!("{}/hop-{}", server.uri(), hop + 1),
                ),
            )
            .mount(&server)
            .await;
    }
    let engine = engine();
    let url = format!("{}/hop-1", server.uri());
    assert!(engine.fetch_target(&url).await.is_none());
    let record = engine.diagnostics(&url).await.unwrap();
    assert_eq!(record.reason, "redirect_limit_or_missing_location");
    assert_eq!(record.http_status, Some(302));
    // 4 redirect responses were fetched (hop-5 is never requested).
    assert_eq!(server.received_requests().await.unwrap().len(), 4);
}

#[tokio::test]
async fn redirect_without_location_header_is_recorded() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/hop-p-1"))
        .respond_with(ResponseTemplate::new(302))
        .mount(&server)
        .await;
    let engine = engine();
    let url = format!("{}/hop-p-1", server.uri());
    assert!(engine.fetch_target(&url).await.is_none());
    let record = engine.diagnostics(&url).await.unwrap();
    assert_eq!(record.reason, "redirect_limit_or_missing_location");
}

#[tokio::test]
async fn unsafe_url_returns_none_without_diagnostics() {
    let engine = engine();
    // Non-HTTP scheme and empty string never touch any state.
    assert!(engine.fetch_target("ftp://example.com/x").await.is_none());
    assert!(engine.fetch_target("").await.is_none());
    assert!(engine.cached("ftp://example.com/x").await.is_none());
    assert!(engine.diagnostics("ftp://example.com/x").await.is_none());
}

#[tokio::test]
async fn unrelated_product_page_yields_no_offer() {
    let server = MockServer::start().await;
    let url = format!("{}/different-phone-p-2", server.uri());
    let schema = product_schema(&format!("{}/unrelated-phone-p-9", server.uri()));
    Mock::given(method("GET"))
        .and(path("/different-phone-p-2"))
        .respond_with(ResponseTemplate::new(200).set_body_string(page(&schema)))
        .mount(&server)
        .await;
    let engine = engine();
    assert!(engine.fetch_target(&url).await.is_none());
    assert_eq!(
        engine.diagnostics(&url).await.unwrap().reason,
        "no_exact_product_offer"
    );
}

#[tokio::test]
async fn hasvariant_page_fails_closed() {
    let server = MockServer::start().await;
    let url = format!("{}/phone-p-1", server.uri());
    let mut schema = product_schema(&url);
    schema["hasVariant"] = serde_json::json!([product_schema(&url)]);
    Mock::given(method("GET"))
        .and(path("/phone-p-1"))
        .respond_with(ResponseTemplate::new(200).set_body_string(page(&schema)))
        .mount(&server)
        .await;
    let engine = engine();
    assert!(engine.fetch_target(&url).await.is_none());
    assert_eq!(
        engine.diagnostics(&url).await.unwrap().reason,
        "no_exact_product_offer"
    );
}
