//! dangler-cloudflare — Cloudflare MCP server, a first-party dangler fleet
//! extension.
//!
//! Serves MCP over stdio. The token resolves per-call from a referenced file
//! (see `api::SETUP_HINT`), so the server starts — and `dangler warm` harvests
//! its schemas — without any provisioning.

mod api;
mod server;

use anyhow::Result;
use rmcp::ServiceExt;

use server::Cloudflare;

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
        read_only = std::env::var_os("CLOUDFLARE_READ_ONLY").is_some(),
        credentials_file = %std::env::var("CLOUDFLARE_CREDENTIALS_FILE")
            .unwrap_or_else(|_| "<unset>".into()),
        "dangler-cloudflare starting (stdio)"
    );
    let service = Cloudflare::new().serve(rmcp::transport::stdio()).await?;
    service.waiting().await?;
    Ok(())
}
