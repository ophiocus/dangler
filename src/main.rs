mod config;
mod fleet;
mod server;

use std::sync::Arc;

use anyhow::Result;
use rmcp::ServiceExt;

use config::Config;
use fleet::Fleet;
use server::Dangler;

#[tokio::main]
async fn main() -> Result<()> {
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

    let path = Config::default_path();
    let cfg = Config::load(&path)?;

    // `dangler warm` — pre-loader mode: harvest every server's schemas into the
    // persistent cache, then exit. No MCP client involved.
    if std::env::args().nth(1).as_deref() == Some("warm") {
        eprintln!(
            "warming {} server(s) from {}",
            cfg.servers.len(),
            path.display()
        );
        let fleet = Fleet::new(cfg);
        let mut failures = 0;
        for (name, res) in fleet.warm_all().await {
            match res {
                Ok(n) => eprintln!("  {name}: {n} tools cached"),
                Err(e) => {
                    failures += 1;
                    eprintln!("  {name}: FAILED — {e:#}");
                }
            }
        }
        eprintln!("cache: {}", fleet::cache_path().display());
        std::process::exit(if failures > 0 { 1 } else { 0 });
    }

    tracing::info!(
        config = %path.display(),
        servers = cfg.servers.len(),
        "dangler starting (stdio)"
    );

    let fleet = Arc::new(Fleet::new(cfg));
    tokio::spawn({
        let fleet = fleet.clone();
        async move { fleet.reap_loop().await }
    });
    let service = Dangler::new(fleet).serve(rmcp::transport::stdio()).await?;
    service.waiting().await?;
    Ok(())
}
