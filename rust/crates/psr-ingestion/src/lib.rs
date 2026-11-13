//! Faithful port of `offer_ingestion_client.OfferIngestionClient`.
//!
//! Every contract check from the Python client is preserved: retry/backoff
//! policy, the `committed` + `scores_recalculated` idempotent replay rule,
//! atomic `require_all_accepted` batches, variant-rejection index re-basing
//! and the catalog paging validations.

use psr_core::TargetRow;
use serde_json::{json, Map, Value};
use std::time::Duration;

pub const RETRYABLE_STATUS_CODES: &[u16] =
    &[429, 500, 502, 503, 504, 520, 521, 522, 523, 524];
pub const MAX_INGESTION_BATCH_SIZE: usize = 500;
pub const CATALOG_PAGE_SIZE: usize = 500;
pub const COMPLETE_TARGET_PAGE_SIZE: usize = 12;
pub const MAX_COMPLETE_TARGET_PAGES: usize = 5;
pub const TARGET_INVALID_REASONS: &[&str] = &[
    "missing_or_oversize_url",
    "malformed_url",
    "non_https_url",
    "unsupported_host",
    "credentials_or_port",
    "retailer_query_restricted",
    "sensitive_query",
    "missing_product_path",
    "seller_identity",
];

#[derive(Debug, Clone, thiserror::Error)]
pub enum IngestionError {
    #[error("HTTP {status} from {path}: {message}")]
    Http {
        status: u16,
        path: String,
        message: String,
    },
    #[error("HTTP {status} returned invalid JSON")]
    InvalidJson { status: u16 },
    #[error("HTTPS request to {path} failed after {attempts} attempts: {cause}")]
    Failed {
        path: String,
        attempts: u32,
        cause: String,
    },
    #[error("{0}")]
    Contract(String),
}

impl IngestionError {
    pub fn contract(message: impl Into<String>) -> Self {
        IngestionError::Contract(message.into())
    }
}

fn is_retryable(status: u16) -> bool {
    RETRYABLE_STATUS_CODES.contains(&status)
}

/// Python `int(...)` over a JSON value (bools coerce, integral floats pass,
/// decimal strings parse).
pub fn json_int(value: &Value) -> Option<i64> {
    match value {
        Value::Bool(true) => Some(1),
        Value::Bool(false) => Some(0),
        Value::Number(number) => {
            if let Some(int) = number.as_i64() {
                Some(int)
            } else if let Some(uint) = number.as_u64() {
                i64::try_from(uint).ok()
            } else if let Some(float) = number.as_f64() {
                if float.fract() == 0.0 && float.abs() <= 9.0e15 {
                    Some(float as i64)
                } else {
                    None
                }
            } else {
                None
            }
        }
        Value::String(text) => {
            let trimmed = text.trim();
            if trimmed.is_empty() {
                return None;
            }
            let (sign, digits) = if let Some(rest) = trimmed.strip_prefix('-') {
                (-1i64, rest)
            } else if let Some(rest) = trimmed.strip_prefix('+') {
                (1i64, rest)
            } else {
                (1i64, trimmed)
            };
            if digits.is_empty() || !digits.chars().all(|c| c.is_ascii_digit()) {
                return None;
            }
            digits.parse::<i64>().ok().map(|value| sign * value)
        }
        _ => None,
    }
}

fn is_true(value: Option<&Value>) -> bool {
    matches!(value, Some(Value::Bool(true)))
}

fn is_false(value: Option<&Value>) -> bool {
    matches!(value, Some(Value::Bool(false)))
}

fn body_dict(body: &Value) -> Option<&Map<String, Value>> {
    body.as_object()
}

/// Projected catalog product (Python dict from `get_published_products`).
#[derive(Debug, Clone, PartialEq)]
pub struct CatalogProduct {
    pub id: i64,
    pub name: String,
    pub slug: String,
    pub attributes: Value,
    pub offer_targets: Vec<TargetRow>,
    pub offer_targets_truncated: Option<bool>,
    pub technical_source_urls: Vec<String>,
}

/// Aggregated result of `ingest_offers`.
#[derive(Debug, Clone, PartialEq, Default, serde::Serialize)]
pub struct IngestionBatchResult {
    pub accepted: i64,
    pub stale_ignored: i64,
    pub variant_rejected: i64,
    pub variant_rejections: Vec<Value>,
    pub changed_paths: Vec<String>,
    pub committed: bool,
    pub offer_identity_contract: Option<String>,
    pub server_scores_recalculated: bool,
}

impl IngestionBatchResult {
    fn empty() -> Self {
        Self {
            committed: true,
            ..Default::default()
        }
    }
}

pub struct IngestionClient {
    base_url: String,
    secret: String,
    http: reqwest::Client,
    max_attempts: u32,
}

impl IngestionClient {
    pub fn new(base_url: &str, secret: &str) -> Result<Self, IngestionError> {
        Self::build(base_url, secret, false)
    }

    /// Test-only constructor: wiremock mock servers speak plain HTTP, while
    /// the production contract requires an `https://` base URL. Never use
    /// this outside tests.
    #[doc(hidden)]
    pub fn new_insecure_for_tests(base_url: &str, secret: &str) -> Result<Self, IngestionError> {
        Self::build(base_url, secret, true)
    }

