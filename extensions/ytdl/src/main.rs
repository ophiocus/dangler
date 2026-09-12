//! dangler-ytdl — YouTube archiving MCP server, a first-party dangler fleet
//! extension.
//!
//! Serves MCP over stdio. The bundled toolkit resolves per-call from
//! `YTDL_HOME` (see `toolkit::SETUP_HINT`), so the server starts — and
//! `dangler warm` harvests its schemas — with no toolkit present at all.

mod server;
mod toolkit;

use anyhow::Result;
use rmcp::ServiceExt;

use server::Ytdl;

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
        home = %std::env::var("YTDL_HOME").unwrap_or_else(|_| "<unset>".into()),
        read_only = std::env::var_os("YTDL_READ_ONLY").is_some(),
        "dangler-ytdl starting (stdio)"
    );
    let service = Ytdl::new().serve(rmcp::transport::stdio()).await?;
    service.waiting().await?;
    Ok(())
}
