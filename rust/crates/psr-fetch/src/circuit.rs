//! Port of `RetailerOfferRefresher` per-host transport state:
//! `_blocked_hosts`, `_transport_failures` and `_transient_blocked_until`.
//!
//! A transient 5xx or connection failure clears during a long catalog run;
//! explicit 403/429/challenge denials stay blocked for the entire run.

use std::collections::{HashMap, HashSet};
use std::time::{Duration, Instant};
use tokio::sync::Mutex;

pub const TRANSIENT_CIRCUIT_COOLDOWN_SECONDS: u64 = 5 * 60;
pub const TRANSIENT_FAILURE_THRESHOLD: u32 = 2;

#[derive(Debug, Default)]
pub struct HostCircuits {
    inner: Mutex<Inner>,
}

#[derive(Debug, Default)]
struct Inner {
    blocked_hosts: HashSet<String>,
    transport_failures: HashMap<String, u32>,
    transient_blocked_until: HashMap<String, Instant>,
}

impl HostCircuits {
    pub fn new() -> Self {
        Self::default()
    }

    fn strip_www(host: &str) -> String {
        host.strip_prefix("www.").unwrap_or(host).to_string()
    }

    /// Python `_transport_failure(host)`.
    pub async fn record_transport_failure(&self, host: &str, cooldown: Duration) {
        let host = Self::strip_www(host);
        let mut inner = self.inner.lock().await;
        let failures = inner.transport_failures.entry(host.clone()).or_insert(0);
        *failures += 1;
        if *failures >= TRANSIENT_FAILURE_THRESHOLD {
            inner
                .transient_blocked_until
                .insert(host, Instant::now() + cooldown);
        }
    }

    /// Success resets the transient failure counter and clears the cooldown.
    pub async fn record_success(&self, host: &str) {
        let host = Self::strip_www(host);
        let mut inner = self.inner.lock().await;
        inner.transport_failures.remove(&host);
        inner.transient_blocked_until.remove(&host);
    }

    /// Explicit denial blocks the host for the whole run.
    pub async fn block_host(&self, host: &str) {
        let host = Self::strip_www(host);
        self.inner.lock().await.blocked_hosts.insert(host);
    }

    /// Python `blocked` check: permanent denial or an open transient cooldown.
    pub async fn is_blocked(&self, host: &str) -> bool {
        let host = Self::strip_www(host);
        let inner = self.inner.lock().await;
        inner.blocked_hosts.contains(&host)
            || inner
                .transient_blocked_until
                .get(&host)
                .map(|until| *until > Instant::now())
                .unwrap_or(false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn transient_cooldown_opens_after_two_failures() {
        let circuits = HostCircuits::new();
        circuits.record_transport_failure("www.example.com", Duration::from_secs(60)).await;
        assert!(!circuits.is_blocked("example.com").await);
        circuits.record_transport_failure("example.com", Duration::from_secs(60)).await;
        assert!(circuits.is_blocked("www.example.com").await);
        // A success clears the cooldown.
        circuits.record_success("www.example.com").await;
        assert!(!circuits.is_blocked("example.com").await);
    }

    #[tokio::test]
    async fn explicit_denial_is_permanent_for_the_run() {
        let circuits = HostCircuits::new();
        circuits.block_host("www.example.com").await;
        circuits.record_success("www.example.com").await;
        assert!(circuits.is_blocked("example.com").await);
    }
}
