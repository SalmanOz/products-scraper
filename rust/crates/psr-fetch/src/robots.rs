//! Robots-aware fetch gate — port of `public_retailer_sources.PublicRetailerSources`
//! (public Apple sources) and `pazarama_category_source`'s robots-checked PDP
//! reader. GET only; redirect destinations are independently robots-checked by
//! the engine.
//!
//! Python holds one RLock across all gate fetches; here the serialization is
//! per host (each host's counts/blocked/pacing live behind its own mutex), so
//! parallel merchant buckets do not wait on each other's crawl-delay sleeps.
//! The rule parser is a faithful subset of CPython 3.15
//! `urllib.robotparser` (group merging, first-matching-line-wins allowance,
//! `match`-anchored wildcard translation, `$` fullmatch, `/robots.txt`
//! implicit allow). Percent-normalization of rule paths is skipped — it is
//! identity for the ASCII paths the scraper targets.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::Mutex;

pub const BOT: &str = "TeknoskorPriceBot";
pub const PUBLIC_MAX_REQUESTS_PER_SOURCE: usize = 150;
pub const PAZARAMA_MAX_PDP_REQUESTS: usize = 36;

const USER_AGENT: &str = "TeknoskorPriceBot/1.0 (+https://teknoskor.com)";

#[derive(Debug, Clone)]
struct Rule {
    allow: bool,
    path: String,
    fullmatch: bool,
    matcher: Option<regex::Regex>,
}

/// Python `translate_pattern`: escaped parts split on `*`; middle parts
/// become lazy `(?>.*?part)` groups (plain lazy groups are equivalent here —
/// only wildcards follow), the last part gets a greedy `.*` prefix. The
/// matcher is anchored at the start (`match`) or both ends (`fullmatch`).
fn translate_pattern(path: &str) -> String {
    let parts: Vec<String> = path.split('*').map(regex::escape).collect();
    let mut out = parts[0].clone();
    for part in parts.iter().take(parts.len().saturating_sub(1)).skip(1) {
        out.push_str(&format!("(?:.*?{part})"));
    }
    if parts.len() > 1 {
        out.push_str(&format!(".*{}", parts[parts.len() - 1]));
    }
    out
}

impl Rule {
    /// Python `RuleLine.__init__` — `None` when the line is unusable
    /// (a `$` not at the end of the path).
    fn new(path: &str, allow: bool) -> Option<Self> {
        if path.is_empty() && !allow {
            // An empty value means allow all.
            return Some(Self {
                allow: true,
                path: String::new(),
                fullmatch: false,
                matcher: None,
            });
        }
        // `re.sub(r'[*]{2,}', '*', path)` and `re.sub(r'[$][$*]+', '$', path)`.
        let mut path = collapse_repeats(path);
        let fullmatch = path.ends_with('$');
        path = path.trim_end_matches('$').to_string();
        if path.contains('$') {
            return None;
        }
        let matcher = if path.contains('*') {
            Some(regex::Regex::new(&format!("(?s){}", translate_pattern(&path))).ok()?)
        } else {
            None
        };
        Some(Self {
            allow,
            path,
            fullmatch,
            matcher,
        })
    }

    /// Python `RuleLine.applies_to` — match length + 1, or 0 when no match.
    fn match_length(&self, path: &str) -> usize {
        if let Some(matcher) = &self.matcher {
            let Some(found) = matcher.find(path) else {
                return 0;
            };
            if self.fullmatch {
                if found.start() == 0 && found.end() == path.len() {
                    return found.end() + 1;
                }
                return 0;
            }
            if found.start() == 0 {
                return found.end() + 1;
            }
            return 0;
        }
        if self.fullmatch {
            if path == self.path {
                return self.path.len() + 1;
            }
            0
        } else if path.starts_with(&self.path) {
            self.path.len() + 1
        } else {
            0
        }
    }
}

