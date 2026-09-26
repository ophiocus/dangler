//! dangler — an MCP pre-loader.
//!
//! One MCP server that fronts a configured fleet of downstream MCP servers,
//! exposing five meta-tools instead of the fleet's full schema surface.
//! `dangler` serves MCP over stdio; `dangler warm` pre-harvests every server's
//! schemas into the persistent cache and exits; `dangler skills` installs every
//! server's skill and reports, which also happens at every start.

mod config;
mod fleet;
mod oauth;
mod server;
mod skills;

use std::process::ExitCode;
use std::sync::Arc;

use anyhow::Result;
use rmcp::ServiceExt;

use config::Config;
use fleet::Fleet;
use server::Dangler;

#[tokio::main]
async fn main() -> Result<ExitCode> {
    // stdout is the MCP transport — all logging goes to stderr.
    let level = if std::env::var_os("DANGLER_DEBUG").is_some() {
        tracing::Level::DEBUG
    } else {
        tracing::Level::INFO
    };
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_max_level(level)
        .init();

    let config_path = Config::default_path();
    let mut config = Config::load(&config_path)?;

    // Forced default: every fleet entry's skill is installed before anything is
    // served, and an entry without one is disabled rather than dangled nameless.
    let report = skills::sync(&mut config)?;
    if std::env::args().nth(1).as_deref() == Some("skills") {
        eprint!("{}", skills::describe(&report));
        return Ok(if report.missing().next().is_some() {
            ExitCode::FAILURE
        } else {
            ExitCode::SUCCESS
        });
    }
    for o in report.missing() {
        tracing::error!(server = %o.server, reason = %o.problem.as_deref().unwrap_or(""), "disabled: no skill");
    }
    for p in &report.pruned {
        tracing::info!(path = %p.display(), "pruned skill of an entry no longer in the fleet");
    }

    if std::env::args().nth(1).as_deref() == Some("warm") {
        return Ok(warm(config, &config_path).await);
    }
    // `dangler auth <server>`: the one step a session cannot take for the
    // operator. Consent happens in their browser, and the grant is stored where
    // dangler keeps credentials — never in the fleet config, never in a repo.
    if std::env::args().nth(1).as_deref() == Some("auth") {
        let Some(name) = std::env::args().nth(2) else {
            eprintln!("usage: dangler auth <server>");
            return Ok(ExitCode::FAILURE);
        };
        let spec = config
            .servers
            .get(&name)
            .ok_or_else(|| anyhow::anyhow!("no server '{name}' in {}", config_path.display()))?;
        let crate::config::Transport::Http { url, oauth } = spec.transport()? else {
            eprintln!("'{name}' is a stdio server — nothing to authorize");
            return Ok(ExitCode::FAILURE);
        };
        if !oauth {
            eprintln!("'{name}' does not declare `auth = \"oauth\"` — it uses headers/header_file");
            return Ok(ExitCode::FAILURE);
        }
        let scopes: Vec<String> = std::env::args().skip(3).collect();
        let scope_refs: Vec<&str> = scopes.iter().map(String::as_str).collect();
        oauth::authorize(&name, &url, &scope_refs).await?;
        return Ok(ExitCode::SUCCESS);
    }
    serve(config, &config_path).await?;
    Ok(ExitCode::SUCCESS)
}

/// Pre-loader mode: harvest every server's schemas into the persistent cache,
/// report per-server results, and exit. No MCP client involved.
async fn warm(config: Config, config_path: &std::path::Path) -> ExitCode {
    eprintln!(
        "warming {} server(s) from {}",
        config.servers.len(),
        config_path.display()
    );
    let fleet = Fleet::new(config);
    let mut failures = 0;
    for (name, result) in fleet.warm_all().await {
        match result {
            Ok(tool_count) => eprintln!("  {name}: {tool_count} tools cached"),
            Err(e) => {
                failures += 1;
                eprintln!("  {name}: FAILED — {e:#}");
            }
        }
    }
    eprintln!("cache: {}", fleet::cache_path().display());
    if failures > 0 {
        ExitCode::FAILURE
    } else {
        ExitCode::SUCCESS
    }
}

/// Server mode: run the MCP stdio server with the idle reaper alongside,
/// until the client closes the transport.
async fn serve(config: Config, config_path: &std::path::Path) -> Result<()> {
    tracing::info!(
        config = %config_path.display(),
        servers = config.servers.len(),
        "dangler starting (stdio)"
    );
    let fleet = Arc::new(Fleet::new(config));
    tokio::spawn({
        let fleet = fleet.clone();
        async move { fleet.reap_loop().await }
    });
    let service = Dangler::new(fleet).serve(rmcp::transport::stdio()).await?;
    service.waiting().await?;
    Ok(())
}
