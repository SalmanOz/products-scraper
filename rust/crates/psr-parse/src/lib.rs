//! HTML → offer observation parsers and challenge detection.

pub mod jsonld;
pub mod product_offer;
mod vatan;

pub use jsonld::{is_truthy, py_number_string, schema_nodes, type_is};
pub use product_offer::{
    document_text, is_challenge_page, is_conditional_price, product_offer,
    product_offer_document, product_offer_permissive, unescape_html,
};
