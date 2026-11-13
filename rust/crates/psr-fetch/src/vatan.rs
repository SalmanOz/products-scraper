//! Read-only fallback for statically reviewed Vatan phone PDPs.

use std::collections::HashSet;
use std::sync::LazyLock;
use std::time::{Duration, Instant};

use psr_core::url::vatan_product_url;
use serde::Deserialize;
use tokio::sync::Mutex;

use crate::MAX_BODY_BYTES;

const SOLVER_URL: &str = "http://localhost:8191/v1";
const REQUEST_DELAY: Duration = Duration::from_millis(500);
const MAX_SOLVER_BYTES: usize = 8 * 1024 * 1024;
const MAX_BROWSER_REQUESTS: usize = 6;
const S25_FE_URL: &str =
    "https://www.vatanbilgisayar.com/samsung-galaxy-s25-fe-8-256-gb-akilli-telefon-buz-mavisi.html";

#[derive(Deserialize)]
struct Batch {
    products: Vec<Profile>,
}

#[derive(Deserialize)]
struct Profile {
    id: u32,
    slug: String,
    name: String,
    ram_gb: u32,
    storage_gb: u32,
    network_generation: String,
    targets: Vec<String>,
}

static REVIEWED_URLS: LazyLock<HashSet<String>> = LazyLock::new(|| {
    let batch: Batch = serde_json::from_str(include_str!(
        "../../../../offer_batches/vatan-exact-2026-11-02.json"
    ))
    .expect("reviewed Vatan batch");
    let mut urls: HashSet<_> = batch
        .products
        .into_iter()
        .flat_map(|profile| profile.targets)
        .filter(|url| vatan_product_url(url))
        .collect();

    let variant_batch: Batch = serde_json::from_str(include_str!(
        "../../../../offer_batches/s25-fe-256-exact-2026-11-02.json"
    ))
    .expect("reviewed S25 FE batch");
    if let [profile] = variant_batch.products.as_slice() {
        if profile.id == 298
            && profile.slug == "samsung-galaxy-s25-fe-256gb"
            && profile.name == "Samsung Galaxy S25 FE 256GB"
            && profile.ram_gb == 8
            && profile.storage_gb == 256
            && profile.network_generation == "5g"
            && profile.targets.iter().any(|url| url == S25_FE_URL)
        {
            urls.insert(S25_FE_URL.to_string());
        }
    }
    urls
});

#[derive(Default)]
struct State {
    last_request: Option<Instant>,
    browser_requests: usize,
    browser_failures: usize,
}

pub(crate) struct VatanReviewedSource {
    solver: reqwest::Client,
    solver_url: String,
    state: Mutex<State>,
}

