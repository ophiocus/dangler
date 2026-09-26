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
    /// Where first-party extensions live, so `<extensions_dir>/<server>/SKILL.md`
    /// is the default skill of every entry. Defaults to the checkout the running
    /// binary was built in; `DANGLER_EXTENSIONS` overrides both.
    pub extensions_dir: Option<PathBuf>,
    /// Where skills are installed (default `~/.claude/skills`; `DANGLER_SKILLS_DIR` wins).
    pub skills_dir: Option<PathBuf>,
    /// The downstream fleet, keyed by the server name used in every meta-tool.
    #[serde(default)]
    pub servers: BTreeMap<String, ServerSpec>,
    /// Entries removed from `servers` because they carry no usable skill,
    /// with the reason. Filled by `skills::sync`, shown by `list_servers`.
    #[serde(skip)]
    pub disabled: BTreeMap<String, String>,
    /// Server name → installed skill name. Filled by `skills::sync`.
    #[serde(skip)]
    pub skills: BTreeMap<String, String>,
}

/// How to reach one downstream MCP server: a stdio child process (`command`)
/// or a streamable-HTTP endpoint (`url`). Exactly one of the two.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ServerSpec {
    /// Executable to spawn (e.g. `npx`, `wsl`, an absolute binary path).
    /// Mutually exclusive with [`ServerSpec::url`].
    pub command: Option<String>,
    /// Streamable-HTTP MCP endpoint, e.g. `https://mcp.example.com/mcp`.
    /// No child process is spawned; dangler is the HTTP client.
    pub url: Option<String>,
    /// Extra HTTP headers for an `url` server (a bearer, an API key).
    /// Prefer [`ServerSpec::header_file`] for anything secret.
    #[serde(default)]
    pub headers: BTreeMap<String, String>,
    /// A file of `Header: value` lines merged under `headers`, so a token
    /// stays out of `dangler.toml` exactly as `env_file` keeps env secrets out.
    pub header_file: Option<PathBuf>,
    /// `"oauth"` for an endpoint that answers 401 with an OAuth challenge:
    /// dangler holds the tokens (`~/.dangler/oauth/<server>.json`) and the
    /// operator grants them once with `dangler auth <server>`.
    pub auth: Option<String>,
    /// Arguments passed to `command`.
    #[serde(default)]
    pub args: Vec<String>,
    /// Extra environment for the child. Remember `WSLENV` when bridging into WSL.
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    /// A dotenv-shaped file of `KEY=VALUE` lines merged into the child's
    /// environment before [`ServerSpec::env`], which wins on a clash.
    ///
    /// This is how a wrapped server's secret stays OUT of `dangler.toml`: the
    /// config references the file, the file holds the value, and neither the
    /// key's value nor the file's contents are ever logged. `#` comments and
    /// blank lines are ignored; surrounding quotes on a value are stripped.
    pub env_file: Option<PathBuf>,
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
    /// The server's skill — its name tag: a `SKILL.md` (or the directory holding
    /// one) whose frontmatter `description` tells a session *when* to reach for
    /// this server, before any schema is loaded. Every entry must have one;
    /// unset means `<--directory>/SKILL.md` for a `uv run`-style command, else
    /// `<extensions_dir>/<server>/SKILL.md`. Installed by dangler at every start.
    pub skill: Option<PathBuf>,
}

/// How a server is reached, resolved from the spec once so the fleet does not
/// re-derive it at every touch.
#[derive(Debug, Clone)]
pub enum Transport {
    /// A stdio child process.
    Stdio { command: String },
    /// A streamable-HTTP endpoint, with OAuth when `oauth` is set.
    Http { url: String, oauth: bool },
}

impl ServerSpec {
    /// Stdio or HTTP, with the "exactly one" rule enforced here rather than at
    /// every call site.
    pub fn transport(&self) -> Result<Transport> {
        match (&self.command, &self.url) {
            (Some(_), Some(_)) => anyhow::bail!("set either `command` or `url`, not both"),
            (None, None) => anyhow::bail!("needs a `command` (stdio) or a `url` (HTTP)"),
            (Some(command), None) => Ok(Transport::Stdio {
                command: command.clone(),
            }),
            (None, Some(url)) => Ok(Transport::Http {
                url: url.clone(),
                oauth: self.auth.as_deref() == Some("oauth"),
            }),
        }
    }

    /// Headers for an HTTP server: `header_file` first, then `headers` on top.
    /// Values are never logged; a missing file fails loudly, naming the file.
    pub fn http_headers(&self) -> Result<BTreeMap<String, String>> {
        let mut out = BTreeMap::new();
        if let Some(path) = &self.header_file {
            let raw = std::fs::read_to_string(path)
                .with_context(|| format!("reading header_file {}", path.display()))?;
            for line in raw.lines() {
                let line = line.trim();
                if line.is_empty() || line.starts_with('#') {
                    continue;
                }
                if let Some((k, v)) = line.split_once(':') {
                    out.insert(k.trim().to_string(), v.trim().to_string());
                }
            }
        }
        out.extend(self.headers.clone());
        Ok(out)
    }

