//! The MCP face of the GoDaddy extension: a static tool surface over the
//! GoDaddy REST APIs, written as a manual [`ServerHandler`] in the same style
//! as dangler's own meta-tool server.
//!
//! Read-only mode: set `GODADDY_READ_ONLY=1` (any non-empty value) and every
//! mutating tool — and any non-GET `raw_api` call — is refused.

use std::sync::Arc;

use rmcp::model::{
    CallToolRequestParams, CallToolResult, ContentBlock, Implementation, JsonObject,
    ListToolsResult, PaginatedRequestParams, ServerCapabilities, ServerInfo, Tool,
};
use rmcp::service::{RequestContext, RoleServer};
use rmcp::{ErrorData as McpError, ServerHandler};
use serde::Deserialize;
use serde_json::{Value, json};

use crate::api;

/// MCP server handler for the GoDaddy tool surface.
#[derive(Clone)]
pub struct Godaddy {
    http: reqwest::Client,
}

/// One DNS record, as the GoDaddy records endpoints exchange them.
#[derive(Deserialize, serde::Serialize)]
struct DnsRecord {
    /// A, AAAA, CNAME, MX, NS, SOA, SRV, TXT
    #[serde(rename = "type")]
    rtype: String,
    /// `@` for the apex, `www`, `*.dev`, …
    name: String,
    data: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    ttl: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    priority: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    weight: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    port: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    service: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    protocol: Option<String>,
}

#[derive(Deserialize)]
struct DomainArgs {
    domain: String,
}

#[derive(Deserialize)]
struct ListDomainsArgs {
    /// Filter by status, e.g. ACTIVE, EXPIRED (optional).
    statuses: Option<String>,
    limit: Option<u32>,
    /// Continue listing after this domain name (pagination marker).
    marker: Option<String>,
}

#[derive(Deserialize)]
struct ListRecordsArgs {
    domain: String,
    /// Record type filter (A, CNAME, …). Required when `name` is set.
    #[serde(rename = "type")]
    rtype: Option<String>,
    name: Option<String>,
}

#[derive(Deserialize)]
struct RecordsArgs {
    domain: String,
    records: Vec<DnsRecord>,
}

#[derive(Deserialize)]
struct SetRecordsArgs {
    domain: String,
    #[serde(rename = "type")]
    rtype: String,
    name: String,
    /// New value(s) for exactly this type+name; other records are untouched.
    records: Vec<SetRecordValue>,
}

/// Record value for the type+name-scoped PUT: type/name come from the path.
#[derive(Deserialize, serde::Serialize)]
struct SetRecordValue {
    data: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    ttl: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    priority: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    weight: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    port: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    service: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    protocol: Option<String>,
}

#[derive(Deserialize)]
struct DeleteRecordArgs {
    domain: String,
    #[serde(rename = "type")]
    rtype: String,
    name: String,
}

#[derive(Deserialize)]
struct NameserversArgs {
    domain: String,
    /// Complete replacement set, 2–13 hostnames.
    nameservers: Vec<String>,
}

#[derive(Deserialize)]
struct RawApiArgs {
    /// GET, POST, PUT, PATCH, DELETE
    method: String,
    /// Absolute API path, e.g. `/v1/subscriptions` or `/v2/customers/{id}/domains`.
    path: String,
    /// Query parameters as a flat string map.
    #[serde(default)]
    query: Option<JsonObject>,
    /// JSON request body for POST/PUT/PATCH.
    #[serde(default)]
    body: Option<Value>,
}

fn schema(literal: Value) -> Arc<JsonObject> {
    Arc::new(
        literal
            .as_object()
            .expect("schema literal is an object")
            .clone(),
    )
}

fn parse_args<T: for<'de> Deserialize<'de>>(args: Option<JsonObject>) -> Result<T, McpError> {
    serde_json::from_value(Value::Object(args.unwrap_or_default()))
        .map_err(|e| McpError::invalid_params(format!("bad arguments: {e}"), None))
}

fn text_result(value: Value) -> CallToolResult {
    CallToolResult::success(vec![ContentBlock::text(
        serde_json::to_string_pretty(&value).unwrap_or_else(|_| value.to_string()),
    )])
}

fn api_error(e: anyhow::Error) -> McpError {
    McpError::internal_error(format!("{e:#}"), None)
}

fn read_only() -> bool {
    std::env::var("GODADDY_READ_ONLY").is_ok_and(|v| !v.is_empty())
}

fn refuse_write(tool: &str) -> McpError {
    McpError::invalid_params(
        format!("'{tool}' is a write operation and GODADDY_READ_ONLY is set"),
        None,
    )
}

