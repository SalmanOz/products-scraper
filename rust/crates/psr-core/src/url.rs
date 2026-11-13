//! Faithful port of `retailer_offer_refresh._safe_url`, `_variant_query`,
//! `ALLOWED_HOSTS`, `public_retailer_sources.public_source`/`SOURCES`,
//! `pazarama_category_source.pazarama_product_url` and
//! `turk_telekom_source.product_url`.
//!
//! Every check keeps Python's fail-closed behaviour: anything unexpected
//! returns `None`/`false` and never an approximation.

use crate::purl::{parse_qsl, parse_url, unquote, urljoin_str, ParsedUrl, UrlError};
use std::collections::BTreeMap;

/// `retailer_offer_refresh._MERCHANT_DOMAINS` + public `SOURCES` +
/// the Türk Telekom eMağaza host. Hosts are accepted as the apex or with
/// a `www.` prefix, exactly like `ALLOWED_HOSTS`.
pub const MERCHANT_DOMAINS: &[(&str, &str)] = &[
    ("n11.com", "n11"),
    ("pazarama.com", "Pazarama"),
    ("idefix.com", "idefix"),
    ("vatanbilgisayar.com", "Vatan Bilgisayar"),
    ("hepsiburada.com", "Hepsiburada"),
    ("trendyol.com", "Trendyol"),
    ("amazon.com.tr", "Amazon TR"),
    ("turkcell.com.tr", "Pasaj"),
    ("mediamarkt.com.tr", "MediaMarkt"),
    ("teknosa.com", "Teknosa"),
    ("pttavm.com", "PttAVM"),
    ("apple.com", "Apple Store"),
    ("samsung.com", "Samsung"),
    ("mi.com", "Xiaomi"),
    ("pt.com.tr", "Pozitif Teknoloji"),
    ("troyestore.com", "Troy"),
];

pub const TURK_TELEKOM_HOST: &str = "emagaza.turktelekom.com.tr";
pub const TURK_TELEKOM_MERCHANT: &str = "Türk Telekom eMağaza";

pub const PUBLIC_SOURCES: &[(&str, &str)] = &[("pt.com.tr", "Pozitif Teknoloji"), ("troyestore.com", "Troy")];

/// `retailer_offer_refresh.ALLOWED_HOSTS`.
pub fn allowed_host_merchant(host: &str) -> Option<&'static str> {
    for (apex, merchant) in MERCHANT_DOMAINS {
        if host == *apex || host == format!("www.{apex}") {
            return Some(merchant);
        }
    }
    if host == TURK_TELEKOM_HOST {
        return Some(TURK_TELEKOM_MERCHANT);
    }
    None
}

/// `public_retailer_sources.public_source`.
pub fn public_source(url: &str) -> Option<String> {
    let check = || -> Option<String> {
        let parsed = parse_url(url).ok()?;
        let host = parsed.hostname.as_ref().clone()?;
        let host = host.strip_prefix("www.").unwrap_or(&host);
        if !PUBLIC_SOURCES.iter().any(|(apex, _)| *apex == host) {
            return None;
        }
        if parsed.scheme != "https" {
            return None;
        }
        if let Some(query) = &parsed.query {
            if !query.is_empty() {
                return None;
            }
        }
        if !parsed.fragment_string().is_empty() {
            return None;
        }
        if parsed.username.is_some() || parsed.password.is_some() {
            return None;
        }
        match parsed.port() {
            Ok(None) | Ok(Some(443)) => {}
            _ => return None,
        }
        if url.chars().any(|c| (c as u32) <= 0x20 || c == '\\') {
            return None;
        }
        if regex::Regex::new(r"(?i)%2f|%5c|%3f|%23|%00")
            .unwrap()
            .is_match(&parsed.path)
        {
            return None;
        }
        Some(host.to_string())
    };
    check()
}

/// `pazarama_category_source.HOST` + `pazarama_product_url`.
pub const PAZARAMA_HOST: &str = "www.pazarama.com";

