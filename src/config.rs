use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::Deserialize;

#[derive(Debug, Clone, Deserialize)]
pub struct Config {
    /// Reap a warm child after this many seconds unused (default 600; 0 = never).
    /// Per-server `idle_timeout_secs` overrides this.
    pub idle_timeout_secs: Option<u64>,
    #[serde(default)]
    pub servers: BTreeMap<String, ServerSpec>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ServerSpec {
    pub command: String,
    #[serde(default)]
    pub args: Vec<String>,
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    pub cwd: Option<PathBuf>,
    /// Per-server idle reap override (seconds; 0 = never reap this server).
    pub idle_timeout_secs: Option<u64>,
}

impl Config {
    pub fn load(path: &Path) -> Result<Self> {
        let raw = std::fs::read_to_string(path)
            .with_context(|| format!("reading config {}", path.display()))?;
        let cfg: Config =
            toml::from_str(&raw).with_context(|| format!("parsing {}", path.display()))?;
        Ok(cfg)
    }

    /// DANGLER_CONFIG env var, else ./dangler.toml
    pub fn default_path() -> PathBuf {
        std::env::var_os("DANGLER_CONFIG")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("dangler.toml"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_full_server_spec() {
        let cfg: Config = toml::from_str(
            r#"
            [servers.alpha]
            command = "npx"
            args = ["-y", "some-mcp-server"]
            cwd = "/tmp"
            [servers.alpha.env]
            FOO = "bar"

            [servers.beta]
            command = "beta.exe"
            idle_timeout_secs = 0
            "#,
        )
        .unwrap();
        assert_eq!(cfg.servers.len(), 2);
        assert_eq!(cfg.servers["beta"].idle_timeout_secs, Some(0));
        assert_eq!(cfg.servers["alpha"].idle_timeout_secs, None);
        assert_eq!(cfg.idle_timeout_secs, None);
        let alpha = &cfg.servers["alpha"];
        assert_eq!(alpha.command, "npx");
        assert_eq!(alpha.args, vec!["-y", "some-mcp-server"]);
        assert_eq!(alpha.env["FOO"], "bar");
        assert_eq!(alpha.cwd.as_deref(), Some(std::path::Path::new("/tmp")));
        let beta = &cfg.servers["beta"];
        assert!(beta.args.is_empty() && beta.env.is_empty() && beta.cwd.is_none());
    }

    #[test]
    fn empty_config_is_valid() {
        let cfg: Config = toml::from_str("").unwrap();
        assert!(cfg.servers.is_empty());
    }
}
