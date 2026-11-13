//! Wiremock integration tests for `RobotsGate` (public_retailer_sources /
//! pazarama gate parity). Every failure mode must keep the exact Python
//! semantics: fail closed, block the host, and return the same reason strings.

use std::time::{Duration, Instant};

use psr_fetch::robots::{PAZARAMA_MAX_PDP_REQUESTS, PUBLIC_MAX_REQUESTS_PER_SOURCE};
use psr_fetch::RobotsGate;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

const ALLOW_ALL: &str = "User-agent: *\nDisallow:\n";

fn http() -> reqwest::Client {
    reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(5))
        .timeout(Duration::from_secs(20))
        .build()
        .unwrap()
}

fn gate(server: &MockServer) -> RobotsGate {
    RobotsGate::custom(
        http(),
        Some("127.0.0.1".to_string()),
        Some(format!("{}/robots.txt", server.uri())),
        150,
        5 * 1024 * 1024,
    )
}

fn gate_with_budget(server: &MockServer, max_requests: usize) -> RobotsGate {
    RobotsGate::custom(
        http(),
        Some("127.0.0.1".to_string()),
        Some(format!("{}/robots.txt", server.uri())),
        max_requests,
        5 * 1024 * 1024,
    )
}

fn gate_with_body_cap(server: &MockServer, max_body_bytes: usize) -> RobotsGate {
    RobotsGate::custom(
        http(),
        Some("127.0.0.1".to_string()),
        Some(format!("{}/robots.txt", server.uri())),
        150,
        max_body_bytes,
    )
}

async fn mount_robots(server: &MockServer, body: &'static str) {
    Mock::given(method("GET"))
        .and(path("/robots.txt"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_string(body)
                .append_header("Content-Type", "text/plain"),
        )
        .mount(server)
        .await;
}

#[tokio::test]
async fn production_budget_constants_match_python() {
    // public_retailer_sources._MAX_REQUESTS_PER_SOURCE and
    // pazarama_category_source's PDP budget.
    assert_eq!(PUBLIC_MAX_REQUESTS_PER_SOURCE, 150);
    assert_eq!(PAZARAMA_MAX_PDP_REQUESTS, 36);
}

#[tokio::test]
async fn robots_disallow_never_requests_the_page() {
    let server = MockServer::start().await;
    mount_robots(&server, "User-agent: *\nDisallow: /\n").await;
    Mock::given(method("GET"))
        .and(path("/p-1"))
        .respond_with(ResponseTemplate::new(200).set_body_string("<html>ok</html>"))
        .mount(&server)
        .await;

    let gate = gate(&server);
    let error = gate
        .fetch(&format!("{}/p-1", server.uri()))
        .await
        .err()
        .unwrap();
    assert_eq!(error.reason(), "robots_disallowed");
    // Only the robots document was fetched; the target page must not be hit.
    assert_eq!(server.received_requests().await.unwrap().len(), 1);
}

#[tokio::test]
async fn html_robots_policy_unavailable_blocks_the_host() {
    let server = MockServer::start().await;
    // Retailers serve an HTML 404/challenge with status 200 at robots.txt.
    Mock::given(method("GET"))
        .and(path("/robots.txt"))
        .respond_with(
            ResponseTemplate::new(200)
                .set_body_string("<!doctype html><html><body>challenge</body></html>")
                .append_header("Content-Type", "text/html"),
        )
        .mount(&server)
        .await;

    let gate = gate(&server);
    let error = gate
        .fetch(&format!("{}/p-1", server.uri()))
        .await
        .err()
        .unwrap();
    assert_eq!(error.reason(), "robots_policy_unavailable");
    // The host stays blocked for the rest of the run without another request.
    let second = gate.fetch(&format!("{}/p-2", server.uri())).await.err().unwrap();
    assert_eq!(second.reason(), "circuit_or_budget_open");
    assert_eq!(server.received_requests().await.unwrap().len(), 1);
}

#[tokio::test]
async fn robots_404_is_policy_unavailable() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/robots.txt"))
        .respond_with(ResponseTemplate::new(404).set_body_string("missing"))
        .mount(&server)
        .await;

    let gate = gate(&server);
    let error = gate
        .fetch(&format!("{}/p-1", server.uri()))
        .await
        .err()
        .unwrap();
    assert_eq!(error.reason(), "robots_policy_unavailable");
}

#[tokio::test]
async fn denial_blocks_the_host_but_the_page_is_still_returned() {
    let server = MockServer::start().await;
    mount_robots(&server, ALLOW_ALL).await;
    Mock::given(method("GET"))
        .and(path("/p-1"))
        .respond_with(ResponseTemplate::new(403).set_body_string("denied"))
        .mount(&server)
        .await;

    let gate = gate(&server);
    // Python `_request` returns the response after blocking on 403; the page
    // reaches the caller and the circuit only stops the *next* request.
    let page = gate.fetch(&format!("{}/p-1", server.uri())).await.unwrap();
    assert_eq!(page.status, 403);

    let second = gate.fetch(&format!("{}/p-2", server.uri())).await.err().unwrap();
    assert_eq!(second.reason(), "circuit_or_budget_open");
    assert_eq!(server.received_requests().await.unwrap().len(), 2);
}

