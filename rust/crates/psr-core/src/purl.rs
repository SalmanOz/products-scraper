//! Python `urllib.parse` compatible URL parsing subset.
//!
//! The Python scraper validates every retailer URL through `urlparse` with
//! strict assumptions (lowercased hostname, explicit port checks, fragment
//! presence, control-character tolerance). WHATWG-style parsers normalise too
//! aggressively for parity, so we mirror CPython's `urlparse`/`parse_qsl`/
//! `urljoin` for the subset the scraper actually relies on.

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum UrlError {
    #[error("Port could not be cast to integer value as {0:?}")]
    InvalidPort(String),
    #[error("Port out of range 0-65535")]
    PortOutOfRange,
    #[error("Query string contains too many fields")]
    TooManyFields,
    #[error("Invalid IPv6 URL")]
    InvalidIpv6,
    #[error("Invalid URL: {0:?}")]
    InvalidUrl(String),
}

/// CPython `SplitResult` equivalent. `query`/`fragment` track presence
/// (`geturl` must reproduce a bare `?`/`#`), while truthiness checks treat
/// `None` and `""` the same way Python code does.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedUrl {
    pub scheme: String,
    pub netloc: String,
    pub hostname: Option<String>,
    port_raw: Option<String>,
    pub username: Option<String>,
    pub password: Option<String>,
    pub path: String,
    pub query: Option<String>,
    pub fragment: Option<String>,
}

fn is_scheme_start(c: char) -> bool {
    c.is_ascii_alphabetic()
}

fn is_scheme_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '+' || c == '-' || c == '.'
}

