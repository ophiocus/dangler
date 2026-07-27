use std::collections::HashMap;
use std::path::PathBuf;

use anyhow::{Context, Result, anyhow};
use rmcp::ServiceExt;
use rmcp::model::{CallToolRequestParams, CallToolResult, JsonObject, Tool};
use rmcp::service::{Peer, RoleClient, RunningService};
use rmcp::transport::TokioChildProcess;
use tokio::process::Command;
use tokio::sync::Mutex;

use crate::config::{Config, ServerSpec};

/// The downstream fleet: configured MCP servers, spawned lazily on first touch.
pub struct Fleet {
    config: Config,
    children: Mutex<HashMap<String, RunningService<RoleClient, ()>>>,
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

    /// Spawn the server if cold; return a cloned peer handle for issuing requests.
    pub async fn get_or_spawn(&self, name: &str) -> Result<Peer<RoleClient>> {
        let spec = self.spec(name)?.clone();
        let mut children = self.children.lock().await;
        if let Some(running) = children.get(name) {
            return Ok(running.peer().clone());
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
        let running =
            ().serve(transport)
                .await
                .with_context(|| format!("MCP handshake with '{name}'"))?;
        let peer = running.peer().clone();
        children.insert(name.to_string(), running);
        tracing::info!(server = name, "spawned downstream server");
        Ok(peer)
    }

    /// Spawn if needed, harvest the full tool list, cache it, and return it.
    pub async fn load(&self, name: &str) -> Result<Vec<Tool>> {
        let peer = self.get_or_spawn(name).await?;
        let tools = peer
            .list_all_tools()
            .await
            .with_context(|| format!("listing tools of '{name}'"))?;
        self.cache
            .lock()
            .await
            .insert(name.to_string(), tools.clone());
        self.persist_cache().await;
        Ok(tools)
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

    pub async fn call(
        &self,
        server: &str,
        tool: &str,
        arguments: Option<JsonObject>,
    ) -> Result<CallToolResult> {
        let peer = self.get_or_spawn(server).await?;
        let mut params = CallToolRequestParams::new(tool.to_string());
        if let Some(a) = arguments {
            params = params.with_arguments(a);
        }
        peer.call_tool(params)
            .await
            .with_context(|| format!("calling {server}/{tool}"))
    }

    /// Reap a running child. Returns false if it wasn't running.
    pub async fn drop_server(&self, name: &str) -> Result<bool> {
        tracing::debug!(server = name, "drop requested");
        let running = self.children.lock().await.remove(name);
        tracing::debug!(server = name, "child handle removed from registry");
        match running {
            Some(r) => {
                // cancel() waits for the child to exit, which bridged processes
                // (e.g. wsl.exe) don't always do on stdin close — bound the wait;
                // the transport's kill_on_drop reaps the process either way.
                let cancelled =
                    tokio::time::timeout(std::time::Duration::from_secs(3), r.cancel()).await;
                if cancelled.is_err() {
                    tracing::warn!(server = name, "graceful cancel timed out; killing");
                }
                tracing::info!(server = name, "dropped downstream server");
                Ok(true)
            }
            None => Ok(false),
        }
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
    use std::sync::{Arc, Mutex as StdMutex, OnceLock};

    // cache_path() reads process-global env — serialize the tests that touch it.
    fn env_lock() -> &'static StdMutex<()> {
        static LOCK: OnceLock<StdMutex<()>> = OnceLock::new();
        LOCK.get_or_init(|| StdMutex::new(()))
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

    fn test_config(names: &[&str]) -> Config {
        let servers = names
            .iter()
            .map(|n| {
                (
                    n.to_string(),
                    crate::config::ServerSpec {
                        command: "unused".into(),
                        args: vec![],
                        env: Default::default(),
                        cwd: None,
                    },
                )
            })
            .collect();
        Config { servers }
    }

    #[tokio::test]
    async fn search_matches_name_and_description_and_reports_unindexed() {
        let _guard = env_lock().lock().unwrap();
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
        let _guard = env_lock().lock().unwrap();
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