fn collapse_repeats(path: &str) -> String {
    // `[*]{2,}` → `*`, `[$][$*]+` → `$`.
    let mut out = String::with_capacity(path.len());
    let mut chars = path.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '*' {
            out.push('*');
            while chars.peek() == Some(&'*') {
                chars.next();
            }
        } else if c == '$' {
            if chars.peek().is_some_and(|next| *next == '$' || *next == '*') {
                out.push('$');
                while chars.peek().is_some_and(|next| *next == '$' || *next == '*') {
                    chars.next();
                }
            } else {
                out.push('$');
            }
        } else {
            out.push(c);
        }
    }
    out
}

#[derive(Debug, Clone, Default)]
struct Group {
    agents: Vec<String>,
    rules: Vec<Rule>,
    crawl_delay: Option<f64>,
    request_rate: Option<(f64, u32)>, // (seconds, requests)
}

impl Group {
    /// Python `Entry.applies_to`: the passed agent is truncated at `/`,
    /// lowercased, and a file token matches when it is a substring.
    /// The `*` token never matches here — it is only the `_find_entry`
    /// fallback.
    fn applies_to(&self, useragent: &str) -> bool {
        let token = useragent.split('/').next().unwrap_or("").to_lowercase();
        self.agents
            .iter()
            .any(|agent| agent != "*" && token.contains(&agent.to_lowercase()))
    }

    /// Python `Entry.allowance`: longest match wins; on equal length the
    /// Disallow side wins the tie.
    fn allowance(&self, path: &str) -> bool {
        let mut best_match: usize = 0;
        let mut allowance = true;
        for rule in &self.rules {
            let length = rule.match_length(path);
            if length == 0 {
                continue;
            }
            if length > best_match {
                best_match = length;
                allowance = rule.allow;
            } else if length == best_match && !allowance {
                allowance = rule.allow;
            }
        }
        allowance
    }
}

/// Python `merge_entries`: shared agents, concatenated rules, the newer
/// delay/rate wins when set.
fn merge_groups(existing: &Group, entry: &Group) -> Group {
    Group {
        agents: existing
            .agents
            .iter()
            .filter(|agent| entry.agents.contains(agent))
            .cloned()
            .collect(),
        rules: existing
            .rules
            .iter()
            .chain(entry.rules.iter())
            .cloned()
            .collect(),
        crawl_delay: if entry.crawl_delay.is_none() {
            existing.crawl_delay
        } else {
            entry.crawl_delay
        },
        request_rate: if entry.request_rate.is_none() {
            existing.request_rate
        } else {
            entry.request_rate
        },
    }
}

#[derive(Debug, Clone, Default)]
pub struct RobotRules {
    /// Python `self.groups`: lowercase agent → merged entry, in insertion
    /// order.
    groups: Vec<(String, Group)>,
}

