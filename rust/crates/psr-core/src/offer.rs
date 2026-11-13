//! Offer observation and catalog target types shared across crates.

use serde::{Deserialize, Serialize};

/// Result row produced by parsers — the Python `_product_offer` dict.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct OfferObservation {
    pub merchant: String,
    pub price: f64,
    pub currency: String,
    pub availability: String,
    pub source_title: String,
    pub url: String,
    pub observed_at: String,
}

/// Projected catalog offer target — Python's projected target dict with the
/// four fields the refresh pipeline consumes.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct TargetRow {
    pub merchant_name: String,
    pub source_url: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub checked_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub seller_name: Option<String>,
}

/// UTC timestamp in Python `datetime.now(timezone.utc).isoformat()` shape
/// (`+00:00` replaced by `Z`, microseconds omitted only when exactly zero).
pub fn utc_now_iso() -> String {
    let now = chrono::Utc::now();
    let base = now.format("%Y-%m-%dT%H:%M:%S");
    let micros = now.timestamp_subsec_micros();
    if micros == 0 {
        format!("{base}Z")
    } else {
        format!("{base}.{:06}Z", micros)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn timestamp_shape() {
        let stamp = utc_now_iso();
        assert!(stamp.ends_with('Z'));
        assert!(stamp.starts_with("20"));
        // Either 2026-11-13T09:03:00Z or with fractional seconds.
        let with_fraction = stamp.contains('.');
        if with_fraction {
            assert_eq!(stamp.len(), "2026-11-13T09:03:00.123456Z".len());
        }
    }
}
