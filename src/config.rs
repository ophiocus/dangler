//! Fleet configuration: `dangler.toml` describes every downstream MCP server
//! dangler can front. Unknown keys are rejected so typos surface at startup
//! instead of silently deserializing to defaults.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde::Deserialize;

/// Top-level `dangler.toml` shape.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    /// Reap a warm child after this many seconds unused (default 600; 0 = never).
    /// Per-server [`ServerSpec::idle_timeout_secs`] overrides this.
    pub idle_timeout_secs: Option<u64>,
    /// The downstream fleet, keyed by the server name used in every meta-tool.
    #[serde(default)]
    pub servers: BTreeMap<String, ServerSpec>,
}

/// How to launch one downstream MCP server (stdio child process).
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ServerSpec {
    /// Executable to spawn (e.g. `npx`, `wsl`, an absolute binary path).
    pub command: String,
    /// Arguments passed to `command`.
    #[serde(default)]
    pub args: Vec<String>,
    /// Extra environment for the child. Remember `WSLENV` when bridging into WSL.
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    /// Working directory for the child; inherits dangler's when unset.
    pub cwd: Option<PathBuf>,
    /// Per-server idle reap override (seconds; 0 = never reap this server).
    pub idle_timeout_secs: Option<u64>,
    /// Which account/identity this server acts as (e.g. "tecnocratica",
    /// "personal"). Surfaced in list_servers and search_tools so a caller
    /// knows which hat a tool wears *before* invoking it.
    pub identity: Option<String>,
    /// How to provision this server (e.g. "npm run authorize as the
    /// Tecnocrática account"). Shown in list_servers and appended to spawn
    /// failures so an unprovisioned server explains itself.
    pub setup_hint: Option<String>,
}

impl Config {
    /// Read and parse a config file, with the file path in any error.
    pub fn load(path: &Path) -> Result<Self> {
        let raw = std::fs::read_to_string(path)
            .with_context(|| format!("reading config {}", path.display()))?;
        let cfg: Config =
            toml::from_str(&raw).with_context(|| format!("parsing {}", path.display()))?;
        Ok(cfg)
    }

    /// `DANGLER_CONFIG` env var, else `./dangler.toml`.
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
            identity = "tecnocratica"
            setup_hint = "run npm run authorize as the right account"
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
        assert_eq!(alpha.identity.as_deref(), Some("tecnocratica"));
        assert!(alpha.setup_hint.as_deref().unwrap().contains("authorize"));
        assert!(cfg.servers["beta"].identity.is_none());
        assert_eq!(alpha.cwd.as_deref(), Some(std::path::Path::new("/tmp")));
        let beta = &cfg.servers["beta"];
        assert!(beta.args.is_empty() && beta.env.is_empty() && beta.cwd.is_none());
    }

    #[test]
    fn empty_config_is_valid() {
        let cfg: Config = toml::from_str("").unwrap();
        assert!(cfg.servers.is_empty());
    }

    #[test]
    fn unknown_keys_are_rejected() {
        // Catches config typos (e.g. `idle_timeout_sec`) at startup.
        assert!(toml::from_str::<Config>("idle_timeout_sec = 60").is_err());
        assert!(
            toml::from_str::<Config>(
                r#"
                [servers.x]
                command = "npx"
                arg = ["typo"]
                "#
            )
            .is_err()
        );
    }
}