pub fn pazarama_product_url(url: &str) -> bool {
    let check = || -> Option<bool> {
        if url.chars().count() > 600 {
            return Some(false);
        }
        if url.chars().any(|c| (c as u32) <= 0x20 || c == '\\') {
            return Some(false);
        }
        let parsed = parse_url(url).ok()?;
        let path = unquote(&parsed.path);
        Some(
            parsed.scheme == "https"
                && parsed.hostname.as_deref() == Some(PAZARAMA_HOST)
                && parsed.username.is_none()
                && parsed.password.is_none()
                && matches!(parsed.port_lossy(), None | Some(443))
                && parsed.query_string().is_empty()
                && parsed.fragment_string().is_empty()
                && !path
                    .get(1..)
                    .map_or(true, |rest| rest.chars().any(|c| (c as u32) <= 0x20 || c == '\\' || c == '/' || c == '?' || c == '#'))
                && regex::Regex::new(r"(?i)^/[a-z0-9][a-z0-9-]*-p-[a-z0-9-]+$")
                    .unwrap()
                    .is_match(&path),
        )
    };
    check().unwrap_or(false)
}

/// `turk_telekom_source.HOST` + `product_url`.
pub const TURK_TELEKOM_PDP_HOST: &str = "emagaza.turktelekom.com.tr";

pub fn turk_telekom_product_url(url: &str) -> bool {
    let check = || -> Option<bool> {
        if url.chars().count() > 500 {
            return Some(false);
        }
        if url.chars().any(|c| (c as u32) <= 0x20 || c == '\\') {
            return Some(false);
        }
        let parsed = parse_url(url).ok()?;
        let pairs = parse_qsl(parsed.query_string(), true, 3).ok()?;
        Some(
            parsed.scheme == "https"
                && parsed.hostname.as_deref() == Some(TURK_TELEKOM_PDP_HOST)
                && parsed.username.is_none()
                && parsed.password.is_none()
                && matches!(parsed.port_lossy(), None | Some(443))
                && parsed.fragment_string().is_empty()
                && regex::Regex::new(r"^/tr/products/[a-z0-9-]+/[a-z0-9-]+$")
                    .unwrap()
                    .is_match(&parsed.path)
                && pairs.len() == 1
                && pairs[0].0 == "v"
                && regex::Regex::new(r"^[1-9]\d{2,7}$").unwrap().is_match(&pairs[0].1),
        )
    };
    check().unwrap_or(false)
}

/// `retailer_offer_refresh._VARIANT_KEYS`.
pub const VARIANT_KEYS: &[&str] = &[
    "sku", "skuid", "selectedsku", "selectedskuid", "variant", "variantid",
    "selectedvariant", "selectedvariantid", "variation", "variationid",
    "selectedvariation", "selectedvariationid", "productvariant",
    "productvariantid", "storage", "storagegb", "internalstorage",
    "dahilihafiza", "memory", "memorysize", "memorygb", "ram", "ramgb",
    "capacity", "capacitygb",
];

/// `retailer_offer_refresh._variant_query`.
pub fn variant_query(
    parsed: &ParsedUrl,
) -> Result<BTreeMap<String, String>, UrlError> {
    let pairs = parse_qsl(parsed.query_string(), true, 50)?;
    let mut selected: BTreeMap<String, String> = BTreeMap::new();
    for (key, value) in pairs {
        let normalized: String = key
            .to_lowercase()
            .chars()
            .filter(|c| c.is_ascii_alphanumeric())
            .collect();
        if !VARIANT_KEYS.contains(&normalized.as_str()) {
            continue;
        }
        let value = value.trim().to_lowercase();
        if value.is_empty() {
            return Err(UrlError::InvalidUrl("Ambiguous selected variant".into()));
        }
        if let Some(existing) = selected.get(&normalized) {
            if existing != &value {
                return Err(UrlError::InvalidUrl("Ambiguous selected variant".into()));
            }
        }
        selected.insert(normalized, value);
    }
    Ok(selected)
}

const SENSITIVE_KEY_RE: &str = r"token|auth|password|secret|signature|session|email|api.?key|access.?key|credential|add.?to.?cart|wc.?ajax|nonce|pao.?key|pao.?edit";

