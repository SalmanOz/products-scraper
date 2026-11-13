//! Port of `RetailerOfferRefresher._fetch_target` (retailer_offer_refresh.py
//! lines 706-929): the generic URL walk — safe-URL gate, per-URL cache,
//! manual bounded redirects with variant preservation, 403/429/challenge host
//! blocking, transient 5xx circuit, 5 MB body cap, challenge detection and
//! JSON-LD offer parsing.
//!
//! Deferred (fail-closed, phase 2/3):
//! - dedicated catalog readers (hepsiburada/trendyol/n11/teknosa/mediamarkt/
//!   apple/turktelekom/pasaj) answer `dedicated_reader_pending`;
//! - gate denials surface as `transport_error` in the engine diagnostics,
//!   exactly like the Python `except requests.RequestException` handler; the
//!   detailed gate reason stays in the gate's own report.

use std::collections::{BTreeMap, HashMap};
use std::time::Duration;
use tokio::sync::Mutex;

use psr_core::offer::OfferObservation;
use psr_core::purl::{urljoin_str, ParsedUrl};
use psr_core::url::{
    pazarama_product_url, permissive_safe_url, public_source, same_resource,
    same_resource_permissive, safe_url, variant_query,
};
use psr_parse::{is_challenge_page, product_offer_document, product_offer_permissive};

use crate::circuit::{HostCircuits, TRANSIENT_CIRCUIT_COOLDOWN_SECONDS};
use crate::retailer_gate::RetailerGate;
use crate::vatan::VatanReviewedSource;

/// `retailer_offer_refresh._MAX_BODY_BYTES`.
pub const MAX_BODY_BYTES: usize = 5 * 1024 * 1024;

const REDIRECT_STATUSES: [u16; 5] = [301, 302, 303, 307, 308];

/// Reasons recorded in the per-URL diagnostics. Strings match the Python
/// `_record_attempt` vocabulary exactly.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AttemptReason {
    AccessDenied,
    RateLimited,
    RetailerUnavailable,
    HostCircuitOpen,
    UnsafeOrChangedVariantRedirect,
    RedirectLimitOrMissingLocation,
    UnexpectedFinalDestination,
    InvalidOrOversizedResponse,
    ChallengePage,
    HttpError,
    TransportError,
    InvalidResponse,
    NoExactProductOffer,
    ObservedExactOffer,
    DedicatedReaderPending,
    ReviewedUrlRedirect,
}

impl AttemptReason {
    pub fn as_str(&self) -> &'static str {
        match self {
            AttemptReason::AccessDenied => "access_denied",
            AttemptReason::RateLimited => "rate_limited",
            AttemptReason::RetailerUnavailable => "retailer_unavailable",
            AttemptReason::HostCircuitOpen => "host_circuit_open",
            AttemptReason::UnsafeOrChangedVariantRedirect => "unsafe_or_changed_variant_redirect",
            AttemptReason::RedirectLimitOrMissingLocation => "redirect_limit_or_missing_location",
            AttemptReason::UnexpectedFinalDestination => "unexpected_final_destination",
            AttemptReason::InvalidOrOversizedResponse => "invalid_or_oversized_response",
            AttemptReason::ChallengePage => "challenge_page",
            AttemptReason::HttpError => "http_error",
            AttemptReason::TransportError => "transport_error",
            AttemptReason::InvalidResponse => "invalid_response",
            AttemptReason::NoExactProductOffer => "no_exact_product_offer",
            AttemptReason::ObservedExactOffer => "observed_exact_offer",
            AttemptReason::DedicatedReaderPending => "dedicated_reader_pending",
            AttemptReason::ReviewedUrlRedirect => "reviewed_url_redirect",
        }
    }
}

/// Python `_record_attempt` row: `{'reason': …, 'http_status': …}`.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct AttemptRecord {
    pub reason: String,
    pub http_status: Option<u16>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub transport: Option<String>,
}

#[derive(Debug)]
struct FetchedPage {
    status: u16,
    body: String,
    /// `Location` response header (None = absent or empty, which Python's
    /// falsy-string handling collapses to None as well).
    location: Option<String>,
    /// Python `getattr(response, 'url', None) or current`.
    final_url: String,
}

/// The transport outcome of one hop, mirroring the Python exception classes.
enum Hop {
    Page(FetchedPage),
    /// `requests.RequestException` — transport errors and every gate denial
    /// (all gates raise RequestException subclasses).
    Transport,
}

/// Generic-path fetch engine. Shared across the whole run so the cache and
/// host circuits persist across products, exactly like reusing the Python
/// `RetailerOfferRefresher` instance.
pub struct FetchEngine {
    http: reqwest::Client,
    public_gate: RetailerGate,
    pazarama_gate: RetailerGate,
    vatan: VatanReviewedSource,
    circuits: HostCircuits,
    cache: Mutex<HashMap<String, Option<OfferObservation>>>,
    diagnostics: Mutex<HashMap<String, AttemptRecord>>,
    permissive: bool,
}