impl RobotRules {
    /// Parse a robots.txt document — a faithful subset of
    /// `RobotFileParser.parse` (Python 3.15).
    pub fn parse(text: &str) -> Self {
        let mut entries: Vec<Group> = Vec::new();
        let mut state = 0; // 0 start, 1 saw user-agent, 2 saw a rule line
        let mut current = Group::default();
        for raw_line in text.lines() {
            let line = raw_line.split('#').next().unwrap_or("").trim().to_string();
            if line.is_empty() {
                continue;
            }
            let Some((field_raw, value_raw)) = line.split_once(':') else {
                continue;
            };
            let field = field_raw.trim().to_lowercase();
            let value = value_raw.trim().to_string();
            match field.as_str() {
                "user-agent" => {
                    if state == 2 {
                        entries.push(std::mem::take(&mut current));
                    }
                    current.agents.push(value);
                    state = 1;
                }
                "disallow" if state != 0 => {
                    state = 2;
                    if let Some(rule) = Rule::new(&value, false) {
                        current.rules.push(rule);
                    }
                }
                "allow" if state != 0 => {
                    state = 2;
                    if let Some(rule) = Rule::new(&value, true) {
                        current.rules.push(rule);
                    }
                }
                "crawl-delay" if state != 0 => {
                    state = 2;
                    if value.chars().all(|c| c.is_ascii_digit()) && !value.is_empty() {
                        current.crawl_delay = value.parse::<f64>().ok();
                    }
                }
                "request-rate" if state != 0 => {
                    state = 2;
                    let numbers: Vec<&str> = value.split('/').collect();
                    let valid = |text: &str| {
                        !text.is_empty() && text.chars().all(|c| c.is_ascii_digit())
                    };
                    if numbers.len() == 2 && valid(numbers[0].trim()) && valid(numbers[1].trim()) {
                        // Python RequestRate(requests, seconds).
                        let seconds = numbers[1].trim().parse::<f64>().ok();
                        let requests = numbers[0].trim().parse::<u32>().ok();
                        if let (Some(seconds), Some(requests)) = (seconds, requests) {
                            current.request_rate = Some((seconds, requests));
                        }
                    }
                }
                "sitemap" => {
                    // Independent of the user-agent line; no state change.
                }
                _ => {
                    // Unknown fields do not change the state.
                }
            }
        }
        if state != 0 {
            entries.push(current);
        }
        // `RobotFileParser._add_entry`: merge per lowercase agent key.
        let mut groups: Vec<(String, Group)> = Vec::new();
        for entry in &entries {
            for agent in &entry.agents {
                let key = agent.to_lowercase();
                if let Some(existing) =
                    groups.iter_mut().find(|(group_key, _)| *group_key == key)
                {
                    existing.1 = merge_groups(&existing.1, entry);
                } else {
                    groups.push((key, entry.clone()));
                }
            }
        }
        Self { groups }
    }

    /// Python `_find_entry`: exact key, then the first entry whose non-`*`
    /// token is a substring of the agent token, then the `*` group.
    fn find_entry(&self, useragent: &str) -> Option<&Group> {
        let lowered = useragent.to_lowercase();
        if let Some((_, group)) = self.groups.iter().find(|(key, _)| *key == lowered) {
            return Some(group);
        }
        for (_, group) in &self.groups {
            if group.applies_to(useragent) {
                return Some(group);
            }
        }
        self.groups
            .iter()
            .find(|(key, _)| key == "*")
            .map(|(_, group)| group)
    }

    /// `RobotFileParser.can_fetch` subset. The URL is reduced to its
    /// path+query+fragment; percent-normalization is skipped (identity for
    /// the ASCII paths the scraper targets).
    pub fn can_fetch(&self, agent: &str, url: &str) -> bool {
        let Ok(parsed) = psr_core::parse_url(url) else {
            return true;
        };
        let mut path = parsed.path.clone();
        if let Some(query) = &parsed.query {
            path.push('?');
            path.push_str(query);
        }
        if let Some(fragment) = &parsed.fragment {
            path.push('#');
            path.push_str(fragment);
        }
        if path.is_empty() {
            path = "/".to_string();
        }
        // The /robots.txt URI is implicitly allowed.
        if path == "/robots.txt" {
            return true;
        }
        match self.find_entry(agent) {
            Some(group) => group.allowance(&path),
            None => true,
        }
    }

    pub fn crawl_delay(&self, agent: &str) -> Option<f64> {
        self.find_entry(agent).and_then(|group| group.crawl_delay)
    }

    pub fn request_rate(&self, agent: &str) -> Option<(f64, u32)> {
        self.find_entry(agent).and_then(|group| group.request_rate)
    }
}

/// The robots policy looked like an HTML error page, not a robots document.
fn looks_like_html(text: &str) -> bool {
    regex::Regex::new(r"(?i)<(?:!doctype\s+html|html|head|body)\b")
        .map(|re| re.is_match(text))
        .unwrap_or(false)
}

