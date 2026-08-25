//! Thin GoDaddy REST client: `sso-key` auth, prod/OTE base URL, JSON in/out.
//!
//! Credentials resolve lazily — the server starts and advertises its schema
//! without them (so `dangler warm` can harvest a cold, unprovisioned server),
//! and every tool call surfaces a setup hint when they're missing.

use anyhow::{Context, Result, anyhow, bail};
use serde_json::{Value, json};

/// Where credentials come from, in order:
/// 1. `GODADDY_PAT` — a Personal Access Token (Bearer auth; the current
///    scheme since GoDaddy's 2026-07 Developer Platform launch, and the only
///    one v3 endpoints accept).
/// 2. `GODADDY_API_KEY` + `GODADDY_API_SECRET` — legacy sso-key pair
///    (deprecated by GoDaddy, supported through 2026; still required for the
///    older families: certificates, shoppers, subscriptions, …).
/// 3. `GODADDY_CREDENTIALS_FILE` — path to a file with a `PAT=...` line, or
///    `KEY=...`/`SECRET=...` lines (kept outside any repo; dangler.toml
///    names the path).
pub const SETUP_HINT: &str = "Create a Personal Access Token in the dashboard at \
     https://developer.godaddy.com (preferred; scope it to what you need) and set \
     GODADDY_PAT — or a legacy production API key at https://developer.godaddy.com/keys \
     as GODADDY_API_KEY + GODADDY_API_SECRET. Alternatively point \
     GODADDY_CREDENTIALS_FILE at a file containing PAT=... (or KEY=... and \
     SECRET=... lines).";

/// Base URL selection: `GODADDY_ENV=ote` targets the test environment.
fn base_url() -> &'static str {
    match std::env::var("GODADDY_ENV").as_deref() {
        Ok("ote") => "https://api.ote-godaddy.com",
        _ => "https://api.godaddy.com",
    }
}

/// A resolved credential: PAT (Bearer) or legacy sso-key pair.
enum Auth {
    Pat(String),
    SsoKey { key: String, secret: String },
}

impl Auth {
    fn header_value(&self) -> String {
        match self {
            Auth::Pat(pat) => format!("Bearer {pat}"),
            Auth::SsoKey { key, secret } => format!("sso-key {key}:{secret}"),
        }
    }
}

fn load_creds() -> Result<Auth> {
    if let Ok(pat) = std::env::var("GODADDY_PAT")
        && !pat.is_empty()
    {
        return Ok(Auth::Pat(pat));
    }
    if let (Ok(key), Ok(secret)) = (
        std::env::var("GODADDY_API_KEY"),
        std::env::var("GODADDY_API_SECRET"),
    ) && !key.is_empty()
        && !secret.is_empty()
    {
        return Ok(Auth::SsoKey { key, secret });
    }
    if let Ok(path) = std::env::var("GODADDY_CREDENTIALS_FILE") {
        let raw = std::fs::read_to_string(&path)
            .with_context(|| format!("reading GODADDY_CREDENTIALS_FILE {path}"))?;
        let mut pat = None;
        let mut key = None;
        let mut secret = None;
        for line in raw.lines() {
            let line = line.trim();
            if let Some(v) = line.strip_prefix("PAT=") {
                pat = Some(v.trim().to_string());
            } else if let Some(v) = line.strip_prefix("KEY=") {
                key = Some(v.trim().to_string());
            } else if let Some(v) = line.strip_prefix("SECRET=") {
                secret = Some(v.trim().to_string());
            }
        }
        if let Some(pat) = pat
            && !pat.is_empty()
        {
            return Ok(Auth::Pat(pat));
        }
        return match (key, secret) {
            (Some(key), Some(secret)) if !key.is_empty() && !secret.is_empty() => {
                Ok(Auth::SsoKey { key, secret })
            }
            _ => bail!("{path} must contain a PAT=... line, or KEY=... and SECRET=... lines"),
        };
    }
    bail!("no GoDaddy credentials configured. {SETUP_HINT}")
}

/// One GoDaddy REST call. `path` starts with `/` (e.g. `/v1/domains`);
/// `query` is a list of `(k, v)` pairs; `body` is serialized as JSON when set.
pub async fn call(
    http: &reqwest::Client,
    method: &str,
    path: &str,
    query: &[(String, String)],
    body: Option<&Value>,
) -> Result<Value> {
    let auth = load_creds()?;
    if !path.starts_with('/') {
        bail!("path must start with '/', got '{path}'");
    }
    let method: reqwest::Method = method
        .to_uppercase()
        .parse()
        .map_err(|_| anyhow!("invalid HTTP method '{method}'"))?;
    let url = format!("{}{}", base_url(), path);

    let mut req = http
        .request(method, &url)
        .header("Authorization", auth.header_value())
        .header("Accept", "application/json");
    if !query.is_empty() {
        req = req.query(query);
    }
    // Some GoDaddy endpoints act on a delegate account via X-Shopper-Id.
    if let Ok(shopper) = std::env::var("GODADDY_SHOPPER_ID")
        && !shopper.is_empty()
    {
        req = req.header("X-Shopper-Id", shopper);
    }
    if let Some(b) = body {
        req = req.json(b);
    }

    let resp = req.send().await.with_context(|| format!("calling {url}"))?;
    let status = resp.status();
    let text = resp.text().await.unwrap_or_default();

    if !status.is_success() {
        // GoDaddy errors are JSON {code, message, fields?} — pass them through
        // verbatim so the model can react (e.g. ACCESS_DENIED on small-account
        // restricted endpoints, INVALID_BODY with field details).
        bail!("GoDaddy API {status} on {path}: {}", truncated(&text, 2000));
    }
    if text.trim().is_empty() {
        // 204 No Content — every successful DNS write lands here.
        return Ok(json!({"ok": true, "status": status.as_u16()}));
    }
    serde_json::from_str(&text)
        .with_context(|| format!("non-JSON response from {path}: {}", truncated(&text, 500)))
}

fn truncated(s: &str, max: usize) -> &str {
    match s.char_indices().nth(max) {
        Some((idx, _)) => &s[..idx],
        None => s,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn truncation_is_char_safe() {
        assert_eq!(truncated("héllo", 2), "hé");
        assert_eq!(truncated("hi", 10), "hi");
    }

    #[test]
    fn creds_file_parses() {
        let dir = std::env::temp_dir().join("dangler-godaddy-test");
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("creds");
        std::fs::write(&p, "KEY=abc\nSECRET=def\n").unwrap();
        // SAFETY: test-local env mutation; tests touching env run in one process.
        unsafe {
            std::env::remove_var("GODADDY_PAT");
            std::env::remove_var("GODADDY_API_KEY");
            std::env::remove_var("GODADDY_API_SECRET");
            std::env::set_var("GODADDY_CREDENTIALS_FILE", &p);
        }
        assert_eq!(
            load_creds().unwrap().header_value(),
            "sso-key abc:def",
            "KEY/SECRET lines resolve to sso-key auth"
        );
        // A PAT= line wins over the key pair.
        std::fs::write(&p, "PAT=tok\nKEY=abc\nSECRET=def\n").unwrap();
        assert_eq!(load_creds().unwrap().header_value(), "Bearer tok");
        unsafe {
            std::env::remove_var("GODADDY_CREDENTIALS_FILE");
        }
    }
}
