//! Port of `retailer_offer_refresh._product_offer` — the generic public PDP
//! reader — plus `_is_conditional_price` and `_challenge_page`.
//!
//! Deferred to the per-source milestone (documented in `MIGRATION.md`):
//! `_vatan_selected_variant`, `_pazarama_visible_cash_price` and
//! `_public_retailer_product`. Those hosts fail closed here (`None`), never
//! with an unverified offer.

use crate::jsonld::{is_truthy, py_number_string, schema_nodes, type_is, VIVO_X300_VATAN_PDP};
use psr_core::purl::{parse_url, urljoin_str};
use psr_core::url::{pazarama_product_url, public_source, UrlTrust};
use psr_core::utc_now_iso;
use psr_core::OfferObservation;
use scraper::{Html, Selector};
use serde_json::{json, Map, Value};
use std::collections::HashMap;

const MAX_PRICE: f64 = 5_000_000.0;

/// `retailer_offer_refresh._AVAILABILITY`.
fn availability_of(text: &str) -> Option<&'static str> {
    match last_segment(text) {
        "InStock" => Some("in_stock"),
        "LimitedAvailability" => Some("limited"),
        "PreOrder" | "PreSale" => Some("preorder"),
        "OutOfStock" | "SoldOut" | "Discontinued" => Some("out_of_stock"),
        _ => None,
    }
}

fn last_segment(text: &str) -> &str {
    text.trim_end_matches('/').rsplit('/').next().unwrap_or("")
}

/// Python `html.unescape` for the named/numeric entities the scraper meets.
pub fn unescape_html(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    let mut chars = input.chars().peekable();
    while let Some(c) = chars.next() {
        if c != '&' {
            out.push(c);
            continue;
        }
        let mut entity = String::new();
        let mut terminated = false;
        while let Some(&next) = chars.peek() {
            if next == ';' {
                chars.next();
                terminated = true;
                break;
            }
            if entity.len() > 10 {
                break;
            }
            entity.push(next);
            chars.next();
        }
        if terminated {
            match entity.as_str() {
                "amp" => out.push('&'),
                "lt" => out.push('<'),
                "gt" => out.push('>'),
                "quot" => out.push('"'),
                "apos" => out.push('\''),
                "nbsp" => out.push('\u{00a0}'),
                other => {
                    let decoded = other
                        .strip_prefix("#x")
                        .or_else(|| other.strip_prefix("#X"))
                        .and_then(|hex| u32::from_str_radix(hex, 16).ok())
                        .and_then(char::from_u32)
                        .or_else(|| {
                            other
                                .strip_prefix('#')
                                .and_then(|dec| dec.parse::<u32>().ok())
                                .and_then(char::from_u32)
                        });
                    match decoded {
                        Some(decoded) => out.push(decoded),
                        None => {
                            out.push('&');
                            out.push_str(other);
                            out.push(';');
                        }
                    }
                }
            }
        } else {
            out.push('&');
            out.push_str(&entity);
        }
    }
    out
}

