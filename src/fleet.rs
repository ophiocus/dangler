//! The downstream side of dangler: lazily spawned MCP child processes with a
//! persistent schema cache and idle reaping.
//!
//! Lifecycle of a child: cold → [`Fleet::acquire`] spawns it and marks it
//! in-use → [`Fleet::release`] stamps `last_used` → the background
//! [`Fleet::reap_loop`] cancels it once idle past its timeout (or
//! [`Fleet::drop_server`] does so on demand). Cached schemas outlive the
//! process, so `search_tools` answers even for cold servers.
//!
//! Nothing is fixed at startup. [`Fleet::refresh`] re-reads `dangler.toml`
//! when it changes (or when a call names an entry dangler doesn't know), and a
//! warm child whose credential files changed since it spawned is respawned on
//! its next idle acquire, so a new identity or a re-minted token needs no restart.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime};

use anyhow::{Context, Result, anyhow};
use rmcp::ServiceExt;
use rmcp::model::{CallToolRequestParams, CallToolResult, JsonObject, Tool};
use rmcp::service::{Peer, RoleClient, RunningService};
use rmcp::transport::TokioChildProcess;
use rmcp::transport::streamable_http_client::{
    StreamableHttpClientTransport, StreamableHttpClientTransportConfig,
};
use tokio::process::Command;
use tokio::sync::Mutex;

use crate::config::{Config, ServerSpec, Transport};

/// Reap a warm child after this long unused, unless configured otherwise.
pub const DEFAULT_IDLE_TIMEOUT_SECS: u64 = 600;
const REAP_SCAN_INTERVAL: Duration = Duration::from_secs(30);
const CANCEL_TIMEOUT: Duration = Duration::from_secs(3);

/// A warm downstream server and the bookkeeping the reaper needs.
struct Child {
    /// The live rmcp client session; owns the child process via its transport.
    service: RunningService<RoleClient, ()>,
    /// Updated on acquire and release; the idle clock reaping measures against.
    last_used: Instant,
    /// Requests currently using this child — a child with traffic is never reaped.
    inflight: u32,
    /// Credential files and how they looked when the child spawned.
    credentials: Vec<(PathBuf, Stamp)>,
    /// Its config entry changed or left while it was busy: retire it once idle.
    stale: bool,
}

/// What a file looked like: modification time and size, `None` while absent.
type Stamp = Option<(SystemTime, u64)>;

fn stamp(path: &Path) -> Stamp {
    let meta = std::fs::metadata(path).ok()?;
    Some((
        meta.modified().unwrap_or(SystemTime::UNIX_EPOCH),
        meta.len(),
    ))
}

/// Whether any credential file was created, rewritten or removed since the stamps were taken.
fn credentials_changed(credentials: &[(PathBuf, Stamp)]) -> bool {
    credentials.iter().any(|(path, then)| stamp(path) != *then)
}