impl FetchEngine {
    /// Python `RetailerOfferRefresher.TRANSIENT_CIRCUIT_COOLDOWN_SECONDS`.
    pub const TRANSIENT_CIRCUIT_COOLDOWN: Duration =
        Duration::from_secs(TRANSIENT_CIRCUIT_COOLDOWN_SECONDS);

    /// Production engine: `_safe_url` trust gate enforced.
    pub fn new(http: reqwest::Client) -> Self {
        Self::build(http, false)
    }

    /// Test engine: accepts plain-HTTP localhost targets (wiremock) while
    /// keeping the walk logic identical.
    #[doc(hidden)]
    pub fn permissive_for_tests(http: reqwest::Client) -> Self {
        Self::build(http, true)
    }

    fn build(http: reqwest::Client, permissive: bool) -> Self {
        Self {
            http: http.clone(),
            public_gate: RetailerGate::public(http.clone()),
            pazarama_gate: RetailerGate::pazarama(http),
            vatan: VatanReviewedSource::new(),
            circuits: HostCircuits::new(),
            cache: Mutex::new(HashMap::new()),
            diagnostics: Mutex::new(HashMap::new()),
            permissive,
        }
    }

    fn check_url(&self, url: &str) -> Option<ParsedUrl> {
        if self.permissive {
            permissive_safe_url(url)
        } else {
            safe_url(url)
        }
    }

    fn same_resource(&self, first: &str, second: &str) -> bool {
        if self.permissive {
            same_resource_permissive(first, second)
        } else {
            same_resource(first, second)
        }
    }

    /// Redirect-destination acceptance. Production relies on `_safe_url`'s
    /// manifest check; the permissive test layer has no manifest, so it
    /// enforces the equivalent invariant that a redirect never leaves the
    /// current host family.
    fn destination_ok(&self, destination: &str, current_apex: &str) -> bool {
        let Some(parsed) = self.check_url(destination) else {
            return false;
        };
        if !self.permissive {
            return true;
        }
        let host = parsed.hostname.as_deref().unwrap_or("");
        let apex = host.strip_prefix("www.").unwrap_or(host);
        apex == current_apex
    }

    async fn record(&self, source_url: &str, reason: AttemptReason, status: Option<u16>) {
        self.record_reason(source_url, reason.as_str(), status).await;
    }

    async fn record_reason(&self, source_url: &str, reason: &str, status: Option<u16>) {
        self.diagnostics.lock().await.insert(
            source_url.to_string(),
            AttemptRecord {
                reason: reason.to_string(),
                http_status: status,
                transport: None,
            },
        );
    }

    pub async fn diagnostics(&self, source_url: &str) -> Option<AttemptRecord> {
        self.diagnostics.lock().await.get(source_url).cloned()
    }

    /// The engine's trust mode (production manifest gate vs permissive
    /// wiremock mode).
    pub fn is_permissive(&self) -> bool {
        self.permissive
    }

    /// Python `refresh_existing_offer_targets` marks non-selected URLs that
    /// were never attempted with `target_budget_skipped` so no report row
    /// ever looks like a successful observation.
    pub async fn record_skipped(&self, source_url: &str) {
        let mut diagnostics = self.diagnostics.lock().await;
        diagnostics
            .entry(source_url.to_string())
            .or_insert_with(|| AttemptRecord {
                reason: "target_budget_skipped".to_string(),
                http_status: None,
                transport: None,
            });
    }

    pub async fn cached(&self, source_url: &str) -> Option<Option<OfferObservation>> {
        self.cache.lock().await.get(source_url).cloned()
    }

    /// Python `_fetch_target`.
    pub async fn fetch_target(&self, source_url: &str) -> Option<OfferObservation> {
        // Lines 707-709: an unsafe URL returns None before any state changes.
        let parsed = self.check_url(source_url)?;
        {
            let cache = self.cache.lock().await;
            if let Some(cached) = cache.get(source_url) {
                return cached.clone();
            }
        }

        // Lines 713-831: preflight files and dedicated source readers are
        // phase-2/3 work. Fail closed for their hosts instead of walking.
        if is_dedicated_reader_host(source_url) {
            self.record(source_url, AttemptReason::DedicatedReaderPending, None)
                .await;
            self.cache
                .lock()
                .await
                .insert(source_url.to_string(), None);
            return None;
        }

        self.walk(source_url, &parsed).await
    }

