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

/// The upstream face of dangler: five meta-tools over a dynamic fleet.
/// Implemented as a manual ServerHandler (no #[tool] macros) because the whole point
/// is that the real tool surface lives downstream and is discovered at runtime.
#[derive(Clone)]
pub struct Dangler {
    fleet: Arc<Fleet>,
}

#[derive(Deserialize)]
struct NameArg {
    name: String,
}

#[derive(Deserialize)]
struct SearchArgs {
    query: String,
}

#[derive(Deserialize)]
struct CallArgs {
    server: String,
    tool: String,
    #[serde(default)]
    arguments: Option<JsonObject>,
}

fn schema(v: serde_json::Value) -> Arc<JsonObject> {
    Arc::new(v.as_object().expect("schema literal is an object").clone())
}

fn parse<T: for<'de> Deserialize<'de>>(args: Option<JsonObject>) -> Result<T, McpError> {
    serde_json::from_value(serde_json::Value::Object(args.unwrap_or_default()))
        .map_err(|e| McpError::invalid_params(format!("bad arguments: {e}"), None))
}

fn text_result(v: serde_json::Value) -> CallToolResult {
    CallToolResult::success(vec![ContentBlock::text(
        serde_json::to_string_pretty(&v).unwrap_or_else(|_| v.to_string()),
    )])
}

fn internal(e: anyhow::Error) -> McpError {
    McpError::internal_error(format!("{e:#}"), None)
}

impl Dangler {
    pub fn new(fleet: Arc<Fleet>) -> Self {
        Self { fleet }
    }

    fn meta_tools() -> Vec<Tool> {
        vec![
            Tool::new(
                "list_servers",
                "List the configured downstream MCP servers: name, warm/cold status, and \
                 cached tool count (if the server has been loaded before).",
                schema(json!({"type": "object", "properties": {}})),
            ),
            Tool::new(
                "search_tools",
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
                "load_server",
                "Spawn a downstream server (if cold) and return its full tool schemas. \
                 Use this to see exactly how to call its tools via call_tool.",
                schema(json!({
                    "type": "object",
                    "properties": {"name": {"type": "string", "description": "server name from list_servers"}},
                    "required": ["name"]
                })),
            ),
            Tool::new(
                "call_tool",
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
                "drop_server",
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
            "list_servers" => {
                let statuses = self.fleet.statuses().await;
                let rows: Vec<_> = statuses
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
            "search_tools" => {
                let args: SearchArgs = parse(request.arguments)?;
                let hits = self.fleet.search(&args.query).await;
                let unindexed = self.fleet.unindexed().await;
                let rows: Vec<_> = hits
                    .iter()
                    .map(|h| json!({"server": h.server, "tool": h.tool, "description": h.description}))
                    .collect();
                Ok(text_result(json!({
                    "hits": rows,
                    "not_indexed_yet": unindexed,
                })))
            }
            "load_server" => {
                let args: NameArg = parse(request.arguments)?;
                let tools = self.fleet.load(&args.name).await.map_err(internal)?;
                Ok(text_result(json!({
                    "server": args.name,
                    "tools": tools,
                })))
            }
            "call_tool" => {
                let args: CallArgs = parse(request.arguments)?;
                self.fleet
                    .call(&args.server, &args.tool, args.arguments)
                    .await
                    .map_err(internal)
            }
            "drop_server" => {
                let args: NameArg = parse(request.arguments)?;
                let was_running = self.fleet.drop_server(&args.name).await.map_err(internal)?;
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
