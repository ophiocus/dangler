//! dangler-godaddy — GoDaddy MCP server, a first-party dangler fleet extension.
//!
//! Serves MCP over stdio. Credentials resolve per-call from the environment
//! (see `api::SETUP_HINT`), so the server starts — and `dangler warm` harvests
//! its schemas — without any provisioning.

mod api;
mod server;

use anyhow::Result;
use rmcp::ServiceExt;

use server::Godaddy;

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

    tracing::info!(
        env = %std::env::var("GODADDY_ENV").unwrap_or_else(|_| "prod".into()),
        read_only = std::env::var_os("GODADDY_READ_ONLY").is_some(),
        "dangler-godaddy starting (stdio)"
    );
    let service = Godaddy::new().serve(rmcp::transport::stdio()).await?;
    service.waiting().await?;
    Ok(())
}