    /// The child's extra environment: `env_file` first, then `env` on top.
    ///
    /// A missing or unreadable `env_file` is an error here rather than a silent
    /// spawn without credentials — the failure then names the file, and the
    /// server's `setup_hint` says how to fill it.
    pub fn child_env(&self) -> Result<BTreeMap<String, String>> {
        let mut out = BTreeMap::new();
        if let Some(path) = &self.env_file {
            let raw = std::fs::read_to_string(path)
                .with_context(|| format!("reading env_file {}", path.display()))?;
            for line in raw.lines() {
                let line = line.trim();
                if line.is_empty() || line.starts_with('#') {
                    continue;
                }
                let Some((k, v)) = line.split_once('=') else {
                    continue;
                };
                let v = v.trim();
                let v = v
                    .strip_prefix('"')
                    .and_then(|s| s.strip_suffix('"'))
                    .or_else(|| v.strip_prefix('\'').and_then(|s| s.strip_suffix('\'')))
                    .unwrap_or(v);
                out.insert(k.trim().to_string(), v.to_string());
            }
        }
        out.extend(self.env.clone());
        Ok(out)
    }
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
    fn a_server_is_stdio_or_http_but_not_both() {
        let cfg: Config = toml::from_str(
            r#"
            [servers.child]
            command = "npx"

            [servers.remote]
            url = "https://mcp.example.com/mcp"
            auth = "oauth"

            [servers.bearer]
            url = "https://mcp.example.com/mcp"

            [servers.both]
            command = "npx"
            url = "https://mcp.example.com/mcp"

            [servers.neither]
            identity = "nobody"
            "#,
        )
        .unwrap();

        assert!(matches!(
            cfg.servers["child"].transport().unwrap(),
            Transport::Stdio { .. }
        ));
        assert!(matches!(
            cfg.servers["remote"].transport().unwrap(),
            Transport::Http { oauth: true, .. }
        ));
        assert!(matches!(
            cfg.servers["bearer"].transport().unwrap(),
            Transport::Http { oauth: false, .. }
        ));
        assert!(
            cfg.servers["both"]
                .transport()
                .unwrap_err()
                .to_string()
                .contains("not both")
        );
        assert!(
            cfg.servers["neither"]
                .transport()
                .unwrap_err()
                .to_string()
                .contains("`url`")
        );
    }

    #[test]
    fn header_file_is_merged_under_the_inline_headers() {
        let dir = std::env::temp_dir().join("dangler-header-file-test");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("headers");
        std::fs::write(
            &path,
            "# bus token\nAuthorization: Bearer abc123\nX-Kept: from-file\n",
        )
        .unwrap();

        let cfg: Config = toml::from_str(&format!(
            r#"
            [servers.remote]
            url = "https://mcp.example.com/mcp"
            header_file = "{}"
            [servers.remote.headers]
            X-Kept = "from-config"
            "#,
            path.display().to_string().replace('\\', "/")
        ))
        .unwrap();

        let h = cfg.servers["remote"].http_headers().unwrap();
        assert_eq!(h["Authorization"], "Bearer abc123");
        assert_eq!(h["X-Kept"], "from-config");
    }

    #[test]
    fn env_file_is_merged_under_the_inline_env() {
        let dir = std::env::temp_dir().join("dangler-env-file-test");
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("creds");
        std::fs::write(
            &path,
            "# a comment\n\nSECRET=mongodb://host/db\nQUOTED=\"with spaces\"\nOVERRIDDEN=from-file\n",
        )
        .unwrap();

        let cfg: Config = toml::from_str(&format!(
            r#"
            [servers.alpha]
            command = "npx"
            env_file = "{}"
            [servers.alpha.env]
            OVERRIDDEN = "from-config"
            "#,
            path.display().to_string().replace('\\', "/")
        ))
        .unwrap();

        let env = cfg.servers["alpha"].child_env().unwrap();
        assert_eq!(env["SECRET"], "mongodb://host/db");
        assert_eq!(env["QUOTED"], "with spaces");
        // the inline map wins, so a config can override one key of a shared file
        assert_eq!(env["OVERRIDDEN"], "from-config");
        assert!(!env.contains_key("# a comment"));
    }

    #[test]
    fn missing_env_file_names_the_file() {
        let cfg: Config = toml::from_str(
            r#"
            [servers.alpha]
            command = "npx"
            env_file = "/no/such/credentials"
            "#,
        )
        .unwrap();
        let err = cfg.servers["alpha"].child_env().unwrap_err().to_string();
        assert!(err.contains("credentials"), "{err}");
    }

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
        assert_eq!(alpha.command.as_deref(), Some("npx"));
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
