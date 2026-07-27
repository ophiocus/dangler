use std::collections::HashMap;
use std::path::PathBuf;
use std::time::{Duration, Instant};

use anyhow::{Context, Result, anyhow};
use rmcp::ServiceExt;
use rmcp::model::{CallToolRequestParams, CallToolResult, JsonObject, Tool};
use rmcp::service::{Peer, RoleClient, RunningService};
use rmcp::transport::TokioChildProcess;
use tokio::process::Command;
use tokio::sync::Mutex;

use crate::config::{Config, ServerSpec};

/// Reap a warm child after this long unused, unless configured otherwise.
pub const DEFAULT_IDLE_TIMEOUT_SECS: u64 = 600;
const REAP_SCAN_INTERVAL: Duration = Duration::from_secs(30);
const CANCEL_TIMEOUT: Duration = Duration::from_secs(3);

struct Child {
    service: RunningService<RoleClient, ()>,
    /// Updated on acquire and release; the idle clock reaping measures against.
    last_used: Instant,
    /// Requests currently using this child — a child with traffic is never reaped.
    inflight: u32,
}

/// The downstream fleet: configured MCP servers, spawned lazily on first touch,
/// reaped when idle.
pub struct Fleet {
    config: Config,
    children: Mutex<HashMap<String, Child>>,
    /// Tool schemas harvested from each server we've spawned at least once.
    cache: Mutex<HashMap<String, Vec<Tool>>>,
}

pub struct ServerStatus {
    pub name: String,
    pub warm: bool,
    pub cached_tools: Option<usize>,
}

pub struct ToolHit {
    pub server: String,
    pub tool: String,
    pub description: String,
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
            config,
            children: Mutex::new(HashMap::new()),
            cache: Mutex::new(cache),
        }
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

    pub fn spec(&self, name: &str) -> Result<&ServerSpec> {
        self.config
            .servers
            .get(name)
            .ok_or_else(|| anyhow!("no server '{name}' in config"))
    }

    /// Effective idle timeout for a server: per-server override, else global,
    /// else the default. 0 anywhere means "never reap".
    fn idle_timeout(&self, name: &str) -> Option<Duration> {
        let secs = self
            .config
            .servers
            .get(name)
            .and_then(|s| s.idle_timeout_secs)
            .or(self.config.idle_timeout_secs)
            .unwrap_or(DEFAULT_IDLE_TIMEOUT_SECS);
        (secs > 0).then(|| Duration::from_secs(secs))
    }

    pub async fn statuses(&self) -> Vec<ServerStatus> {
        let children = self.children.lock().await;
        let cache = self.cache.lock().await;
        self.config
            .servers
            .keys()
            .map(|name| ServerStatus {
                name: name.clone(),
                warm: children.contains_key(name),
                cached_tools: cache.get(name).map(|t| t.len()),
            })
            .collect()
    }

    /// Spawn the server if cold; mark it in-use and return a peer handle.
    /// Every acquire must be paired with a release.
    async fn acquire(&self, name: &str) -> Result<Peer<RoleClient>> {
        let spec = self.spec(name)?.clone();
        let mut children = self.children.lock().await;
        if let Some(child) = children.get_mut(name) {
            child.inflight += 1;
            child.last_used = Instant::now();
            return Ok(child.service.peer().clone());
        }
        let mut cmd = Command::new(&spec.command);
        cmd.args(&spec.args);
        for (k, v) in &spec.env {
            cmd.env(k, v);
        }
        if let Some(cwd) = &spec.cwd {
            cmd.current_dir(cwd);
        }
        let transport = TokioChildProcess::new(cmd)
            .with_context(|| format!("spawning '{name}' ({})", spec.command))?;
        let service =
            ().serve(transport)
                .await
                .with_context(|| format!("MCP handshake with '{name}'"))?;
        let peer = service.peer().clone();
        children.insert(
            name.to_string(),
            Child {
                service,
                last_used: Instant::now(),
                inflight: 1,
            },
        );
        tracing::info!(server = name, "spawned downstream server");
        Ok(peer)
    }

    async fn release(&self, name: &str) {
        if let Some(child) = self.children.lock().await.get_mut(name) {
            child.inflight = child.inflight.saturating_sub(1);
            child.last_used = Instant::now();
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
        let mut victims = Vec::new();
        {
            let mut children = self.children.lock().await;
            let names: Vec<String> = children
                .iter()
                .filter(|(name, c)| {
                    should_reap(
                        c.inflight,
                        now.duration_since(c.last_used),
                        self.idle_timeout(name),
                    )
                })
                .map(|(name, _)| name.clone())
                .collect();
            for name in names {
                if let Some(c) = children.remove(&name) {
                    victims.push((name, c.service));
                }
            }
        }
        let mut reaped = Vec::new();
        for (name, service) in victims {
            Self::cancel_service(service, &name).await;
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
        let names: Vec<String> = self.config.servers.keys().cloned().collect();
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
            for t in tools {
                let desc = t.description.as_deref().unwrap_or("");
                if t.name.to_lowercase().contains(&q) || desc.to_lowercase().contains(&q) {
                    hits.push(ToolHit {
                        server: server.clone(),
                        tool: t.name.to_string(),
                        description: desc.to_string(),
                    });
                }
            }
        }
        hits
    }

    /// Servers that have never been loaded (so search can say what it couldn't see).
    pub async fn unindexed(&self) -> Vec<String> {
        let cache = self.cache.lock().await;
        self.config
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
            command: "unused".into(),
            args: vec![],
            env: Default::default(),
            cwd: None,
            idle_timeout_secs: secs,
        }
    }

    fn test_config(names: &[&str]) -> Config {
        let servers = names
            .iter()
            .map(|n| (n.to_string(), spec_with_timeout(None)))
            .collect();
        Config {
            idle_timeout_secs: None,
            servers,
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

        let fleet = Fleet {
            config: cfg,
            children: Mutex::new(HashMap::new()),
            cache: Mutex::new(HashMap::new()),
        };
        assert_eq!(
            fleet.idle_timeout("default"),
            Some(Duration::from_secs(DEFAULT_IDLE_TIMEOUT_SECS))
        );
        assert_eq!(fleet.idle_timeout("fast"), Some(Duration::from_secs(30)));
        assert_eq!(fleet.idle_timeout("never"), None);

        let mut cfg2 = test_config(&["srv"]);
        cfg2.idle_timeout_secs = Some(120);
        let fleet2 = Fleet {
            config: cfg2,
            children: Mutex::new(HashMap::new()),
            cache: Mutex::new(HashMap::new()),
        };
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
}
