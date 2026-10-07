//! The MCP face of the Cloudflare extension: a named tool surface over the
//! Cloudflare v4 REST API, written as a manual [`ServerHandler`] in the same
//! style as dangler's own meta-tool server.
//!
//! The point of the named surface: the vendor's own plugin exposes an `execute`
//! tool that runs arbitrary JavaScript against the whole API under an account
//! login. These tools are the operations the fleet actually performs, each one
//! auditable from its name, and all of them behind one revocable scoped token.
//!
//! Read-only mode: set `CLOUDFLARE_READ_ONLY=1` (any non-empty value) and every
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

/// MCP server handler for the Cloudflare tool surface.
#[derive(Clone)]
pub struct Cloudflare {
    http: reqwest::Client,
}

#[derive(Deserialize)]
struct ZoneArgs {
    zone_id: String,
}

#[derive(Deserialize)]
struct ListZonesArgs {
    /// Exact zone name, e.g. "tecnocratica.com.co".
    name: Option<String>,
    status: Option<String>,
    page: Option<u32>,
    per_page: Option<u32>,
}

#[derive(Deserialize)]
struct ListDnsArgs {
    zone_id: String,
    #[serde(rename = "type")]
    rtype: Option<String>,
    /// Exact record name, e.g. "api--myevery.tecnocratica.com.co".
    name: Option<String>,
    page: Option<u32>,
    per_page: Option<u32>,
}

#[derive(Deserialize)]
struct CreateDnsArgs {
    zone_id: String,
    #[serde(rename = "type")]
    rtype: String,
    name: String,
    content: String,
    ttl: Option<u32>,
    /// Orange-cloud this record. Only meaningful for A/AAAA/CNAME.
    proxied: Option<bool>,
    priority: Option<u32>,
    comment: Option<String>,
}

#[derive(Deserialize)]
struct UpdateDnsArgs {
    zone_id: String,
    record_id: String,
    #[serde(rename = "type")]
    rtype: Option<String>,
    name: Option<String>,
    content: Option<String>,
    ttl: Option<u32>,
    proxied: Option<bool>,
    comment: Option<String>,
}

#[derive(Deserialize)]
struct DeleteDnsArgs {
    zone_id: String,
    record_id: String,
}

#[derive(Deserialize)]
struct PurgeArgs {
    zone_id: String,
    /// Purge the whole zone. Mutually exclusive with the selective fields.
    #[serde(default)]
    everything: bool,
    #[serde(default)]
    files: Vec<String>,
    /// Cache-tag purge. Available on every plan since 2025-04; only matches
    /// responses the origin tagged with a `Cache-Tag` header, on proxied names.
    #[serde(default)]
    tags: Vec<String>,
    #[serde(default)]
    hosts: Vec<String>,
    #[serde(default)]
    prefixes: Vec<String>,
}

#[derive(Deserialize)]
struct ListCertsArgs {
    zone_id: Option<String>,
}

#[derive(Deserialize)]
struct CertArgs {
    certificate_id: String,
}

#[derive(Deserialize)]
struct IssueCertArgs {
    /// PEM-encoded CSR. Generate it where the private key must stay — the key
    /// is never part of this call and must never be sent.
    csr: String,
    hostnames: Vec<String>,
    /// Days until expiry: 7, 30, 90, 365, 730, 1095 or 5475. Default 5475.
    requested_validity: Option<u32>,
    /// "origin-rsa", "origin-ecc" or "keyless-certificate". Default origin-rsa.
    request_type: Option<String>,
}