fn hex_val(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

/// CPython removes ASCII tab/newline/CR from URLs before parsing.
pub fn strip_unsafe_chars(input: &str) -> String {
    input
        .chars()
        .filter(|c| !matches!(c, '\t' | '\n' | '\r'))
        .collect()
}

/// `urllib.parse.unquote_plus` with `errors='replace'`.
pub fn unquote_plus(input: &str) -> String {
    unquote_impl(input, true)
}

/// `urllib.parse.unquote` with `errors='replace'`.
pub fn unquote(input: &str) -> String {
    unquote_impl(input, false)
}

fn unquote_impl(input: &str, plus: bool) -> String {
    let bytes = input.as_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut i = 0usize;
    while i < bytes.len() {
        match bytes[i] {
            b'+' if plus => {
                out.push(b' ');
                i += 1;
            }
            b'%' if i + 2 < bytes.len() => {
                if let (Some(hi), Some(lo)) = (hex_val(bytes[i + 1]), hex_val(bytes[i + 2])) {
                    out.push((hi << 4) | lo);
                    i += 3;
                } else {
                    out.push(bytes[i]);
                    i += 1;
                }
            }
            b => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// `urllib.parse.parse_qsl(qs, keep_blank_values=True, max_num_fields=N)`.
pub fn parse_qsl(
    query: &str,
    keep_blank: bool,
    max_fields: usize,
) -> Result<Vec<(String, String)>, UrlError> {
    let field_count = query.split('&').count();
    if max_fields > 0 && field_count > max_fields {
        return Err(UrlError::TooManyFields);
    }
    let mut out = Vec::new();
    for pair in query.split('&') {
        if pair.is_empty() {
            continue;
        }
        let (name_raw, value_raw) = match pair.split_once('=') {
            Some((n, v)) => (n, Some(v)),
            None => (pair, None),
        };
        let name = unquote_plus(name_raw);
        match value_raw {
            Some(v) => out.push((name, unquote_plus(v))),
            None => {
                if keep_blank {
                    out.push((name, String::new()));
                }
            }
        }
    }
    Ok(out)
}

impl ParsedUrl {
    pub fn query_string(&self) -> &str {
        self.query.as_deref().unwrap_or("")
    }

    pub fn fragment_string(&self) -> &str {
        self.fragment.as_deref().unwrap_or("")
    }

    /// Python `parsed.port` — raises `ValueError` on malformed ports.
    pub fn port(&self) -> Result<Option<u16>, UrlError> {
        match &self.port_raw {
            None => Ok(None),
            Some(raw) if raw.is_empty() => Ok(None),
            Some(raw) => {
                if !raw.chars().all(|c| c.is_ascii_digit()) {
                    return Err(UrlError::InvalidPort(raw.clone()));
                }
                let value: u32 = raw
                    .parse()
                    .map_err(|_| UrlError::InvalidPort(raw.clone()))?;
                if value > 65535 {
                    return Err(UrlError::PortOutOfRange);
                }
                Ok(Some(value as u16))
            }
        }
    }

    /// `parsed.port` accesses that tolerate malformed ports; surrounding
    /// Python code catches the `ValueError`, which maps to `None` here.
    pub fn port_lossy(&self) -> Option<u16> {
        self.port().ok().flatten()
    }

    /// `SplitResult.geturl()`.
    pub fn geturl(&self) -> String {
        let mut out = String::new();
        if !self.scheme.is_empty() {
            out.push_str(&self.scheme);
            out.push(':');
        }
        if !self.netloc.is_empty() {
            out.push_str("//");
            out.push_str(&self.netloc);
        }
        out.push_str(&self.path);
        if let Some(q) = &self.query {
            out.push('?');
            out.push_str(q);
        }
        if let Some(f) = &self.fragment {
            out.push('#');
            out.push_str(f);
        }
        out
    }

    /// `urlparse(...)._replace(fragment='').geturl()`.
    pub fn geturl_without_fragment(&self) -> String {
        let mut clone = self.clone();
        clone.fragment = None;
        clone.geturl()
    }

    /// Python `parsed.hostname` (lowercased, brackets stripped).
    pub fn host_stripped(&self) -> Option<String> {
        self.hostname
            .as_ref()
            .map(|h| h.strip_prefix("www.").map(|s| s.to_string()).unwrap_or_else(|| h.clone()))
    }
}

fn split_scheme(rest: &str) -> (Option<String>, String) {
    let bytes: Vec<char> = rest.chars().collect();
    if bytes.is_empty() || !is_scheme_start(bytes[0]) {
        return (None, rest.to_string());
    }
    let mut idx = 1usize;
    while idx < bytes.len() && is_scheme_char(bytes[idx]) {
        idx += 1;
    }
    if idx >= bytes.len() || bytes[idx] != ':' {
        return (None, rest.to_string());
    }
    // The colon must appear before any '/', '?' or '#'.
    let before_colon: String = bytes[..idx].iter().collect();
    let after_colon: String = bytes[idx + 1..].iter().collect();
    if before_colon.contains(['/', '?', '#']) {
        return (None, rest.to_string());
    }
    (Some(before_colon.to_ascii_lowercase()), after_colon)
}

/// Python `urlparse`.
pub fn parse_url(input: &str) -> Result<ParsedUrl, UrlError> {
    let filtered = strip_unsafe_chars(input);
    let (head, fragment) = match filtered.split_once('#') {
        Some((a, b)) => (a.to_string(), Some(b.to_string())),
        None => (filtered.clone(), None),
    };
    let (scheme_raw, rest) = split_scheme(&head);
    let scheme = scheme_raw.unwrap_or_default();

    let mut parsed = ParsedUrl {
        scheme: scheme.clone(),
        netloc: String::new(),
        hostname: None,
        port_raw: None,
        username: None,
        password: None,
        path: String::new(),
        query: None,
        fragment,
    };

    let remainder: String = rest;
    if let Some(after_slashes) = remainder.strip_prefix("//") {
        let end = after_slashes
            .find(['/', '?', '#'])
            .unwrap_or(after_slashes.len());
        let authority = &after_slashes[..end];
        let tail = &after_slashes[end..];
        parse_authority(authority, &mut parsed)?;
        let (path, query) = split_path_query(tail);
        parsed.path = path;
        parsed.query = query;
    } else {
        let (path, query) = split_path_query(&remainder);
        parsed.path = path;
        parsed.query = query;
    }
    Ok(parsed)
}

fn split_path_query(tail: &str) -> (String, Option<String>) {
    match tail.split_once('?') {
        Some((p, q)) => (p.to_string(), Some(q.to_string())),
        None => (tail.to_string(), None),
    }
}

fn parse_authority(authority: &str, parsed: &mut ParsedUrl) -> Result<(), UrlError> {
    parsed.netloc = authority.to_string();
    let (userinfo, hostinfo) = match authority.rfind('@') {
        Some(idx) => (Some(&authority[..idx]), &authority[idx + 1..]),
        None => (None, authority),
    };
    if let Some(info) = userinfo {
        match info.split_once(':') {
            Some((u, p)) => {
                if !u.is_empty() {
                    parsed.username = Some(unquote(u));
                }
                if !p.is_empty() {
                    parsed.password = Some(unquote(p));
                }
            }
            None => parsed.username = Some(unquote(info)),
        }
    }
    let have_open_bracket = hostinfo.starts_with('[');
    let (raw_host, port_part) = if have_open_bracket {
        let close = hostinfo
            .find(']')
            .ok_or_else(|| UrlError::InvalidIpv6)?;
        let host = &hostinfo[..=close];
        let after = &hostinfo[close + 1..];
        match after.strip_prefix(':') {
            Some(p) => (host.to_string(), Some(p.to_string())),
            None => {
                if after.is_empty() {
                    (host.to_string(), None)
                } else {
                    return Err(UrlError::InvalidUrl(after.to_string()));
                }
            }
        }
    } else {
        match hostinfo.split_once(':') {
            Some((h, p)) => (h.to_string(), Some(p.to_string())),
            None => (hostinfo.to_string(), None),
        }
    };
    parsed.hostname = Some(raw_host.to_ascii_lowercase());
    parsed.port_raw = port_part;
    Ok(())
}

fn remove_dot_segments(path: &str) -> String {
    let mut out: Vec<&str> = Vec::new();
    for segment in path.split('/') {
        match segment {
            "." => continue,
            ".." => {
                out.pop();
            }
            other => out.push(other),
        }
    }
    out.join("/")
}

/// Python `urllib.parse.urljoin` for the subset the scraper relies on:
/// absolute URLs, scheme-relative URLs, root-relative paths and dot-relative
/// paths against an http(s) base.
pub fn urljoin(base: &ParsedUrl, reference: &str) -> Result<String, UrlError> {
    let filtered = strip_unsafe_chars(reference);
    let (head, fragment) = match filtered.split_once('#') {
        Some((a, b)) => (a.to_string(), Some(b.to_string())),
        None => (filtered.clone(), None),
    };
    let (ref_scheme, rest) = split_scheme(&head);

    let rebuild = |scheme: &str, netloc: &str, path: &str, query: Option<&str>| {
        let mut out = String::new();
        if !scheme.is_empty() {
            out.push_str(scheme);
            out.push(':');
        }
        if !netloc.is_empty() {
            out.push_str("//");
            out.push_str(netloc);
        }
        out.push_str(path);
        if let Some(q) = query {
            out.push('?');
            out.push_str(q);
        }
        if let Some(f) = &fragment {
            out.push('#');
            out.push_str(f);
        }
        out
    };

    if let Some(scheme) = &ref_scheme {
        if !scheme.is_empty() {
            return Ok(rebuild(scheme, "", &rest, None));
        }
    }
    if rest.is_empty() {
        return Ok(rebuild(
            &base.scheme,
            &base.netloc,
            &base.path,
            base.query.as_deref(),
        ));
    }
    if let Some(rest) = rest.strip_prefix("//") {
        return Ok(rebuild(&base.scheme, rest, "", None));
    }
    if let Some(frag) = rest.strip_prefix('#') {
        let _ = frag;
        return Ok(rebuild(
            &base.scheme,
            &base.netloc,
            &base.path,
            base.query.as_deref(),
        ));
    }
    if let Some(query) = rest.strip_prefix('?') {
        return Ok(rebuild(
            &base.scheme,
            &base.netloc,
            &base.path,
            Some(query),
        ));
    }
    if let Some(path) = rest.strip_prefix('/') {
        return Ok(rebuild(
            &base.scheme,
            &base.netloc,
            &format!("/{path}"),
            None,
        ));
    }
    // Relative path merge against the base directory.
    let base_dir = match base.path.rfind('/') {
        Some(idx) => &base.path[..=idx],
        None => "/",
    };
    let merged = format!("{base_dir}{rest}");
    let cleaned = remove_dot_segments(&merged);
    let cleaned = if cleaned.starts_with('/') {
        cleaned
    } else {
        format!("/{cleaned}")
    };
    Ok(rebuild(
        &base.scheme,
        &base.netloc,
        &cleaned,
        base.query.as_deref(),
    ))
}

/// Parse and join in one step; mirror of Python code that calls
/// `urljoin(final_url, value)` directly.
pub fn urljoin_str(base_url: &str, reference: &str) -> Result<String, UrlError> {
    let base = parse_url(base_url)?;
    urljoin(&base, reference)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_basic_https_url() {
        let parsed = parse_url("HTTPS://Example.com:443/path/to/page?q=1&r=2#frag").unwrap();
        assert_eq!(parsed.scheme, "https");
        assert_eq!(parsed.hostname.as_deref(), Some("example.com"));
        assert_eq!(parsed.port().unwrap(), Some(443));
        assert_eq!(parsed.path, "/path/to/page");
        assert_eq!(parsed.query.as_deref(), Some("q=1&r=2"));
        assert_eq!(parsed.fragment.as_deref(), Some("frag"));
    }

    #[test]
    fn strips_tab_newline_cr_like_python() {
        let parsed = parse_url("https://example.com/pa\tth").unwrap();
        assert_eq!(parsed.path, "/path");
    }

    #[test]
    fn port_parity() {
        assert_eq!(parse_url("https://a.com/").unwrap().port().unwrap(), None);
        assert_eq!(
            parse_url("https://a.com:/").unwrap().port().unwrap(),
            None
        );
        assert_eq!(
            parse_url("https://a.com:8443/").unwrap().port().unwrap(),
            Some(8443)
        );
        assert!(parse_url("https://a.com:b/").unwrap().port().is_err());
        assert!(parse_url("https://a.com:99999/").unwrap().port().is_err());
    }

    #[test]
    fn userinfo_detection() {
        let parsed = parse_url("https://user:pw@example.com/").unwrap();
        assert_eq!(parsed.username.as_deref(), Some("user"));
        assert_eq!(parsed.password.as_deref(), Some("pw"));
        let clean = parse_url("https://example.com/").unwrap();
        assert!(clean.username.is_none() && clean.password.is_none());
    }

    #[test]
    fn empty_query_and_fragment_presence() {
        let parsed = parse_url("https://a.com/b?").unwrap();
        assert_eq!(parsed.query.as_deref(), Some(""));
        assert_eq!(parsed.geturl(), "https://a.com/b?");
        let plain = parse_url("https://a.com/b").unwrap();
        assert_eq!(plain.query, None);
    }

    #[test]
    fn parse_qsl_parity() {
        let pairs = parse_qsl("a=1&b=&c&%2Fx=%2By", true, 50).unwrap();
        assert_eq!(
            pairs,
            vec![
                ("a".into(), "1".into()),
                ("b".into(), "".into()),
                ("c".into(), "".into()),
                ("/x".into(), "+y".into()),
            ]
        );
        assert!(parse_qsl("a=1", true, 1).is_ok());
        assert!(parse_qsl("a=1&b=2", true, 1).is_err());
        // Without keep_blank_values the bare key is dropped.
        let pairs = parse_qsl("a=1&c", false, 50).unwrap();
        assert_eq!(pairs.len(), 1);
    }

    #[test]
    fn urljoin_parity() {
        let base = parse_url("https://example.com/dir/page.html").unwrap();
        assert_eq!(
            urljoin(&base, "https://other.com/x").unwrap(),
            "https://other.com/x"
        );
        assert_eq!(
            urljoin(&base, "/root").unwrap(),
            "https://example.com/root"
        );
        assert_eq!(
            urljoin(&base, "sibling").unwrap(),
            "https://example.com/dir/sibling"
        );
        assert_eq!(
            urljoin(&base, "../up").unwrap(),
            "https://example.com/up"
        );
        assert_eq!(
            urljoin(&base, "//cdn.example.com/a").unwrap(),
            "https://cdn.example.com/a"
        );
        assert_eq!(
            urljoin(&base, "#frag").unwrap(),
            "https://example.com/dir/page.html#frag"
        );
    }
}
