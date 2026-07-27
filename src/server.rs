//! The upstream face of dangler: a five-meta-tool MCP server over the fleet.
//!
//! Implemented as a manual [`ServerHandler`] (no `#[tool]` macros) because the
//! whole point is that the real tool surface lives downstream and is discovered
//! at runtime — dangler's own schema must stay tiny and static.

use std::sync::Arc;

use rmcp::model::{
    CallToolRequestParams, CallToolResult, ContentBlock, Implementation, JsonObject,
    ListToolsResult, PaginatedRequestParams, ServerCapabilities, ServerInfo, Tool,
};
use rmcp::service::{RequestContext, RoleServer};
use rmcp::{ErrorData as McpError, ServerHandler};
use serde::Deserialize;
use serde_json::json;

use crate::fleet::Fleet;

// Meta-tool names — the single source of truth shared by the advertised tool
// list and the dispatch match below.
const TOOL_LIST_SERVERS: &str = "list_servers";
const TOOL_SEARCH_TOOLS: &str = "search_tools";
const TOOL_LOAD_SERVER: &str = "load_server";
const TOOL_CALL_TOOL: &str = "call_tool";
const TOOL_DROP_SERVER: &str = "drop_server";

/// MCP server handler exposing the meta-tools; all real work delegates to [`Fleet`].
#[derive(Clone)]
pub struct Dangler {
    fleet: Arc<Fleet>,
}

/// Arguments for meta-tools addressing one server by name (`load_server`, `drop_server`).
#[derive(Deserialize)]
struct ServerNameArgs {
    name: String,
}

/// Arguments for `search_tools`.
#[derive(Deserialize)]
struct SearchArgs {
    query: String,
}

/// Arguments for `call_tool`: which downstream tool to invoke, and with what.
#[derive(Deserialize)]
struct CallArgs {
    server: String,
    tool: String,
    #[serde(default)]
    arguments: Option<JsonObject>,
}

/// Wrap a `json!` object literal as the `Arc<JsonObject>` a [`Tool`] schema wants.
fn schema(literal: serde_json::Value) -> Arc<JsonObject> {
    Arc::new(
        literal
            .as_object()
            .expect("schema literal is an object")
            .clone(),
    )
}

/// Deserialize meta-tool arguments, mapping failures to an MCP invalid-params error.
fn parse_args<T: for<'de> Deserialize<'de>>(args: Option<JsonObject>) -> Result<T, McpError> {
    serde_json::from_value(serde_json::Value::Object(args.unwrap_or_default()))
        .map_err(|e| McpError::invalid_params(format!("bad arguments: {e}"), None))
}

/// Render a JSON value as a successful text tool result (pretty-printed).
fn text_result(value: serde_json::Value) -> CallToolResult {
    CallToolResult::success(vec![ContentBlock::text(
        serde_json::to_string_pretty(&value).unwrap_or_else(|_| value.to_string()),
    )])
}

/// Map a fleet error (spawn/handshake/downstream failure) to an MCP internal error.
fn internal_error(e: anyhow::Error) -> McpError {
    McpError::internal_error(format!("{e:#}"), None)
}

impl Dangler {
    pub fn new(fleet: Arc<Fleet>) -> Self {
        Self { fleet }
    }

