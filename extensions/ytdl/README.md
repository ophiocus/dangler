# dangler-ytdl

Local YouTube archiving as an MCP server: video, MP3 audio, and transcripts,
driven through a bundled yt-dlp / ffmpeg / deno toolkit.

A first-party dangler fleet extension — a plain stdio MCP server, listed in
`dangler.toml` like any other, with no runtime coupling to dangler.

## Scope

Personal, local, non-monetized archiving. The server says so in its own MCP
instructions, so a model reading its tool surface sees the constraint before it
sees the tools. It is not a redistribution pipeline and should not be built into
one.

## The toolkit lives outside the repo

The binaries are large and the captured browser session is a credential, so
neither belongs in version control. `YTDL_HOME` points at a directory holding:

```
bin/yt-dlp.exe  bin/deno.exe  bin/ffmpeg.exe  bin/ffprobe.exe
scripts/transcript.py
auth/cookies.txt        (optional — a captured session)
downloads/              (default destination)
```

Nothing is expected on PATH except a Python interpreter for the transcript
tools. Per the lazy-provisioning house rule the server starts and lists its
tools with `YTDL_HOME` unset or wrong; the calls that need the toolkit are the
ones that explain what is missing.

## Tools

| Tool | Writes to disk | Notes |
| --- | --- | --- |
| `video_info` | no | Title, uploader, duration, resolutions on offer, caption languages. Digest by default, `full` for yt-dlp's whole dump |
| `download_video` | yes | Optional `max_height`, optional `playlist`. Returns the paths written |
| `download_audio` | yes | MP3 at best quality |
| `fetch_transcript` | only with `output_file` or `whisper` | Uploaded captions, then auto-generated; `whisper` allows local ASR, which downloads the media |
| `session_status` | no | Whether a browser session is captured and how old it is |
| `list_downloads` | no | Newest first |

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `YTDL_HOME` | *required* | The toolkit directory |
| `YTDL_DOWNLOADS` | `<YTDL_HOME>/downloads` | Where downloads land |
| `YTDL_PYTHON` | `python` | Interpreter for the transcript scripts |
| `YTDL_TIMEOUT_SECS` | `900` | Ceiling on any one invocation |
| `YTDL_READ_ONLY` | unset | Refuse every tool that writes to disk |
| `DANGLER_DEBUG` | unset | Debug-level logging, to stderr |

## Downloads are synchronous, and stay that way

A download holds the MCP call open until it finishes, and a long video can take
minutes. The timeout exists to stop a wedged extractor holding it open forever,
not to bound normal work — raise `YTDL_TIMEOUT_SECS` rather than lowering
expectations.

**An async job model is not the fix, it is a trap.** Two lifecycles would eat
it. A job that returns immediately leaves `inflight == 0`, so dangler's idle
reaper is free to cancel the child, and `kill_on_drop` takes the running
download with it — the fleet would be killing its own background work on a
timer it has every right to enforce. Above that, a poll-until-done tool is a
call the model has to keep making, and paying for a round trip per poll is how
you get chewed alive by the thing you were trying to be efficient with.

If a long download really does need to outlive a call, the work has to leave
the MCP child entirely: detach a process that writes to disk on its own, and
let these tools report on the filesystem rather than on a job table. Until
there is a reason to build that, synchronous with a generous ceiling is the
honest shape. The levers meanwhile are `YTDL_TIMEOUT_SECS` and, on the dangler
side, `idle_timeout_secs = 0` for this server.

## When YouTube refuses

YouTube declines anonymous extraction on many videos, which surfaces as a 429
and a message about confirming you are not a bot. The fix is a captured browser
session, and **capturing one is the operator's step at the console**, not
something this server does:

```cmd
bin\ytlogin.cmd anon      :: headless, no credentials, try this first
bin\ytlogin.cmd login     :: interactive sign-in, for age-restricted material
```

`session_status` reports whether a jar exists and how stale it is. The jar is a
credential equivalent to being signed in as that account; this server reads its
presence and age, never its contents.