/// A poisoned lock only means another thread panicked mid-update; the data is still usable.
fn locked<T>(m: &std::sync::Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// The file the fleet was loaded from, so it can be re-read when it changes.
struct Source {
    path: PathBuf,
    /// The file as of the last successful load.
    loaded: std::sync::Mutex<Stamp>,
    /// Why the last re-read failed (the previous config stays in force), if it did.
    error: std::sync::Mutex<Option<String>>,
}

/// The downstream fleet: configured MCP servers, spawned lazily on first touch,
/// reaped when idle.
pub struct Fleet {
    config: std::sync::RwLock<Arc<Config>>,
    source: Option<Source>,
    children: Mutex<HashMap<String, Child>>,
    /// Tool schemas harvested from each server we've spawned at least once.
    cache: Mutex<HashMap<String, Vec<Tool>>>,
}

/// One row of `list_servers`: a configured server and what we know about it.
pub struct ServerStatus {
    pub name: String,
    /// Whether a child process is currently running for this server.
    pub warm: bool,
    /// Cached tool count, if the server has ever been loaded (`None` = never indexed).
    pub cached_tools: Option<usize>,
    /// The account/identity this server acts as, from config.
    pub identity: Option<String>,
    /// Provisioning instructions, from config.
    pub setup_hint: Option<String>,
    /// The installed skill (name tag) that says when to use this server.
    pub skill: Option<String>,
    /// Set when the entry is not served: it carries no usable skill, and this says why.
    pub disabled: Option<String>,
}

/// One `search_tools` match: a tool on some downstream server.
pub struct ToolHit {
    pub server: String,
    pub tool: String,
    pub description: String,
    /// The account/identity the owning server acts as.
    pub identity: Option<String>,
}

/// DANGLER_CACHE env var, else ~/.dangler/cache.json — schemas survive restarts so
/// search_tools works from a cold start across the whole warmed fleet.
pub fn cache_path() -> PathBuf {
    if let Some(p) = std::env::var_os("DANGLER_CACHE") {
        return p.into();
    }
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .unwrap_or_default()
        .join(".dangler")
        .join("cache.json")
}

/// The reap decision, kept pure for testability. `timeout` of None means never reap.
fn should_reap(inflight: u32, idle_for: Duration, timeout: Option<Duration>) -> bool {
    match timeout {
        Some(t) => inflight == 0 && idle_for >= t,
        None => false,
    }
}

impl Fleet {
    pub fn new(config: Config) -> Self {
        let cache: HashMap<String, Vec<Tool>> = std::fs::read_to_string(cache_path())
            .ok()
            .and_then(|raw| serde_json::from_str(&raw).ok())
            .unwrap_or_default();
        if !cache.is_empty() {
            tracing::info!(
                servers = cache.len(),
                path = %cache_path().display(),
                "loaded persisted schema cache"
            );
        }
        Self {
            config: std::sync::RwLock::new(Arc::new(config)),
            source: None,
            children: Mutex::new(HashMap::new()),
            cache: Mutex::new(cache),
        }
    }

    /// A fleet that re-reads `path` (already loaded and skill-synced into
    /// `config`) whenever the file changes. See [`Fleet::refresh`].
    pub fn watching(config: Config, path: &Path) -> Self {
        let mut fleet = Self::new(config);
        fleet.source = Some(Source {
            path: path.to_path_buf(),
            loaded: std::sync::Mutex::new(stamp(path)),
            error: std::sync::Mutex::new(None),
        });
        fleet
    }

    /// The config in force now.
    fn cfg(&self) -> Arc<Config> {
        self.config
            .read()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
    }

    /// Re-read `dangler.toml` if it changed since the last load (one `stat`
    /// otherwise), or unconditionally with `force`. The skills are synced again,
    /// so a new entry's skill is installed at that moment. A file that fails to
    /// parse leaves the previous config in force; the error is logged, kept for
    /// `list_servers`, and the file is retried on the next call. Warm children
    /// whose entry changed or left are retired: at once if idle, else when their
    /// last request ends.
    pub async fn refresh(&self, force: bool) {
        let Some(src) = &self.source else { return };
        let now = stamp(&src.path);
        if !force && now == *locked(&src.loaded) {
            return;
        }
        let loaded = Config::load(&src.path)
            .and_then(|mut c| crate::skills::sync(&mut c).map(|report| (c, report)));
        let (new, report) = match loaded {
            Ok(x) => x,
            Err(e) => {
                let msg = format!("{e:#}");
                tracing::error!(error = %msg, "dangler.toml did not reload; the previous config stays in force");
                *locked(&src.error) = Some(msg);
                return;
            }
        };
        for o in report.missing() {
            tracing::error!(server = %o.server, reason = %o.problem.as_deref().unwrap_or(""), "disabled: no skill");
        }
        *locked(&src.loaded) = now;
        *locked(&src.error) = None;
        let new = Arc::new(new);
        let old = std::mem::replace(
            &mut *self.config.write().unwrap_or_else(|e| e.into_inner()),
            new.clone(),
        );
        if !force || old.servers != new.servers {
            tracing::info!(servers = new.servers.len(), path = %src.path.display(), "reloaded fleet config");
        }

        let mut retired = Vec::new();
        {
            let mut children = self.children.lock().await;
            let changed: Vec<String> = children
                .keys()
                .filter(|n| old.servers.get(*n) != new.servers.get(*n))
                .cloned()
                .collect();
            for name in changed {
                if children[&name].inflight == 0 {
                    retired.push((name.clone(), children.remove(&name).expect("listed above")));
                } else if let Some(c) = children.get_mut(&name) {
                    c.stale = true;
                }
            }
        }
        for (name, child) in retired {
            tracing::info!(server = %name, "retired: its config entry changed or left");
            Self::cancel_service(child.service, &name).await;
        }
    }

    /// `refresh`, forced when `name` is not a served entry, so a server added
    /// to the file (or whose skill was added after it was disabled) is found on
    /// the call that names it.
    pub async fn refresh_for(&self, name: &str) {
        let known = self.cfg().servers.contains_key(name);
        self.refresh(!known).await;
    }

    /// The reload error to show beside the fleet, if the file is currently broken.
    pub fn reload_error(&self) -> Option<String> {
        self.source.as_ref().and_then(|s| locked(&s.error).clone())
    }

    async fn persist_cache(&self) {
        let cache = self.cache.lock().await;
        let path = cache_path();
        let write = || -> Result<()> {
            if let Some(dir) = path.parent() {
                std::fs::create_dir_all(dir)?;
            }
            std::fs::write(&path, serde_json::to_vec_pretty(&*cache)?)?;
            Ok(())
        };
        if let Err(e) = write() {
            tracing::warn!(path = %path.display(), error = %e, "failed to persist schema cache");
        }
    }

    /// Look up a server's launch spec, erroring on names not in the config —
    /// and saying so when the name is there but disabled for carrying no skill.
    fn spec(&self, name: &str) -> Result<ServerSpec> {
        let cfg = self.cfg();
        cfg.servers.get(name).cloned().ok_or_else(|| {
            let broken = self
                .reload_error()
                .map(|e| format!(" (dangler.toml did not reload: {e})"))
                .unwrap_or_default();
            match cfg.disabled.get(name) {
                Some(reason) => anyhow!("server '{name}' is {reason}{broken}"),
                None => anyhow!("no server '{name}' in config{broken}"),
            }
        })
    }

    /// Credential files of a server: its spec's, plus the OAuth grant dangler holds for it.
    fn credential_files(name: &str, spec: &ServerSpec) -> Vec<PathBuf> {
        let mut files = spec.credential_files();
        if let Ok(Transport::Http { oauth: true, .. }) = spec.transport() {
            files.push(crate::oauth::store_path(name));
        }
        files
    }

    /// Effective idle timeout for a server: per-server override, else global,
    /// else the default. 0 anywhere means "never reap".
    fn idle_timeout(&self, name: &str) -> Option<Duration> {
        let cfg = self.cfg();
        let secs = cfg
            .servers
            .get(name)
            .and_then(|s| s.idle_timeout_secs)
            .or(cfg.idle_timeout_secs)
            .unwrap_or(DEFAULT_IDLE_TIMEOUT_SECS);
        (secs > 0).then(|| Duration::from_secs(secs))
    }

    /// The configured account/identity label for a server, if any.
    pub fn identity_of(&self, name: &str) -> Option<String> {
        self.cfg()
            .servers
            .get(name)
            .and_then(|s| s.identity.clone())
    }

    /// The installed skill name for a server, if any.
    pub fn skill_of(&self, name: &str) -> Option<String> {
        self.cfg().skills.get(name).cloned()
    }

    /// Status of every configured server, warm or cold — and, after them, the
    /// entries disabled for carrying no skill, so a caller sees why a name is missing.
    pub async fn statuses(&self) -> Vec<ServerStatus> {
        let cfg = self.cfg();
        let children = self.children.lock().await;
        let cache = self.cache.lock().await;
        let mut rows: Vec<ServerStatus> = cfg
            .servers
            .iter()
            .map(|(name, spec)| ServerStatus {
                name: name.clone(),
                warm: children.contains_key(name),
                cached_tools: cache.get(name).map(|t| t.len()),
                identity: spec.identity.clone(),
                setup_hint: spec.setup_hint.clone(),
                skill: cfg.skills.get(name).cloned(),
                disabled: None,
            })
            .collect();
        rows.extend(cfg.disabled.iter().map(|(name, reason)| ServerStatus {
            name: name.clone(),
            warm: false,
            cached_tools: cache.get(name).map(|t| t.len()),
            identity: None,
            setup_hint: None,
            skill: None,
            disabled: Some(reason.clone()),
        }));
        rows
    }

    /// Spawn the server if cold; mark it in-use and return a peer handle.
    /// Every acquire must be paired with a release.
    async fn acquire(&self, name: &str) -> Result<Peer<RoleClient>> {
        let spec = self.spec(name)?;
        let mut children = self.children.lock().await;
        if let Some(child) = children.get_mut(name) {
            // A busy child is shared even when stale; it is retired once idle.
            let fresh = !child.stale && !credentials_changed(&child.credentials);
            if fresh || child.inflight > 0 {
                child.inflight += 1;
                child.last_used = Instant::now();
                return Ok(child.service.peer().clone());
            }
            let old = children.remove(name).expect("present above");
            tracing::info!(
                server = name,
                "respawning: its config entry or credential files changed since it started"
            );
            let owned = name.to_string();
            tokio::spawn(async move { Self::cancel_service(old.service, &owned).await });
        }
        let credentials: Vec<(PathBuf, Stamp)> = Self::credential_files(name, &spec)
            .into_iter()
            .map(|p| {
                let s = stamp(&p);
                (p, s)
            })
            .collect();
        // Unprovisioned servers should explain themselves at the failure site.
        let hint = spec
            .setup_hint
            .as_deref()
            .map(|h| format!(" — setup: {h}"))
            .unwrap_or_default();
        let service = match spec
            .transport()
            .with_context(|| format!("config for '{name}'"))?
        {
            Transport::Stdio { command } => {
                let mut cmd = Command::new(&command);
                cmd.args(&spec.args);
                // env_file (secrets, by reference) merged under the inline env map.
                for (k, v) in spec
                    .child_env()
                    .with_context(|| format!("environment for '{name}'{hint}"))?
                {
                    cmd.env(k, v);
                }
                if let Some(cwd) = &spec.cwd {
                    cmd.current_dir(cwd);
                }
                let transport = TokioChildProcess::new(cmd)
                    .with_context(|| format!("spawning '{name}' ({command}){hint}"))?;
                ().serve(transport)
                    .await
                    .with_context(|| format!("MCP handshake with '{name}'{hint}"))?
            }
            // No child process: dangler is the HTTP client. OAuth tokens are
            // dangler's to hold and refresh (see oauth.rs); a static bearer or
            // API key comes from headers / header_file.
            Transport::Http { url, oauth } => {
                let config = StreamableHttpClientTransportConfig::with_uri(url.clone());
                if oauth {
                    let client = crate::oauth::client(name, &url)
                        .await
                        .with_context(|| format!("authorizing '{name}'{hint}"))?;
                    ().serve(StreamableHttpClientTransport::with_client(client, config))
                        .await
                        .with_context(|| format!("MCP handshake with '{name}' at {url}{hint}"))?
                } else {
                    let mut headers = reqwest::header::HeaderMap::new();
                    for (k, v) in spec
                        .http_headers()
                        .with_context(|| format!("headers for '{name}'{hint}"))?
                    {
                        headers.insert(
                            reqwest::header::HeaderName::from_bytes(k.as_bytes())?,
                            reqwest::header::HeaderValue::from_str(&v)?,
                        );
                    }
                    let client = reqwest::Client::builder()
                        .default_headers(headers)
                        .build()?;
                    ().serve(StreamableHttpClientTransport::with_client(client, config))
                        .await
                        .with_context(|| format!("MCP handshake with '{name}' at {url}{hint}"))?
                }
            }
        };
        let peer = service.peer().clone();
        children.insert(
            name.to_string(),
            Child {
                service,
                last_used: Instant::now(),
                inflight: 1,
                credentials,
                stale: false,
            },
        );
        tracing::info!(server = name, "spawned downstream server");
        Ok(peer)
    }

    /// Mark a request finished: decrement `inflight` and restart the idle clock.
    /// A stale child (its entry changed while it was busy) is retired once idle.
    async fn release(&self, name: &str) {
        let retired = {
            let mut children = self.children.lock().await;
            match children.get_mut(name) {
                Some(child) => {
                    child.inflight = child.inflight.saturating_sub(1);
                    child.last_used = Instant::now();
                    if child.stale && child.inflight == 0 {
                        children.remove(name)
                    } else {
                        None
                    }
                }
                None => None,
            }
        };
        if let Some(child) = retired {
            tracing::info!(server = name, "retired: its config entry changed or left");
            Self::cancel_service(child.service, name).await;
        }
    }

    /// Spawn if needed, harvest the full tool list, cache it, and return it.
    pub async fn load(&self, name: &str) -> Result<Vec<Tool>> {
        let peer = self.acquire(name).await?;
        let result = peer
            .list_all_tools()
            .await
            .with_context(|| format!("listing tools of '{name}'"));
        self.release(name).await;
        let tools = result?;
        self.cache
            .lock()
            .await
            .insert(name.to_string(), tools.clone());
        self.persist_cache().await;
        Ok(tools)
    }

    /// Proxy one tool call to a downstream server (spawning it if cold).
    pub async fn call(
        &self,
        server: &str,
        tool: &str,
        arguments: Option<JsonObject>,
    ) -> Result<CallToolResult> {
        let peer = self.acquire(server).await?;
        let mut params = CallToolRequestParams::new(tool.to_string());
        if let Some(a) = arguments {
            params = params.with_arguments(a);
        }
        let result = peer
            .call_tool(params)
            .await
            .with_context(|| format!("calling {server}/{tool}"));
        self.release(server).await;
        result
    }

    async fn cancel_service(service: RunningService<RoleClient, ()>, name: &str) {
        // cancel() waits for the child to exit, which bridged processes (e.g.
        // wsl.exe) don't always do promptly — bound the wait; the transport's
        // kill_on_drop reaps the process either way.
        if tokio::time::timeout(CANCEL_TIMEOUT, service.cancel())
            .await
            .is_err()
        {
            tracing::warn!(server = name, "graceful cancel timed out; killing");
        }
    }

    /// Reap a running child. Returns false if it wasn't running.
    /// Manual and unconditional — in-flight requests to it will fail.
    pub async fn drop_server(&self, name: &str) -> Result<bool> {
        let child = self.children.lock().await.remove(name);
        match child {
            Some(c) => {
                Self::cancel_service(c.service, name).await;
                tracing::info!(server = name, "dropped downstream server");
                Ok(true)
            }
            None => Ok(false),
        }
    }

    /// One reap pass: remove and cancel every idle child. Returns reaped names.
    pub async fn reap_idle(&self) -> Vec<String> {
        let now = Instant::now();
        let victims: Vec<(String, Child)> = self
            .children
            .lock()
            .await
            .extract_if(|name, child| {
                should_reap(
                    child.inflight,
                    now.duration_since(child.last_used),
                    self.idle_timeout(name),
                )
            })
            .collect();
        let mut reaped = Vec::new();
        for (name, child) in victims {
            Self::cancel_service(child.service, &name).await;
            tracing::info!(server = %name, "reaped idle downstream server");
            reaped.push(name);
        }
        reaped
    }

    /// Background loop: scan for idle children forever. Spawn once at startup.
    pub async fn reap_loop(&self) {
        let mut tick = tokio::time::interval(REAP_SCAN_INTERVAL);
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            tick.tick().await;
            self.reap_idle().await;
        }
    }

    /// Pre-loader mode: spawn every configured server, harvest + persist its schemas,
    /// then reap it. Returns (server, result-summary) per server.
    pub async fn warm_all(&self) -> Vec<(String, Result<usize>)> {
        let names: Vec<String> = self.cfg().servers.keys().cloned().collect();
        let mut out = Vec::new();
        for name in names {
            let res = self.load(&name).await.map(|t| t.len());
            self.drop_server(&name).await.ok();
            out.push((name, res));
        }
        out
    }

    /// Substring search over cached schemas (warm a server with `load` to index it).
    pub async fn search(&self, query: &str) -> Vec<ToolHit> {
        let q = query.to_lowercase();
        let cache = self.cache.lock().await;
        let mut hits = Vec::new();
        for (server, tools) in cache.iter() {
            let identity = self.identity_of(server);
            for t in tools {
                let desc = t.description.as_deref().unwrap_or("");
                if t.name.to_lowercase().contains(&q) || desc.to_lowercase().contains(&q) {
                    hits.push(ToolHit {
                        server: server.clone(),
                        tool: t.name.to_string(),
                        description: desc.to_string(),
                        identity: identity.clone(),
                    });
                }
            }
        }
        hits
    }

    /// Servers that have never been loaded (so search can say what it couldn't see).
    pub async fn unindexed(&self) -> Vec<String> {
        let cache = self.cache.lock().await;
        self.cfg()
            .servers
            .keys()
            .filter(|n| !cache.contains_key(*n))
            .cloned()
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rmcp::model::Tool;
    use std::sync::{Arc, OnceLock};

    // cache_path() reads process-global env — serialize the tests that touch it.
    fn env_lock() -> &'static Mutex<()> {
        static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
        LOCK.get_or_init(|| Mutex::new(()))
    }

    fn tool(name: &str, desc: &str) -> Tool {
        Tool::new(
            name.to_string(),
            desc.to_string(),
            Arc::new(
                serde_json::json!({"type": "object", "properties": {}})
                    .as_object()
                    .unwrap()
                    .clone(),
            ),
        )
    }

    fn spec_with_timeout(secs: Option<u64>) -> crate::config::ServerSpec {
        crate::config::ServerSpec {
            command: Some("unused".into()),
            args: vec![],
            env: Default::default(),
            env_file: None,
            url: None,
            headers: Default::default(),
            header_file: None,
            auth: None,
            cwd: None,
            idle_timeout_secs: secs,
            identity: None,
            setup_hint: None,
            skill: None,
            watch: vec![],
        }
    }

    /// A fleet without the persisted cache (which `Fleet::new` reads from the env-chosen path).
    fn bare(config: Config) -> Fleet {
        Fleet {
            config: std::sync::RwLock::new(Arc::new(config)),
            source: None,
            children: Mutex::new(HashMap::new()),
            cache: Mutex::new(HashMap::new()),
        }
    }

    fn test_config(names: &[&str]) -> Config {
        let servers = names
            .iter()
            .map(|n| (n.to_string(), spec_with_timeout(None)))
            .collect();
        Config {
            idle_timeout_secs: None,
            extensions_dir: None,
            skills_dir: None,
            servers,
            disabled: Default::default(),
            skills: Default::default(),
        }
    }

    #[test]
    fn should_reap_respects_inflight_and_timeout() {
        let t = Some(Duration::from_secs(600));
        assert!(should_reap(0, Duration::from_secs(600), t));
        assert!(should_reap(0, Duration::from_secs(9000), t));
        assert!(!should_reap(0, Duration::from_secs(599), t));
        // a child with traffic is never reaped, no matter how stale last_used is
        assert!(!should_reap(1, Duration::from_secs(9000), t));
        // timeout None = reaping disabled
        assert!(!should_reap(0, Duration::from_secs(9000), None));
    }

    #[test]
    fn idle_timeout_resolution_order() {
        let mut cfg = test_config(&["default", "never"]);
        cfg.servers
            .insert("fast".into(), spec_with_timeout(Some(30)));
        cfg.servers
            .insert("never".into(), spec_with_timeout(Some(0)));

        let fleet = bare(cfg);
        assert_eq!(
            fleet.idle_timeout("default"),
            Some(Duration::from_secs(DEFAULT_IDLE_TIMEOUT_SECS))
        );
        assert_eq!(fleet.idle_timeout("fast"), Some(Duration::from_secs(30)));
        assert_eq!(fleet.idle_timeout("never"), None);

        let mut cfg2 = test_config(&["srv"]);
        cfg2.idle_timeout_secs = Some(120);
        let fleet2 = bare(cfg2);
        assert_eq!(fleet2.idle_timeout("srv"), Some(Duration::from_secs(120)));
    }

    #[tokio::test]
    async fn search_matches_name_and_description_and_reports_unindexed() {
        let _guard = env_lock().lock().await;
        let dir = std::env::temp_dir().join("dangler-test-search");
        unsafe { std::env::set_var("DANGLER_CACHE", dir.join("none.json")) };

        let fleet = Fleet::new(test_config(&["indexed", "cold"]));
        fleet.cache.lock().await.insert(
            "indexed".into(),
            vec![
                tool("gd_list_records", "List DNS records for a domain"),
                tool("echo", "Echoes back the input string"),
            ],
        );

        let hits = fleet.search("DNS").await;
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].tool, "gd_list_records");
        assert_eq!(hits[0].server, "indexed");
        assert_eq!(fleet.search("echo").await.len(), 1);
        assert!(fleet.search("nonexistent-term").await.is_empty());
        assert_eq!(fleet.unindexed().await, vec!["cold".to_string()]);
    }

    #[tokio::test]
    async fn cache_persists_and_reloads() {
        let _guard = env_lock().lock().await;
        let path = std::env::temp_dir()
            .join("dangler-test-cache")
            .join("cache.json");
        std::fs::remove_file(&path).ok();
        unsafe { std::env::set_var("DANGLER_CACHE", &path) };

        let fleet = Fleet::new(test_config(&["srv"]));
        fleet.cache.lock().await.insert(
            "srv".into(),
            vec![tool("t1", "first"), tool("t2", "second")],
        );
        fleet.persist_cache().await;
        assert!(path.exists());

        let reloaded = Fleet::new(test_config(&["srv"]));
        let cache = reloaded.cache.lock().await;
        assert_eq!(cache["srv"].len(), 2);
        assert_eq!(cache["srv"][0].name, "t1");
        drop(cache);
        assert!(reloaded.unindexed().await.is_empty());
    }

    #[test]
    fn credential_stamps_see_creation_rewrite_and_removal() {
        let dir = std::env::temp_dir().join("dangler-test-credstamp");
        std::fs::create_dir_all(&dir).unwrap();
        let token = dir.join("token.json");
        std::fs::remove_file(&token).ok();

        let taken = |p: &Path| vec![(p.to_path_buf(), stamp(p))];
        let absent = taken(&token);
        assert!(!credentials_changed(&absent));
        std::fs::write(&token, "{\"v\":1}").unwrap();
        assert!(
            credentials_changed(&absent),
            "a token minted after spawn counts"
        );

        let minted = taken(&token);
        assert!(!credentials_changed(&minted));
        std::fs::write(&token, "{\"v\":22}").unwrap();
        assert!(credentials_changed(&minted), "a re-consent counts");

        let reminted = taken(&token);
        std::fs::remove_file(&token).unwrap();
        assert!(credentials_changed(&reminted), "a revoked token counts");
    }

    #[tokio::test]
    async fn config_reloads_new_entries_and_keeps_the_old_one_when_broken() {
        let _guard = env_lock().lock().await;
        let dir = std::env::temp_dir().join("dangler-test-reload");
        std::fs::remove_dir_all(&dir).ok();
        std::fs::create_dir_all(dir.join("skills")).unwrap();
        unsafe { std::env::set_var("DANGLER_CACHE", dir.join("none.json")) };
        let skill = dir.join("SKILL.md");
        std::fs::write(&skill, "---\nname: t-mail\ndescription: test\n---\nbody\n").unwrap();
        let fwd = |p: &Path| p.display().to_string().replace('\\', "/");
        let entry = |name: &str| {
            format!(
                "[servers.{name}]\ncommand = \"unused\"\nskill = \"{}\"\n",
                fwd(&skill)
            )
        };
        let head = format!("skills_dir = \"{}\"\n", fwd(&dir.join("skills")));
        let path = dir.join("dangler.toml");
        std::fs::write(&path, format!("{head}{}", entry("first"))).unwrap();

        let mut cfg = Config::load(&path).unwrap();
        crate::skills::sync(&mut cfg).unwrap();
        let fleet = Fleet::watching(cfg, &path);
        assert!(fleet.spec("second").is_err());

        // An entry added while dangler runs is served on the call that names it.
        std::fs::write(
            &path,
            format!("{head}{}{}", entry("first"), entry("second")),
        )
        .unwrap();
        fleet.refresh_for("second").await;
        assert!(fleet.spec("second").is_ok());
        assert_eq!(fleet.skill_of("second").as_deref(), Some("t-mail"));

        // A broken file keeps the fleet as it was and says why.
        std::fs::write(&path, format!("{head}[servers.third\n")).unwrap();
        fleet.refresh(false).await;
        assert!(fleet.spec("second").is_ok());
        let err = fleet.spec("third").unwrap_err().to_string();
        assert!(err.contains("did not reload"), "{err}");

        // Fixed: the error clears and the removal takes effect.
        std::fs::write(&path, format!("{head}{}", entry("first"))).unwrap();
        fleet.refresh(false).await;
        assert!(fleet.reload_error().is_none());
        assert!(fleet.spec("second").is_err());
    }
}