/// JSON schema fragment for one DNS record object (shared by the write tools).
fn record_schema() -> Value {
    json!({
        "type": "object",
        "properties": {
            "type": {"type": "string", "description": "A, AAAA, CNAME, MX, NS, SRV, TXT"},
            "name": {"type": "string", "description": "'@' for apex, 'www', '*.dev', …"},
            "data": {"type": "string", "description": "record value (IP, hostname, text…)"},
            "ttl":  {"type": "integer", "description": "seconds; GoDaddy minimum 600"},
            "priority": {"type": "integer"}, "weight": {"type": "integer"},
            "port": {"type": "integer"}, "service": {"type": "string"},
            "protocol": {"type": "string"}
        },
        "required": ["type", "name", "data"]
    })
}

const TOOL_LIST_DOMAINS: &str = "list_domains";
const TOOL_GET_DOMAIN: &str = "get_domain";
const TOOL_CHECK_AVAILABILITY: &str = "check_availability";
const TOOL_LIST_TLDS: &str = "list_tlds";
const TOOL_LIST_DNS: &str = "list_dns_records";
const TOOL_ADD_DNS: &str = "add_dns_records";
const TOOL_SET_DNS: &str = "set_dns_records";
const TOOL_DELETE_DNS: &str = "delete_dns_record";
const TOOL_REPLACE_ALL_DNS: &str = "replace_all_dns_records";
const TOOL_SET_NAMESERVERS: &str = "set_nameservers";
const TOOL_LIST_SUBSCRIPTIONS: &str = "list_subscriptions";
const TOOL_RAW_API: &str = "raw_api";

impl Godaddy {
    pub fn new() -> Self {
        Self {
            http: reqwest::Client::new(),
        }
    }