    /// Python lines 832-929.
    async fn walk(&self, source_url: &str, parsed: &ParsedUrl) -> Option<OfferObservation> {
        // `_safe_url` already validated the variant query, so this cannot
        // fail for a checked URL.
        let requested_variant: BTreeMap<String, String> =
            variant_query(parsed).unwrap_or_default();
        let mut current = source_url.to_string();
        let mut candidate: Option<OfferObservation> = None;
        let mut browser_response = false;

        self.record(source_url, AttemptReason::NoExactProductOffer, None)
            .await;

        for redirects in 0..4usize {
            let Some(parsed) = self.check_url(&current) else {
                break;
            };
            let reviewed_vatan = !self.permissive && VatanReviewedSource::is_reviewed(&current);
            if reviewed_vatan {
                if let Err(reason) = self.vatan.prepare(&current).await {
                    self.record_reason(source_url, reason, None).await;
                    break;
                }
            }
            let host = host_key(&parsed);
            if !reviewed_vatan && self.circuits.is_blocked(&host).await {
                self.record(source_url, AttemptReason::HostCircuitOpen, None)
                    .await;
                break;
            }

            match self.transport(&current).await {
                Hop::Transport => {
                    self.record(source_url, AttemptReason::TransportError, None)
                        .await;
                    self.circuits
                        .record_transport_failure(&host, Self::TRANSIENT_CIRCUIT_COOLDOWN)
                        .await;
                    break;
                }
                Hop::Page(mut page) => {
                    if page.status == 403 && reviewed_vatan {
                        match self.vatan.browser_get(&current).await {
                            Ok(html) => {
                                browser_response = true;
                                page = FetchedPage {
                                    status: 200,
                                    body: html,
                                    location: None,
                                    final_url: current.clone(),
                                };
                            }
                            Err(reason) => {
                                self.record_reason(source_url, &reason, Some(403)).await;
                                self.circuits.block_host(&host).await;
                                break;
                            }
                        }
                    }
                    let status = page.status;
                    if status == 403 || status == 429 {
                        self.record(
                            source_url,
                            if status == 403 {
                                AttemptReason::AccessDenied
                            } else {
                                AttemptReason::RateLimited
                            },
                            Some(status),
                        )
                        .await;
                        self.circuits.block_host(&host).await;
                        break;
                    }
                    if matches!(status, 500 | 502 | 503 | 504) {
                        self.record(source_url, AttemptReason::RetailerUnavailable, Some(status))
                            .await;
                        self.circuits
                            .record_transport_failure(&host, Self::TRANSIENT_CIRCUIT_COOLDOWN)
                            .await;
                        break;
                    }
                    if REDIRECT_STATUSES.contains(&status) {
                        if reviewed_vatan {
                            self.record(source_url, AttemptReason::ReviewedUrlRedirect, Some(status))
                                .await;
                            break;
                        }
                        if redirects == 3 || page.location.as_deref().unwrap_or("").is_empty() {
                            self.record(
                                source_url,
                                AttemptReason::RedirectLimitOrMissingLocation,
                                Some(status),
                            )
                            .await;
                            break;
                        }
                        let location = page.location.unwrap_or_default();
                        let destination = match urljoin_str(&current, &location) {
                            Ok(destination) => destination,
                            Err(_) => {
                                // Python urljoin ValueError lands in the
                                // (TypeError, ValueError) handler.
                                self.record(source_url, AttemptReason::InvalidResponse, None)
                                    .await;
                                break;
                            }
                        };
                        let rejected = !self.destination_ok(&destination, &host)
                            || (!requested_variant.is_empty()
                                && variant_destination_differs(&destination, &requested_variant))
                            || (public_source(source_url).is_some()
                                && public_source(&destination) != public_source(source_url))
                            || (pazarama_product_url(source_url)
                                && !pazarama_product_url(&destination));
                        if rejected {
                            self.record(
                                source_url,
                                AttemptReason::UnsafeOrChangedVariantRedirect,
                                Some(status),
                            )
                            .await;
                            break;
                        }
                        current = destination;
                        continue;
                    }
                    if status != 200 {
                        self.record(source_url, AttemptReason::HttpError, Some(status))
                            .await;
                        break;
                    }
                    if self.check_url(&page.final_url).is_none()
                        || !self.same_resource(&page.final_url, &current)
                    {
                        self.record(
                            source_url,
                            AttemptReason::UnexpectedFinalDestination,
                            Some(status),
                        )
                        .await;
                        break;
                    }
                    let html = page.body;
                    if html.len() > MAX_BODY_BYTES {
                        self.record(
                            source_url,
                            AttemptReason::InvalidOrOversizedResponse,
                            Some(status),
                        )
                        .await;
                        break;
                    }
                    // Success resets the transient failure counter and clears
                    // the cooldown (Python lines 913-916). The reset happens
                    // before the parse so the parsed document never spans an
                    // await point; the observable host state is identical
                    // because a challenge still ends in a permanent block.
                    self.circuits.record_success(&host).await;
                    let (challenge, offer_now) =
                        process_page(&html, &page.final_url, self.permissive);
                    if challenge {
                        self.record(source_url, AttemptReason::ChallengePage, Some(status))
                            .await;
                        self.circuits.block_host(&host).await;
                        break;
                    }
                    candidate = offer_now;
                    self.record(
                        source_url,
                        if candidate.is_some() {
                            AttemptReason::ObservedExactOffer
                        } else {
                            AttemptReason::NoExactProductOffer
                        },
                        Some(status),
                    )
                    .await;
                    break;
                }
            }
        }

        if browser_response {
            if let Some(attempt) = self.diagnostics.lock().await.get_mut(source_url) {
                attempt.transport = Some("vatan_browser".to_string());
            }
        }
        self.cache
            .lock()
            .await
            .insert(source_url.to_string(), candidate.clone());
        candidate
    }

