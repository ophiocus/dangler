use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::Deserialize;

#[derive(Debug, Clone, Deserialize)]
pub struct Config {
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
            "#,
        )
        .unwrap();
        assert_eq!(cfg.servers.len(), 2);
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
