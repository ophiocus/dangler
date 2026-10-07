//! Thin Cloudflare v4 REST client: bearer auth, JSON in/out, envelope unwrapped.
//!
//! Credentials resolve lazily — the server starts and advertises its schema
//! without them (so `dangler warm` can harvest a cold, unprovisioned server),
//! and every tool call surfaces a setup hint when they're missing.

use anyhow::{Context, Result, anyhow, bail};
use serde_json::{Value, json};

/// Where the token comes from, in order:
/// 1. `CLOUDFLARE_API_TOKEN` — a bearer token in the environment.
/// 2. `CLOUDFLARE_CREDENTIALS_FILE` — path to a file with a `TOKEN=...` line,
///    kept outside any repo; `dangler.toml` names the path.
///
/// Scopes, deliberately spelled out: the fleet's uses need **Zone · DNS · Edit**
/// (records), **Zone · Cache Purge · Purge** (purge), **Zone · SSL and
/// Certificates · Edit** (Origin CA), and **Zone · Zone · Read** (listing).
/// Grant only what the task needs — a token is revocable and scopeable, which
/// is the whole reason it is preferred over an account-wide login.
pub const SETUP_HINT: &str = "Create a scoped API token at \
     https://dash.cloudflare.com/profile/api-tokens and write a line `TOKEN=...` into the \
     file CLOUDFLARE_CREDENTIALS_FILE points at (dangler.toml names it), or set \
     CLOUDFLARE_API_TOKEN. Scopes: Zone·Zone·Read plus whichever of Zone·DNS·Edit, \
     Zone·Cache Purge·Purge and Zone·SSL and Certificates·Edit the work needs. Check it \
     with the verify_token tool.";

const BASE_URL: &str = "https://api.cloudflare.com/client/v4";

/// Refuse every mutating tool.
pub fn read_only() -> bool {
    std::env::var("CLOUDFLARE_READ_ONLY").is_ok_and(|v| !v.is_empty())
}

fn load_token() -> Result<String> {
    if let Ok(tok) = std::env::var("CLOUDFLARE_API_TOKEN")
        && !tok.is_empty()
    {
        return Ok(tok);
    }
    if let Ok(path) = std::env::var("CLOUDFLARE_CREDENTIALS_FILE") {
        // An absent file is the normal state of a fresh seat, not an I/O fault:
        // say what to put there instead of quoting the OS error.
        if !std::path::Path::new(&path).is_file() {
            bail!("no credentials file at {path} yet. {SETUP_HINT}");
        }
        let raw = std::fs::read_to_string(&path)
            .with_context(|| format!("reading CLOUDFLARE_CREDENTIALS_FILE {path}"))?;
        for line in raw.lines() {
            let line = line.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            if let Some(v) = line.strip_prefix("TOKEN=") {
                let v = v.trim().trim_matches('"');
                if !v.is_empty() {
                    return Ok(v.to_string());
                }
            }
        }
        bail!("{path} must contain a TOKEN=... line");
    }
    bail!("no Cloudflare credentials configured. {SETUP_HINT}")
}

/// One Cloudflare REST call. `path` starts with `/` (e.g. `/zones`);
/// `query` is a list of `(k, v)` pairs; `body` is serialized as JSON when set.
///
/// Returns the envelope's `result` on success. Cloudflare wraps every response
/// in `{success, errors, messages, result}` and **can answer 200 with
/// `success: false`**, so the flag is checked rather than the status alone.
pub async fn call(
    http: &reqwest::Client,
    method: &str,
    path: &str,
    query: &[(String, String)],
    body: Option<&Value>,
) -> Result<Value> {
    let token = load_token()?;
    if !path.starts_with('/') {
        bail!("path must start with '/', got '{path}'");
    }
    let method: reqwest::Method = method
        .to_uppercase()
        .parse()
        .map_err(|_| anyhow!("invalid HTTP method '{method}'"))?;
    let url = format!("{BASE_URL}{path}");

    let mut req = http
        .request(method, &url)
        .bearer_auth(&token)
        .header("Accept", "application/json");
    if !query.is_empty() {
        req = req.query(query);
    }
    if let Some(b) = body {
        req = req.json(b);
    }

    let resp = req.send().await.with_context(|| format!("calling {url}"))?;
    let status = resp.status();
    let text = resp.text().await.unwrap_or_default();

    if text.trim().is_empty() {
        if status.is_success() {
            return Ok(json!({"ok": true, "status": status.as_u16()}));
        }
        bail!("Cloudflare API {status} on {path} with an empty body");
    }

    let parsed: Value = serde_json::from_str(&text)
        .with_context(|| format!("non-JSON response from {path}: {}", truncated(&text, 500)))?;

    // The envelope is the contract: trust `success`, not the status code.
    let ok = parsed
        .get("success")
        .and_then(Value::as_bool)
        .unwrap_or_else(|| status.is_success());
    if !ok {
        let errors = parsed.get("errors").cloned().unwrap_or(Value::Null);
        bail!(
            "Cloudflare API {status} on {path}: {}",
            truncated(&errors.to_string(), 2000)
        );
    }

    // `result` is the payload; keep `result_info` when it carries pagination so
    // a caller can tell a short page from the end of the list.
    let result = parsed.get("result").cloned().unwrap_or(Value::Null);
    match parsed.get("result_info") {
        Some(info) if !info.is_null() => Ok(json!({"result": result, "result_info": info})),
        _ => Ok(result),
    }
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
    fn creds_file_parses_and_ignores_noise() {
        let dir = std::env::temp_dir().join("dangler-cloudflare-test");
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("creds");
        std::fs::write(&p, "# a comment\n\nTOKEN=  abc123  \n").unwrap();
        // SAFETY: test-local env mutation; tests touching env run in one process.
        unsafe {
            std::env::remove_var("CLOUDFLARE_API_TOKEN");
            std::env::set_var("CLOUDFLARE_CREDENTIALS_FILE", &p);
        }
        assert_eq!(
            load_token().unwrap(),
            "abc123",
            "comments and blank lines are skipped and the value is trimmed"
        );
        unsafe {
            std::env::remove_var("CLOUDFLARE_CREDENTIALS_FILE");
        }
    }

    #[test]
    fn a_file_without_a_token_line_is_an_error_naming_the_path() {
        let dir = std::env::temp_dir().join("dangler-cloudflare-test");
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("empty-creds");
        std::fs::write(&p, "# nothing useful here\n").unwrap();
        unsafe {
            std::env::remove_var("CLOUDFLARE_API_TOKEN");
            std::env::set_var("CLOUDFLARE_CREDENTIALS_FILE", &p);
        }
        let err = load_token().unwrap_err().to_string();
        assert!(err.contains("TOKEN="), "the error says what the file needs");
        unsafe {
            std::env::remove_var("CLOUDFLARE_CREDENTIALS_FILE");
        }
    }

    #[test]
    fn read_only_is_off_unless_set_to_something() {
        unsafe {
            std::env::set_var("CLOUDFLARE_READ_ONLY", "");
        }
        assert!(!read_only(), "an empty value does not arm read-only");
        unsafe {
            std::env::set_var("CLOUDFLARE_READ_ONLY", "1");
        }
        assert!(read_only());
        unsafe {
            std::env::remove_var("CLOUDFLARE_READ_ONLY");
        }
    }
}
