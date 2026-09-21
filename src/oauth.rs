//! OAuth for HTTP downstream servers: dangler holds the tokens, the operator
//! grants them once.
//!
//! An MCP endpoint that answers `401` with a `WWW-Authenticate` challenge wants
//! OAuth 2.1 — discovery, dynamic client registration, authorization code with
//! PKCE, refresh. The library does that dance; this module owns the two things
//! it cannot: **where the tokens live** and **how the operator says yes**.
//!
//! Tokens go to `~/.dangler/oauth/<server>.json`, alongside the fleet config and
//! outside every repo, so the same rule as `env_file` holds: a credential is
//! referenced, never inline, and never printed. Consent happens in the
//! operator's browser against a loopback redirect — `dangler auth <server>` —
//! because an authorization code is theirs to grant, not a thing a session may
//! mint for itself.

use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::path::PathBuf;

use anyhow::{Context, Result, anyhow, bail};
use rmcp::transport::auth::{AuthClient, OAuthState};
use serde::{Deserialize, Serialize};

/// The loopback port the authorization code comes back on. Registered with the
/// provider as the redirect URI, so it has to be stable across runs.
const CALLBACK_PORT: u16 = 8899;

/// What we persist between runs: the dynamically-registered client id and the
/// token response (access + refresh). Never logged, never echoed.
#[derive(Debug, Serialize, Deserialize)]
struct Stored {
    client_id: String,
    token: serde_json::Value,
}

/// `~/.dangler/oauth/<server>.json`.
pub fn store_path(server: &str) -> PathBuf {
    let home = std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .unwrap_or_default();
    home.join(".dangler").join("oauth").join(format!("{server}.json"))
}

/// An HTTP client that carries (and refreshes) the stored bearer.
///
/// Fails with the exact command to run when nothing is stored yet — the same
/// contract as a missing `env_file`: explain, name the fix, do not half-start.
pub async fn client(server: &str, url: &str) -> Result<AuthClient<reqwest::Client>> {
    let path = store_path(server);
    let raw = std::fs::read_to_string(&path).map_err(|_| {
        anyhow!("'{server}' has no OAuth grant yet — run: dangler auth {server}")
    })?;
    let stored: Stored = serde_json::from_str(&raw)
        .with_context(|| format!("parsing {}", path.display()))?;
    let token = serde_json::from_value(stored.token)
        .with_context(|| format!("stored token for '{server}' is not an OAuth token response"))?;

    let issuer = authorization_server(url).await?;
    let mut state = OAuthState::new(&issuer, None)
        .await
        .map_err(|e| anyhow!("OAuth discovery for '{server}': {e}"))?;
    state
        .set_credentials(&stored.client_id, token)
        .await
        .map_err(|e| anyhow!("restoring the '{server}' grant: {e}"))?;
    let manager = state
        .into_authorization_manager()
        .ok_or_else(|| anyhow!("'{server}': stored grant did not restore into an authorized state"))?;
    Ok(AuthClient::new(reqwest::Client::default(), manager))
}

/// Which authorization server guards this resource (RFC 9728).
///
/// The MCP endpoint and the authorization server are different hosts as often
/// as not — OpenArt's resource is `mcp.openart.ai` while its OAuth lives on
/// `openart.ai` — and discovery run against the resource host looks for
/// `/register` there and fails. The protected-resource document is the pointer,
/// so read it first and discover against what it names.
async fn authorization_server(resource: &str) -> Result<String> {
    let parsed = reqwest::Url::parse(resource).with_context(|| format!("parsing {resource}"))?;
    let well_known = format!(
        "{}://{}/.well-known/oauth-protected-resource{}",
        parsed.scheme(),
        parsed.host_str().unwrap_or_default(),
        parsed.path()
    );
    // A failure here is worth saying out loud: silently falling back to the
    // resource host is how discovery ends up asking the wrong server to register.
    let doc: serde_json::Value = match reqwest::get(&well_known).await {
        Ok(r) if r.status().is_success() => r.json().await.unwrap_or_default(),
        Ok(r) => {
            eprintln!("protected-resource metadata at {well_known}: HTTP {}", r.status());
            serde_json::Value::Null
        }
        Err(e) => {
            eprintln!("protected-resource metadata at {well_known}: {e}");
            serde_json::Value::Null
        }
    };
    Ok(doc
        .get("authorization_servers")
        .and_then(|v| v.as_array())
        .and_then(|a| a.first())
        .and_then(|v| v.as_str())
        .map(str::to_string)
        .unwrap_or_else(|| format!("{}://{}", parsed.scheme(), parsed.host_str().unwrap_or_default())))
}