    /// One transport hop: gate-routed for public sources and Pazarama, a
    /// plain GET shaped like `requests.get(url, timeout=(5, 15),
    /// allow_redirects=False, headers={'Accept': 'text/html',
    /// 'Accept-Language': 'tr-TR,tr;q=0.9'})` otherwise.
    async fn transport(&self, url: &str) -> Hop {
        if public_source(url).is_some() {
            return self.gate_hop(&self.public_gate, url).await;
        }
        if pazarama_product_url(url) {
            return self.gate_hop(&self.pazarama_gate, url).await;
        }
        let response = self
            .http
            .get(url)
            .header("Accept", "text/html")
            .header("Accept-Language", "tr-TR,tr;q=0.9")
            .send()
            .await;
        let Ok(response) = response else {
            return Hop::Transport;
        };
        let status = response.status().as_u16();
        let location = response
            .headers()
            .get("location")
            .and_then(|value| value.to_str().ok())
            .map(|value| value.to_string())
            .filter(|value| !value.is_empty());
        let mut final_url = response.url().to_string();
        if final_url.is_empty() {
            final_url = url.to_string();
        }
        match response.text().await {
            Ok(body) => Hop::Page(FetchedPage {
                status,
                body,
                location,
                final_url,
            }),
            Err(_) => Hop::Transport,
        }
    }

    async fn gate_hop(&self, gate: &RetailerGate, url: &str) -> Hop {
        match gate.fetch(url).await {
            Ok(page) => Hop::Page(FetchedPage {
                status: page.status,
                body: page.body,
                location: page.location,
                final_url: url.to_string(),
            }),
            // Every gate denial is a `requests.RequestException` in Python:
            // the engine records `transport_error` and counts a transient
            // failure; the detailed reason stays in the gate report.
            Err(_) => Hop::Transport,
        }
    }
}

/// Python `_fetch_target` routes these exact hosts to dedicated readers
/// before the generic walk (lines 750-831). Their readers are phase-3 work;
/// fail closed meanwhile.
fn is_dedicated_reader_host(source_url: &str) -> bool {
    let Ok(parsed) = psr_core::parse_url(source_url) else {
        return false;
    };
    let Some(host) = parsed.hostname.as_deref() else {
        return false;
    };
    matches!(
        host,
        "www.hepsiburada.com"
            | "www.trendyol.com"
            | "www.n11.com"
            | "www.teknosa.com"
            | "www.mediamarkt.com.tr"
            | "mediamarkt.com.tr"
            | "www.apple.com"
            | "apple.com"
            | "emagaza.turktelekom.com.tr"
            | "www.turkcell.com.tr"
            | "turkcell.com.tr"
    )
}

fn host_key(parsed: &ParsedUrl) -> String {
    let host = parsed.hostname.as_deref().unwrap_or("");
    host.strip_prefix("www.").unwrap_or(host).to_string()
}

/// One 200-page process: challenge detection and offer extraction, entirely
/// synchronous so the parsed document never enters the walk's async state.
fn process_page(
    html: &str,
    final_url: &str,
    permissive: bool,
) -> (bool, Option<OfferObservation>) {
    let document = scraper::Html::parse_document(html);
    if is_challenge_page(&document) {
        return (true, None);
    }
    let candidate = if permissive {
        // The permissive engine re-derives the merchant from the mock trust
        // layer.
        product_offer_permissive(html, final_url)
    } else {
        product_offer_document(&document, final_url)
    };
    (false, candidate)
}

/// Python `_variant_query(destination_url) != requested_variant` — the
/// destination is already URL-checked, so the parse and variant query are
/// total; treat the impossible failure as a difference (fail closed).
fn variant_destination_differs(destination: &str, requested: &BTreeMap<String, String>) -> bool {
    psr_core::parse_url(destination)
        .ok()
        .map(|parsed| variant_query(&parsed).unwrap_or_default() != *requested)
        .unwrap_or(true)
}