    fn build(base_url: &str, secret: &str, allow_http: bool) -> Result<Self, IngestionError> {
        let normalized = base_url.trim().trim_end_matches('/').to_string();
        let parsed = url::parse(&normalized)
            .map_err(|_| IngestionError::contract("TEKNOSKOR_INGESTION_URL must be an https:// base URL"))?;
        if (parsed.scheme != "https" || parsed.netloc.is_empty())
            && !(allow_http && parsed.scheme == "http" && !parsed.netloc.is_empty())
        {
            return Err(IngestionError::contract(
                "TEKNOSKOR_INGESTION_URL must be an https:// base URL",
            ));
        }
        let normalized_secret = secret.trim().to_string();
        if normalized_secret.chars().count() < 32 {
            return Err(IngestionError::contract(
                "SCRAPER_INGESTION_SECRET must contain at least 32 characters",
            ));
        }
        let http = reqwest::Client::builder()
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(120))
            .build()
            .map_err(|error| IngestionError::Contract(format!("HTTP client init failed: {error}")))?;
        Ok(Self {
            base_url: normalized,
            secret: normalized_secret,
            http,
            max_attempts: 4,
        })
    }

    pub fn from_env() -> Result<Self, IngestionError> {
        let base_url = std::env::var("TEKNOSKOR_INGESTION_URL").unwrap_or_default();
        let secret = std::env::var("SCRAPER_INGESTION_SECRET").unwrap_or_default();
        Self::new(&base_url, &secret)
    }

    fn url(&self, path: &str) -> String {
        let trimmed = path.trim_start_matches('/');
        format!("{}/{trimmed}", self.base_url)
    }

    fn request_builder(
        &self,
        method: reqwest::Method,
        url: &str,
        params: &[(String, String)],
        bearer: bool,
        content_type_json: bool,
        body: Option<&Value>,
    ) -> Result<reqwest::RequestBuilder, IngestionError> {
        let mut builder = self.http.request(method, url).query(params);
        if bearer {
            builder = builder.header("Authorization", format!("Bearer {}", self.secret));
        }
        if bearer && !content_type_json {
            builder = builder.header("Accept", "application/json");
        }
        if content_type_json {
            builder = builder.header("Content-Type", "application/json");
        }
        if let Some(value) = body {
            builder = builder.json(value);
        }
        Ok(builder)
    }

    /// `_request_json` — retries transport failures, invalid JSON on retryable
    /// statuses and HTTP retryable statuses with the 1/2/4... second backoff;
    /// replays committed POST batches while score recalculation is pending.
    pub async fn request_json(
        &self,
        method: reqwest::Method,
        path: &str,
        params: &[(String, String)],
        bearer: bool,
        body: Option<Value>,
    ) -> Result<Value, IngestionError> {
        let url = self.url(path);
        let mut last_error: Option<IngestionError> = None;
        let mut scoring_retry_pending = false;

        for attempt in 1..=self.max_attempts {
            let builder = self.request_builder(
                method.clone(),
                &url,
                params,
                bearer,
                body.is_some(),
                body.as_ref(),
            )?;
            let response = match builder.send().await {
                Ok(response) => response,
                Err(error) => {
                    last_error = Some(IngestionError::Contract(error.to_string()));
                    if attempt == self.max_attempts {
                        break;
                    }
                    let delay = Duration::from_secs(1u64 << (attempt - 1));
                    tracing::warn!(
                        "  ⚠️ HTTPS request failed ({attempt}/{max}): {error}; retrying in {}s",
                        delay.as_secs(),
                        max = self.max_attempts
                    );
                    tokio::time::sleep(delay).await;
                    continue;
                }
            };
            let status = response.status().as_u16();

            let parsed = match response.json::<Value>().await {
                Ok(parsed) => parsed,
                Err(_) => {
                    let error = IngestionError::InvalidJson { status };
                    last_error = Some(error.clone());
                    if !is_retryable(status) || attempt == self.max_attempts {
                        return Err(error);
                    }
                    let delay = Duration::from_secs(1u64 << (attempt - 1));
                    tracing::warn!(
                        "  ⚠️ HTTPS endpoint returned invalid JSON with HTTP {status} ({attempt}/{max}); retrying in {}s",
                        delay.as_secs(),
                        max = self.max_attempts
                    );
                    tokio::time::sleep(delay).await;
                    continue;
                }
            };

            if method == reqwest::Method::POST
                && body_dict(&parsed).is_some()
                && is_true(parsed.get("committed"))
            {
                let scores = parsed.get("scores_recalculated");
                let scores_true = is_true(scores);
                let scores_false = is_false(scores);
                if scores_false || (scoring_retry_pending && !scores_true) {
                    scoring_retry_pending = true;
                    if is_retryable(status) && attempt < self.max_attempts {
                        let delay = Duration::from_secs(1u64 << (attempt - 1));
                        tracing::warn!(
                            "  ⚠️ Offer batch committed, but score recalculation failed ({attempt}/{max}); retrying the idempotent batch in {}s",
                            delay.as_secs(),
                            max = self.max_attempts
                        );
                        tokio::time::sleep(delay).await;
                        continue;
                    }
                    return Err(IngestionError::Contract(format!(
                        "Offer batch was committed, but score recalculation failed after {attempt} attempt(s)"
                    )));
                }
                return Ok(parsed);
            }

            if (200..300).contains(&status) {
                return Ok(parsed);
            }

            let message = parsed
                .get("error")
                .and_then(Value::as_str)
                .unwrap_or("request failed")
                .to_string();
            let error = IngestionError::Http {
                status,
                path: path.to_string(),
                message,
            };
            last_error = Some(error.clone());
            if !is_retryable(status) || attempt == self.max_attempts {
                return Err(error);
            }
            let delay = Duration::from_secs(1u64 << (attempt - 1));
            tracing::warn!(
                "  ⚠️ HTTPS endpoint returned HTTP {status} ({attempt}/{max}); retrying in {}s",
                delay.as_secs(),
                max = self.max_attempts
            );
            tokio::time::sleep(delay).await;
        }

        Err(IngestionError::Failed {
            path: path.to_string(),
            attempts: self.max_attempts,
            cause: last_error
                .map(|error| error.to_string())
                .unwrap_or_else(|| "unknown".to_string()),
        })
    }

    /// `get_published_products`.
    pub async fn get_published_products(&self) -> Result<Vec<CatalogProduct>, IngestionError> {
        let mut products = Vec::new();
        let mut seen_ids: std::collections::HashSet<i64> = std::collections::HashSet::new();
        let mut page: usize = 1;
        loop {
            let params = vec![
                ("page".to_string(), page.to_string()),
                ("limit".to_string(), CATALOG_PAGE_SIZE.to_string()),
                ("include_offer_targets".to_string(), "1".to_string()),
            ];
            let body = self
                .request_json(reqwest::Method::GET, "/api/ingestion/catalog", &params, true, None)
                .await?;
            let body = body
                .as_object()
                .filter(|map| map.get("products").map(Value::is_array).unwrap_or(false))
                .ok_or_else(|| {
                    IngestionError::contract("Catalog endpoint returned an unexpected response")
                })?;
            for raw_product in body["products"].as_array().expect("array") {
                let product_id = json_int(raw_product.get("id").unwrap_or(&Value::Null))
                    .ok_or_else(|| {
                        IngestionError::contract("Catalog product is missing a valid id")
                    })?;
                let name = raw_product
                    .get("name")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .trim()
                    .to_string();
                let slug = raw_product
                    .get("slug")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .trim()
                    .to_string();
                if product_id <= 0 || name.is_empty() || slug.is_empty() {
                    return Err(IngestionError::contract(format!(
                        "Catalog product {product_id} is missing name or slug"
                    )));
                }
                if !seen_ids.insert(product_id) {
                    return Err(IngestionError::contract(format!(
                        "Catalog endpoint returned duplicate product id {product_id}"
                    )));
                }

                let attributes = match raw_product.get("attributes") {
                    Some(Value::Object(_)) => raw_product["attributes"].clone(),
                    _ => json!({}),
                };

                let mut truncated: Option<bool> = raw_product
                    .as_object()
                    .and_then(|map| map.get("offer_targets_truncated"))
                    .map(|value| {
                        value.as_bool().ok_or_else(|| {
                            IngestionError::contract(format!(
                                "Catalog product {product_id} has invalid target completeness"
                            ))
                        })
                    })
                    .transpose()?;
                let targets_list: Vec<Value> = match raw_product.get("offer_targets") {
                    Some(Value::Array(items)) => items.clone(),
                    _ => {
                        if truncated.is_some() {
                            return Err(IngestionError::contract(format!(
                                "Catalog product {product_id} has invalid offer targets"
                            )));
                        }
                        Vec::new()
                    }
                };

                // Refresh hints, not reusable prices or SKU proof.
                let mut offer_targets: Vec<TargetRow> = Vec::new();
                for target in targets_list.iter().take(12) {
                    let Some(map) = target.as_object() else {
                        continue;
                    };
                    let source_url = match map.get("source_url").and_then(Value::as_str) {
                        Some(url) if url.chars().count() <= 1000 => url.to_string(),
                        _ => continue,
                    };
                    offer_targets.push(TargetRow {
                        merchant_name: map
                            .get("merchant_name")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_string(),
                        source_url,
                        checked_at: map
                            .get("checked_at")
                            .and_then(Value::as_str)
                            .map(|s| s.to_string()),
                        seller_name: map
                            .get("seller_name")
                            .and_then(Value::as_str)
                            .map(|s| s.to_string()),
                    });
                }
                if offer_targets.len() != targets_list.len() {
                    truncated = Some(true);
                }

                // Existing provenance URLs route fresh reads, never stored facts.
                let mut technical_urls: Vec<String> = Vec::new();
                if let Some(hints) = raw_product.get("technical_source_urls").and_then(Value::as_array) {
                    if hints.len() <= 4 {
                        let hint_re = regex::Regex::new(
                            r"^https://www\.kimovil\.com/en/where-to-buy-[a-z0-9-]+$",
                        )
                        .unwrap();
                        for hint in hints {
                            if let Some(text) = hint.as_str() {
                                if text.chars().count() <= 2048
                                    && hint_re.is_match(text)
                                    && !technical_urls.contains(&text.to_string())
                                {
                                    technical_urls.push(text.to_string());
                                }
                            }
                        }
                    }
                }

                products.push(CatalogProduct {
                    id: product_id,
                    name,
                    slug,
                    attributes,
                    offer_targets,
                    offer_targets_truncated: truncated,
                    technical_source_urls: technical_urls,
                });
            }

            if !is_true(body.get("hasMore")) {
                break;
            }
            page += 1;
        }

        if products.is_empty() {
            return Err(IngestionError::contract(
                "Catalog endpoint returned no published products",
            ));
        }
        Ok(products)
    }

    /// `ingest_offers`.
    pub async fn ingest_offers(
        &self,
        offers: &[Value],
        require_all_accepted: bool,
        listing_contract: bool,
    ) -> Result<IngestionBatchResult, IngestionError> {
        if require_all_accepted && offers.len() > MAX_INGESTION_BATCH_SIZE {
            return Err(IngestionError::contract(
                "Atomic offer ingestion cannot span multiple HTTP batches",
            ));
        }
        if offers.is_empty() {
            return Ok(IngestionBatchResult::empty());
        }

        let mut totals = IngestionBatchResult::empty();
        let mut server_scoring_confirmed = true;
        let path = if listing_contract {
            "/api/ingestion/listing-offers"
        } else {
            "/api/ingestion/offers"
        };

        for start in (0..offers.len()).step_by(MAX_INGESTION_BATCH_SIZE) {
            let batch = &offers[start..(start + MAX_INGESTION_BATCH_SIZE).min(offers.len())];
            let mut payload = Map::new();
            payload.insert("offers".to_string(), Value::Array(batch.to_vec()));
            if require_all_accepted {
                payload.insert("require_all_accepted".to_string(), Value::Bool(true));
            }
            let body = self
                .request_json(
                    reqwest::Method::POST,
                    path,
                    &[],
                    true,
                    Some(Value::Object(payload)),
                )
                .await?;

            if !body_dict(&body).is_some() || !is_true(body.get("committed")) {
                return Err(IngestionError::contract(
                    "Offer ingestion response did not confirm a commit",
                ));
            }
            if listing_contract
                && body.get("offer_identity_contract").and_then(Value::as_str)
                    != Some("seller-source-url-v1")
            {
                return Err(IngestionError::contract(
                    "Listing ingestion did not confirm source URL identity",
                ));
            }
            if require_all_accepted && !is_true(body.get("require_all_accepted")) {
                return Err(IngestionError::contract(
                    "Offer ingestion response did not confirm atomic acceptance",
                ));
            }

            let accepted = json_int(body.get("accepted").unwrap_or(&json!(0)))
                .ok_or_else(|| {
                    IngestionError::contract("Offer ingestion response has invalid counters")
                })?;
            let stale_ignored = json_int(body.get("stale_ignored").unwrap_or(&json!(0)))
                .ok_or_else(|| {
                    IngestionError::contract("Offer ingestion response has invalid counters")
                })?;
            let variant_rejected = json_int(body.get("variant_rejected").unwrap_or(&json!(0)))
                .ok_or_else(|| {
                    IngestionError::contract("Offer ingestion response has invalid counters")
                })?;
            if accepted.min(stale_ignored).min(variant_rejected) < 0
                || accepted + stale_ignored + variant_rejected != batch.len() as i64
            {
                return Err(IngestionError::contract(
                    "Offer ingestion response count does not match submitted batch",
                ));
            }
            if require_all_accepted
                && (accepted != batch.len() as i64 || stale_ignored != 0 || variant_rejected != 0)
            {
                return Err(IngestionError::contract(
                    "Atomic offer ingestion did not accept every submitted offer",
                ));
            }

            let rejections: Vec<Value> = match body.get("variant_rejections") {
                Some(Value::Array(items)) => items.clone(),
                _ => Vec::new(),
            };
            if rejections.len() as i64 != variant_rejected {
                return Err(IngestionError::contract(
                    "Offer ingestion variant rejection details do not match count",
                ));
            }
            let mut seen_indices: std::collections::HashSet<i64> = std::collections::HashSet::new();
            for rejection in &rejections {
                let map = rejection
                    .as_object()
                    .ok_or_else(|| {
                        IngestionError::contract(
                            "Offer ingestion returned malformed variant rejection details",
                        )
                    })?;
                let index = map.get("index");
                let valid_index = index
                    .map(|value| {
                        // `isinstance(index, int) and not isinstance(index, bool)`
                        matches!(value, Value::Number(_))
                    })
                    .unwrap_or(false)
                    .then(|| json_int(index.unwrap_or(&Value::Null)))
                    .flatten();
                let Some(index) = valid_index else {
                    return Err(IngestionError::contract(
                        "Offer ingestion returned malformed variant rejection details",
                    ));
                };
                if index < 0 || index >= batch.len() as i64 || !seen_indices.insert(index) {
                    return Err(IngestionError::contract(
                        "Offer ingestion returned malformed variant rejection details",
                    ));
                }
                let product_slug = map
                    .get("product_slug")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .trim();
                let issues = map.get("issues").and_then(Value::as_array);
                let issues_valid = issues
                    .map(|items| {
                        !items.is_empty()
                            && items.iter().all(|issue| {
                                issue.as_str().map(|s| !s.trim().is_empty()).unwrap_or(false)
                            })
                    })
                    .unwrap_or(false);
                if product_slug.is_empty() || !issues_valid {
                    return Err(IngestionError::contract(
                        "Offer ingestion returned malformed variant rejection details",
                    ));
                }
                // Endpoint indices are local to each HTTP batch. Report indices
                // relative to the complete offer list supplied to this call.
                let mut rebased = rejection.clone();
                rebased["index"] = json!(start as i64 + index);
                totals.variant_rejections.push(rebased);
            }

            totals.accepted += accepted;
            totals.stale_ignored += stale_ignored;
            totals.variant_rejected += variant_rejected;
            server_scoring_confirmed &= is_true(body.get("scores_recalculated"));
            if let Some(changed) = body.get("changed_paths").and_then(Value::as_array) {
                for path in changed {
                    if let Some(text) = path.as_str() {
                        if text.starts_with('/') {
                            totals.changed_paths.push(text.to_string());
                        }
                    }
                }
            }
        }

        // Deduplicate preserving order.
        let mut deduped: Vec<String> = Vec::new();
        for path in &totals.changed_paths {
            if !deduped.contains(path) {
                deduped.push(path.clone());
            }
        }
        totals.changed_paths = deduped;
        if listing_contract {
            totals.offer_identity_contract = Some("seller-source-url-v1".to_string());
        }
        if server_scoring_confirmed {
            // Only an explicit success for EVERY committed HTTP batch can
            // replace a redundant reconciliation. Null/missing is unknown.
            totals.server_scores_recalculated = true;
        }
        Ok(totals)
    }

    /// `ingest_listing_offers` — full-evidence, URL-keyed endpoint only.
    pub async fn ingest_listing_offers(
        &self,
        offers: &[Value],
    ) -> Result<IngestionBatchResult, IngestionError> {
        if offers.is_empty() {
            return Err(IngestionError::contract(
                "Listing ingestion requires a nonempty atomic batch",
            ));
        }
        self.ingest_offers(offers, true, true).await
    }

    /// `reconcile_scores`.
    pub async fn reconcile_scores(&self) -> Result<Value, IngestionError> {
        let body = self
            .request_json(
                reqwest::Method::POST,
                "/api/ingestion/scoring/reconcile",
                &[],
                true,
                Some(json!({})),
            )
            .await?;
        if !is_true(body.get("scores_recalculated")) {
            return Err(IngestionError::contract(
                "Score reconciliation did not confirm success",
            ));
        }
        let total = json_int(body.get("total").unwrap_or(&Value::Null));
        let scored = json_int(body.get("scored").unwrap_or(&Value::Null));
        let skipped = json_int(body.get("skipped").unwrap_or(&Value::Null));
        let (Some(total), Some(scored), Some(skipped)) = (total, scored, skipped) else {
            return Err(IngestionError::contract(
                "Score reconciliation returned invalid counters",
            ));
        };
        if total < 0 || scored < 0 || skipped < 0 || total != scored + skipped {
            return Err(IngestionError::contract(
                "Score reconciliation returned invalid counters",
            ));
        }
        Ok(body)
    }
}