    fn tools() -> Vec<Tool> {
        vec![
            Tool::new(
                TOOL_LIST_DOMAINS,
                "List domains in the GoDaddy account (name, status, expiry). Paginate with \
                 'marker' = last domain of the previous page.",
                schema(json!({
                    "type": "object",
                    "properties": {
                        "statuses": {"type": "string", "description": "comma-separated filter, e.g. ACTIVE"},
                        "limit": {"type": "integer"},
                        "marker": {"type": "string"}
                    }
                })),
            ),
            Tool::new(
                TOOL_GET_DOMAIN,
                "Full detail for one owned domain: status, expiry, nameservers, contacts, \
                 privacy, locked/renewAuto flags.",
                schema(json!({
                    "type": "object",
                    "properties": {"domain": {"type": "string"}},
                    "required": ["domain"]
                })),
            ),
            Tool::new(
                TOOL_CHECK_AVAILABILITY,
                "Check whether a domain is available to register, with price when it is. \
                 NOTE: GoDaddy gates this endpoint behind a 50+-domain account (403 \
                 ACCESS_DENIED below that — an account-tier limit, not a bug). \
                 Alternatives: GoDaddy's free no-auth hosted MCP at \
                 https://api.godaddy.com/v1/domains/mcp, or GODADDY_ENV=ote (test data).",
                schema(json!({
                    "type": "object",
                    "properties": {"domain": {"type": "string"}},
                    "required": ["domain"]
                })),
            ),
            Tool::new(
                TOOL_LIST_TLDS,
                "List TLDs GoDaddy supports for registration.",
                schema(json!({"type": "object", "properties": {}})),
            ),
            Tool::new(
                TOOL_LIST_DNS,
                "List DNS records for an owned domain, optionally filtered by type and name.",
                schema(json!({
                    "type": "object",
                    "properties": {
                        "domain": {"type": "string"},
                        "type": {"type": "string", "description": "A, AAAA, CNAME, MX, NS, SRV, TXT"},
                        "name": {"type": "string", "description": "requires 'type' when set"}
                    },
                    "required": ["domain"]
                })),
            ),
            Tool::new(
                TOOL_ADD_DNS,
                "Append DNS records to a domain (existing records untouched). Safe additive \
                 write.",
                schema(json!({
                    "type": "object",
                    "properties": {
                        "domain": {"type": "string"},
                        "records": {"type": "array", "items": record_schema()}
                    },
                    "required": ["domain", "records"]
                })),
            ),
            Tool::new(
                TOOL_SET_DNS,
                "Replace the value(s) of exactly one type+name pair (e.g. the A records of \
                 '@'). Records of other types/names are untouched. The right tool for \
                 pointing a domain at a server.",
                schema(json!({
                    "type": "object",
                    "properties": {
                        "domain": {"type": "string"},
                        "type": {"type": "string"},
                        "name": {"type": "string"},
                        "records": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "data": {"type": "string"},
                                    "ttl": {"type": "integer"},
                                    "priority": {"type": "integer"}, "weight": {"type": "integer"},
                                    "port": {"type": "integer"}, "service": {"type": "string"},
                                    "protocol": {"type": "string"}
                                },
                                "required": ["data"]
                            }
                        }
                    },
                    "required": ["domain", "type", "name", "records"]
                })),
            ),
            Tool::new(
                TOOL_DELETE_DNS,
                "DESTRUCTIVE: delete all records of one type+name pair from a domain.",
                schema(json!({
                    "type": "object",
                    "properties": {
                        "domain": {"type": "string"},
                        "type": {"type": "string"},
                        "name": {"type": "string"}
                    },
                    "required": ["domain", "type", "name"]
                })),
            ),
            Tool::new(
                TOOL_REPLACE_ALL_DNS,
                "DESTRUCTIVE: replace the domain's ENTIRE record set with the given list — \
                 anything not listed is deleted. Prefer set_dns_records / add_dns_records.",
                schema(json!({
                    "type": "object",
                    "properties": {
                        "domain": {"type": "string"},
                        "records": {"type": "array", "items": record_schema()}
                    },
                    "required": ["domain", "records"]
                })),
            ),
            Tool::new(
                TOOL_SET_NAMESERVERS,
                "DESTRUCTIVE: replace the domain's nameserver set (2-13 hostnames). \
                 Delegating away from GoDaddy DNS makes every dns_* tool moot for this \
                 domain and a wrong set takes the site offline.",
                schema(json!({
                    "type": "object",
                    "properties": {
                        "domain": {"type": "string"},
                        "nameservers": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["domain", "nameservers"]
                })),
            ),
            Tool::new(
                TOOL_LIST_SUBSCRIPTIONS,
                "List the account's product subscriptions (domains, websites+marketing, \
                 email/Microsoft 365, SSL…) — the aggregate view of everything the account \
                 pays GoDaddy for.",
                schema(json!({"type": "object", "properties": {}})),
            ),
            Tool::new(
                TOOL_RAW_API,
                "Escape hatch to ANY GoDaddy REST endpoint (api.godaddy.com; OTE via \
                 GODADDY_ENV=ote): certificates, orders, agreements, shoppers, aftermarket, \
                 v2 customer APIs (e.g. domain forwarding at \
                 /v2/customers/{customerId}/domains/forwards/{fqdn}), v3 zones \
                 (/v3/domains/zones/{zone}/dns-records — PAT auth only)… \
                 Method+path+query+body with the configured auth applied. Treat non-GET \
                 calls as writes with real-world effect.",
                schema(json!({
                    "type": "object",
                    "properties": {
                        "method": {"type": "string", "description": "GET, POST, PUT, PATCH, DELETE"},
                        "path": {"type": "string", "description": "e.g. /v1/agreements or /v1/certificates/{id}"},
                        "query": {"type": "object", "description": "flat string map of query params"},
                        "body": {"description": "JSON body for POST/PUT/PATCH"}
                    },
                    "required": ["method", "path"]
                })),
            ),
        ]
    }

    async fn get(
        &self,
        path: &str,
        query: &[(String, String)],
    ) -> Result<CallToolResult, McpError> {
        api::call(&self.http, "GET", path, query, None)
            .await
            .map(text_result)
            .map_err(api_error)
    }

    async fn write(
        &self,
        method: &str,
        path: &str,
        body: Option<&Value>,
    ) -> Result<CallToolResult, McpError> {
        api::call(&self.http, method, path, &[], body)
            .await
            .map(text_result)
            .map_err(api_error)
    }
}

impl ServerHandler for Godaddy {
    fn get_info(&self) -> ServerInfo {
        ServerInfo::new(ServerCapabilities::builder().enable_tools().build())
            .with_server_info(Implementation::new(
                "dangler-godaddy",
                env!("CARGO_PKG_VERSION"),
            ))
            .with_instructions(
                "GoDaddy account operations: domains, DNS records, subscriptions, and a \
                 raw_api escape hatch to every other GoDaddy REST endpoint. DNS writes: \
                 set_dns_records for one type+name, add_dns_records to append; the two \
                 DESTRUCTIVE tools delete data. Credentials come from the environment — \
                 calls explain what's missing.",
            )
    }

