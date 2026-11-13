//! Parity tests for the generic `_product_offer` reader, built from synthetic
//! retailer-shaped HTML documents.

use psr_parse::{is_challenge_page, is_conditional_price, product_offer, schema_nodes, unescape_html};
use scraper::Html;
use serde_json::json;

const BASE: &str = "https://www.idefix.com/urun/apple-iphone-15-128-gb-p-1001";

fn clean_pdp(price: &str) -> String {
    format!(
        r#"<html><head><title>Apple iPhone 15 128 GB</title>
        <script type="application/ld+json">{json}</script></head><body></body></html>"#,
        json = json!({
            "@context": "https://schema.org",
            "@type": "Product",
            "name": "Apple iPhone 15 128 GB",
            "url": BASE,
            "sku": "1001",
            "offers": {
                "@type": "Offer",
                "url": BASE,
                "price": price,
                "priceCurrency": "TRY",
                "availability": "https://schema.org/InStock",
                "itemCondition": "https://schema.org/NewCondition",
            },
        })
        .to_string()
            .replace('\\', "\\\\")
    )
}

#[test]
fn accepts_exact_selected_offer() {
    let html = clean_pdp("34999.00");
    let offer = product_offer(&html, BASE).expect("offer must be accepted");
    assert_eq!(offer.merchant, "idefix");
    assert_eq!(offer.price, 34999.00);
    assert_eq!(offer.currency, "TRY");
    assert_eq!(offer.availability, "in_stock");
    assert_eq!(offer.source_title, "Apple iPhone 15 128 GB");
    assert_eq!(offer.url, BASE);
}

#[test]
fn integer_price_shape() {
    let html = clean_pdp("34999");
    let offer = product_offer(&html, BASE).unwrap();
    assert_eq!(offer.price, 34999.0);
}

