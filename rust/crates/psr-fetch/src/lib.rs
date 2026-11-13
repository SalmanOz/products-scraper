//! Fetch layer: the generic URL walk from `retailer_offer_refresh._fetch_target`,
//! host transport circuits and robots-checked gates.

pub mod circuit;
pub mod engine;
pub mod robots;

pub use circuit::{HostCircuits, TRANSIENT_CIRCUIT_COOLDOWN_SECONDS, TRANSIENT_FAILURE_THRESHOLD};
pub use engine::{AttemptRecord, AttemptReason, FetchEngine, MAX_BODY_BYTES};
pub use robots::{GateError, GatePage, RobotRules, RobotsGate, BOT};