impl VatanReviewedSource {
    pub(crate) fn new() -> Self {
        let solver = reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(60))
            .build()
            .expect("Vatan solver HTTP client");
        Self {
            solver,
            solver_url: SOLVER_URL.to_string(),
            state: Mutex::default(),
        }
    }

    pub(crate) fn is_reviewed(url: &str) -> bool {
        url.starts_with("https://www.vatanbilgisayar.com/") && REVIEWED_URLS.contains(url)
    }

    pub(crate) async fn prepare(&self, url: &str) -> Result<(), &'static str> {
        Self::authorize(url, &mut *self.state.lock().await).await
    }

    async fn authorize(url: &str, state: &mut State) -> Result<(), &'static str> {
        if state.browser_failures >= 2 {
            return Err("source_circuit_open");
        }
        if !Self::is_reviewed(url) {
            return Err("not_reviewed");
        }
        if let Some(last) = state.last_request {
            tokio::time::sleep(REQUEST_DELAY.saturating_sub(last.elapsed())).await;
        }
        state.last_request = Some(Instant::now());
        Ok(())
    }

    pub(crate) async fn browser_get(&self, url: &str) -> Result<String, String> {
        let mut state = self.state.lock().await;
        Self::authorize(url, &mut state).await?;
        if state.browser_requests >= MAX_BROWSER_REQUESTS {
            return Err("browser_budget_exhausted".into());
        }
        state.browser_requests += 1;

        let response = self
            .solver
            .post(&self.solver_url)
            .json(&serde_json::json!({"cmd": "request.get", "url": url, "maxTimeout": 45000}))
            .send()
            .await;
        let response = response.map_err(|_| browser_unavailable(&mut state))?;
        if response.status().as_u16() != 200 {
            return Err(format!("solver_http_{}", response.status().as_u16()));
        }
        let bytes = match bounded_bytes(response, MAX_SOLVER_BYTES).await {
            Ok(bytes) => bytes,
            Err(BodyError::TooLarge) => return Err("solver_response_too_large".into()),
            Err(BodyError::Transport) => return Err(browser_unavailable(&mut state)),
        };
        let payload: serde_json::Value =
            serde_json::from_slice(&bytes).map_err(|_| browser_unavailable(&mut state))?;
        if payload.get("status").and_then(serde_json::Value::as_str) != Some("ok") {
            return Err("solver_unavailable".into());
        }
        let solution = payload
            .get("solution")
            .and_then(serde_json::Value::as_object)
            .ok_or("invalid_solution")?;
        let status = solution.get("status").and_then(serde_json::Value::as_u64);
        if status != Some(200) {
            if matches!(status, Some(403 | 429 | 500 | 502 | 503 | 504)) {
                state.browser_failures += 1;
            }
            let status = status.map_or_else(|| "None".into(), |status| status.to_string());
            return Err(format!("site_http_{status}"));
        }
        if solution.get("url").and_then(serde_json::Value::as_str) != Some(url) {
            return Err("unexpected_final_url".into());
        }
        let html = solution
            .get("response")
            .and_then(serde_json::Value::as_str)
            .filter(|html| html.len() <= MAX_BODY_BYTES)
            .ok_or("invalid_or_oversized_html")?;
        state.browser_failures = 0;
        Ok(html.to_string())
    }
}

fn browser_unavailable(state: &mut State) -> String {
    state.browser_failures += 1;
    "browser_unavailable".into()
}

enum BodyError {
    TooLarge,
    Transport,
}

async fn bounded_bytes(
    mut response: reqwest::Response,
    limit: usize,
) -> Result<Vec<u8>, BodyError> {
    if response
        .content_length()
        .is_some_and(|size| size > limit as u64)
    {
        return Err(BodyError::TooLarge);
    }
    let mut bytes = Vec::new();
    while let Some(chunk) = response.chunk().await.map_err(|_| BodyError::Transport)? {
        if chunk.len() > limit - bytes.len() {
            return Err(BodyError::TooLarge);
        }
        bytes.extend_from_slice(&chunk);
    }
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use wiremock::matchers::{body_json, method, path};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    const XIAOMI: &str =
        "https://www.vatanbilgisayar.com/xiaomi-17t-12-256gb-akilli-telefon-koyu-mor.html";

    #[tokio::test]
    async fn browser_read_rejects_unreviewed_and_redirected_products() {
        let server = MockServer::start().await;
        let mut source = VatanReviewedSource::new();
        source.solver_url = format!("{}/v1", server.uri());
        assert_eq!(
            source
                .browser_get("https://www.vatanbilgisayar.com/unreviewed-akilli-telefon.html")
                .await,
            Err("not_reviewed".into())
        );
        Mock::given(method("POST"))
            .and(path("/v1"))
            .and(body_json(
                serde_json::json!({"cmd": "request.get", "url": XIAOMI, "maxTimeout": 45000}),
            ))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "status": "ok",
                "solution": {"status": 200, "url": format!("{XIAOMI}?other=1"), "response": "page"}
            })))
            .expect(1)
            .mount(&server)
            .await;
        assert_eq!(
            source.browser_get(XIAOMI).await,
            Err("unexpected_final_url".into())
        );
    }

    #[tokio::test]
    async fn browser_read_accepts_only_the_exact_successful_page() {
        let server = MockServer::start().await;
        let mut source = VatanReviewedSource::new();
        source.solver_url = format!("{}/v1", server.uri());
        Mock::given(method("POST"))
            .and(path("/v1"))
            .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
                "status": "ok",
                "solution": {"status": 200, "url": XIAOMI, "response": "<html>selected PDP</html>"}
            })))
            .expect(1)
            .mount(&server)
            .await;
        assert_eq!(
            source.browser_get(XIAOMI).await.unwrap(),
            "<html>selected PDP</html>"
        );
    }
}
