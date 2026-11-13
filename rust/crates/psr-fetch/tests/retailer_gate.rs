//! Integration tests for bounded retailer transport.

use std::time::{Duration, Instant};

use psr_fetch::retailer_gate::{PAZARAMA_MAX_PDP_REQUESTS, PUBLIC_MAX_REQUESTS_PER_SOURCE};
use psr_fetch::RetailerGate;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

fn http() -> reqwest::Client {
    reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(5))
        .timeout(Duration::from_secs(20))
        .build()
        .unwrap()
}

fn gate() -> RetailerGate {
    RetailerGate::custom(http(), Some("127.0.0.1".to_string()), 150, 5 * 1024 * 1024)
}

fn gate_with_budget(max_requests: usize) -> RetailerGate {
    RetailerGate::custom(
        http(),
        Some("127.0.0.1".to_string()),
        max_requests,
        5 * 1024 * 1024,
    )
}

fn gate_with_body_cap(max_body_bytes: usize) -> RetailerGate {
    RetailerGate::custom(http(), Some("127.0.0.1".to_string()), 150, max_body_bytes)
}

#[tokio::test]
async fn production_budget_constants_match_python() {
    // public_retailer_sources._MAX_REQUESTS_PER_SOURCE and
    // pazarama_category_source's PDP budget.
    assert_eq!(PUBLIC_MAX_REQUESTS_PER_SOURCE, 150);
    assert_eq!(PAZARAMA_MAX_PDP_REQUESTS, 36);
}

#[tokio::test]
async fn denial_blocks_the_host_but_the_page_is_still_returned() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/p-1"))
        .respond_with(ResponseTemplate::new(403).set_body_string("denied"))
        .mount(&server)
        .await;

    let gate = gate();
    // Python `_request` returns the response after blocking on 403; the page
    // reaches the caller and the circuit only stops the *next* request.
    let page = gate.fetch(&format!("{}/p-1", server.uri())).await.unwrap();
    assert_eq!(page.status, 403);

    let second = gate
        .fetch(&format!("{}/p-2", server.uri()))
        .await
        .err()
        .unwrap();
    assert_eq!(second.reason(), "circuit_or_budget_open");
    assert_eq!(server.received_requests().await.unwrap().len(), 1);
}

#[tokio::test]
async fn page_cache_serves_the_second_fetch_without_a_new_request() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/p-1"))
        .respond_with(ResponseTemplate::new(200).set_body_string("<html>stock</html>"))
        .mount(&server)
        .await;

    let gate = gate();
    let url = format!("{}/p-1", server.uri());
    let first = gate.fetch(&url).await.unwrap();
    let second = gate.fetch(&url).await.unwrap();
    assert_eq!(first.body, second.body);
    assert_eq!(first.status, 200);
    assert_eq!(server.received_requests().await.unwrap().len(), 1);
}

#[tokio::test]
async fn oversized_response_fails_without_blocking() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/p-1"))
        .respond_with(ResponseTemplate::new(200).set_body_string("x".repeat(400)))
        .mount(&server)
        .await;

    // Python raises oversized *after* the status check and never blocks the
    // host for it: the next fetch of the same URL retries the request.
    let gate = gate_with_body_cap(128);
    let error = gate
        .fetch(&format!("{}/p-1", server.uri()))
        .await
        .err()
        .unwrap();
    assert_eq!(error.reason(), "oversized_response");
    let again = gate
        .fetch(&format!("{}/p-1", server.uri()))
        .await
        .err()
        .unwrap();
    assert_eq!(again.reason(), "oversized_response");
    assert_eq!(server.received_requests().await.unwrap().len(), 2);
}

#[tokio::test]
async fn minimum_delay_paces_requests_on_the_same_host() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/p-1"))
        .respond_with(ResponseTemplate::new(200).set_body_string("<html>ok</html>"))
        .mount(&server)
        .await;

    let gate = gate();
    gate.fetch(&format!("{}/p-1", server.uri())).await.unwrap();
    // A different URL on the same host must sleep out the remaining crawl
    // delay (same-URL repeats are served from the page cache unpaced).
    let started = Instant::now();
    gate.fetch(&format!("{}/p-2", server.uri())).await.unwrap();
    assert!(started.elapsed() >= Duration::from_millis(450));
}

#[tokio::test]
async fn budget_stops_the_host_after_the_max_requests() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .respond_with(ResponseTemplate::new(200).set_body_string("<html>ok</html>"))
        .mount(&server)
        .await;

    let gate = gate_with_budget(1);
    gate.fetch(&format!("{}/p-1", server.uri())).await.unwrap();
    let error = gate
        .fetch(&format!("{}/p-2", server.uri()))
        .await
        .err()
        .unwrap();
    assert_eq!(error.reason(), "circuit_or_budget_open");
    assert_eq!(server.received_requests().await.unwrap().len(), 1);
}

#[tokio::test]
async fn gate_returns_location_without_following_redirects() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/p-1"))
        .respond_with(
            ResponseTemplate::new(302).append_header("Location", &format!("{}/p-2", server.uri())),
        )
        .mount(&server)
        .await;

    let gate = gate();
    let page = gate.fetch(&format!("{}/p-1", server.uri())).await.unwrap();
    assert_eq!(page.status, 302);
    let expected = format!("{}/p-2", server.uri());
    assert_eq!(page.location.as_deref(), Some(expected.as_str()));
}

#[tokio::test]
async fn transport_error_blocks_the_host() {
    // Reserve and release a port to create a connection-refused target.
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);

    let gate = RetailerGate::custom(http(), Some("127.0.0.1".to_string()), 150, 5 * 1024 * 1024);
    let error = gate
        .fetch(&format!("http://127.0.0.1:{port}/p-1"))
        .await
        .err()
        .unwrap();
    assert_eq!(error.reason(), "transport_error");
    let second = gate
        .fetch(&format!("http://127.0.0.1:{port}/p-2"))
        .await
        .err()
        .unwrap();
    assert_eq!(second.reason(), "circuit_or_budget_open");
}
