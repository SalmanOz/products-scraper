//! Bounded GET transport for public retailer and Pazarama product pages.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::Mutex;

pub const BOT: &str = "TeknoskorPriceBot";
pub const PUBLIC_MAX_REQUESTS_PER_SOURCE: usize = 150;
pub const PAZARAMA_MAX_PDP_REQUESTS: usize = 36;

const USER_AGENT: &str = "TeknoskorPriceBot/1.0 (+https://teknoskor.com)";

#[derive(Debug, Clone)]
pub struct GatePage {
    pub status: u16,
    pub body: String,
    /// `Location` header (None when absent or empty), needed by the engine's
    /// manual redirect walk for gate-fetched responses.
    pub location: Option<String>,
}

#[derive(Debug)]
pub enum GateError {
    /// Python `RequestException('Not a clean reviewed public URL')`.
    NotCleanUrl,
    /// `Source circuit/budget open`.
    CircuitOpen,
    TransportError,
    /// `Oversized public response`.
    OversizedResponse,
}

impl GateError {
    pub fn reason(&self) -> &'static str {
        match self {
            GateError::NotCleanUrl => "not_a_clean_url",
            GateError::CircuitOpen => "circuit_or_budget_open",
            GateError::TransportError => "transport_error",
            GateError::OversizedResponse => "oversized_response",
        }
    }
}

#[derive(Debug, Default)]
struct HostInner {
    blocked: bool,
    counts: usize,
    last: Option<Instant>,
    pages: HashMap<String, GatePage>,
}

#[derive(Debug, Default)]
struct HostState {
    inner: Mutex<HostInner>,
}

/// GET gate with per-host pacing, request limits and a page cache.
pub struct RetailerGate {
    http: reqwest::Client,
    host_apex: Option<String>,
    max_requests: usize,
    max_body_bytes: usize,
    strict_entry: bool,
    hosts: Mutex<HashMap<String, Arc<HostState>>>,
}

impl RetailerGate {
    /// Gate for the reviewed public Apple sources (pt.com.tr, troyestore.com).
    pub fn public(http: reqwest::Client) -> Self {
        Self::custom(http, None, PUBLIC_MAX_REQUESTS_PER_SOURCE, 5 * 1024 * 1024)
            .with_strict_entry(true)
    }

    /// Gate for Pazarama PDP reads (budget 36, 2 MB body cap).
    pub fn pazarama(http: reqwest::Client) -> Self {
        Self::custom(
            http,
            Some("pazarama.com".to_string()),
            PAZARAMA_MAX_PDP_REQUESTS,
            2 * 1024 * 1024,
        )
        .with_strict_entry(true)
    }

    #[doc(hidden)]
    pub fn custom(
        http: reqwest::Client,
        host_apex: Option<String>,
        max_requests: usize,
        max_body_bytes: usize,
    ) -> Self {
        Self {
            http,
            host_apex,
            max_requests,
            max_body_bytes,
            strict_entry: false,
            hosts: Mutex::new(HashMap::new()),
        }
    }

    fn with_strict_entry(mut self, strict: bool) -> Self {
        self.strict_entry = strict;
        self
    }

    pub async fn fetch(&self, url: &str) -> Result<GatePage, GateError> {
        let parsed = psr_core::parse_url(url).map_err(|_| GateError::NotCleanUrl)?;
        let host = parsed.hostname.as_deref().ok_or(GateError::NotCleanUrl)?;
        let apex = host.strip_prefix("www.").unwrap_or(host);
        if self
            .host_apex
            .as_deref()
            .is_some_and(|expected| expected != apex)
        {
            return Err(GateError::NotCleanUrl);
        }
        if self.strict_entry {
            let accepted = if self.host_apex.is_some() {
                psr_core::url::pazarama_product_url(url)
            } else {
                psr_core::url::public_source(url).is_some()
            };
            if !accepted {
                return Err(GateError::NotCleanUrl);
            }
        }

        let state = {
            let mut map = self.hosts.lock().await;
            map.entry(apex.to_string())
                .or_insert_with(|| Arc::new(HostState::default()))
                .clone()
        };

        let mut inner = state.inner.lock().await;
        if let Some(page) = inner.pages.get(url) {
            return Ok(page.clone());
        }
        let page = self.request_locked(&mut inner, url).await?;
        inner.pages.insert(url.to_string(), page.clone());
        Ok(page)
    }

    async fn request_locked(
        &self,
        inner: &mut HostInner,
        url: &str,
    ) -> Result<GatePage, GateError> {
        if inner.blocked || inner.counts >= self.max_requests {
            return Err(GateError::CircuitOpen);
        }
        let delay = Duration::from_millis(500);
        if let Some(last) = inner.last {
            tokio::time::sleep(delay.saturating_sub(last.elapsed())).await;
        }
        inner.counts += 1;
        inner.last = Some(Instant::now());

        let response = self
            .http
            .get(url)
            .header("User-Agent", USER_AGENT)
            .header("Accept", "text/html")
            .header("Accept-Language", "tr-TR,tr;q=0.9")
            .send()
            .await;
        let response = match response {
            Ok(response) => response,
            Err(_) => {
                inner.blocked = true;
                return Err(GateError::TransportError);
            }
        };
        let status = response.status().as_u16();
        let location = response
            .headers()
            .get("location")
            .and_then(|value| value.to_str().ok())
            .map(|value| value.to_string())
            .filter(|value| !value.is_empty());
        let body = match response.text().await {
            Ok(body) => body,
            Err(_) => {
                inner.blocked = true;
                return Err(GateError::TransportError);
            }
        };
        if matches!(status, 403 | 429 | 500 | 502 | 503 | 504) {
            inner.blocked = true;
        }
        if body.len() > self.max_body_bytes {
            return Err(GateError::OversizedResponse);
        }
        Ok(GatePage {
            status,
            body,
            location,
        })
    }
}
