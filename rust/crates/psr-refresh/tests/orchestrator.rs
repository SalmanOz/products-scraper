//! Wiremock tests for the refresh orchestration: selection cap, sequential
//! bucket fetch, report rows and `target_budget_skipped` diagnostics.

use std::sync::Arc;
use std::time::Duration;

use psr_core::TargetRow;
use psr_fetch::FetchEngine;
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

fn engine() -> Arc<FetchEngine> {
    let http = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(5))
        .timeout(Duration::from_secs(20))
        .build()
        .unwrap();
    Arc::new(FetchEngine::permissive_for_tests(http))
}

fn product_page(url: &str) -> String {
    let schema = serde_json::json!({
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
    });
    format!(
        "<!doctype html><html><head><title>Shop</title></head><body><script type=\"application/ld+json\">{}</script></body></html>",
        serde_json::to_string(&schema).unwrap()
    )
}

#[tokio::test]
async fn refresh_report_observes_selected_and_marks_budget_skips() {
    let server = MockServer::start().await;
    // 13 valid product pages: the selection cap is 12.
    for index in 1..=13usize {
        let page_path = format!("/samsung-galaxy-s25-p-{index}");
        let url = format!("{}{}", server.uri(), page_path);
        Mock::given(method("GET"))
            .and(path(&page_path))
            .respond_with(
                ResponseTemplate::new(200)
                    .set_body_string(product_page(&url))
                    .append_header("Content-Type", "text/html"),
            )
            .mount(&server)
            .await;
    }
    let engine = engine();
    let targets: Vec<TargetRow> = (1..=13usize)
        .map(|index| TargetRow {
            merchant_name: "Mock Merchant".to_string(),
            source_url: format!("{}/samsung-galaxy-s25-p-{}", server.uri(), index),
            checked_at: None,
            seller_name: None,
        })
        .collect();
    let report = psr_refresh::refresh_report(
        &engine,
        &targets,
        "Samsung Galaxy S25 256GB",
        &Default::default(),
    )
    .await;
    assert_eq!(report.len(), 12, "selection cap is 12");
    let observed = report
        .iter()
        .filter(|row| row.offer.is_some() && !row.identity_verified)
        .count();
    assert_eq!(
        observed, 12,
        "every selected page observes an offer, none verified by the noop matcher"
    );
    // The 13th URL is marked in the engine diagnostics.
    let skipped_url = format!("{}/samsung-galaxy-s25-p-13", server.uri());
    let skipped = engine.diagnostics(&skipped_url).await.unwrap();
    assert_eq!(skipped.reason, "target_budget_skipped");
    assert_eq!(skipped.http_status, None);
    // 12 pages fetched, the skipped URL never requested.
    assert_eq!(server.received_requests().await.unwrap().len(), 12);
}

#[tokio::test]
async fn refresh_report_carries_attempt_reasons() {
    let server = MockServer::start().await;
    Mock::given(method("GET"))
        .and(path("/denied-p-1"))
        .respond_with(ResponseTemplate::new(403))
        .mount(&server)
        .await;
    let engine = engine();
    let targets = vec![TargetRow {
        merchant_name: "Mock Merchant".to_string(),
        source_url: format!("{}/denied-p-1", server.uri()),
        checked_at: None,
        seller_name: None,
    }];
    let report = psr_refresh::refresh_report(
        &engine,
        &targets,
        "Samsung Galaxy S25 256GB",
        &Default::default(),
    )
    .await;
    assert_eq!(report.len(), 1);
    assert!(report[0].offer.is_none());
    assert_eq!(report[0].attempt.as_ref().unwrap().reason, "access_denied");
    assert_eq!(report[0].attempt.as_ref().unwrap().http_status, Some(403));
    assert!(!report[0].identity_verified);
}
