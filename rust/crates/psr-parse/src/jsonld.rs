//! Port of `retailer_offer_refresh._schema_nodes` and `_type_is`.
//!
//! Traverses only document lists and `@graph` branches — never arbitrary
//! `hasVariant` or recommendation branches whose price may belong to another
//! phone (that invariant is load-bearing for correctness, not style).

use std::sync::LazyLock;

use regex::{Captures, Regex};
use serde_json::Value;
use scraper::{Html, Selector};

/// `retailer_offer_refresh.VIVO_X300_VATAN_PDP`.
pub const VIVO_X300_VATAN_PDP: &str = "https://www.vatanbilgisayar.com/vivo-x300-16-512gb-akilli-telefon-pembe.html";

/// `retailer_offer_refresh._type_is`.
pub fn type_is(node: &Value, expected: &str) -> bool {
    let values: Vec<&Value> = match node.get("@type") {
        None => return false,
        Some(Value::Array(items)) => items.iter().collect(),
        Some(single) => vec![single],
    };
    values.iter().any(|value| {
        value
            .as_str()
            .map(|text| last_segment(text) == expected)
            .unwrap_or(false)
    })
}

/// Python `value.rstrip('/').rsplit('/', 1)[-1]`.
fn last_segment(text: &str) -> &str {
    let trimmed = text.trim_end_matches('/');
    trimmed.rsplit('/').next().unwrap_or("")
}

/// `retailer_offer_refresh._schema_nodes`.
///
pub fn schema_nodes(doc: &Html) -> Vec<Value> {
    schema_nodes_with_vatan_reviews(doc, false)
}

pub(crate) fn schema_nodes_with_vatan_reviews(doc: &Html, repair_reviews: bool) -> Vec<Value> {
    let mut nodes: Vec<Value> = Vec::new();
    let selector = match Selector::parse(r#"script[type="application/ld+json"]"#) {
        Ok(selector) => selector,
        Err(_) => return nodes,
    };
    for script in doc.select(&selector) {
        if nodes.len() >= 2000 {
            break;
        }
        let text: String = script.text().collect::<Vec<_>>().join("");
        let parsed: Value = match serde_json::from_str(&text) {
            Ok(parsed) => parsed,
            Err(_) if repair_reviews => {
                if let Some(product) = repair_vatan_review_jsonld(&text) {
                    // Only this selected product may use the repaired metadata.
                    nodes.push(product);
                }
                continue;
            }
            Err(_) => continue,
        };
        let mut pending = vec![parsed];
        while let Some(node) = pending.pop() {
            if nodes.len() >= 2000 {
                break;
            }
            match node {
                Value::Array(items) => {
                    let capped: Vec<Value> = items.into_iter().take(2000).collect();
                    pending.extend(capped);
                }
                Value::Object(map) => {
                    let graph = map.get("@graph").cloned();
                    nodes.push(Value::Object(map));
                    if let Some(graph) = graph {
                        if graph.is_object() || graph.is_array() {
                            pending.push(graph);
                        }
                    }
                }
                _ => {}
            }
        }
    }
    nodes
}

/// Escape control characters only in reviewBody strings. The Product,
/// Offer, SKU and variant metadata must still pass selected-PDP checks.
fn repair_vatan_review_jsonld(raw: &str) -> Option<Value> {
    static REVIEW: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r#"("reviewBody"\s*:\s*")((?:\\.|[^"\\])*)(")"#).unwrap());
    let repaired = REVIEW.replace_all(raw, |captures: &Captures<'_>| {
        let mut text = captures[1].to_string();
        for c in captures[2].chars() {
            if c <= '\u{1f}' {
                text.push_str(&format!("\\u{:04x}", c as u32));
            } else {
                text.push(c);
            }
        }
        text.push_str(&captures[3]);
        text
    });
    if repaired == raw {
        return None;
    }
    let product: Value = serde_json::from_str(&repaired).ok()?;
    let variants = product.get("hasVariant")?.as_array()?;
    let offer = product.get("offers")?;
    (type_is(&product, "Product") && !variants.is_empty() && type_is(offer, "Offer"))
        .then_some(product)
}

/// Python truthiness of arbitrary JSON values (`product.get('hasVariant')`).
pub fn is_truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().map(|f| f != 0.0).unwrap_or(false),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(items)) => !items.is_empty(),
        Some(Value::Object(map)) => !map.is_empty(),
    }
}

/// Python `str(number)` for JSON numbers, used by the price-shape regex.
pub fn py_number_string(number: &serde_json::Number) -> String {
    if let Some(int) = number.as_i64() {
        return int.to_string();
    }
    if let Some(uint) = number.as_u64() {
        return uint.to_string();
    }
    match number.as_f64() {
        Some(float) => {
            if float == float.trunc() && float.abs() < 1e15 {
                // Python renders integral floats with one decimal: 3.0 → "3.0".
                format!("{float:.1}")
            } else {
                format!("{float}")
            }
        }
        None => number.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn type_is_parity() {
        assert!(type_is(&json!({"@type": "Product"}), "Product"));
        assert!(type_is(&json!({"@type": ["Product", "Thing"]}), "Product"));
        assert!(type_is(&json!({"@type": "https://schema.org/Product/"}), "Product"));
        assert!(type_is(&json!({"@type": "https://schema.org/Offer"}), "Offer"));
        assert!(!type_is(&json!({}), "Product"));
        assert!(!type_is(&json!({"@type": 5}), "Product"));
    }

    #[test]
    fn py_number_string_parity() {
        assert_eq!(py_number_string(&serde_json::Number::from(3500)), "3500");
        assert_eq!(
            py_number_string(&serde_json::Number::from_f64(3.5).unwrap()),
            "3.5"
        );
        assert_eq!(
            py_number_string(&serde_json::Number::from_f64(3.0).unwrap()),
            "3.0"
        );
    }

    #[test]
    fn traverses_graph_and_lists_with_caps() {
        let html = r#"
        <html><body>
        <script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"Product","name":"A"},{"@type":"Offer"}]}</script>
        <script type="application/ld+json">[{"@type":"Product","name":"B"}]</script>
        <script type="application/ld+json">not json</script>
        </body></html>
        "#;
        let doc = Html::parse_document(html);
        let nodes = schema_nodes(&doc);
        // @context/@graph container + Product A + Offer + list-wrapped Product B
        // — the container itself counts, exactly like the Python traversal.
        assert_eq!(nodes.len(), 4);
        assert!(nodes.iter().any(|n| n.get("name").and_then(Value::as_str) == Some("A")));
        assert!(nodes.iter().any(|n| n.get("name").and_then(Value::as_str) == Some("B")));
    }
}