    /// The static meta-tool surface — the only schemas an MCP client ever pays for up front.
    fn meta_tools() -> Vec<Tool> {
        vec![
            Tool::new(
                TOOL_LIST_SERVERS,
                "List the configured downstream MCP servers: name, warm/cold status, and \
                 cached tool count (if the server has been loaded before).",
                schema(json!({"type": "object", "properties": {}})),
            ),
            Tool::new(
                TOOL_SEARCH_TOOLS,
                "Search tool names and descriptions across all downstream servers whose \
                 schemas are cached. Servers never loaded are not indexed yet — the result \
                 names them so you can load_server them first.",
                schema(json!({
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "substring, case-insensitive"}},
                    "required": ["query"]
                })),
            ),
            Tool::new(
                TOOL_LOAD_SERVER,
                "Spawn a downstream server (if cold) and return its full tool schemas. \
                 Use this to see exactly how to call its tools via call_tool.",
                schema(json!({
                    "type": "object",
                    "properties": {"name": {"type": "string", "description": "server name from list_servers"}},
                    "required": ["name"]
                })),
            ),
            Tool::new(
                TOOL_CALL_TOOL,
                "Call a tool on a downstream server (spawns it if cold). 'arguments' must \
                 match the tool's schema as returned by load_server.",
                schema(json!({
                    "type": "object",
                    "properties": {
                        "server": {"type": "string"},
                        "tool": {"type": "string"},
                        "arguments": {"type": "object", "description": "arguments for the downstream tool"}
                    },
                    "required": ["server", "tool"]
                })),
            ),
            Tool::new(
                TOOL_DROP_SERVER,
                "Stop a running downstream server and free its process. Its cached schemas \
                 remain searchable.",
                schema(json!({
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"]
                })),
            ),
        ]
    }
}

impl ServerHandler for Dangler {
    fn get_info(&self) -> ServerInfo {
        ServerInfo::new(ServerCapabilities::builder().enable_tools().build())
            .with_server_info(Implementation::new("dangler", env!("CARGO_PKG_VERSION")))
            .with_instructions(
                "dangler fronts a fleet of MCP servers so their schemas don't all load up \
                 front. Discover with list_servers/search_tools, inspect with load_server, \
                 then drive real work with call_tool {server, tool, arguments}.",
            )
    }

    async fn list_tools(
        &self,
        _request: Option<PaginatedRequestParams>,
        _ctx: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, McpError> {
        Ok(ListToolsResult {
            tools: Self::meta_tools(),
            next_cursor: None,
            meta: None,
        })
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        _ctx: RequestContext<RoleServer>,
    ) -> Result<CallToolResult, McpError> {
        tracing::debug!(tool = %request.name, "meta-tool call");
        match request.name.as_ref() {
            TOOL_LIST_SERVERS => {
                let rows: Vec<_> = self
                    .fleet
                    .statuses()
                    .await
                    .iter()
                    .map(|s| {
                        json!({
                            "name": s.name,
                            "status": if s.warm { "warm" } else { "cold" },
                            "cached_tools": s.cached_tools,
                        })
                    })
                    .collect();
                Ok(text_result(json!({"servers": rows})))
            }
            TOOL_SEARCH_TOOLS => {
                let args: SearchArgs = parse_args(request.arguments)?;
                let hits: Vec<_> = self
                    .fleet
                    .search(&args.query)
                    .await
                    .iter()
                    .map(|h| json!({"server": h.server, "tool": h.tool, "description": h.description}))
                    .collect();
                Ok(text_result(json!({
                    "hits": hits,
                    "not_indexed_yet": self.fleet.unindexed().await,
                })))
            }
            TOOL_LOAD_SERVER => {
                let args: ServerNameArgs = parse_args(request.arguments)?;
                let tools = self.fleet.load(&args.name).await.map_err(internal_error)?;
                Ok(text_result(json!({
                    "server": args.name,
                    "tools": tools,
                })))
            }
            TOOL_CALL_TOOL => {
                let args: CallArgs = parse_args(request.arguments)?;
                self.fleet
                    .call(&args.server, &args.tool, args.arguments)
                    .await
                    .map_err(internal_error)
            }
            TOOL_DROP_SERVER => {
                let args: ServerNameArgs = parse_args(request.arguments)?;
                let was_running = self
                    .fleet
                    .drop_server(&args.name)
                    .await
                    .map_err(internal_error)?;
                Ok(text_result(json!({
                    "server": args.name,
                    "dropped": was_running,
                })))
            }
            other => Err(McpError::invalid_params(
                format!("unknown tool '{other}'"),
                None,
            )),
        }
    }
}
