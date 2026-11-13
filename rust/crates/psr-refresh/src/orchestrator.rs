//! Port of `RetailerOfferRefresher.refresh_existing_offer_targets`
//! (retailer_offer_refresh.py lines 931-977): target selection, merchant
//! grouping, per-bucket sequential fetch with parallel buckets and the
//! `target_budget_skipped` diagnostics.
//!
//! Deferred (phase 2, fail-closed):
//! - `register_catalog_targets` on the dedicated sources (no-op here);
//! - `_offer_identity_matches` — the orchestrator reports observations with
//!   `identity_verified: false`; nothing unverified is auto-ingested.

use std::collections::HashMap;
use std::sync::Arc;

use psr_core::url::allowed_host_merchant;
use psr_core::{select_offer_targets_trust, OfferObservation, TargetRow};
use psr_fetch::{AttemptRecord, FetchEngine};
use tokio::sync::Semaphore;

use crate::identity::{ExpectedSpecs, IdentityMatcher, NoopMatcher};

/// Python `RetailerOfferRefresher.MAX_TARGETS_PER_REFRESH`.
pub const MAX_TARGETS_PER_REFRESH: usize = 12;

/// Python `ThreadPoolExecutor(max_workers=4)` over merchant buckets.
pub const MAX_PARALLEL_MERCHANTS: usize = 4;

/// One report row: Python `target_report` + the observation.
#[derive(Debug, Clone, serde::Serialize)]
pub struct ObservedTarget {
    pub source_url: String,
    pub merchant: String,
    pub attempt: Option<AttemptRecord>,
    pub offer: Option<OfferObservation>,
    /// False until the identity matcher is ported — the fail-closed marker.
    pub identity_verified: bool,
}

/// Port of `refresh_existing_offer_targets`. Returns the selected targets in
/// request order with their fetch outcome. Targets outside the selection
/// budget are marked `target_budget_skipped` in the engine diagnostics,
/// exactly like Python.
pub async fn refresh_targets(
    engine: &Arc<FetchEngine>,
    targets: &[TargetRow],
    matcher: &dyn IdentityMatcher,
    product_name: &str,
    specs: &ExpectedSpecs,
) -> Vec<ObservedTarget> {
    if targets.is_empty() {
        return Vec::new();
    }
    let trust = if engine.is_permissive() {
        psr_core::url::UrlTrust::Permissive
    } else {
        psr_core::url::UrlTrust::Manifest
    };
    let target_values: Vec<serde_json::Value> = targets
        .iter()
        .map(serde_json::to_value)
        .collect::<Result<_, _>>()
        .map_err(|_| "target serialization failed")
        .unwrap_or_default();
    let selected = select_offer_targets_trust(&target_values, MAX_TARGETS_PER_REFRESH, trust);

    // Python registers catalog targets on the dedicated sources; their
    // readers are deferred, so this is a no-op for now.
    register_catalog_targets(product_name, &selected, specs);

    // Every safe target URL is grouped by its manifest merchant. In
    // permissive (test) mode localhost URLs use the mock merchant.
    let mut by_merchant: HashMap<String, Vec<String>> = HashMap::new();
    let mut merchant_of: HashMap<String, String> = HashMap::new();
    for target in targets {
        let Some(merchant) = merchant_for(engine, &target.source_url) else {
            continue;
        };
        by_merchant
            .entry(merchant.clone())
            .or_default()
            .push(target.source_url.clone());
        merchant_of.insert(target.source_url.clone(), merchant);
    }
    let urls: Vec<String> = selected.iter().map(|row| row.source_url.clone()).collect();
    if urls.is_empty() {
        return Vec::new();
    }

    // Non-selected URLs with no recorded diagnostics are marked skipped so
    // the report never shows an unattempted URL as an observation.
    for url in by_merchant.values().flatten() {
        if !urls.contains(url) {
            engine.record_skipped(url).await;
        }
    }

    // Selected URLs grouped by merchant, bucket order preserved.
    let mut selected_by_merchant: HashMap<String, Vec<String>> = HashMap::new();
    for url in &urls {
        let merchant = merchant_of
            .get(url)
            .cloned()
            .unwrap_or_else(|| "unknown".to_string());
        selected_by_merchant
            .entry(merchant)
            .or_default()
            .push(url.clone());
    }

    let semaphore = Arc::new(Semaphore::new(MAX_PARALLEL_MERCHANTS));
    let mut handles = Vec::new();
    for (_merchant, bucket) in selected_by_merchant {
        let semaphore = semaphore.clone();
        let engine = engine.clone();
        handles.push(tokio::spawn(async move {
            // A 403/429 from the first PDP must open the circuit before
            // another request to that retailer starts — buckets are
            // sequential; merchants run in parallel.
            let _permit = semaphore.acquire_owned().await;
            let mut fetched = Vec::new();
            for url in bucket {
                let offer = engine.fetch_target(&url).await;
                let attempt = engine.diagnostics(&url).await;
                fetched.push((url, offer, attempt));
            }
            fetched
        }));
    }

    let mut fetched_by_url: HashMap<String, (Option<OfferObservation>, Option<AttemptRecord>)> =
        HashMap::new();
    for handle in handles {
        for (url, offer, attempt) in handle.await.unwrap_or_default() {
            fetched_by_url.insert(url, (offer, attempt));
        }
    }

    urls.iter()
        .filter_map(|url| {
            let (offer, attempt) = fetched_by_url.get(url)?;
            let merchant = merchant_of.get(url)?.clone();
            let offer = offer.clone();
            let identity_verified = match &offer {
                Some(offer) => {
                    matcher.has_observed_offer_evidence(product_name, offer, specs)
                        && matcher.is_strict_match(product_name, &offer.source_title, specs)
                }
                None => false,
            };
            Some(ObservedTarget {
                source_url: url.clone(),
                merchant,
                attempt: attempt.clone(),
                offer,
                identity_verified,
            })
        })
        .collect()
}

/// Convenience wrapper with the diagnostics-only matcher.
pub async fn refresh_report(
    engine: &Arc<FetchEngine>,
    targets: &[TargetRow],
    product_name: &str,
    specs: &ExpectedSpecs,
) -> Vec<ObservedTarget> {
    refresh_targets(engine, targets, &NoopMatcher, product_name, specs).await
}

/// Python `register_catalog_targets` — deferred with the dedicated readers.
fn register_catalog_targets(_product_name: &str, _selected: &[TargetRow], _specs: &ExpectedSpecs) {
    // Phase 2/3: forwarded to the dedicated source readers.
}

fn merchant_for(engine: &FetchEngine, url: &str) -> Option<String> {
    let parsed = psr_core::parse_url(url).ok()?;
    let host = parsed.hostname?;
    if engine.is_permissive() {
        return Some(match host.as_str() {
            "127.0.0.1" | "localhost" | "::1" => "Mock Merchant".to_string(),
            _ => allowed_host_merchant(&host)?.to_string(),
        });
    }
    allowed_host_merchant(&host).map(|merchant| merchant.to_string())
}