/// The one interactive step: register, open consent in the operator's browser,
/// catch the code on loopback, exchange it, and persist the grant.
pub async fn authorize(server: &str, url: &str, scopes: &[&str]) -> Result<()> {
    let redirect = format!("http://127.0.0.1:{CALLBACK_PORT}/callback");
    let issuer = authorization_server(url).await?;
    eprintln!("authorization server for '{server}': {issuer}");
    let mut state = OAuthState::new(&issuer, None)
        .await
        .map_err(|e| anyhow!("OAuth discovery for '{server}': {e}"))?;
    state
        .start_authorization(scopes, &redirect, Some("dangler"))
        .await
        .map_err(|e| anyhow!("registering dangler with '{server}': {e}"))?;

    let auth_url = match &state {
        OAuthState::Session(session) => session.get_authorization_url().to_string(),
        _ => bail!("'{server}': authorization did not start"),
    };
    println!("\nOpen this URL and approve dangler for '{server}':\n\n{auth_url}\n");
    println!("Waiting for the redirect on {redirect} …");

    let (code, csrf) = wait_for_code()?;
    state
        .handle_callback(&code, &csrf)
        .await
        .map_err(|e| anyhow!("exchanging the authorization code: {e}"))?;

    let (client_id, token) = state
        .get_credentials()
        .await
        .map_err(|e| anyhow!("reading the granted credentials: {e}"))?;
    let token = token.ok_or_else(|| anyhow!("'{server}' granted no token"))?;

    let path = store_path(server);
    std::fs::create_dir_all(path.parent().expect("store path has a parent"))?;
    std::fs::write(
        &path,
        serde_json::to_vec_pretty(&Stored { client_id, token: serde_json::to_value(token)? })?,
    )?;
    restrict(&path);
    println!("granted — token stored at {} (value not shown)", path.display());
    Ok(())
}

/// Block on loopback for the provider's redirect, and answer the browser.
fn wait_for_code() -> Result<(String, String)> {
    let listener = TcpListener::bind(("127.0.0.1", CALLBACK_PORT))
        .with_context(|| format!("listening on 127.0.0.1:{CALLBACK_PORT} for the OAuth redirect"))?;
    let (mut stream, _) = listener.accept()?;
    let mut line = String::new();
    BufReader::new(stream.try_clone()?).read_line(&mut line)?;
    // "GET /callback?code=…&state=… HTTP/1.1"
    let target = line.split_whitespace().nth(1).unwrap_or_default().to_string();
    let query = target.split_once('?').map(|(_, q)| q).unwrap_or_default();
    let mut code = None;
    let mut csrf = None;
    for pair in query.split('&') {
        match pair.split_once('=') {
            Some(("code", v)) => code = Some(urldecode(v)),
            Some(("state", v)) => csrf = Some(urldecode(v)),
            Some(("error", v)) => bail!("the provider refused: {}", urldecode(v)),
            _ => {}
        }
    }
    let body = "<!doctype html><meta charset=utf8><title>dangler</title>\
                <body style=\"font:16px system-ui;padding:3rem\">Granted. You can close this tab.";
    let _ = write!(
        stream,
        "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    Ok((
        code.ok_or_else(|| anyhow!("the redirect carried no authorization code"))?,
        csrf.unwrap_or_default(),
    ))
}

/// Percent-decoding, enough for an authorization code and a CSRF token.
fn urldecode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        match b[i] {
            b'%' if i + 2 < b.len() => {
                if let Ok(v) = u8::from_str_radix(&s[i + 1..i + 3], 16) {
                    out.push(v);
                    i += 3;
                    continue;
                }
                out.push(b[i]);
                i += 1;
            }
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            c => {
                out.push(c);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Owner-only where the platform has a mode bit; a no-op on Windows, where the
/// per-user profile directory is the boundary.
fn restrict(path: &std::path::Path) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
    #[cfg(not(unix))]
    let _ = path;
}
