---
name: tinyfab
description: Look up Carlos's owned Fab / Unreal Marketplace asset packs — search by title, vendor, category or licence, read one pack's full metadata (engine versions, formats, licence tiers, media, Fab URL), and attach research references (tutorials, videos, docs, notes) — through the `tinyfab` server in the dangler fleet. Use whenever a task asks what assets we own, which pack to use for a level or a pipeline test, "do we have a Megascans/Quixel/<vendor> pack for…", "which packs are Personal / CC-BY licensed", or wants a tutorial or note pinned to a pack.
---

# tinyfab — the owned-asset catalogue name tag

The `tinyfab` fleet server is TinyFab's own stdio MCP mode (`tinyfab.exe --mcp`, the
release build in `I:\tinyfab`). It reads the local store at
`%APPDATA%\TinyFab\tinyfab.db` — one row per owned listing, 666 on Desky as of
2026-10-15 — and never talks to Fab or Epic itself. Reach it through dangler:

1. `load_server {name: "tinyfab"}` once, to see the schemas.
2. `call_tool {server: "tinyfab", tool: "list_assets", arguments: {licence: "Personal", limit: 20}}`.

## Which tool

| Need | Tool | Writes |
|---|---|---|
| find packs — `search` (substring over title / vendor / description), `category` (e.g. `assets/megascans`), `vendor` (exact), `licence` (exact tier name), `limit` (default 20) | `list_assets` | no |
| everything about one pack, plus its references | `get_asset {id}` | no |
| just the references on a pack | `list_references {asset_id}` | no |
| pin a tutorial / video / doc / note to a pack (`kind`, `title`, `url`, `notes`) | `add_reference` | the local store only |
| how many packs are catalogued | `stats` | no |

Filters combine with AND; results are ordered by title.

**`list_assets` rows are slim (~0.7 KB each, default `limit` 20):** `id`, `title`,
`vendor`, `categories`, `description`, and under `fab` — `licenses`, `formats`,
`engine_versions` (`UE_5.7` …), `distribution_method`, `url`, `listing_uid`.
**`get_asset` is heavy (~45 KB):** it adds `long_description`, `media`, the launcher
catalog entry (`raw`) and the whole Fab listing page JSON (`fab.listing_raw`, ~38 KB).
Shortlist with `list_assets`, then `get_asset` only the one or two packs that matter;
use `stats` for counts.

## Licences: what Fab offers, not what was bought

`fab.licenses` on a row is the list of **tiers the Fab listing offers**, read from its public
page. It is **not** the tier Carlos holds — the store has no record of that. Groups seen:

| Group | Names | Note |
|---|---|---|
| `standard` | `Personal`, `Professional` | the usual pair; a pack offering `Personal` is the safe default for personal projects |
| `standard` | `UEFN - Reference only` | a handful; not a production licence |
| `cc` | `CC-BY` | attribution required |
| `legacy` | `UE Marketplace License` | carried over from the Unreal Marketplace |

Pass the name exactly as above to the `licence` filter.

## Why a row has no licence

About half the rows (332 of 666 on 2026-10-15) have an empty `fab.licenses`. That is not
a sync failure; there are three causes:

- **Delisted** — the Fab page answered 404. The row's `fab.listing_raw` (in `get_asset`)
  carries `"notPublic": true`; it is recorded once and not asked again (164 rows). In a
  slim row it shows as a `fab.listing_uid` with no licences.
- **Legacy Unreal Marketplace item** — never migrated to Fab: `fab.url` points at
  `www.unrealengine.com` and `fab.listing_uid` is empty, so there is no Fab page to read.
- **Engine builds** — rows like `Unreal Engine`, `4.27-Chaos` under `fab/…` categories
  with no URL at all. They are entitlements, not asset packs.

When the licence decides a choice, say which case applies rather than guessing a tier.

## Rules

- **The MCP is read / annotate only.** Refreshing the catalogue is Carlos's action:
  `tinyfab sync` (and `tinyfab licences` for the licence pass) from his own console. A
  sync may open an Epic sign-in window or a WebView2 window on fab.com — never start one
  from a session, and never try to sign in for him.
- Data looks stale or a pack is missing → tell Carlos a sync is due; do not work around it.
- `add_reference` writes to the shared local store that the TinyFab app shows; add
  references only when the task asks for them, and give each a `kind` and a `url` or
  `notes`. `TINYFAB_READ_ONLY=1` in the fleet entry's env refuses it, and every other
  tool keeps working.
- "open tinyfab store" failing at start means `%APPDATA%\TinyFab` is unreachable on this
  machine; TinyFab lives on Desky, so another seat has no catalogue until it is synced there.