mod url {
    /// Minimal URL validation for the ingestion base URL (scheme + netloc).
    pub fn parse(input: &str) -> Result<psr_core::ParsedUrl, psr_core::UrlError> {
        psr_core::parse_url(input)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use wiremock::matchers::{body_partial_json, header, method, path, query_param};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    fn client_for(server: &MockServer) -> IngestionClient {
        IngestionClient::new_insecure_for_tests(&server.uri(), &"s".repeat(40)).unwrap()
    }

    fn offer(index: usize) -> Value {
        json!({
            "product_slug": format!("product-{index}"),
            "merchant": "n11",
            "price": 1000.5,
            "currency": "TRY",
            "availability": "in_stock",
            "source_title": "Apple iPhone 15 128 GB",
            "url": "https://www.n11.com/apple-iphone-15-p-A1",
            "observed_at": "2026-11-13T09:00:00.000000Z",
        })
    }

    fn counters(committed: i64, accepted: usize) -> Value {
        json!({
            "committed": committed == 1,
            "accepted": accepted,
            "stale_ignored": 0,
            "variant_rejected": 0,
            "variant_rejections": [],
            "changed_paths": [],
            "scores_recalculated": true,
        })
    }

    #[tokio::test]
    async fn rejects_http_base_url() {
        assert!(IngestionClient::new("http://example.com", &"s".repeat(40)).is_err());
        assert!(IngestionClient::new("", &"s".repeat(40)).is_err());
        assert!(IngestionClient::new("https://example.com", "short").is_err());
    }

    #[tokio::test]
    async fn empty_batch_makes_no_request() {
        let server = MockServer::start().await;
        let client = client_for(&server);
        let result = client.ingest_offers(&[], false, false).await.unwrap();
        assert!(result.committed);
        assert_eq!(result.accepted, 0);
        assert_eq!(server.received_requests().await.unwrap().len(), 0);
    }

    #[tokio::test]
    async fn happy_path_single_batch() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/ingestion/offers"))
            .and(header("Authorization", "Bearer ".to_string() + &"s".repeat(40)))
            .and(body_partial_json(json!({"offers": [offer(0)]})))
            .respond_with(ResponseTemplate::new(200).set_body_json(counters(1, 1)))
            .mount(&server)
            .await;
        let client = client_for(&server);
        let result = client
            .ingest_offers(&[offer(0)], false, false)
            .await
            .unwrap();
        assert_eq!(result.accepted, 1);
        assert!(result.server_scores_recalculated);
        assert!(result.changed_paths.is_empty());
    }

