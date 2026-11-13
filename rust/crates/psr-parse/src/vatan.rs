//! Evidence for the selected first-party Vatan offer beside other variants.

use std::sync::LazyLock;

use psr_core::url::{safe_url, same_resource};
use regex::Regex;
use scraper::{ElementRef, Html, Selector};
use serde_json::Value;

use crate::product_offer::{element_text_stripped, unescape_html};

fn vatan_url(url: &str) -> Option<psr_core::purl::ParsedUrl> {
    let parsed = safe_url(url)?;
    matches!(
        parsed.hostname.as_deref(),
        Some("www.vatanbilgisayar.com" | "vatanbilgisayar.com")
    )
    .then_some(parsed)
}

fn sku_text(product: &Value) -> Option<String> {
    match product.get("sku")? {
        Value::String(sku) => Some(sku.clone()),
        Value::Number(sku) => Some(sku.to_string()),
        _ => None,
    }
}

fn exactly_one<'a>(doc: &'a Html, selector: &str) -> Option<ElementRef<'a>> {
    let selector = Selector::parse(selector).expect("Vatan selector");
    let mut matches = doc.select(&selector);
    let element = matches.next()?;
    matches.next().is_none().then_some(element)
}

pub(crate) fn selected_variant(
    doc: &Html,
    product: &Value,
    offer: &Value,
    final_url: &str,
    price: f64,
    availability: &str,
) -> bool {
    selected_variant_evidence(doc, product, offer, final_url, price, availability).is_some()
}

fn selected_variant_evidence(
    doc: &Html,
    product: &Value,
    offer: &Value,
    final_url: &str,
    price: f64,
    availability: &str,
) -> Option<()> {
    static SKU: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[0-9]{5,7}$").unwrap());
    static BUY: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)\bSepete Ekle\b").unwrap());
    static CASH: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"(?i)^([0-9]{1,3}(?:\.[0-9]{3})*(?:,[0-9]{1,2})?)\s*(?:TL|₺)$").unwrap()
    });

    let parsed = vatan_url(final_url)?;
    let condition = offer.get("itemCondition")?.as_str()?.trim_end_matches('/');
    if !parsed.query_string().is_empty()
        || availability != "in_stock"
        || condition.rsplit('/').next() != Some("NewCondition")
        || offer.get("url")?.as_str()? != final_url
    {
        return None;
    }
    let seller = offer.get("seller")?;
    if seller.get("name")?.as_str()? != "Vatan Bilgisayar" {
        return None;
    }
    vatan_url(seller.get("url")?.as_str()?)?;

    let sku = sku_text(product).filter(|sku| SKU.is_match(sku))?;
    let variants = product.get("hasVariant")?.as_array()?;
    if variants.is_empty() {
        return None;
    }
    for variant in variants {
        let url = variant.get("url")?.as_str()?;
        if !vatan_url(url)?.query_string().is_empty()
            || same_resource(url, final_url)
            || sku_text(variant).as_deref() == Some(&sku)
        {
            return None;
        }
    }

    let heading = exactly_one(doc, ".product-detail h1.product-list__product-name")?;
    let code = exactly_one(doc, ".product-detail .product-id")?;
    let cart = exactly_one(
        doc,
        ".product-detail button#add-to-cart-button.detail-cart-button.basketBTN",
    )?;
    let title = unescape_html(product.get("name")?.as_str()?);
    let sku_in_code = Regex::new(&format!(r"/\s*{sku}\b")).expect("numeric SKU");
    if title.split_whitespace().collect::<Vec<_>>().join(" ") != element_text_stripped(heading)
        || !sku_in_code.is_match(&element_text_stripped(code))
        || cart.value().attr("disabled").is_some()
        || cart.value().classes().any(|class| class == "disabled")
        || cart
            .value()
            .attr("aria-disabled")
            .is_some_and(|value| value.eq_ignore_ascii_case("true"))
        || !BUY.is_match(&element_text_stripped(cart))
    {
        return None;
    }

    let web_price = Selector::parse(".product-detail .web-ozel-prod").expect("Vatan selector");
    let price_selector = if doc.select(&web_price).next().is_some() {
        ".product-detail .web-ozel-prod .web-ozel-fiyat"
    } else {
        ".product-detail .product-detail-price"
    };
    let visible = element_text_stripped(exactly_one(doc, price_selector)?);
    let amount = CASH.captures(&visible)?.get(1)?.as_str();
    let cash = amount
        .replace('.', "")
        .replace(',', ".")
        .parse::<f64>()
        .ok()?;
    (price == cash).then_some(())
}

#[cfg(test)]
mod tests {
    use crate::product_offer;
    use serde_json::json;

    const URL: &str =
        "https://www.vatanbilgisayar.com/xiaomi-17t-12-256gb-akilli-telefon-koyu-mor.html";

    #[test]
    fn repaired_vatan_reviews_still_require_selected_cash_and_stock() {
        let schema = json!({
            "@type": "Product",
            "name": "Xiaomi 17T 12/256GB Ak&#x131;ll&#x131; Telefon Mor",
            "sku": "157948",
            "hasVariant": [{
                "@type": "Product",
                "sku": "157949",
                "url": "https://www.vatanbilgisayar.com/xiaomi-17t-12-256gb-akilli-telefon-beyaz.html"
            }],
            "offers": {
                "@type": "Offer",
                "url": URL,
                "price": "54999",
                "priceCurrency": "TRY",
                "availability": "https://schema.org/InStock",
                "itemCondition": "https://schema.org/NewCondition",
                "seller": {"name": "Vatan Bilgisayar", "url": "https://www.vatanbilgisayar.com"}
            },
            "review": {"reviewBody": "Line one\nLine two"}
        });
        let schema = schema.to_string().replace("one\\nLine", "one\nLine");
        let html = format!(
            r#"
            <script type="application/ld+json">{schema}</script>
            <main class="product-detail">
              <h1 class="product-list__product-name">Xiaomi 17T 12/256GB Akıllı Telefon Mor</h1>
              <div class="product-id">17T_Mor / 157948</div>
              <span class="product-detail-price">54.999 TL</span>
              <button id="add-to-cart-button" class="detail-cart-button basketBTN">Sepete Ekle</button>
            </main>"#
        );
        let observed = product_offer(&html, URL).unwrap();
        assert_eq!(observed.price, 54_999.0);
        assert_eq!(observed.availability, "in_stock");
        assert!(product_offer(&html.replace("54.999 TL", "53.999 TL"), URL).is_none());
        assert!(product_offer(&html.replace("<button", "<button disabled"), URL).is_none());
        assert!(product_offer(&html, "https://www.idefix.com/urun/xiaomi").is_none());
    }
}