#[tokio::test]
async fn page_cache_serves_the_second_fetch_without_a_new_request() {
    let server = MockServer::start().await;
    mount_robots(&server, ALLOW_ALL).await;
    Mock::given(method("GET"))
        .and(path("/p-1"))
        .respond_with(ResponseTemplate::new(200).set_body_string("<html>stock</html>"))
        .mount(&server)
        .await;

    let gate = gate(&server);
    let url = format!("{}/p-1", server.uri());
    let first = gate.fetch(&url).await.unwrap();
    let second = gate.fetch(&url).await.unwrap();
    assert_eq!(first.body, second.body);
    assert_eq!(first.status, 200);
    assert_eq!(server.received_requests().await.unwrap().len(), 2); // robots + page
}

#[tokio::test]
async fn oversized_response_fails_without_blocking() {
    let server = MockServer::start().await;
    mount_robots(&server, ALLOW_ALL).await;
    Mock::given(method("GET"))
        .and(path("/p-1"))
        .respond_with(ResponseTemplate::new(200).set_body_string("x".repeat(400)))
        .mount(&server)
        .await;

    // Python raises oversized *after* the status check and never blocks the
    // host for it: the next fetch of the same URL retries the request.
    let gate = gate_with_body_cap(&server, 128);
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
    assert_eq!(server.received_requests().await.unwrap().len(), 3); // robots + 2 pages
}

#[tokio::test]
async fn crawl_delay_exceeding_run_budget_never_requests_the_page() {
    let server = MockServer::start().await;
    mount_robots(&server, "User-agent: *\nDisallow:\nCrawl-delay: 60\n").await;

    let gate = gate(&server);
    let error = gate
        .fetch(&format!("{}/p-1", server.uri()))
        .await
        .err()
        .unwrap();
    assert_eq!(error.reason(), "crawl_delay_exceeds_budget");
    assert_eq!(server.received_requests().await.unwrap().len(), 1);
}

#[tokio::test]
async fn crawl_delay_paces_requests_on_the_same_host() {
    let server = MockServer::start().await;
    mount_robots(&server, "User-agent: *\nDisallow:\nCrawl-delay: 1\n").await;
    Mock::given(method("GET"))
        .and(path("/p-1"))
        .respond_with(ResponseTemplate::new(200).set_body_string("<html>ok</html>"))
        .mount(&server)
        .await;

    let gate = gate(&server);
    gate.fetch(&format!("{}/p-1", server.uri())).await.unwrap();
    // A different URL on the same host must sleep out the remaining crawl
    // delay (same-URL repeats are served from the page cache unpaced).
    let started = Instant::now();
    gate.fetch(&format!("{}/p-2", server.uri())).await.unwrap();
    assert!(started.elapsed() >= Duration::from_millis(900));
}

#[tokio::test]
async fn budget_stops_the_host_after_the_max_requests() {
    let server = MockServer::start().await;
    mount_robots(&server, ALLOW_ALL).await;
    Mock::given(method("GET"))
        .respond_with(ResponseTemplate::new(200).set_body_string("<html>ok</html>"))
        .mount(&server)
        .await;

    // The robots document counts toward the same per-host budget.
    let gate = gate_with_budget(&server, 2);
    gate.fetch(&format!("{}/p-1", server.uri())).await.unwrap();
    let error = gate
        .fetch(&format!("{}/p-2", server.uri()))
        .await
        .err()
        .unwrap();
    assert_eq!(error.reason(), "circuit_or_budget_open");
    assert_eq!(server.received_requests().await.unwrap().len(), 2); // robots + p-1
}

#[tokio::test]
async fn gate_returns_location_without_following_redirects() {
    let server = MockServer::start().await;
    mount_robots(&server, ALLOW_ALL).await;
    Mock::given(method("GET"))
        .and(path("/p-1"))
        .respond_with(
            ResponseTemplate::new(302)
                .append_header("Location", &format!("{}/p-2", server.uri())),
        )
        .mount(&server)
        .await;

    let gate = gate(&server);
    let page = gate.fetch(&format!("{}/p-1", server.uri())).await.unwrap();
    assert_eq!(page.status, 302);
    let expected = format!("{}/p-2", server.uri());
    assert_eq!(page.location.as_deref(), Some(expected.as_str()));
}

#[tokio::test]
async fn robots_transport_error_blocks_the_host() {
    // Python: a RequestException while fetching robots.txt blocks the host and
    // records `transport_error`; the next fetch hits the circuit. Bind and
    // release a port to guarantee a connection-refused target.
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    drop(listener);

    let gate = RobotsGate::custom(
        http(),
        Some("127.0.0.1".to_string()),
        Some(format!("http://127.0.0.1:{port}/robots.txt")),
        150,
        5 * 1024 * 1024,
    );
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