/// BS4 `get_text(' ', strip=True)` for an element subtree.
fn element_text_stripped(element: scraper::ElementRef) -> String {
    element
        .text()
        .map(|piece| piece.trim().to_string())
        .filter(|piece| !piece.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

/// Whole-document visible text (`soup.get_text(' ', strip=True)`).
pub fn document_text(doc: &Html) -> String {
    let mut parts: Vec<String> = Vec::new();
    for node in doc.tree.nodes() {
        if let scraper::Node::Text(text) = node.value() {
            let trimmed = text.to_string();
            let trimmed = trimmed.trim();
            if !trimmed.is_empty() {
                parts.push(trimmed.to_string());
            }
        }
    }
    parts.join(" ")
}

fn normalize_fold(value: &str) -> String {
    // `_is_conditional_price.fold`: NFKD-strip accents + ı→i, lowercased.
    let folded = value.to_lowercase().replace('ı', "i");
    let mut out = String::with_capacity(folded.len());
    for c in folded.chars() {
        if is_combining_mark(c) {
            continue;
        }
        out.push(c);
    }
    out
}

fn is_combining_mark(c: char) -> bool {
    matches!(
        c as u32,
        0x0300..=0x036F | 0x1AB0..=0x1AFF | 0x1DC0..=0x1DFF | 0x20D0..=0x20FF | 0xFE20..=0xFE2F
    )
}

/// `retailer_offer_refresh._is_conditional_price`.
pub fn is_conditional_price(doc: &Html, offer: &Value, price: f64) -> bool {
    for key in ["validForMemberTier", "eligibleCustomerType", "eligibleTransactionVolume"] {
        if offer.get(key).is_some() {
            return true;
        }
    }
    let label = normalize_fold(&format!(
        "{} {}",
        offer.get("name").and_then(Value::as_str).unwrap_or(""),
        offer.get("description").and_then(Value::as_str).unwrap_or("")
    ));
    let condition = r"(?:sepette(?:\s+indirim)?|sepete\s+ozel|kuponla|uyelere\s+ozel|premium[\s\x27’]*a?\s+ozel)";
    if regex::Regex::new(condition).unwrap().is_match(&label) {
        return true;
    }
    // Visible conditional prices are denial evidence even if JSON-LD silently
    // loses that condition.
    let visible = normalize_fold(&document_text(doc));
    let amount = r"([0-9]{1,3}(?:\.[0-9]{3})+(?:,[0-9]{1,2})?|[0-9]{4,7}(?:,[0-9]{1,2})?)";
    let pattern = format!(r"{condition}\s*:?\s*{amount}\s*(?:tl|₺)");
    let re = regex::Regex::new(&pattern).unwrap();
    for capture in re.captures_iter(&visible) {
        let amount_text = capture.get(1).map(|m| m.as_str()).unwrap_or("");
        let normalized = amount_text.replace('.', "").replace(',', ".");
        if normalized.parse::<f64>() == Ok(price) {
            return true;
        }
    }
    false
}

/// `retailer_offer_refresh._challenge_page`.
pub fn is_challenge_page(doc: &Html) -> bool {
    let mut heading_parts: Vec<String> = Vec::new();
    for selector in ["title", "h1"] {
        if let Ok(sel) = Selector::parse(selector) {
            for element in doc.select(&sel) {
                heading_parts.push(element_text_stripped(element));
            }
        }
    }
    let heading = heading_parts.join(" ");
    if regex::Regex::new(
        r"(?i)captcha|robot check|just a moment|attention required|you have been blocked|access denied",
    )
    .unwrap()
    .is_match(&heading)
    {
        return true;
    }
    if let Ok(form_sel) =
        Selector::parse(r#"form[action*="validateCaptcha"], #captchacharacters, #challenge-form"#)
    {
        if doc.select(&form_sel).next().is_some() {
            return true;
        }
    }
    let visible = document_text(doc);
    regex::Regex::new(r"(?i)al[ıi]şverişe devam etmek için|continue shopping.*click the button")
        .unwrap()
        .is_match(&visible)
}

/// `str()` on the raw price must be a plain decimal number: `\d+(?:\.\d{1,2})?`.
fn price_from_raw(raw: &Value) -> Option<f64> {
    if matches!(raw, Value::Bool(_)) {
        return None;
    }
    let text = match raw {
        Value::Number(number) => py_number_string(number),
        Value::String(text) => text.clone(),
        _ => return None,
    };
    let re = regex::Regex::new(r"^\d+(?:\.\d{1,2})?$").unwrap();
    if !re.is_match(&text) {
        return None;
    }
    let price: f64 = text.parse().ok()?;
    if !price.is_finite() || price <= 0.0 || price > MAX_PRICE {
        return None;
    }
    Some(price)
}

/// Python `resolve` over `@id`-only reference nodes.
fn resolve_ref(node: &Value, by_id: &HashMap<String, &Value>) -> Value {
    if let Value::Object(map) = node {
        if map.len() == 1 && map.contains_key("@id") {
            let id = map.get("@id").and_then(Value::as_str).unwrap_or("");
            return by_id
                .get(id)
                .map(|value| (*value).clone())
                .unwrap_or_else(|| json!({}));
        }
    }
    node.clone()
}

fn explicit_identity_mismatch(product: &Value, final_url: &str, trust: UrlTrust) -> bool {
    for key in ["url", "@id"] {
        if let Some(Value::String(value)) = product.get(key) {
            if value.is_empty() {
                continue;
            }
            let joined = match urljoin_str(final_url, value) {
                Ok(joined) => joined,
                Err(_) => return true,
            };
            if !trust.same_resource(&joined, final_url) {
                return true;
            }
        }
    }
    false
}

fn own_offer(
    product: &Value,
    doc: &Html,
    final_url: &str,
    by_id: &HashMap<String, &Value>,
    trust: UrlTrust,
) -> Option<Value> {
    let offers_raw = product.get("offers").unwrap_or(&Value::Null);
    let offers: Vec<&Value> = match offers_raw {
        Value::Array(items) => items.iter().collect(),
        single => vec![single],
    };
    if offers.len() != 1 {
        return None;
    }
    let offer = resolve_ref(offers[0], by_id);
    let map = offer.as_object()?;
    if !type_is(&offer, "Offer") {
        return None;
    }
    if type_is(&offer, "AggregateOffer") || map.contains_key("lowPrice") || map.contains_key("highPrice") {
        return None;
    }
    let mut offer = offer.clone();
    // Troy publishes an http Offer URL on its HTTPS PDP. Normalize document
    // metadata only, with an exact HTTPS canonical.
    if public_source(final_url).as_deref() == Some("troyestore.com") {
        if let Some(Value::String(raw)) = offer.get("url").cloned() {
            if raw.starts_with("http://") {
                let canonical = Selector::parse(r#"link[rel="canonical"]"#)
                    .ok()
                    .and_then(|sel| doc.select(&sel).next())
                    .and_then(|element| element.value().attr("href"))
                    .map(|href| href.to_string());
                if canonical.as_deref() == Some(final_url) {
                    let secured = format!("https://{}", &raw["http://".len()..]);
                    let netloc_equal = parse_url(&secured)
                        .ok()
                        .zip(parse_url(final_url).ok())
                        .map(|(a, b)| a.netloc == b.netloc)
                        .unwrap_or(false);
                    if public_source(&secured).is_some()
                        && netloc_equal
                        && trust.same_resource(&secured, final_url)
                    {
                        offer["url"] = json!(secured);
                    }
                }
            }
        }
    }
    Some(offer)
}

/// `retailer_offer_refresh._product_offer` — generic public reader
/// (production URL-faith layer).
pub fn product_offer(html: &str, final_url: &str) -> Option<OfferObservation> {
    let (merchant, trust) = checked_merchant(final_url, UrlTrust::Manifest)?;
    let doc = Html::parse_document(html);
    product_offer_doc(&doc, final_url, &merchant, trust)
}

/// Test entry for the wiremock engine: identical walk on relaxed URL checks.
pub fn product_offer_permissive(html: &str, final_url: &str) -> Option<OfferObservation> {
    let (merchant, trust) = checked_merchant(final_url, UrlTrust::Permissive)?;
    let doc = Html::parse_document(html);
    product_offer_doc(&doc, final_url, &merchant, trust)
}

/// Pre-parsed entry used by the fetch engine so one HTML parse serves both
/// the challenge check and the offer extraction.
pub fn product_offer_document(
    doc: &Html,
    final_url: &str,
) -> Option<OfferObservation> {
    let (merchant, trust) = checked_merchant(final_url, UrlTrust::Manifest)?;
    product_offer_doc(doc, final_url, &merchant, trust)
}

fn checked_merchant(final_url: &str, trust: UrlTrust) -> Option<(String, UrlTrust)> {
    let parsed = trust.check(final_url)?;
    let host = parsed.hostname.clone()?;
    let apex = host.strip_prefix("www.").unwrap_or(&host).to_string();
    // MediaMarkt and Teknosa publish marketplace offers under their own
    // hosts; the generic parser must not turn another seller's buy box into
    // a retailer-owned offer. Their exact first-party readers verify the
    // visible selected seller.
    if apex == "mediamarkt.com.tr" || apex == "teknosa.com" {
        return None;
    }
    let merchant = trust.merchant(&host)?.to_string();
    Some((merchant, trust))
}

fn product_offer_doc(
    doc: &Html,
    final_url: &str,
    merchant: &str,
    trust: UrlTrust,
) -> Option<OfferObservation> {
    let nodes = schema_nodes(doc);
    let products: Vec<&Value> = nodes
        .iter()
        .filter(|node| type_is(node, "Product"))
        .collect();
    let mut by_id: HashMap<String, &Value> = HashMap::new();
    for node in &nodes {
        if let Some(id) = node.get("@id").and_then(Value::as_str) {
            by_id.insert(id.to_string(), node);
        }
    }

    // An explicit URL links the Product/Offer to the fetched PDP. If there are
    // several Products but no such link, guessing the main one is unsafe.
    let mut selected: Vec<&Value> = Vec::new();
    for product in &products {
        if explicit_identity_mismatch(product, final_url, trust) {
            continue;
        }
        let offer = own_offer(product, doc, final_url, &by_id, trust);
        let mut identities: Vec<String> = Vec::new();
        for key in ["url", "@id"] {
            if let Some(Value::String(value)) = product.get(key) {
                identities.push(value.clone());
            }
        }
        if let Some(offer) = &offer {
            if let Some(Value::String(url)) = offer.get("url") {
                identities.push(url.clone());
            }
        }
        if identities.iter().any(|identity| trust.same_resource(identity, final_url)) {
            selected.push(product);
        }
    }
    if selected.is_empty() && products.len() == 1 {
        let product = products[0];
        if explicit_identity_mismatch(product, final_url, trust) {
            return None;
        }
        selected = products;
    }
    if selected.len() != 1 {
        return None;
    }
    let product = selected[0];

    // The reviewed Vivo PDP must remain a selected SKU next to the other
    // color; otherwise the generic path would skip Vatan's seller/cash/cart
    // assertions when the site's schema changes.
    if final_url == VIVO_X300_VATAN_PDP && !is_truthy(product.get("hasVariant")) {
        return None;
    }

    let offer = own_offer(product, doc, final_url, &by_id, trust)?;
    let title = product.get("name").and_then(Value::as_str)?;
    if title.trim().is_empty() || utf16_code_units(title) > 1000 {
        return None;
    }
    if let Some(Value::String(offer_url)) = offer.get("url") {
        if !trust.same_resource(offer_url, final_url) {
            return None;
        }
    }

    // itemOffered cross-checks.
    if let Some(item_reference) = offer.get("itemOffered") {
        if let Some(map) = item_reference.as_object() {
            let reference_id = map.get("@id").filter(|v| !v.is_null());
            if let Some(reference_id) = reference_id {
                if Some(reference_id) != product.get("@id").filter(|v| !v.is_null()) {
                    return None;
                }
            }
        }
        let item = resolve_ref(item_reference, &by_id);
        if let Some(item_map) = item.as_object() {
            let item_name = item_map.get("name").filter(|v| !v.is_null());
            if let Some(item_name) = item_name {
                if item_name != &json!(title) {
                    return None;
                }
            }
            let item_id = item_map.get("@id").filter(|v| !v.is_null());
            if let Some(item_id) = item_id {
                if Some(item_id) != product.get("@id").filter(|v| !v.is_null()) {
                    return None;
                }
            }
        }
    }

    if offer.get("priceCurrency").and_then(Value::as_str) != Some("TRY") {
        return None;
    }
    let price = price_from_raw(offer.get("price")?)?;

    let is_pazarama = pazarama_product_url(final_url);
    if !is_pazarama && is_conditional_price(doc, &offer, price) {
        return None;
    }

    let availability = availability_of(offer.get("availability").and_then(Value::as_str)?)?;

    // Never read priceSpecification or installment figures as a cash price.
    for key in ["billingDuration", "billingIncrement", "loanTerm"] {
        if offer.get(key).is_some() {
            return None;
        }
    }
    if let Some(spec_value) = offer.get("priceSpecification") {
        let specs: Vec<&Value> = match spec_value {
            Value::Array(items) => items.iter().collect(),
            other => vec![other],
        };
        for spec in specs {
            if let Some(spec_map) = spec.as_object() {
                for key in ["billingDuration", "billingIncrement", "loanTerm", "billingStart"] {
                    if spec_map.contains_key(key) {
                        return None;
                    }
                }
            }
        }
    }
    let name_text = offer
        .get("name")
        .map(|value| match value {
            Value::String(text) => text.clone(),
            other => other.to_string(),
        })
        .unwrap_or_default();
    if regex::Regex::new(r"(?i)\b(?:taksit|installment|monthly|aylık|aylik)\b")
        .unwrap()
        .is_match(&name_text)
    {
        return None;
    }

    // hasVariant products require the dedicated Vatan selected-SKU reader
    // (deferred); the generic path rejects them exactly like Python does when
    // `_vatan_selected_variant` returns False for a non-Vatan host.
    if is_truthy(product.get("hasVariant")) {
        return None;
    }
    // Pazarama's Sepette Offer.price requires the dedicated visible-cash
    // reader (deferred); public Apple sources require the reviewed SKU proof
    // reader (deferred). Fail closed, never promote an unverified price.
    if is_pazarama || public_source(final_url).is_some() {
        return None;
    }

    let has_variant = is_truthy(product.get("hasVariant"));
    let source_title = if has_variant {
        unescape_html(title.trim())
    } else {
        title.trim().to_string()
    };
    let url = match offer.get("url") {
        Some(Value::String(offer_url)) if !offer_url.is_empty() => {
            urljoin_str(final_url, offer_url).unwrap_or_else(|_| final_url.to_string())
        }
        _ => final_url.to_string(),
    };

    Some(OfferObservation {
        merchant: merchant.to_string(),
        price,
        currency: "TRY".to_string(),
        availability: availability.to_string(),
        source_title,
        url,
        observed_at: utc_now_iso(),
    })
}

fn utf16_code_units(text: &str) -> usize {
    // Python compares len(title.encode('utf-16-le', ...)) > 2000 bytes,
    // i.e. more than 1000 UTF-16 code units.
    text.encode_utf16().count()
}

// Keep the unused Map import referenced for future projection helpers.
#[allow(dead_code)]
type JsonMap = Map<String, Value>;
