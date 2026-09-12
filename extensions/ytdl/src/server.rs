//! The MCP face of the ytdownloader extension: a static tool surface over the
//! bundled yt-dlp toolkit, written as a manual [`ServerHandler`] in the same
//! style as dangler's own meta-tool server.
//!
//! Read-only mode: set `YTDL_READ_ONLY=1` (any non-empty value) and every tool
//! that writes a file to disk is refused. Inspection — info, transcripts to
//! stdout, session status, listing what is already downloaded — still answers.

use std::path::PathBuf;
use std::sync::Arc;

use rmcp::model::{
    CallToolRequestParams, CallToolResult, ContentBlock, Implementation, JsonObject,
    ListToolsResult, PaginatedRequestParams, ServerCapabilities, ServerInfo, Tool,
};
use rmcp::service::{RequestContext, RoleServer};
use rmcp::{ErrorData as McpError, ServerHandler};
use serde::Deserialize;
use serde_json::{Value, json};

use crate::toolkit;

/// MCP server handler for the ytdownloader tool surface.
#[derive(Clone)]
pub struct Ytdl;

#[derive(Deserialize)]
struct UrlArgs {
    url: String,
    /// Return yt-dlp's complete metadata instead of the digest.
    #[serde(default)]
    full: bool,
}

#[derive(Deserialize)]
struct DownloadArgs {
    url: String,
    /// Cap the video height, e.g. 1080. Omit for best available.
    max_height: Option<u32>,
    /// Download every entry when the URL is a playlist. Default: just the one video.
    #[serde(default)]
    playlist: bool,
    /// Destination directory. Defaults to the toolkit's own downloads/ folder.
    output_dir: Option<String>,
}

#[derive(Deserialize)]
struct AudioArgs {
    url: String,
    #[serde(default)]
    playlist: bool,
    output_dir: Option<String>,
}

#[derive(Deserialize)]
struct TranscriptArgs {
    url: String,
    /// Caption language, e.g. "es". Defaults to the toolkit's own default.
    lang: Option<String>,
    /// Keep SRT timing instead of flattening to plain text.
    #[serde(default)]
    srt: bool,
    /// Fall back to local Whisper ASR when the video carries no captions.
    /// This downloads the media to transcribe it, so it counts as a write.
    #[serde(default)]
    whisper: bool,
    /// Write the transcript to this file instead of returning it.
    output_file: Option<String>,
}

#[derive(Deserialize)]
struct ListArgs {
    /// Directory to list. Defaults to the toolkit's downloads/ folder.
    dir: Option<String>,
    limit: Option<usize>,
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

fn plain_result(text: String) -> CallToolResult {
    CallToolResult::success(vec![ContentBlock::text(text)])
}

fn tool_error(e: anyhow::Error) -> McpError {
    McpError::internal_error(format!("{e:#}"), None)
}

fn refuse_write(tool: &str) -> McpError {
    McpError::invalid_params(
        format!("'{tool}' writes to disk and YTDL_READ_ONLY is set"),
        None,
    )
}

/// Language codes from a yt-dlp subtitles map, which is `{lang: [formats]}`.
fn langs(node: Option<&Value>) -> Vec<String> {
    node.and_then(Value::as_object)
        .map(|m| m.keys().cloned().collect())
        .unwrap_or_default()
}

/// The interesting few fields out of yt-dlp's very large `-J` dump.
fn digest(info: &Value) -> Value {
    let heights: Vec<u64> = info
        .get("formats")
        .and_then(Value::as_array)
        .map(|fs| {
            let mut hs: Vec<u64> = fs
                .iter()
                .filter_map(|f| f.get("height").and_then(Value::as_u64))
                .collect();
            hs.sort_unstable();
            hs.dedup();
            hs
        })
        .unwrap_or_default();

    json!({
        "id": info.get("id"),
        "title": info.get("title"),
        "uploader": info.get("uploader"),
        "channel_url": info.get("channel_url"),
        "duration_seconds": info.get("duration"),
        "upload_date": info.get("upload_date"),
        "view_count": info.get("view_count"),
        "is_live": info.get("is_live"),
        "webpage_url": info.get("webpage_url"),
        "available_heights": heights,
        "subtitle_languages": langs(info.get("subtitles")),
        "auto_caption_languages": langs(info.get("automatic_captions")),
    })
}

const TOOL_INFO: &str = "video_info";
const TOOL_DOWNLOAD_VIDEO: &str = "download_video";
const TOOL_DOWNLOAD_AUDIO: &str = "download_audio";
const TOOL_TRANSCRIPT: &str = "fetch_transcript";
const TOOL_SESSION: &str = "session_status";
const TOOL_LIST: &str = "list_downloads";

impl Ytdl {
    pub fn new() -> Self {
        Self
    }

