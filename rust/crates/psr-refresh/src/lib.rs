//! Refresh orchestration: target selection, per-merchant concurrency and the
//! run cache (port of `retailer_offer_refresh.refresh_existing_offer_targets`).

pub mod identity;
pub mod orchestrator;

pub use identity::{ExpectedSpecs, IdentityMatcher, NoopMatcher};
pub use orchestrator::{refresh_report, refresh_targets, ObservedTarget, MAX_PARALLEL_MERCHANTS, MAX_TARGETS_PER_REFRESH};
