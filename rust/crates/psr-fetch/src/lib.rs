//! Fetch layer: the generic URL walk from `retailer_offer_refresh._fetch_target`,
//! host transport circuits and bounded retailer gates.

pub mod circuit;
pub mod engine;
pub mod retailer_gate;
mod vatan;

pub use circuit::{HostCircuits, TRANSIENT_CIRCUIT_COOLDOWN_SECONDS, TRANSIENT_FAILURE_THRESHOLD};
pub use engine::{AttemptRecord, AttemptReason, FetchEngine, MAX_BODY_BYTES};
pub use retailer_gate::{GateError, GatePage, RetailerGate, BOT};