fn has_user_agent_line(text: &str) -> bool {
    regex::Regex::new(r"(?im)^\s*user-agent\s*:").unwrap().is_match(text)
}

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
    /// `Robots policy unavailable`.
    RobotsPolicyUnavailable,
    /// `Robots disallows URL`.
    RobotsDisallowed,
    /// `Source crawl delay exceeds run budget`.
    CrawlDelayTooLong,
    TransportError,
    /// `Oversized public response`.
    OversizedResponse,
}

impl GateError {
    pub fn reason(&self) -> &'static str {
        match self {
            GateError::NotCleanUrl => "not_a_clean_url",
            GateError::CircuitOpen => "circuit_or_budget_open",
            GateError::RobotsPolicyUnavailable => "robots_policy_unavailable",
            GateError::RobotsDisallowed => "robots_disallowed",
            GateError::CrawlDelayTooLong => "crawl_delay_exceeds_budget",
            GateError::TransportError => "transport_error",
            GateError::OversizedResponse => "oversized_response",
        }
    }
}

#[derive(Debug, Default)]
struct HostInner {
    robots: Option<RobotRules>,
    blocked: bool,
    counts: usize,
    last: Option<Instant>,
    pages: HashMap<String, GatePage>,
}

#[derive(Debug, Default)]
struct HostState {
    inner: Mutex<HostInner>,
}

/// Robots-checked GET gate. `host_apex` `None` = reviewed public sources (any
/// `public_source` URL, per-host robots); `Some(apex)` = a fixed host family
/// (Pazarama PDPs). Production constructors enforce the entry contract;
/// `custom` (tests) relaxes it for wiremock localhost targets.
pub struct RobotsGate {
    http: reqwest::Client,
    host_apex: Option<String>,
    robots_url_override: Option<String>,
    max_requests: usize,
    max_body_bytes: usize,
    strict_entry: bool,
    hosts: Mutex<HashMap<String, Arc<HostState>>>,
}

impl RobotsGate {
    /// Gate for the reviewed public Apple sources (pt.com.tr, troyestore.com).
    pub fn public(http: reqwest::Client) -> Self {
        Self::custom(http, None, None, PUBLIC_MAX_REQUESTS_PER_SOURCE, 5 * 1024 * 1024)
            .with_strict_entry(true)
    }

    /// Gate for Pazarama PDP reads (budget 36, 2 MB body cap).
    pub fn pazarama(http: reqwest::Client) -> Self {
        Self::custom(
            http,
            Some("www.pazarama.com".to_string()),
            Some("https://www.pazarama.com/robots.txt".to_string()),
            PAZARAMA_MAX_PDP_REQUESTS,
            2 * 1024 * 1024,
        )
        .with_strict_entry(true)
    }

