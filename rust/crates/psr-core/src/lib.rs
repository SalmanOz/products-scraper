//! Shared foundation for the Rust scraper: Python-compatible URL semantics,
//! retailer URL safety contracts, offer/target types and target selection.

pub mod offer;
pub mod purl;
pub mod targets;
pub mod url;

pub use offer::{utc_now_iso, OfferObservation, TargetRow};
pub use purl::{parse_qsl, parse_url, urljoin_str, ParsedUrl, UrlError};
pub use targets::{select_offer_targets, select_offer_targets_trust};
pub use url::{
    permissive_safe_url, public_source, same_resource, same_resource_permissive, safe_url,
    variant_query, UrlTrust,
};