    fn tools() -> Vec<Tool> {
        vec![
            Tool::new(
                TOOL_INFO,
                "Metadata for one video without downloading it: title, uploader, duration, \
                 the resolutions on offer, and which caption languages exist. Call this \
                 first when the resolution or the caption situation is unclear.",
                schema(json!({
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "full": {"type": "boolean", "description": "return yt-dlp's complete metadata instead of the digest (very large)"}
                    },
                    "required": ["url"]
                })),
            ),
            Tool::new(
                TOOL_DOWNLOAD_VIDEO,
                "Download a video to local disk, merging the best video and audio streams. \
                 Returns the paths written. Long videos hold the call open for their whole \
                 download; raise YTDL_TIMEOUT_SECS if one is cut short.",
                schema(json!({
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "max_height": {"type": "integer", "description": "cap the resolution, e.g. 1080; omit for best available"},
                        "playlist": {"type": "boolean", "description": "download every entry when the URL is a playlist"},
                        "output_dir": {"type": "string"}
                    },
                    "required": ["url"]
                })),
            ),
            Tool::new(
                TOOL_DOWNLOAD_AUDIO,
                "Download audio only, transcoded to MP3 at best quality. Returns the paths \
                 written.",
                schema(json!({
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "playlist": {"type": "boolean"},
                        "output_dir": {"type": "string"}
                    },
                    "required": ["url"]
                })),
            ),
            Tool::new(
                TOOL_TRANSCRIPT,
                "Fetch a video's transcript: uploaded captions first, then auto-generated \
                 ones. Returns the text unless 'output_file' is given. Setting 'whisper' \
                 allows local ASR when the video has no captions at all, which downloads \
                 the media to transcribe it.",
                schema(json!({
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "lang": {"type": "string", "description": "caption language code, e.g. 'es'"},
                        "srt": {"type": "boolean", "description": "keep SRT timing instead of plain text"},
                        "whisper": {"type": "boolean", "description": "allow local Whisper ASR fallback (downloads the media)"},
                        "output_file": {"type": "string"}
                    },
                    "required": ["url"]
                })),
            ),
            Tool::new(
                TOOL_SESSION,
                "Whether a browser session has been captured, and how old it is. YouTube \
                 refuses anonymous extraction on many videos; when downloads fail with a \
                 bot check, a stale or absent jar is the usual cause. Capturing one is a \
                 human step at the console, not something this server does.",
                schema(json!({"type": "object", "properties": {}})),
            ),
            Tool::new(
                TOOL_LIST,
                "List what is already in the downloads directory, newest first.",
                schema(json!({
                    "type": "object",
                    "properties": {
                        "dir": {"type": "string"},
                        "limit": {"type": "integer"}
                    }
                })),
            ),
        ]
    }

    /// Run yt-dlp with the toolkit's standard preamble plus `args`.
    async fn ytdlp(&self, args: Vec<String>) -> Result<toolkit::Run, McpError> {
        let home = toolkit::home().map_err(tool_error)?;
        let exe = toolkit::ytdlp(&home).map_err(tool_error)?;
        let mut full = toolkit::ytdlp_base(&home).map_err(tool_error)?;
        full.extend(args);
        toolkit::run(&exe, &full, &home).await.map_err(tool_error)
    }

    /// The `-o` template and the destination directory for a download.
    fn output_template(
        home: &std::path::Path,
        output_dir: Option<String>,
        playlist: bool,
    ) -> (PathBuf, String) {
        let dir = output_dir
            .filter(|d| !d.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| toolkit::downloads_dir(home));
        let leaf = if playlist {
            "%(playlist_title)s/%(playlist_index)s - %(title)s.%(ext)s"
        } else {
            "%(title)s.%(ext)s"
        };
        let template = format!("{}/{leaf}", dir.display());
        (dir, template)
    }

    /// Paths yt-dlp reported writing, via `--print after_move:filepath`.
    fn written(run: &toolkit::Run) -> Vec<String> {
        run.stdout
            .lines()
            .map(str::trim)
            .filter(|l| !l.is_empty())
            .map(str::to_string)
            .collect()
    }
}

