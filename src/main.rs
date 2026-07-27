//! dangler — an MCP pre-loader.
//!
//! One MCP server that fronts a configured fleet of downstream MCP servers,
//! exposing five meta-tools instead of the fleet's full schema surface.
//! `dangler` serves MCP over stdio; `dangler warm` pre-harvests every server's
//! schemas into the persistent cache and exits.

mod config;
mod fleet;
mod server;

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
    let config = Config::load(&config_path)?;

    if std::env::args().nth(1).as_deref() == Some("warm") {
        return Ok(warm(config, &config_path).await);
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