fn is_ip_literal(host: &str) -> bool {
    // Python uses `ipaddress.ip_address(host)`, which rejects non-IP values.
    // Hostnames are lowercased before this check, mirroring urlparse.
    let host = host.trim_matches(|c| c == '[' || c == ']');
    host.parse::<std::net::IpAddr>().is_ok()
        // Python also accepts bare hex like 0x7f.0.0.1 through inet_aton
        // behaviour only in sockets; ip_address is stricter, so digits-only
        // dotted forms are enough here.
        || (host.split('.').count() == 4 && host.chars().all(|c| c.is_ascii_digit()))
}

/// `retailer_offer_refresh._safe_url`.
pub fn safe_url(url: &str) -> Option<ParsedUrl> {
    let check = || -> Option<ParsedUrl> {
        if url.is_empty() || url.chars().count() > 1000 {
            return None;
        }
        if url.chars().any(|c| (c as u32) <= 0x20 || c == '\\') {
            return None;
        }
        let parsed = match parse_url(url) {
            Ok(parsed) => parsed,
            Err(_) => return None,
        };
        let host = parsed.hostname.as_ref()?;
        if parsed.scheme != "https"
            || parsed.username.is_some()
            || parsed.password.is_some()
        {
            return None;
        }
        match parsed.port() {
            Ok(None) | Ok(Some(443)) => {}
            _ => return None,
        }
        if allowed_host_merchant(host).is_none() {
            return None;
        }
        let pairs = parse_qsl(parsed.query_string(), true, 50).ok()?;
        let stripped = host.strip_prefix("www.").unwrap_or(host);
        if PUBLIC_SOURCES.iter().any(|(apex, _)| *apex == stripped)
            && public_source(url).is_none()
        {
            return None;
        }
        if stripped == "pazarama.com" && !pazarama_product_url(url) {
            return None;
        }
        if host == TURK_TELEKOM_HOST && !turk_telekom_product_url(url) {
            return None;
        }
        let sensitive = regex::Regex::new(SENSITIVE_KEY_RE).unwrap();
        if pairs.iter().any(|(key, _)| sensitive.is_match(key)) {
            return None;
        }
        variant_query(&parsed).ok()?;
        if is_ip_literal(host) {
            return None;
        }
        Some(parsed)
    };
    check()
}

/// `retailer_offer_refresh._same_resource`.
pub fn same_resource(first: &str, second: &str) -> bool {
    same_resource_mode(first, second, false)
}

/// Test-only variant used by the permissive fetch engine: same structural
/// comparison against relaxed URL checks (wiremock runs plain HTTP on
/// localhost, which `_safe_url` would reject).
pub fn same_resource_permissive(first: &str, second: &str) -> bool {
    same_resource_mode(first, second, true)
}

fn same_resource_mode(first: &str, second: &str, permissive: bool) -> bool {
    let joined = match urljoin_str(second, first) {
        Ok(joined) => joined,
        Err(_) => return false,
    };
    let a = parse_url(&joined).ok().map(|p| p.geturl_without_fragment());
    let b = parse_url(second).ok().map(|p| p.geturl_without_fragment());
    let (Some(a), Some(b)) = (a, b) else {
        return false;
    };
    let check = |url: &str| {
        if permissive {
            permissive_safe_url(url)
        } else {
            safe_url(url)
        }
    };
    let (Some(pa), Some(pb)) = (check(&a), check(&b)) else {
        return false;
    };
    if permissive {
        // Structural equivalence against non-manifest hosts: same host
        // (www-insensitive), same path, same selected variant query.
        let host_a = pa.hostname.as_deref().unwrap_or("").strip_prefix("www.").unwrap_or(pa.hostname.as_deref().unwrap_or(""));
        let host_b = pb.hostname.as_deref().unwrap_or("").strip_prefix("www.").unwrap_or(pb.hostname.as_deref().unwrap_or(""));
        return host_a == host_b
            && pa.path.trim_end_matches('/') == pb.path.trim_end_matches('/')
            && variant_query(&pa).ok() == variant_query(&pb).ok();
    }
    let merchant_a = allowed_host_merchant(pa.hostname.as_deref().unwrap_or(""));
    let merchant_b = allowed_host_merchant(pb.hostname.as_deref().unwrap_or(""));
    if merchant_a.is_none() || merchant_a != merchant_b {
        return false;
    }
    if pa.path.trim_end_matches('/') != pb.path.trim_end_matches('/') {
        return false;
    }
    match (variant_query(&pa), variant_query(&pb)) {
        (Ok(va), Ok(vb)) => va == vb,
        _ => false,
    }
}