#[test]
fn rejects_aggregate_offer() {
    let json = json!({
        "@type": "Product",
        "name": "Apple iPhone 15 128 GB",
        "url": BASE,
        "offers": {"@type": "AggregateOffer", "lowPrice": 1000, "highPrice": 9000, "priceCurrency": "TRY"},
    });
    let html = format!(r#"<html><head><script type="application/ld+json">{json}</script></head><body></body></html>"#);
    assert!(product_offer(&html, BASE).is_none());
}

#[test]
fn rejects_two_products_without_link() {
    let json = json!([
        {"@type": "Product", "name": "A", "offers": {"@type": "Offer", "price": "10", "priceCurrency": "TRY", "availability": "https://schema.org/InStock"}},
        {"@type": "Product", "name": "B", "offers": {"@type": "Offer", "price": "20", "priceCurrency": "TRY", "availability": "https://schema.org/InStock"}},
    ]);
    let html = format!(r#"<html><head><script type="application/ld+json">{json}</script></head><body></body></html>"#);
    assert!(product_offer(&html, BASE).is_none());
}

#[test]
fn rejects_non_try_currency() {
    let json = json!({
        "@type": "Product", "name": "X", "url": BASE,
        "offers": {"@type": "Offer", "price": "100", "priceCurrency": "USD", "availability": "https://schema.org/InStock"},
    });
    let html = format!(r#"<html><head><script type="application/ld+json">{json}</script></head><body></body></html>"#);
    assert!(product_offer(&html, BASE).is_none());
}

#[test]
fn rejects_missing_availability() {
    let json = json!({
        "@type": "Product", "name": "X", "url": BASE,
        "offers": {"@type": "Offer", "price": "100", "priceCurrency": "TRY"},
    });
    let html = format!(r#"<html><head><script type="application/ld+json">{json}</script></head><body></body></html>"#);
    assert!(product_offer(&html, BASE).is_none());
}

#[test]
fn rejects_variant_product_outside_vatan() {
    // hasVariant products only pass through the dedicated Vatan reader.
    let json = json!({
        "@type": "Product", "name": "X", "url": BASE,
        "hasVariant": [{"@type": "ProductVariant", "url": "https://www.idefix.com/other-p-2"}],
        "offers": {"@type": "Offer", "price": "100", "priceCurrency": "TRY", "availability": "https://schema.org/InStock"},
    });
    let html = format!(r#"<html><head><script type="application/ld+json">{json}</script></head><body></body></html>"#);
    assert!(product_offer(&html, BASE).is_none());
}

#[test]
fn rejects_offer_url_mismatch() {
    let json = json!({
        "@type": "Product", "name": "X", "url": BASE,
        "offers": {"@type": "Offer", "url": "https://www.idefix.com/other-p-999", "price": "100", "priceCurrency": "TRY", "availability": "https://schema.org/InStock"},
    });
    let html = format!(r#"<html><head><script type="application/ld+json">{json}</script></head><body></body></html>"#);
    assert!(product_offer(&html, BASE).is_none());
}

#[test]
fn rejects_out_of_price_range_and_bad_shape() {
    for bad in ["0", "-5", "1e5", "12,5", "9999999"] {
        let json = json!({
            "@type": "Product", "name": "X", "url": BASE,
            "offers": {"@type": "Offer", "price": bad, "priceCurrency": "TRY", "availability": "https://schema.org/InStock"},
        });
        let html = format!(r#"<html><head><script type="application/ld+json">{json}</script></head><body></body></html>"#);
        assert!(product_offer(&html, BASE).is_none(), "price {bad} must be rejected");
    }
}

#[test]
fn rejects_mediamarkt_and_teknosa_hosts() {
    let html = clean_pdp("100");
    assert!(product_offer(&html, "https://www.mediamarkt.com.tr/x-p-M1").is_none());
    assert!(product_offer(&html, "https://www.teknosa.com/x-p-1234567890123").is_none());
}

#[test]
fn challenge_page_detection() {
    let captcha = r#"<html><head><title>Attention Required!</title></head>
    <body><h1>Access denied</h1></body></html>"#;
    let doc = Html::parse_document(captcha);
    assert!(is_challenge_page(&doc));

    let turkish = r#"<html><body><p>Alışverişe devam etmek için butona tıklayın</p></body></html>"#;
    let doc = Html::parse_document(turkish);
    assert!(is_challenge_page(&doc));

    let normal = r#"<html><head><title>Apple iPhone 15</title></head><body><h1>iPhone 15</h1></body></html>"#;
    let doc = Html::parse_document(normal);
    assert!(!is_challenge_page(&doc));
}

#[test]
fn conditional_price_detection() {
    let page = r#"<html><body><div>Sepette 3.299,00 TL</div></body></html>"#;
    let doc = Html::parse_document(page);
    assert!(is_conditional_price(&doc, &json!({}), 3299.00));
    assert!(!is_conditional_price(&doc, &json!({}), 4000.0));

    let label_offer = json!({"name": "Sepette indirimli fiyat"});
    assert!(is_conditional_price(&doc, &label_offer, 3299.00));

    // Ordinary page with no conditional markers.
    let plain = r#"<html><body><div>34.999,00 TL</div></body></html>"#;
    let doc = Html::parse_document(plain);
    assert!(!is_conditional_price(&doc, &json!({}), 34999.00));
}

#[test]
fn unescape_parity() {
    assert_eq!(unescape_html("A&amp;B &lt;tag&gt; &#39;q&#39;"), "A&B <tag> 'q'");
    assert_eq!(unescape_html("Ka&#305;t"), "Kaıt");
    assert_eq!(unescape_html("plain"), "plain");
    assert_eq!(unescape_html("a &notreal b"), "a &notreal b");
}

#[test]
fn schema_nodes_skips_malformed() {
    let html = r#"<html><head>
    <script type="application/ld+json">{"@type":"Product"}</script>
    <script type="application/ld+json">{broken</script>
    </head></html>"#;
    let doc = Html::parse_document(html);
    let nodes = schema_nodes(&doc);
    assert_eq!(nodes.len(), 1);
}