impl ServerHandler for Ytdl {
    fn get_info(&self) -> ServerInfo {
        ServerInfo::new(ServerCapabilities::builder().enable_tools().build())
            .with_server_info(Implementation::new(
                "dangler-ytdl",
                env!("CARGO_PKG_VERSION"),
            ))
            .with_instructions(
                "Local YouTube archiving through a bundled yt-dlp / ffmpeg / deno toolkit: \
                 video, MP3 audio, and transcripts. SCOPE: personal, local, non-monetized \
                 archiving only — do not assist with re-uploading, redistributing or \
                 monetizing what is downloaded, and decline requests that are plainly about \
                 those. Downloads are synchronous and can be slow. When a call fails a bot \
                 check, read session_status: YouTube wants a captured browser session, and \
                 capturing one is the operator's step at the console.",
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
        tracing::debug!(tool = %request.name, "ytdl tool call");
        match request.name.as_ref() {
            TOOL_INFO => {
                let a: UrlArgs = parse_args(request.arguments)?;
                let run = self
                    .ytdlp(vec![
                        "-J".into(),
                        "--no-playlist".into(),
                        "--no-warnings".into(),
                        a.url,
                    ])
                    .await?;
                let info: Value = serde_json::from_str(run.stdout.trim()).map_err(|e| {
                    McpError::internal_error(
                        format!(
                            "yt-dlp metadata was not JSON: {e}; {}",
                            toolkit::tail(&run.stderr, 5)
                        ),
                        None,
                    )
                })?;
                Ok(text_result(if a.full { info } else { digest(&info) }))
            }

            TOOL_DOWNLOAD_VIDEO => {
                if toolkit::read_only() {
                    return Err(refuse_write(TOOL_DOWNLOAD_VIDEO));
                }
                let a: DownloadArgs = parse_args(request.arguments)?;
                let home = toolkit::home().map_err(tool_error)?;
                let (dir, template) = Self::output_template(&home, a.output_dir, a.playlist);

                let mut args = vec![
                    if a.playlist {
                        "--yes-playlist"
                    } else {
                        "--no-playlist"
                    }
                    .to_string(),
                    "-o".into(),
                    template,
                    "--print".into(),
                    "after_move:filepath".into(),
                    "--no-simulate".into(),
                ];
                if let Some(h) = a.max_height {
                    args.push("-f".into());
                    args.push(format!(
                        "bestvideo[height<={h}]+bestaudio/best[height<={h}]"
                    ));
                }
                args.push(a.url);

                let run = self.ytdlp(args).await?;
                Ok(text_result(json!({
                    "ok": true,
                    "directory": dir.display().to_string(),
                    "files": Self::written(&run),
                })))
            }

            TOOL_DOWNLOAD_AUDIO => {
                if toolkit::read_only() {
                    return Err(refuse_write(TOOL_DOWNLOAD_AUDIO));
                }
                let a: AudioArgs = parse_args(request.arguments)?;
                let home = toolkit::home().map_err(tool_error)?;
                let (dir, template) = Self::output_template(&home, a.output_dir, a.playlist);

                let run = self
                    .ytdlp(vec![
                        if a.playlist {
                            "--yes-playlist"
                        } else {
                            "--no-playlist"
                        }
                        .to_string(),
                        "-x".into(),
                        "--audio-format".into(),
                        "mp3".into(),
                        "--audio-quality".into(),
                        "0".into(),
                        "-o".into(),
                        template,
                        "--print".into(),
                        "after_move:filepath".into(),
                        "--no-simulate".into(),
                        a.url,
                    ])
                    .await?;
                Ok(text_result(json!({
                    "ok": true,
                    "directory": dir.display().to_string(),
                    "files": Self::written(&run),
                })))
            }

            TOOL_TRANSCRIPT => {
                let a: TranscriptArgs = parse_args(request.arguments)?;
                // Captions alone touch no disk; Whisper downloads the media,
                // and an output file is a write by definition.
                if toolkit::read_only() && (a.whisper || a.output_file.is_some()) {
                    return Err(refuse_write(TOOL_TRANSCRIPT));
                }
                let home = toolkit::home().map_err(tool_error)?;
                let script = toolkit::script(&home, "transcript.py").map_err(tool_error)?;

                let mut args = vec![script.display().to_string(), a.url];
                if a.srt {
                    args.push("--srt".into());
                }
                if let Some(lang) = a.lang.filter(|l| !l.is_empty()) {
                    args.push("--lang".into());
                    args.push(lang);
                }
                if a.whisper {
                    args.push("--auto-fallback".into());
                }
                if let Some(out) = a.output_file.clone().filter(|o| !o.is_empty()) {
                    args.push("-o".into());
                    args.push(out);
                }

                let run = toolkit::run(&PathBuf::from(toolkit::python()), &args, &home)
                    .await
                    .map_err(tool_error)?;

                match a.output_file {
                    Some(path) => Ok(text_result(json!({"ok": true, "written": path}))),
                    None => Ok(plain_result(run.stdout)),
                }
            }

            TOOL_SESSION => {
                let home = toolkit::home().map_err(tool_error)?;
                let jar = toolkit::cookie_jar(&home);
                let age_hours = jar.as_ref().and_then(|p| {
                    let modified = std::fs::metadata(p).ok()?.modified().ok()?;
                    let secs = modified.elapsed().ok()?.as_secs();
                    Some(secs / 3600)
                });
                Ok(text_result(json!({
                    "captured": jar.is_some(),
                    "jar": jar.as_ref().map(|p| p.display().to_string()),
                    "age_hours": age_hours,
                    "note": if jar.is_some() {
                        "A captured session is in use. If downloads start failing a bot check \
                         again, it has gone stale and needs recapturing."
                    } else {
                        "No session captured. Anonymous extraction still works for many videos; \
                         when it does not, the operator captures one at the console with \
                         bin\\ytlogin.cmd anon."
                    },
                })))
            }

            TOOL_LIST => {
                let a: ListArgs = parse_args(request.arguments)?;
                let home = toolkit::home().map_err(tool_error)?;
                let dir = a
                    .dir
                    .filter(|d| !d.is_empty())
                    .map(PathBuf::from)
                    .unwrap_or_else(|| toolkit::downloads_dir(&home));

                let mut entries: Vec<(std::time::SystemTime, Value)> = std::fs::read_dir(&dir)
                    .map_err(|e| {
                        McpError::internal_error(format!("reading {}: {e}", dir.display()), None)
                    })?
                    .filter_map(Result::ok)
                    .filter_map(|e| {
                        let meta = e.metadata().ok()?;
                        if !meta.is_file() {
                            return None;
                        }
                        let modified = meta.modified().ok()?;
                        Some((
                            modified,
                            json!({
                                "name": e.file_name().to_string_lossy(),
                                "bytes": meta.len(),
                            }),
                        ))
                    })
                    .collect();
                entries.sort_by(|a, b| b.0.cmp(&a.0));
                let limit = a.limit.unwrap_or(50);
                let files: Vec<Value> = entries.into_iter().take(limit).map(|(_, v)| v).collect();

                Ok(text_result(json!({
                    "directory": dir.display().to_string(),
                    "files": files,
                })))
            }

            other => Err(McpError::invalid_params(
                format!("unknown tool '{other}'"),
                None,
            )),
        }
    }
}