/// Test-only relaxation of `_safe_url` for the wiremock fetch engine: accepts
/// plain HTTP and any host (wiremock runs on `http://127.0.0.1:port`) while
/// keeping the structural variant-query validation, so redirect and
/// `unexpected_final_destination` parity stay testable.
pub fn permissive_safe_url(url: &str) -> Option<ParsedUrl> {
    if url.is_empty() || url.chars().count() > 1000 {
        return None;
    }
    if url.chars().any(|c| (c as u32) <= 0x20 || c == '\\') {
        return None;
    }
    let parsed = parse_url(url).ok()?;
    parsed.hostname.as_ref()?;
    // Keep the structural variant validation so ambiguous selected variants
    // fail closed exactly as `_safe_url` does in production.
    variant_query(&parsed).ok()?;
    if matches!(parsed.scheme.as_str(), "http" | "https") {
        Some(parsed)
    } else {
        None
    }
}

/// Which URL-faith layer a parse call uses. Production code always uses
/// `Manifest`; wiremock tests run plain HTTP on localhost, which the manifest
/// gate would reject, so the test engine switches the whole pipeline to
/// `Permissive`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UrlTrust {
    /// Production: `_safe_url` / `_same_resource` / `ALLOWED_HOSTS`.
    Manifest,
    /// Test-only relaxation for `http://127.0.0.1:port` targets.
    Permissive,
}

impl UrlTrust {
    pub fn check(&self, url: &str) -> Option<ParsedUrl> {
        match self {
            UrlTrust::Manifest => safe_url(url),
            UrlTrust::Permissive => permissive_safe_url(url),
        }
    }

    pub fn same_resource(&self, first: &str, second: &str) -> bool {
        match self {
            UrlTrust::Manifest => same_resource(first, second),
            UrlTrust::Permissive => same_resource_permissive(first, second),
        }
    }