    async fn list_tools(
        &self,
        _request: Option<PaginatedRequestParams>,
        _ctx: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, McpError> {
        Ok(ListToolsResult {
            tools: Self::tools(),
            next_cursor: None,
            meta: None,
        })
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        _ctx: RequestContext<RoleServer>,
    ) -> Result<CallToolResult, McpError> {
        tracing::debug!(tool = %request.name, "godaddy tool call");
        match request.name.as_ref() {
            TOOL_LIST_DOMAINS => {
                let a: ListDomainsArgs = parse_args(request.arguments)?;
                let mut q = Vec::new();
                if let Some(s) = a.statuses {
                    q.push(("statuses".into(), s));
                }
                if let Some(l) = a.limit {
                    q.push(("limit".into(), l.to_string()));
                }
                if let Some(m) = a.marker {
                    q.push(("marker".into(), m));
                }
                self.get("/v1/domains", &q).await
            }
            TOOL_GET_DOMAIN => {
                let a: DomainArgs = parse_args(request.arguments)?;
                self.get(&format!("/v1/domains/{}", a.domain), &[]).await
            }
            TOOL_CHECK_AVAILABILITY => {
                let a: DomainArgs = parse_args(request.arguments)?;
                self.get("/v1/domains/available", &[("domain".into(), a.domain)])
                    .await
            }
            TOOL_LIST_TLDS => self.get("/v1/domains/tlds", &[]).await,
            TOOL_LIST_DNS => {
                let a: ListRecordsArgs = parse_args(request.arguments)?;
                let path = match (&a.rtype, &a.name) {
                    (Some(t), Some(n)) => format!("/v1/domains/{}/records/{t}/{n}", a.domain),
                    (Some(t), None) => format!("/v1/domains/{}/records/{t}", a.domain),
                    (None, Some(_)) => {
                        return Err(McpError::invalid_params(
                            "'name' requires 'type'".to_string(),
                            None,
                        ));
                    }
                    (None, None) => format!("/v1/domains/{}/records", a.domain),
                };
                self.get(&path, &[]).await
            }
            TOOL_ADD_DNS => {
                if read_only() {
                    return Err(refuse_write(TOOL_ADD_DNS));
                }
                let a: RecordsArgs = parse_args(request.arguments)?;
                let body = serde_json::to_value(&a.records).map_err(|e| {
                    McpError::internal_error(format!("serializing records: {e}"), None)
                })?;
                self.write(
                    "PATCH",
                    &format!("/v1/domains/{}/records", a.domain),
                    Some(&body),
                )
                .await
            }
            TOOL_SET_DNS => {
                if read_only() {
                    return Err(refuse_write(TOOL_SET_DNS));
                }
                let a: SetRecordsArgs = parse_args(request.arguments)?;
                let body = serde_json::to_value(&a.records).map_err(|e| {
                    McpError::internal_error(format!("serializing records: {e}"), None)
                })?;
                self.write(
                    "PUT",
                    &format!("/v1/domains/{}/records/{}/{}", a.domain, a.rtype, a.name),
                    Some(&body),
                )
                .await
            }
            TOOL_DELETE_DNS => {
                if read_only() {
                    return Err(refuse_write(TOOL_DELETE_DNS));
                }
                let a: DeleteRecordArgs = parse_args(request.arguments)?;
                self.write(
                    "DELETE",
                    &format!("/v1/domains/{}/records/{}/{}", a.domain, a.rtype, a.name),
                    None,
                )
                .await
            }
            TOOL_REPLACE_ALL_DNS => {
                if read_only() {
                    return Err(refuse_write(TOOL_REPLACE_ALL_DNS));
                }
                let a: RecordsArgs = parse_args(request.arguments)?;
                let body = serde_json::to_value(&a.records).map_err(|e| {
                    McpError::internal_error(format!("serializing records: {e}"), None)
                })?;
                self.write(
                    "PUT",
                    &format!("/v1/domains/{}/records", a.domain),
                    Some(&body),
                )
                .await
            }
            TOOL_SET_NAMESERVERS => {
                if read_only() {
                    return Err(refuse_write(TOOL_SET_NAMESERVERS));
                }
                let a: NameserversArgs = parse_args(request.arguments)?;
                let body = json!({"nameServers": a.nameservers});
                self.write("PATCH", &format!("/v1/domains/{}", a.domain), Some(&body))
                    .await
            }
            TOOL_LIST_SUBSCRIPTIONS => self.get("/v1/subscriptions", &[]).await,
            TOOL_RAW_API => {
                let a: RawApiArgs = parse_args(request.arguments)?;
                let is_get = a.method.eq_ignore_ascii_case("get");
                if read_only() && !is_get {
                    return Err(refuse_write(TOOL_RAW_API));
                }
                let query: Vec<(String, String)> = a
                    .query
                    .unwrap_or_default()
                    .into_iter()
                    .map(|(k, v)| {
                        let v = match v {
                            Value::String(s) => s,
                            other => other.to_string(),
                        };
                        (k, v)
                    })
                    .collect();
                api::call(&self.http, &a.method, &a.path, &query, a.body.as_ref())
                    .await
                    .map(text_result)
                    .map_err(api_error)
            }
            other => Err(McpError::invalid_params(
                format!("unknown tool '{other}'"),
                None,
            )),
        }
    }
}