    #[doc(hidden)]
    pub fn custom(
        http: reqwest::Client,
        host_apex: Option<String>,
        robots_url_override: Option<String>,
        max_requests: usize,
        max_body_bytes: usize,
    ) -> Self {
        Self {
            http,
            host_apex,
            robots_url_override,
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

    fn hosts(&self, url: &str) -> Option<(String, String)> {
        let parsed = psr_core::parse_url(url).ok()?;
        let host = parsed.hostname.clone()?;
        if let Some(apex) = &self.host_apex {
            let bare_apex = apex.strip_prefix("www.").unwrap_or(apex);
            if host != *apex && host.strip_prefix("www.").unwrap_or(&host) != bare_apex {
                return None;
            }
        }
        let apex = host.strip_prefix("www.").unwrap_or(&host).to_string();
        let robots_url = match &self.robots_url_override {
            Some(explicit) => explicit.clone(),
            None => format!("https://www.{apex}/robots.txt"),
        };
        Some((apex, robots_url))
    }

    /// Fetch with robots gate, per-host pacing and a per-URL page cache.
    pub async fn fetch(&self, url: &str) -> Result<GatePage, GateError> {
        if self.strict_entry {
            match (&self.host_apex, self.robots_url_override.is_some()) {
                (None, false) => {
                    if psr_core::url::public_source(url).is_none() {
                        return Err(GateError::NotCleanUrl);
                    }
                }
                (Some(_), true) => {
                    if !psr_core::url::pazarama_product_url(url) {
                        return Err(GateError::NotCleanUrl);
                    }
                }
                _ => {}
            }
        }
        let (apex, robots_url) = self.hosts(url).ok_or(GateError::NotCleanUrl)?;

        let state = {
            let mut map = self.hosts.lock().await;
            map.entry(apex.clone())
                .or_insert_with(|| Arc::new(HostState::default()))
                .clone()
        };

        // Python holds the gate RLock across the whole `fetch`; here the same
        // phases run under one per-host lock hold (robots policy, can_fetch,
        // page cache, target GET). Crawl-delay sleeps inside that hold
        // serialize concurrent requests to the same host.
        let mut inner = state.inner.lock().await;
        if inner.robots.is_none() {
            let robots_page = self.request_locked(&mut inner, &robots_url, true).await?;
            // Some retailers return their HTML 404/challenge with status 200
            // at robots.txt. RobotFileParser would otherwise interpret that
            // as an empty allow-all policy.
            let valid = robots_page.status == 200
                && !looks_like_html(&robots_page.body)
                && has_user_agent_line(&robots_page.body);
            if !valid {
                inner.blocked = true;
                return Err(GateError::RobotsPolicyUnavailable);
            }
            inner.robots = Some(RobotRules::parse(&robots_page.body));
        }
        let rules = inner.robots.clone().unwrap_or_default();
        if !rules.can_fetch(BOT, url) {
            return Err(GateError::RobotsDisallowed);
        }
        if let Some(page) = inner.pages.get(url) {
            return Ok(page.clone());
        }
        let page = self.request_locked(&mut inner, url, false).await?;
        inner.pages.insert(url.to_string(), page.clone());
        Ok(page)
    }

    /// Python `PublicRetailerSources._request`: budget check, pacing, count,
    /// transport, denial blocking and the body cap — for the robots document
    /// and the target page alike. Requires the host lock to be held.
    async fn request_locked(
        &self,
        inner: &mut HostInner,
        url: &str,
        is_robots: bool,
    ) -> Result<GatePage, GateError> {
        if inner.blocked || inner.counts >= self.max_requests {
            return Err(GateError::CircuitOpen);
        }
        // Pacing: crawl delay, request rate and a 0.5s floor. While the
        // policy is still unknown (the robots fetch itself) only the floor
        // applies, exactly like `self._robots.get(host)` returning None.
        let mut interval = 0.5f64;
        if let Some(rules) = &inner.robots {
            if let Some(delay) = rules.crawl_delay(BOT) {
                interval = interval.max(delay);
            }
            if let Some((seconds, requests)) = rules.request_rate(BOT) {
                interval = interval.max(seconds / requests.max(1) as f64);
            }
        }
        if interval > 30.0 {
            return Err(GateError::CrawlDelayTooLong);
        }
        if let Some(last) = inner.last {
            let elapsed = last.elapsed().as_secs_f64();
            if interval > elapsed {
                tokio::time::sleep(Duration::from_secs_f64(interval - elapsed)).await;
            }
        }
        inner.counts += 1;
        inner.last = Some(Instant::now());

        let accept = if is_robots { "text/plain" } else { "text/html" };
        let response = self
            .http
            .get(url)
            .header("User-Agent", USER_AGENT)
            .header("Accept", accept)
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

#[cfg(test)]
mod tests {
    use super::*;

    fn rules(text: &str) -> RobotRules {
        RobotRules::parse(text)
    }

    #[test]
    fn basic_parsing_and_matching() {
        let robots = rules(
            "User-agent: *\nDisallow: /private/\nAllow: /private/ok\nCrawl-delay: 2\n",
        );
        assert!(robots.can_fetch(BOT, "https://www.example.com/public/page"));
        assert!(!robots.can_fetch(BOT, "https://www.example.com/private/x"));
        assert!(robots.can_fetch(BOT, "https://www.example.com/private/ok"));
        assert_eq!(robots.crawl_delay(BOT), Some(2.0));
    }

    #[test]
    fn wildcard_and_anchor() {
        // `/*\.pdf$` in the file — fullmatch on the wildcard translation.
        let robots = rules("User-agent: *\nDisallow: /*.pdf$\n");
        assert!(!robots.can_fetch(BOT, "https://e.com/a/b.pdf"));
        assert!(robots.can_fetch(BOT, "https://e.com/a/b.pdfx"));
    }

    #[test]
    fn specific_agent_group_wins() {
        let robots = rules(
            "User-agent: TeknoskorPriceBot\nDisallow: /\n\nUser-agent: *\nDisallow:\n",
        );
        assert!(!robots.can_fetch(BOT, "https://e.com/anything"));
        // Other agents fall back to the '*' group which allows everything.
        assert!(robots.can_fetch("Googlebot", "https://e.com/anything"));
    }

    #[test]
    fn no_rules_means_allow() {
        let robots = rules("");
        assert!(robots.can_fetch(BOT, "https://e.com/"));
    }

    #[test]
    fn empty_disallow_allows_all() {
        let robots = rules("User-agent: *\nDisallow:\n");
        assert!(robots.can_fetch(BOT, "https://e.com/private/secret"));
    }

    #[test]
    fn robots_txt_itself_is_implicitly_allowed() {
        let robots = rules("User-agent: *\nDisallow: /\n");
        assert!(robots.can_fetch(BOT, "https://e.com/robots.txt"));
    }

    #[test]
    fn longest_match_wins_and_allow_wins_ties() {
        // Later specific Allow beats the shorter Disallow, like Python 3.15.
        let robots = rules(
            "User-agent: *\nDisallow: /private/\nAllow: /private/ok\n",
        );
        assert!(robots.can_fetch(BOT, "https://www.example.com/private/ok"));
        assert!(!robots.can_fetch(BOT, "https://www.example.com/private/x"));
        // Equal-length Allow/Disallow lines: Allow wins the tie in both
        // orders (verified against CPython 3.15 RobotFileParser).
        let robots = rules(
            "User-agent: *\nDisallow: /a/b\nAllow: /a/b\n",
        );
        assert!(robots.can_fetch(BOT, "https://e.com/a/b"));
        let robots = rules(
            "User-agent: *\nAllow: /a/b\nDisallow: /a/b\n",
        );
        assert!(robots.can_fetch(BOT, "https://e.com/a/b"));
    }

    #[test]
    fn wildcard_middle_pattern_anchored_at_start() {
        let robots = rules("User-agent: *\nDisallow: /*/product/*\n");
        assert!(!robots.can_fetch(BOT, "https://e.com/shop/product/x"));
        assert!(robots.can_fetch(BOT, "https://e.com/product/x"));
    }

    #[test]
    fn same_agent_sections_merge() {
        let robots = rules(
            "User-agent: *\nDisallow: /a/\n\nUser-agent: Googlebot\nDisallow: /\n\nUser-agent: *\nDisallow: /b/\n",
        );
        // Both '*' sections merge into one group: /a/ and /b/ both blocked.
        assert!(!robots.can_fetch(BOT, "https://e.com/a/x"));
        assert!(!robots.can_fetch(BOT, "https://e.com/b/y"));
        assert!(robots.can_fetch(BOT, "https://e.com/c/z"));
    }

    #[test]
    fn crawl_delay_must_be_an_integer() {
        assert_eq!(rules("User-agent: *\nCrawl-delay: 2.5\n").crawl_delay(BOT), None);
        assert_eq!(rules("User-agent: *\nCrawl-delay: 7\n").crawl_delay(BOT), Some(7.0));
    }
}