    pub fn merchant(&self, host: &str) -> Option<&'static str> {
        match self {
            UrlTrust::Manifest => allowed_host_merchant(host),
            UrlTrust::Permissive => match host {
                "127.0.0.1" | "localhost" | "::1" => Some("Mock Merchant"),
                _ => allowed_host_merchant(host),
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn allowed_hosts_parity() {
        assert_eq!(allowed_host_merchant("www.n11.com"), Some("n11"));
        assert_eq!(allowed_host_merchant("n11.com"), Some("n11"));
        assert_eq!(
            allowed_host_merchant("www.hepsiburada.com"),
            Some("Hepsiburada")
        );
        assert_eq!(allowed_host_merchant("pt.com.tr"), Some("Pozitif Teknoloji"));
        assert_eq!(
            allowed_host_merchant("emagaza.turktelekom.com.tr"),
            Some("Türk Telekom eMağaza")
        );
        assert_eq!(allowed_host_merchant("evil.com"), None);
        assert_eq!(allowed_host_merchant("sub.n11.com"), None);
    }

    #[test]
    fn safe_url_accepts_clean_pdp() {
        let parsed = safe_url("https://www.hepsiburada.com/apple-iphone-15-p-HBCV00004AB?q=utm_source=x").unwrap();
        assert_eq!(parsed.hostname.as_deref(), Some("www.hepsiburada.com"));
    }

    #[test]
    fn safe_url_rejects_bad_inputs() {
        assert!(safe_url("http://www.hepsiburada.com/x-p-HBCV1").is_none());
        assert!(safe_url("https://user:pw@www.hepsiburada.com/x-p-H1").is_none());
        assert!(safe_url("https://www.hepsiburada.com:8443/x-p-H1").is_none());
        assert!(safe_url("https://evil.com/x-p-H1").is_none());
        assert!(safe_url("https://127.0.0.1/x-p-H1").is_none());
        // >1000 chars
        let long = format!("https://www.hepsiburada.com/{}-p-H1", "a".repeat(1200));
        assert!(safe_url(&long).is_none());
        // control characters / backslash
        assert!(safe_url("https://www.hepsiburada.com/\\x-p-H1").is_none());
    }

    #[test]
    fn safe_url_rejects_sensitive_query_keys() {
        assert!(safe_url("https://www.hepsiburada.com/x-p-H1?token=abc").is_none());
        assert!(safe_url("https://www.hepsiburada.com/x-p-H1?sessionid=abc").is_none());
        assert!(safe_url("https://www.hepsiburada.com/x-p-H1?add-to-cart=1").is_none());
        assert!(safe_url("https://www.hepsiburada.com/x-p-H1?nonce=abc").is_none());
        // Ordinary variant keys pass.
        assert!(safe_url("https://www.hepsiburada.com/x-p-H1?sku=12345").is_some());
    }

    #[test]
    fn variant_query_parity() {
        let parsed = parse_url("https://www.vatanbilgisayar.com/x.html?ram=8&STORAGE=256&renk=siyah").unwrap();
        let selected = variant_query(&parsed).unwrap();
        assert_eq!(selected.get("ram").map(String::as_str), Some("8"));
        assert_eq!(selected.get("storage").map(String::as_str), Some("256"));
        // Ambiguous repeated variant with different values must fail.
        let parsed = parse_url("https://www.vatanbilgisayar.com/x.html?ram=8&ram=16").unwrap();
        assert!(variant_query(&parsed).is_err());
        // Empty value fails.
        let parsed = parse_url("https://www.vatanbilgisayar.com/x.html?ram=").unwrap();
        assert!(variant_query(&parsed).is_err());
        // Repeated identical value is accepted.
        let parsed = parse_url("https://www.vatanbilgisayar.com/x.html?ram=8&ram=8").unwrap();
        assert_eq!(variant_query(&parsed).unwrap().len(), 1);
    }

    #[test]
    fn public_source_parity() {
        assert_eq!(public_source("https://pt.com.tr/iphone-15").as_deref(), Some("pt.com.tr"));
        assert_eq!(
            public_source("https://www.troyestore.com/iphone").as_deref(),
            Some("troyestore.com")
        );
        assert_eq!(public_source("https://pt.com.tr/iphone?ref=x"), None);
        assert_eq!(public_source("http://pt.com.tr/iphone"), None);
        assert_eq!(public_source("https://pt.com.tr/iphone#top"), None);
        assert_eq!(public_source("https://hepsiburada.com/x-p-H1"), None);
        assert_eq!(public_source("https://pt.com.tr/%2fiphone"), None);
    }

    #[test]
    fn pazarama_product_url_parity() {
        assert!(pazarama_product_url("https://www.pazarama.com/apple-iphone-15-256gb-p-HB-1234"));
        assert!(!pazarama_product_url("https://www.pazarama.com/apple-iphone-15-256gb-p-HB-1234?q=1"));
        assert!(!pazarama_product_url("https://www.pazarama.com/kategori/cep-telefonu"));
        assert!(!pazarama_product_url("http://www.pazarama.com/x-p-1"));
    }

    #[test]
    fn turk_telekom_product_url_parity() {
        assert!(turk_telekom_product_url(
            "https://emagaza.turktelekom.com.tr/tr/products/iphone-15/iphone-15-128gb?v=12345"
        ));
        assert!(!turk_telekom_product_url(
            "https://emagaza.turktelekom.com.tr/tr/products/iphone-15?v=12"
        ));
        assert!(!turk_telekom_product_url(
            "https://emagaza.turktelekom.com.tr/tr/products/iphone-15/iphone-15-128gb?v=99999999999"
        ));
    }

    #[test]
    fn same_resource_parity() {
        let base = "https://www.hepsiburada.com/apple-iphone-15-p-HBCV1";
        assert!(same_resource(base, base));
        // Identical variant queries on both sides count as the same resource.
        assert!(same_resource(
            "https://www.hepsiburada.com/apple-iphone-15-p-HBCV1?sku=100",
            "https://www.hepsiburada.com/apple-iphone-15-p-HBCV1?sku=100"
        ));
        assert!(!same_resource(
            "https://www.hepsiburada.com/apple-iphone-15-p-HBCV1?sku=100",
            base
        ));
        assert!(!same_resource(
            "https://www.hepsiburada.com/apple-iphone-16-p-HBCV2",
            base
        ));
        assert!(!same_resource(
            "https://www.trendyol.com/apple-iphone-15-p-HBCV1",
            base
        ));
    }
}
