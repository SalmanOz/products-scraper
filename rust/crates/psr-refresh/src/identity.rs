//! Deferred identity matching (phase 2): `TRPriceScraper.is_strict_match`
//! and `has_observed_offer_evidence` (tr_price_scraper.py lines 571-840).
//!
//! The Rust refresh pipeline never promotes an unverified offer into the
//! ingestion queue: until the matcher is ported, callers either run the
//! Python identity gate on the report rows or keep the observations as
//! diagnostics-only output. `NoopMatcher` exists solely so the CLI can
//! print raw observations for human parity review.

use psr_core::OfferObservation;

/// The expected product attribute subset the Python matcher consumes.
#[derive(Debug, Clone, Default, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct ExpectedSpecs {
    pub ram_gb: Option<i64>,
    pub storage_gb: Option<i64>,
    pub network_generation: Option<String>,
}

/// Port seam for `TRPriceScraper` identity checks (phase 2).
pub trait IdentityMatcher {
    /// `is_strict_match(product_name, source_title, expected_specs)`.
    fn is_strict_match(
        &self,
        product_name: &str,
        source_title: &str,
        specs: &ExpectedSpecs,
    ) -> bool;

    /// `has_observed_offer_evidence(product_name, offer, expected_specs)`.
    fn has_observed_offer_evidence(
        &self,
        product_name: &str,
        offer: &OfferObservation,
        specs: &ExpectedSpecs,
    ) -> bool;
}

/// Diagnostics-only matcher: never verifies. Observations surfaced with this
/// matcher are marked unverified and must not be ingested.
#[derive(Debug, Clone, Copy, Default)]
pub struct NoopMatcher;

impl IdentityMatcher for NoopMatcher {
    fn is_strict_match(
        &self,
        _product_name: &str,
        _source_title: &str,
        _specs: &ExpectedSpecs,
    ) -> bool {
        false
    }

    fn has_observed_offer_evidence(
        &self,
        _product_name: &str,
        _offer: &OfferObservation,
        _specs: &ExpectedSpecs,
    ) -> bool {
        false
    }
}