    #[tokio::test]
    async fn retries_server_error_then_succeeds() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(503).set_body_json(json!({"error": "overloaded"})))
            .up_to_n_times(1)
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(200).set_body_json(counters(1, 1)))
            .mount(&server)
            .await;
        let client = client_for(&server);
        let result = client
            .ingest_offers(&[offer(0)], false, false)
            .await
            .unwrap();
        assert_eq!(result.accepted, 1);
        assert_eq!(server.received_requests().await.unwrap().len(), 2);
    }

    #[tokio::test]
    async fn non_retryable_status_fails_fast() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(400).set_body_json(json!({"error": "bad batch"})))
            .expect(1)
            .mount(&server)
            .await;
        let client = client_for(&server);
        let error = client
            .ingest_offers(&[offer(0)], false, false)
            .await
            .unwrap_err();
        match error {
            IngestionError::Http { status, message, .. } => {
                assert_eq!(status, 400);
                assert_eq!(message, "bad batch");
            }
            other => panic!("unexpected error: {other}"),
        }
    }

    #[tokio::test]
    async fn invalid_json_on_retryable_status_is_retried() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(502).set_body_string("<html>oops</html>"))
            .up_to_n_times(1)
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(200).set_body_json(counters(1, 1)))
            .mount(&server)
            .await;
        let client = client_for(&server);
        let result = client
            .ingest_offers(&[offer(0)], false, false)
            .await
            .unwrap();
        assert_eq!(result.accepted, 1);
        assert_eq!(server.received_requests().await.unwrap().len(), 2);
    }

    #[tokio::test]
    async fn invalid_json_on_non_retryable_status_fails() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(400).set_body_string("nope"))
            .expect(1)
            .mount(&server)
            .await;
        let client = client_for(&server);
        let error = client
            .ingest_offers(&[offer(0)], false, false)
            .await
            .unwrap_err();
        assert!(matches!(error, IngestionError::InvalidJson { status: 400 }));
    }

    #[tokio::test]
    async fn committed_batch_replays_idempotently_until_scores_true() {
        let server = MockServer::start().await;
        // A retryable status after commit gives the idempotent replay another
        // chance; a plain 200 with scores_recalculated=false would fail fast
        // (see the next test).
        let first = json!({
            "committed": true,
            "accepted": 1,
            "stale_ignored": 0,
            "variant_rejected": 0,
            "variant_rejections": [],
            "changed_paths": [],
            "scores_recalculated": false,
        });
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(502).set_body_json(first.clone()))
            .up_to_n_times(1)
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(200).set_body_json(counters(1, 1)))
            .mount(&server)
            .await;
        let client = client_for(&server);
        let result = client
            .ingest_offers(&[offer(0)], false, false)
            .await
            .unwrap();
        assert_eq!(result.accepted, 1);
        let requests = server.received_requests().await.unwrap();
        assert_eq!(requests.len(), 2);
        // The replay must carry the identical idempotent payload.
        assert_eq!(requests[0].body, requests[1].body);
    }

    #[tokio::test]
    async fn committed_200_with_scores_false_fails_fast() {
        let server = MockServer::start().await;
        let body = json!({
            "committed": true,
            "accepted": 1,
            "stale_ignored": 0,
            "variant_rejected": 0,
            "variant_rejections": [],
            "changed_paths": [],
            "scores_recalculated": false,
        });
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(200).set_body_json(body))
            .mount(&server)
            .await;
        let client = client_for(&server);
        let error = client
            .ingest_offers(&[offer(0)], false, false)
            .await
            .unwrap_err();
        assert!(error
            .to_string()
            .contains("score recalculation failed after 1 attempt(s)"));
        assert_eq!(server.received_requests().await.unwrap().len(), 1);
    }

    #[tokio::test]
    async fn counter_mismatch_fails() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(200).set_body_json(counters(1, 0)))
            .mount(&server)
            .await;
        let client = client_for(&server);
        let error = client
            .ingest_offers(&[offer(0)], false, false)
            .await
            .unwrap_err();
        assert!(error
            .to_string()
            .contains("count does not match submitted batch"));
    }

    #[tokio::test]
    async fn require_all_accepted_enforced() {
        let server = MockServer::start().await;
        // Server echoes require_all_accepted but accepted 0 of 2.
        let body = json!({
            "committed": true,
            "require_all_accepted": true,
            "accepted": 0,
            "stale_ignored": 0,
            "variant_rejected": 2,
            "variant_rejections": [
                {"index": 0, "product_slug": "product-0", "issues": ["identity"]},
                {"index": 1, "product_slug": "product-1", "issues": ["price"]},
            ],
            "changed_paths": [],
            "scores_recalculated": true,
        });
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(200).set_body_json(body))
            .mount(&server)
            .await;
        let client = client_for(&server);
        let error = client
            .ingest_offers(&[offer(0), offer(1)], true, false)
            .await
            .unwrap_err();
        assert!(error
            .to_string()
            .contains("did not accept every submitted offer"));
    }

    #[tokio::test]
    async fn missing_atomic_echo_fails() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(200).set_body_json(counters(1, 1)))
            .mount(&server)
            .await;
        let client = client_for(&server);
        let error = client
            .ingest_offers(&[offer(0)], true, false)
            .await
            .unwrap_err();
        assert!(error.to_string().contains("atomic acceptance"));
    }

    #[tokio::test]
    async fn variant_rejection_validation() {
        let server = MockServer::start().await;
        let body = json!({
            "committed": true,
            "accepted": 0,
            "stale_ignored": 0,
            "variant_rejected": 1,
            "variant_rejections": [
                {"index": 7, "product_slug": "product-0", "issues": []},
            ],
            "changed_paths": [],
            "scores_recalculated": true,
        });
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(200).set_body_json(body))
            .mount(&server)
            .await;
        let client = client_for(&server);
        let error = client
            .ingest_offers(&[offer(0)], false, false)
            .await
            .unwrap_err();
        assert!(error.to_string().contains("malformed variant rejection"));
    }

    #[tokio::test]
    async fn multi_batch_splits_and_rebases_indices() {
        let server = MockServer::start().await;
        let offers: Vec<Value> = (0..502).map(offer).collect();
        // First batch of 500 accepted; second batch of 2 with one rejection
        // whose local index 1 must become global 501.
        Mock::given(method("POST"))
            .and(body_partial_json(json!({"offers": [offer(0)]})))
            .respond_with(ResponseTemplate::new(200).set_body_json(counters(1, 500)))
            .up_to_n_times(1)
            .mount(&server)
            .await;
        let second = json!({
            "committed": true,
            "accepted": 1,
            "stale_ignored": 0,
            "variant_rejected": 1,
            "variant_rejections": [
                {"index": 1, "product_slug": "product-501", "issues": ["identity"]},
            ],
            "changed_paths": ["/products/product-501"],
            "scores_recalculated": true,
        });
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(200).set_body_json(second))
            .mount(&server)
            .await;
        let client = client_for(&server);
        let result = client.ingest_offers(&offers, false, false).await.unwrap();
        assert_eq!(result.accepted, 501);
        assert_eq!(result.variant_rejected, 1);
        assert_eq!(result.variant_rejections[0]["index"], 501);
        assert_eq!(result.changed_paths, vec!["/products/product-501"]);
        assert_eq!(server.received_requests().await.unwrap().len(), 2);
    }

    #[tokio::test]
    async fn changed_paths_deduped_in_order() {
        let server = MockServer::start().await;
        let body = json!({
            "committed": true,
            "accepted": 1,
            "stale_ignored": 0,
            "variant_rejected": 0,
            "variant_rejections": [],
            "changed_paths": ["/a", "/b", "/a", "not-a-path", "/b"],
            "scores_recalculated": true,
        });
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(200).set_body_json(body))
            .mount(&server)
            .await;
        let client = client_for(&server);
        let result = client
            .ingest_offers(&[offer(0)], false, false)
            .await
            .unwrap();
        assert_eq!(result.changed_paths, vec!["/a", "/b"]);
    }

    #[tokio::test]
    async fn listing_contract_checks() {
        let server = MockServer::start().await;
        let client = client_for(&server);

        // Missing identity contract echo must fail (checked before the atomic
        // echo, exactly like the Python client).
        Mock::given(method("POST"))
            .and(path("/api/ingestion/listing-offers"))
            .respond_with(ResponseTemplate::new(200).set_body_json(counters(1, 1)))
            .up_to_n_times(1)
            .mount(&server)
            .await;

        // Correct echo succeeds and exposes the contract.
        let ok = json!({
            "committed": true,
            "offer_identity_contract": "seller-source-url-v1",
            "require_all_accepted": true,
            "accepted": 1,
            "stale_ignored": 0,
            "variant_rejected": 0,
            "variant_rejections": [],
            "changed_paths": [],
            "scores_recalculated": true,
        });
        Mock::given(method("POST"))
            .and(path("/api/ingestion/listing-offers"))
            .respond_with(ResponseTemplate::new(200).set_body_json(ok))
            .up_to_n_times(1)
            .mount(&server)
            .await;
        let error = client
            .ingest_listing_offers(&[offer(0)])
            .await
            .unwrap_err();
        assert!(error.to_string().contains("source URL identity"));

        // Empty listing batch must fail closed.
        assert!(client.ingest_listing_offers(&[]).await.is_err());

        let result = client.ingest_listing_offers(&[offer(0)]).await.unwrap();
        assert_eq!(
            result.offer_identity_contract.as_deref(),
            Some("seller-source-url-v1")
        );
        assert!(result.server_scores_recalculated);
    }

    #[tokio::test]
    async fn atomic_batch_over_500_rejected_locally() {
        let server = MockServer::start().await;
        let client = client_for(&server);
        let offers: Vec<Value> = (0..501).map(offer).collect();
        let error = client
            .ingest_offers(&offers, true, false)
            .await
            .unwrap_err();
        assert!(error
            .to_string()
            .contains("cannot span multiple HTTP batches"));
        assert_eq!(server.received_requests().await.unwrap().len(), 0);
    }

    #[tokio::test]
    async fn catalog_pages_products() {
        let server = MockServer::start().await;
        let page1 = json!({
            "hasMore": true,
            "products": [
                {
                    "id": 1,
                    "name": "Apple iPhone 15 128 GB",
                    "slug": "apple-iphone-15-128-gb",
                    "attributes": {"ram_gb": 6, "storage_gb": 128},
                    "offer_targets": [
                        {"merchant_name": "n11", "source_url": "https://www.n11.com/a-p-A1", "checked_at": "2026-11-12T09:00:00Z"},
                        {"merchant_name": "Trendyol", "source_url": "https://www.trendyol.com/b-p-T1"},
                        // Oversized URL is dropped by the projection.
                        {"merchant_name": "Teknosa", "source_url": format!("https://www.teknosa.com/{}-p-12345", "x".repeat(1100))},
                    ],
                    "offer_targets_truncated": false,
                },
            ],
        });
        let page2 = json!({
            "hasMore": false,
            "products": [
                {
                    "id": 2,
                    "name": "Samsung Galaxy S25 Ultra 512 GB",
                    "slug": "samsung-galaxy-s25-ultra-512-gb",
                    "attributes": {},
                    "offer_targets": [],
                    "offer_targets_truncated": false,
                    "technical_source_urls": [
                        "https://www.kimovil.com/en/where-to-buy-samsung-galaxy-s25-ultra",
                        "https://www.kimovil.com/en/where-to-buy-samsung-galaxy-s25-ultra", // duplicate dropped
                        "https://evil.com/where-to-buy-x", // non-kimovil dropped
                    ],
                },
            ],
        });
        Mock::given(method("GET"))
            .and(path("/api/ingestion/catalog"))
            .and(query_param("page", "1"))
            .respond_with(ResponseTemplate::new(200).set_body_json(page1))
            .mount(&server)
            .await;
        Mock::given(method("GET"))
            .and(path("/api/ingestion/catalog"))
            .and(query_param("page", "2"))
            .respond_with(ResponseTemplate::new(200).set_body_json(page2))
            .mount(&server)
            .await;
        let client = client_for(&server);
        let products = client.get_published_products().await.unwrap();
        assert_eq!(products.len(), 2);
        assert_eq!(products[0].offer_targets.len(), 2);
        assert_eq!(products[0].offer_targets_truncated, Some(true));
        assert_eq!(
            products[1].technical_source_urls,
            vec!["https://www.kimovil.com/en/where-to-buy-samsung-galaxy-s25-ultra"]
        );
        assert_eq!(
            products[0].attributes["storage_gb"],
            json!(128)
        );
    }

    #[tokio::test]
    async fn catalog_contract_failures() {
        let server = MockServer::start().await;
        let client = client_for(&server);

        // Duplicate id.
        let dup = json!({
            "hasMore": false,
            "products": [
                {"id": 1, "name": "A", "slug": "a", "offer_targets": [], "offer_targets_truncated": false},
                {"id": 1, "name": "B", "slug": "b", "offer_targets": [], "offer_targets_truncated": false},
            ],
        });
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(200).set_body_json(dup))
            .up_to_n_times(1)
            .mount(&server)
            .await;
        let error = client.get_published_products().await.unwrap_err();
        assert!(error.to_string().contains("duplicate product id"));

        // Missing slug.
        let missing = json!({
            "hasMore": false,
            "products": [
                {"id": 1, "name": "A", "offer_targets": [], "offer_targets_truncated": false},
            ],
        });
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(200).set_body_json(missing))
            .up_to_n_times(1)
            .mount(&server)
            .await;
        let error = client.get_published_products().await.unwrap_err();
        assert!(error.to_string().contains("missing name or slug"));

        // Non-boolean truncated flag.
        let bad_flag = json!({
            "hasMore": false,
            "products": [
                {"id": 1, "name": "A", "slug": "a", "offer_targets": [], "offer_targets_truncated": 1},
            ],
        });
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(200).set_body_json(bad_flag))
            .up_to_n_times(1)
            .mount(&server)
            .await;
        let error = client.get_published_products().await.unwrap_err();
        assert!(error.to_string().contains("invalid target completeness"));

        // Empty catalog.
        let empty = json!({"hasMore": false, "products": []});
        Mock::given(method("GET"))
            .respond_with(ResponseTemplate::new(200).set_body_json(empty))
            .up_to_n_times(1)
            .mount(&server)
            .await;
        let error = client.get_published_products().await.unwrap_err();
        assert!(error.to_string().contains("no published products"));
    }

    #[tokio::test]
    async fn reconcile_scores_contract() {
        let server = MockServer::start().await;
        let client = client_for(&server);

        // Broken counters fail closed (mounted first; LIFO keeps precedence).
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "scores_recalculated": true, "total": 10, "scored": 4, "skipped": 2
            })))
            .up_to_n_times(1)
            .mount(&server)
            .await;
        let error = client.reconcile_scores().await.unwrap_err();
        assert!(error.to_string().contains("invalid counters"));

        // Missing success flag fails closed.
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "total": 10, "scored": 8, "skipped": 2
            })))
            .up_to_n_times(1)
            .mount(&server)
            .await;
        let error = client.reconcile_scores().await.unwrap_err();
        assert!(error.to_string().contains("did not confirm success"));

        Mock::given(method("POST"))
            .and(path("/api/ingestion/scoring/reconcile"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "scores_recalculated": true, "total": 10, "scored": 8, "skipped": 2
            })))
            .up_to_n_times(1)
            .mount(&server)
            .await;
        let body = client.reconcile_scores().await.unwrap();
        assert_eq!(body["scored"], json!(8));
    }
}
