# tinyfab — a fleet entry's name tag, code elsewhere

TinyFab (`ophiocus/tinyfab`, checked out at `I:\tinyfab`) is a Rust/egui catalogue of
owned Fab listings with its own stdio MCP mode, `tinyfab.exe --mcp`. The server is
first-party but lives in its own repository, so its fleet entry is a plain `command`
pointing at the **release** binary and there is no code to build here. This folder
exists because **every fleet entry carries its skill**: `SKILL.md` is the name tag
dangler installs into the client's skills directory at start.

Keep the entry on `target/release/tinyfab.exe`. A registration that pointed at the
debug binary is how the previous one rotted (D:\UE5_claude, WORKFLOW.md §3); rebuild
with `cargo build --release` in `I:\tinyfab` after pulling.

The store is `%APPDATA%\TinyFab\tinyfab.db`; the server opens (and creates) it on start
and answers `tools/list` with nothing synced. Syncing from Fab/Epic is a console action
(`tinyfab sync`), never an MCP call. The one tool that writes, `add_reference`, is
refused when the entry's env sets `TINYFAB_READ_ONLY=1`.
