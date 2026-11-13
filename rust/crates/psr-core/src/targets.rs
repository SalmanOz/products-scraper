//! Port of `offer_target_selection.select_offer_targets`.
//!
//! Bounded refresh planning: one row per platform first, then extra rows for
//! distinct sellers, then remaining variants, capped at `maximum` targets.

use crate::offer::TargetRow;
use crate::url::UrlTrust;
use serde_json::Value;
use std::collections::HashMap;

pub const PRIORITY_RETAILERS: &[(&str, u32)] = &[
    ("Hepsiburada", 0),
    ("Trendyol", 1),
    ("n11", 2),
    ("Teknosa", 3),
];

fn priority(merchant: &str) -> u32 {
    PRIORITY_RETAILERS
        .iter()
        .find(|(name, _)| *name == merchant)
        .map(|(_, priority)| *priority)
        .unwrap_or(4)
}

/// `select_offer_targets(targets, maximum=12)` — production trust layer.
pub fn select_offer_targets(targets: &[Value], maximum: usize) -> Vec<TargetRow> {
    select_offer_targets_trust(targets, maximum, UrlTrust::Manifest)
}

/// Trust-parameterized variant used by the permissive wiremock pipeline.
pub fn select_offer_targets_trust(
    targets: &[Value],
    maximum: usize,
    trust: UrlTrust,
) -> Vec<TargetRow> {
    // Deduplicate by URL keeping the first row, later seller_name wins.
    let mut unique: Vec<(String, TargetRow)> = Vec::new();
    for target in targets {
        let source_url = match target {
            Value::String(url) => url.clone(),
            Value::Object(map) => map
                .get("source_url")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
            _ => continue,
        };
        let parsed = match trust.check(&source_url) {
            Some(parsed) => parsed,
            None => continue,
        };
        let merchant_name = trust
            .merchant(parsed.hostname.as_deref().unwrap_or(""))
            .unwrap_or_default()
            .to_string();
        let seller_name = target
            .as_object()
            .and_then(|map| map.get("seller_name"))
            .and_then(Value::as_str)
            .map(|s| s.to_string());
        let row = TargetRow {
            merchant_name: merchant_name.clone(),
            source_url: source_url.clone(),
            checked_at: target
                .as_object()
                .and_then(|map| map.get("checked_at"))
                .and_then(Value::as_str)
                .map(|s| s.to_string()),
            seller_name: seller_name.clone(),
        };
        if let Some(existing) = unique.iter_mut().find(|(url, _)| *url == source_url) {
            if let Some(seller) = seller_name {
                existing.1.seller_name = Some(seller);
            }
        } else {
            unique.push((source_url, row));
        }
    }

    let mut platforms: Vec<TargetRow> = Vec::new();
    let mut sellers: Vec<TargetRow> = Vec::new();
    let mut variants: Vec<TargetRow> = Vec::new();
    let mut seen_platforms: HashMap<String, ()> = HashMap::new();
    let mut seen_sellers: HashMap<(String, String), ()> = HashMap::new();
    for (_, row) in &unique {
        let platform = row.merchant_name.clone();
        let seller = row
            .seller_name
            .clone()
            .unwrap_or_else(|| platform.clone())
            .trim()
            .to_lowercase();
        let identity = (platform.clone(), seller);
        if !seen_platforms.contains_key(&platform) {
            platforms.push(row.clone());
            seen_platforms.insert(platform, ());
        } else if !seen_sellers.contains_key(&identity) {
            sellers.push(row.clone());
        } else {
            variants.push(row.clone());
        }
        seen_sellers.insert(identity, ());
    }
    // Stable sort preserves Python's `list.sort` behaviour for equal keys.
    platforms.sort_by_key(|row| priority(&row.merchant_name));

    let mut selected: Vec<TargetRow> = Vec::with_capacity(maximum);
    for bucket in [platforms, sellers, variants] {
        for row in bucket {
            if selected.len() == maximum {
                return selected;
            }
            selected.push(row);
        }
    }
    selected
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn dedupes_and_prioritises_platforms() {
        let targets = json!([
            {"source_url": "https://www.n11.com/a-p-A1", "seller_name": "Satıcı X"},
            {"source_url": "https://www.hepsiburada.com/a-p-H1"},
            {"source_url": "https://www.trendyol.com/a-p-T1"},
            {"source_url": "https://www.teknosa.com/a-p-12345"},
            {"source_url": "https://www.n11.com/b-p-B1"},
            {"source_url": "https://www.hepsiburada.com/a-p-H1"}, // duplicate
            {"source_url": "https://www.idefix.com/a-p-I1"},
        ]);
        let selected = select_offer_targets(targets.as_array().unwrap(), 12);
        let urls: Vec<&str> = selected.iter().map(|row| row.source_url.as_str()).collect();
        // Platform rows first, ordered by priority, deduplicated.
        assert_eq!(
            urls,
            vec![
                "https://www.hepsiburada.com/a-p-H1",
                "https://www.trendyol.com/a-p-T1",
                "https://www.n11.com/a-p-A1",
                "https://www.teknosa.com/a-p-12345",
                "https://www.idefix.com/a-p-I1",
                "https://www.n11.com/b-p-B1",
            ]
        );
        // The duplicate row keeps its first merchant and gains nothing.
        assert_eq!(selected[0].merchant_name, "Hepsiburada");
    }

    #[test]
    fn seller_name_override_on_duplicate_url() {
        let targets = json!([
            {"source_url": "https://www.n11.com/a-p-A1"},
            {"source_url": "https://www.n11.com/a-p-A1", "seller_name": "Mağaza B"},
        ]);
        let selected = select_offer_targets(targets.as_array().unwrap(), 12);
        assert_eq!(selected.len(), 1);
        assert_eq!(selected[0].seller_name.as_deref(), Some("Mağaza B"));
    }

    #[test]
    fn caps_at_maximum() {
        let mut targets = vec![];
        for i in 0..20 {
            targets.push(json!({"source_url": format!("https://www.idefix.com/urun-{i}-p-I{i}")}));
        }
        let selected = select_offer_targets(&targets, 5);
        assert_eq!(selected.len(), 5);
    }

    #[test]
    fn drops_unsafe_urls() {
        let targets = json!([
            {"source_url": "https://www.n11.com/a-p-A1?token=x"},
            {"source_url": "http://www.n11.com/a-p-A2"},
        ]);
        assert!(select_offer_targets(targets.as_array().unwrap(), 12).is_empty());
    }
}
