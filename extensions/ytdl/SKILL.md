---
name: ytdl
description: Save a YouTube video, its audio as MP3, or its transcript/captions to local disk for personal, non-monetized archiving — through the `ytdl` server in the dangler fleet. Use when the user wants to download or keep a YouTube video or playlist, rip the audio, pull a transcript or subtitles, or check what resolutions and caption languages a video offers.
---

# ytdl — the local YouTube archiving name tag

The `ytdl` fleet server drives a bundled yt-dlp / ffmpeg / deno toolkit that lives outside
the repo (`YTDL_HOME`). Reach it through dangler:

1. `load_server {name: "ytdl"}` once, to see the schemas.
2. `call_tool {server: "ytdl", tool: "video_info", arguments: {url: "https://www.youtube.com/watch?v=…"}}`.

## Which tool

| Need | Tool | Writes to disk |
|---|---|---|
| title, uploader, duration, resolutions, caption languages | `video_info` | no |
| the video (optional `max_height`, optional `playlist`) | `download_video` | yes |
| audio only, MP3 | `download_audio` | yes |
| captions as text or SRT (`whisper` for local ASR when there are none) | `fetch_transcript` | only with `output_file` / `whisper` |
| is a browser session captured, how old | `session_status` | no |
| what has been saved, newest first | `list_downloads` | no |

## Rules

- **Scope is personal, local, non-monetized.** Decline re-upload, redistribution or
  commercial use; the server states the same constraint in its own instructions.
- `video_info` first for anything long: downloads are synchronous and hold the call open
  until done. A wedged extractor is bounded by `YTDL_TIMEOUT_SECS`; raise it for long
  videos rather than splitting the work.
- `429` / "Sign in to confirm you're not a bot" means YouTube wants a browser session, not
  a newer yt-dlp. Report it: the user captures one at the console with `bin\ytlogin.cmd anon`
  (no credentials involved). The cookie jar it produces is a credential — never read or copy it.
- A "toolkit missing" error means `YTDL_HOME` is unset or wrong on this machine; the
  server's `setup_hint` names the directory.
- `YTDL_READ_ONLY=1` in the fleet env refuses every tool that writes.
