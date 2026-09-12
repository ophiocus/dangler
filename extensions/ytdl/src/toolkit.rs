//! Locating and driving the bundled ytdownloader toolkit.
//!
//! The toolkit is a directory carrying its own `bin/yt-dlp.exe`, `bin/deno.exe`,
//! `bin/ffmpeg.exe` and a `scripts/` folder — nothing is expected on the host
//! PATH except a Python interpreter for the transcript scripts.
//!
//! Resolution is lazy, in the same spirit as the godaddy extension's
//! credentials: the server starts and advertises its schema with no toolkit
//! present (so `dangler warm` can harvest a cold server), and every call that
//! needs the binaries fails with [`SETUP_HINT`] instead.

use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::Duration;

use anyhow::{Context, Result, anyhow, bail};
use tokio::process::Command;

pub const SETUP_HINT: &str = "Point YTDL_HOME at the ytdownloader toolkit directory — the one \
     containing bin/yt-dlp.exe, bin/deno.exe, bin/ffmpeg.exe and scripts/. dangler.toml names \
     the path. A Python interpreter is needed for the transcript tools; override the default \
     `python` with YTDL_PYTHON.";

/// How long a single toolkit invocation may run before it is killed.
///
/// Downloads are synchronous: a long video holds the MCP call open for its
/// whole duration. The ceiling keeps a wedged extractor from holding it open
/// forever.
fn timeout_secs() -> u64 {
    std::env::var("YTDL_TIMEOUT_SECS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(900)
}

/// Refuse anything that writes to disk.
pub fn read_only() -> bool {
    std::env::var("YTDL_READ_ONLY").is_ok_and(|v| !v.is_empty())
}

/// The toolkit root, from `YTDL_HOME`.
pub fn home() -> Result<PathBuf> {
    let raw = std::env::var("YTDL_HOME")
        .ok()
        .filter(|v| !v.is_empty())
        .ok_or_else(|| anyhow!("YTDL_HOME is not set. {SETUP_HINT}"))?;
    let path = PathBuf::from(raw);
    if !path.is_dir() {
        bail!("YTDL_HOME ({}) is not a directory. {SETUP_HINT}", path.display());
    }
    Ok(path)
}

/// A toolkit file that must exist, with the setup hint attached when it doesn't.
fn required(home: &Path, rel: &str) -> Result<PathBuf> {
    let path = home.join(rel);
    if !path.exists() {
        bail!("{} is missing from the toolkit. {SETUP_HINT}", path.display());
    }
    Ok(path)
}

/// Where downloads land when the caller names no directory.
pub fn downloads_dir(home: &Path) -> PathBuf {
    match std::env::var("YTDL_DOWNLOADS") {
        Ok(v) if !v.is_empty() => PathBuf::from(v),
        _ => home.join("downloads"),
    }
}

/// The captured browser session, when one exists.
///
/// This is a credential: a signed-in jar is equivalent to being logged in as
/// that account. It is read for its presence and age, never for its contents.
pub fn cookie_jar(home: &Path) -> Option<PathBuf> {
    let jar = home.join("auth").join("cookies.txt");
    jar.is_file().then_some(jar)
}

/// The yt-dlp invocation every download shares: bundled JS runtime, bundled
/// ffmpeg, and the captured session when one has been captured.
pub fn ytdlp_base(home: &Path) -> Result<Vec<String>> {
    let deno = required(home, "bin/deno.exe")?;
    let bin = home.join("bin");
    let mut args = vec![
        "--js-runtimes".to_string(),
        format!("deno:{}", deno.display()),
        "--ffmpeg-location".to_string(),
        bin.display().to_string(),
    ];
    if let Some(jar) = cookie_jar(home) {
        args.push("--cookies".to_string());
        args.push(jar.display().to_string());
    }
    Ok(args)
}

pub fn ytdlp(home: &Path) -> Result<PathBuf> {
    required(home, "bin/yt-dlp.exe")
}

pub fn script(home: &Path, name: &str) -> Result<PathBuf> {
    required(home, &format!("scripts/{name}"))
}

pub fn python() -> String {
    match std::env::var("YTDL_PYTHON") {
        Ok(v) if !v.is_empty() => v,
        _ => "python".to_string(),
    }
}

/// What a finished toolkit invocation produced.
pub struct Run {
    pub stdout: String,
    pub stderr: String,
}

/// Run one toolkit command to completion, capturing both streams.
///
/// stdout is captured rather than inherited because this process's own stdout
/// is the MCP transport — a child writing to it would corrupt the protocol.
pub async fn run(program: &Path, args: &[String], cwd: &Path) -> Result<Run> {
    tracing::debug!(program = %program.display(), ?args, "toolkit invocation");
    let child = Command::new(program)
        .args(args)
        .current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .with_context(|| format!("spawning {}", program.display()))?;

    let secs = timeout_secs();
    let out = tokio::time::timeout(Duration::from_secs(secs), child.wait_with_output())
        .await
        .map_err(|_| {
            anyhow!(
                "timed out after {secs}s — raise YTDL_TIMEOUT_SECS for long videos, or check \
                 whether the extractor is stuck"
            )
        })?
        .with_context(|| format!("running {}", program.display()))?;

    let stdout = String::from_utf8_lossy(&out.stdout).into_owned();
    let stderr = String::from_utf8_lossy(&out.stderr).into_owned();

    if !out.status.success() {
        bail!(
            "{} exited with {}: {}",
            program.display(),
            out.status,
            tail(&stderr, 20)
        );
    }
    Ok(Run { stdout, stderr })
}

/// The last `n` non-empty lines, for error messages that quote a child's noise
/// without relaying a screenful of progress bars.
pub fn tail(text: &str, n: usize) -> String {
    let lines: Vec<&str> = text.lines().filter(|l| !l.trim().is_empty()).collect();
    let start = lines.len().saturating_sub(n);
    lines[start..].join("\n")
}