#[derive(Deserialize)]
struct RawApiArgs {
    /// GET, POST, PUT, PATCH, DELETE
    method: String,
    /// Absolute API path under /client/v4, e.g. `/zones` or `/accounts/{id}/tunnels`.
    path: String,
    #[serde(default)]
    query: Option<JsonObject>,
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

fn refuse_write(tool: &str) -> McpError {
    McpError::invalid_params(
        format!("'{tool}' is a write operation and CLOUDFLARE_READ_ONLY is set"),
        None,
    )
}

/// Drop the `None`s so a PATCH sends only what the caller named.
fn compact(pairs: Vec<(&str, Option<Value>)>) -> Value {
    let mut map = serde_json::Map::new();
    for (k, v) in pairs {
        if let Some(v) = v {
            map.insert(k.to_string(), v);
        }
    }
    Value::Object(map)
}

fn page_query(page: Option<u32>, per_page: Option<u32>) -> Vec<(String, String)> {
    let mut q = Vec::new();
    if let Some(p) = page {
        q.push(("page".into(), p.to_string()));
    }
    if let Some(pp) = per_page {
        q.push(("per_page".into(), pp.to_string()));
    }
    q
}

const TOOL_VERIFY: &str = "verify_token";
const TOOL_LIST_ZONES: &str = "list_zones";
const TOOL_GET_ZONE: &str = "get_zone";
const TOOL_LIST_DNS: &str = "list_dns_records";
const TOOL_CREATE_DNS: &str = "create_dns_record";
const TOOL_UPDATE_DNS: &str = "update_dns_record";
const TOOL_DELETE_DNS: &str = "delete_dns_record";
const TOOL_PURGE: &str = "purge_cache";
const TOOL_LIST_CERTS: &str = "list_origin_certificates";
const TOOL_GET_CERT: &str = "get_origin_certificate";
const TOOL_ISSUE_CERT: &str = "issue_origin_certificate";
const TOOL_REVOKE_CERT: &str = "revoke_origin_certificate";
const TOOL_RAW_API: &str = "raw_api";

impl Cloudflare {
    pub fn new() -> Self {
        Self {
            http: reqwest::Client::new(),
        }
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

    fn tools() -> Vec<Tool> {
        vec![
            Tool::new(
                TOOL_VERIFY,
                "Check that the configured token is live and report what it can do. The first \
                 call to make when anything returns an authentication error — it distinguishes \
                 a missing token from a token missing a scope.",
                schema(json!({"type": "object", "properties": {}})),
            ),
            Tool::new(
                TOOL_LIST_ZONES,
                "List zones on the account: id, name, status, plan, nameservers. The zone id \
                 is the handle every other tool takes, so this is usually the first call.",
                schema(json!({
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "exact zone name, e.g. tecnocratica.com.co"},
                        "status": {"type": "string", "description": "active, pending, initializing, moved…"},
                        "page": {"type": "integer"},
                        "per_page": {"type": "integer"}
                    }
                })),
            ),
            Tool::new(
                TOOL_GET_ZONE,
                "Full detail for one zone: status, plan, nameservers, original registrar, \
                 development mode.",
                schema(json!({
                    "type": "object",
                    "properties": {"zone_id": {"type": "string"}},
                    "required": ["zone_id"]
                })),
            ),
            Tool::new(
                TOOL_LIST_DNS,
                "List a zone's DNS records, optionally filtered by type and exact name. \
                 Returns each record's id, which the update and delete tools need.",
                schema(json!({
                    "type": "object",
                    "properties": {
                        "zone_id": {"type": "string"},
                        "type": {"type": "string", "description": "A, AAAA, CNAME, MX, TXT, SRV, NS…"},
                        "name": {"type": "string", "description": "exact record name, fully qualified"},
                        "page": {"type": "integer"},
                        "per_page": {"type": "integer"}
                    },
                    "required": ["zone_id"]
                })),
            ),
            Tool::new(
                TOOL_CREATE_DNS,
                "Add one DNS record. Additive — existing records are untouched. Set 'proxied' \
                 to put the record behind Cloudflare; note the free Universal certificate only \
                 covers the apex and one label beneath it, so a proxied name with two labels \
                 fails TLS.",
                schema(json!({
                    "type": "object",
                    "properties": {
                        "zone_id": {"type": "string"},
                        "type": {"type": "string"},
                        "name": {"type": "string"},
                        "content": {"type": "string", "description": "IP, hostname or text value"},
                        "ttl": {"type": "integer", "description": "seconds; 1 means automatic"},
                        "proxied": {"type": "boolean"},
                        "priority": {"type": "integer", "description": "MX and SRV only"},
                        "comment": {"type": "string"}
                    },
                    "required": ["zone_id", "type", "name", "content"]
                })),
            ),
            Tool::new(
                TOOL_UPDATE_DNS,
                "Patch one existing record by id. Only the fields given are changed, so this \
                 is the tool for flipping 'proxied' or repointing an address without restating \
                 the record.",
                schema(json!({
                    "type": "object",
                    "properties": {
                        "zone_id": {"type": "string"},
                        "record_id": {"type": "string"},
                        "type": {"type": "string"},
                        "name": {"type": "string"},
                        "content": {"type": "string"},
                        "ttl": {"type": "integer"},
                        "proxied": {"type": "boolean"},
                        "comment": {"type": "string"}
                    },
                    "required": ["zone_id", "record_id"]
                })),
            ),
            Tool::new(
                TOOL_DELETE_DNS,
                "DESTRUCTIVE: delete one DNS record by id.",
                schema(json!({
                    "type": "object",
                    "properties": {
                        "zone_id": {"type": "string"},
                        "record_id": {"type": "string"}
                    },
                    "required": ["zone_id", "record_id"]
                })),
            ),
            Tool::new(
                TOOL_PURGE,
                "Purge the edge cache for a zone. Give exactly one of: everything, files, \
                 tags, hosts, prefixes. 'everything' discards the whole zone's cache and makes \
                 the next requests all miss, so prefer the narrow forms. Every method, tags \
                 included, works on every plan, under per-account rate limits. Tag purge only \
                 reaches responses the origin sent with a Cache-Tag header through a proxied \
                 name — purging a tag nothing carries succeeds and does nothing.",
                schema(json!({
                    "type": "object",
                    "properties": {
                        "zone_id": {"type": "string"},
                        "everything": {"type": "boolean"},
                        "files": {"type": "array", "items": {"type": "string"}, "description": "absolute URLs"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "hosts": {"type": "array", "items": {"type": "string"}},
                        "prefixes": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["zone_id"]
                })),
            ),
            Tool::new(
                TOOL_LIST_CERTS,
                "List Origin CA certificates, optionally for one zone. These are the \
                 certificates an origin presents to Cloudflare, not the edge certificate \
                 visitors see.",
                schema(json!({
                    "type": "object",
                    "properties": {"zone_id": {"type": "string"}}
                })),
            ),
            Tool::new(
                TOOL_GET_CERT,
                "One Origin CA certificate by id, including its PEM.",
                schema(json!({
                    "type": "object",
                    "properties": {"certificate_id": {"type": "string"}},
                    "required": ["certificate_id"]
                })),
            ),
            Tool::new(
                TOOL_ISSUE_CERT,
                "Sign a CSR into an Origin CA certificate and return the PEM. The private key \
                 stays wherever the CSR was generated and is never part of this call — \
                 generate the CSR on the origin host, send only the CSR. Needs the token scope \
                 Zone · SSL and Certificates · Edit.",
                schema(json!({
                    "type": "object",
                    "properties": {
                        "csr": {"type": "string", "description": "PEM-encoded certificate signing request"},
                        "hostnames": {"type": "array", "items": {"type": "string"}},
                        "requested_validity": {"type": "integer", "description": "days: 7, 30, 90, 365, 730, 1095 or 5475"},
                        "request_type": {"type": "string", "description": "origin-rsa (default), origin-ecc, keyless-certificate"}
                    },
                    "required": ["csr", "hostnames"]
                })),
            ),
            Tool::new(
                TOOL_REVOKE_CERT,
                "DESTRUCTIVE: revoke an Origin CA certificate. Any origin still presenting it \
                 stops being trusted by Cloudflare, which takes the site down if nothing has \
                 replaced it.",
                schema(json!({
                    "type": "object",
                    "properties": {"certificate_id": {"type": "string"}},
                    "required": ["certificate_id"]
                })),
            ),
            Tool::new(
                TOOL_RAW_API,
                "Escape hatch to any other Cloudflare v4 endpoint — tunnels, rulesets, \
                 Workers, Access. Paths are relative to /client/v4. Prefer a named tool when \
                 one exists; this one is unaudited by construction.",
                schema(json!({
                    "type": "object",
                    "properties": {
                        "method": {"type": "string"},
                        "path": {"type": "string", "description": "e.g. /zones or /accounts/{id}/cfd_tunnel"},
                        "query": {"type": "object", "description": "flat string map"},
                        "body": {"description": "JSON body for POST/PUT/PATCH"}
                    },
                    "required": ["method", "path"]
                })),
            ),
        ]
    }
}

impl ServerHandler for Cloudflare {
    fn get_info(&self) -> ServerInfo {
        ServerInfo::new(ServerCapabilities::builder().enable_tools().build())
            .with_server_info(Implementation::new(
                "dangler-cloudflare",
                env!("CARGO_PKG_VERSION"),
            ))
            .with_instructions(
                "Cloudflare account operations under the identity named in dangler.toml: \
                 zones, DNS records, cache purge, Origin CA certificates, and a raw_api \
                 escape hatch. Call list_zones first — the zone id is the handle everything \
                 else takes. Three tools are DESTRUCTIVE (delete_dns_record, \
                 revoke_origin_certificate, and purge_cache with everything). On any \
                 authentication error call verify_token, which separates a missing token from \
                 a missing scope. Credentials come from a referenced file; calls explain what \
                 is missing.",
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
        tracing::debug!(tool = %request.name, "cloudflare tool call");
        match request.name.as_ref() {
            TOOL_VERIFY => self.get("/user/tokens/verify", &[]).await,

            TOOL_LIST_ZONES => {
                let a: ListZonesArgs = parse_args(request.arguments)?;
                let mut q = page_query(a.page, a.per_page);
                if let Some(n) = a.name {
                    q.push(("name".into(), n));
                }
                if let Some(s) = a.status {
                    q.push(("status".into(), s));
                }
                self.get("/zones", &q).await
            }

            TOOL_GET_ZONE => {
                let a: ZoneArgs = parse_args(request.arguments)?;
                self.get(&format!("/zones/{}", a.zone_id), &[]).await
            }

            TOOL_LIST_DNS => {
                let a: ListDnsArgs = parse_args(request.arguments)?;
                let mut q = page_query(a.page, a.per_page);
                if let Some(t) = a.rtype {
                    q.push(("type".into(), t));
                }
                if let Some(n) = a.name {
                    q.push(("name".into(), n));
                }
                self.get(&format!("/zones/{}/dns_records", a.zone_id), &q)
                    .await
            }

            TOOL_CREATE_DNS => {
                if api::read_only() {
                    return Err(refuse_write(TOOL_CREATE_DNS));
                }
                let a: CreateDnsArgs = parse_args(request.arguments)?;
                let body = compact(vec![
                    ("type", Some(json!(a.rtype))),
                    ("name", Some(json!(a.name))),
                    ("content", Some(json!(a.content))),
                    ("ttl", a.ttl.map(|v| json!(v))),
                    ("proxied", a.proxied.map(|v| json!(v))),
                    ("priority", a.priority.map(|v| json!(v))),
                    ("comment", a.comment.map(|v| json!(v))),
                ]);
                self.write(
                    "POST",
                    &format!("/zones/{}/dns_records", a.zone_id),
                    Some(&body),
                )
                .await
            }

            TOOL_UPDATE_DNS => {
                if api::read_only() {
                    return Err(refuse_write(TOOL_UPDATE_DNS));
                }
                let a: UpdateDnsArgs = parse_args(request.arguments)?;
                let body = compact(vec![
                    ("type", a.rtype.map(|v| json!(v))),
                    ("name", a.name.map(|v| json!(v))),
                    ("content", a.content.map(|v| json!(v))),
                    ("ttl", a.ttl.map(|v| json!(v))),
                    ("proxied", a.proxied.map(|v| json!(v))),
                    ("comment", a.comment.map(|v| json!(v))),
                ]);
                if body.as_object().is_some_and(serde_json::Map::is_empty) {
                    return Err(McpError::invalid_params(
                        "nothing to update — give at least one of type, name, content, ttl, \
                         proxied or comment"
                            .to_string(),
                        None,
                    ));
                }
                self.write(
                    "PATCH",
                    &format!("/zones/{}/dns_records/{}", a.zone_id, a.record_id),
                    Some(&body),
                )
                .await
            }

            TOOL_DELETE_DNS => {
                if api::read_only() {
                    return Err(refuse_write(TOOL_DELETE_DNS));
                }
                let a: DeleteDnsArgs = parse_args(request.arguments)?;
                self.write(
                    "DELETE",
                    &format!("/zones/{}/dns_records/{}", a.zone_id, a.record_id),
                    None,
                )
                .await
            }

            TOOL_PURGE => {
                if api::read_only() {
                    return Err(refuse_write(TOOL_PURGE));
                }
                let a: PurgeArgs = parse_args(request.arguments)?;
                // Cloudflare takes exactly one selector. Catching it here gives a
                // better message than the API's generic 400.
                let selectors = [
                    ("everything", a.everything),
                    ("files", !a.files.is_empty()),
                    ("tags", !a.tags.is_empty()),
                    ("hosts", !a.hosts.is_empty()),
                    ("prefixes", !a.prefixes.is_empty()),
                ];
                let given: Vec<&str> = selectors
                    .iter()
                    .filter(|(_, set)| *set)
                    .map(|(n, _)| *n)
                    .collect();
                if given.len() != 1 {
                    return Err(McpError::invalid_params(
                        format!(
                            "give exactly one of everything, files, tags, hosts, prefixes — got {}",
                            if given.is_empty() {
                                "none".to_string()
                            } else {
                                given.join(" and ")
                            }
                        ),
                        None,
                    ));
                }
                let body = match given[0] {
                    "everything" => json!({"purge_everything": true}),
                    "files" => json!({"files": a.files}),
                    "tags" => json!({"tags": a.tags}),
                    "hosts" => json!({"hosts": a.hosts}),
                    _ => json!({"prefixes": a.prefixes}),
                };
                self.write(
                    "POST",
                    &format!("/zones/{}/purge_cache", a.zone_id),
                    Some(&body),
                )
                .await
            }

            TOOL_LIST_CERTS => {
                let a: ListCertsArgs = parse_args(request.arguments)?;
                let q = match a.zone_id {
                    Some(z) => vec![("zone_id".to_string(), z)],
                    None => Vec::new(),
                };
                self.get("/certificates", &q).await
            }

            TOOL_GET_CERT => {
                let a: CertArgs = parse_args(request.arguments)?;
                self.get(&format!("/certificates/{}", a.certificate_id), &[])
                    .await
            }

            TOOL_ISSUE_CERT => {
                if api::read_only() {
                    return Err(refuse_write(TOOL_ISSUE_CERT));
                }
                let a: IssueCertArgs = parse_args(request.arguments)?;
                if a.hostnames.is_empty() {
                    return Err(McpError::invalid_params(
                        "hostnames must name at least one host".to_string(),
                        None,
                    ));
                }
                // A CSR carries no private key. Refuse an obvious paste of one
                // rather than forwarding it to Cloudflare.
                if a.csr.contains("PRIVATE KEY") {
                    return Err(McpError::invalid_params(
                        "that looks like a private key, not a CSR — send only the \
                         CERTIFICATE REQUEST block; the key never leaves the origin"
                            .to_string(),
                        None,
                    ));
                }
                let body = compact(vec![
                    ("csr", Some(json!(a.csr))),
                    ("hostnames", Some(json!(a.hostnames))),
                    (
                        "requested_validity",
                        Some(json!(a.requested_validity.unwrap_or(5475))),
                    ),
                    (
                        "request_type",
                        Some(json!(
                            a.request_type.unwrap_or_else(|| "origin-rsa".to_string())
                        )),
                    ),
                ]);
                self.write("POST", "/certificates", Some(&body)).await
            }

            TOOL_REVOKE_CERT => {
                if api::read_only() {
                    return Err(refuse_write(TOOL_REVOKE_CERT));
                }
                let a: CertArgs = parse_args(request.arguments)?;
                self.write(
                    "DELETE",
                    &format!("/certificates/{}", a.certificate_id),
                    None,
                )
                .await
            }

            TOOL_RAW_API => {
                let a: RawApiArgs = parse_args(request.arguments)?;
                let is_get = a.method.eq_ignore_ascii_case("get");
                if api::read_only() && !is_get {
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
