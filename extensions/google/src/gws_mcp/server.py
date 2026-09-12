"""gws-mcp server — Google Docs + Sheets CRUD, Drive read-only. stdio transport.

CLI:
  gws-mcp            run the MCP server (stdio)
  gws-mcp auth       interactive Google consent (blocking; writes the token file)
  gws-mcp whoami     print the authorized account
  gws-mcp revoke     revoke + delete the local token
  gws-mcp doctor     check secret/token/APIs without starting the server
"""
from __future__ import annotations

import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
from mcp.server.fastmcp import FastMCP

from . import auth

mcp = FastMCP(
    "gws-mcp",
    instructions=(
        "Google Docs and Sheets with full read/write, Google Drive read-only. "
        "Edit existing files IN PLACE by id (never recreate to iterate). "
        "If a tool reports a missing/invalid token, tell the user to run `gws-mcp auth`."
    ),
)


# ----------------------------------------------------------------- services
@lru_cache(maxsize=None)
def _svc(name: str, version: str):
    return build(name, version, credentials=auth.load_credentials(), cache_discovery=False)


def _docs():
    return _svc("docs", "v1")


def _sheets():
    return _svc("sheets", "v4")


def _drive():
    return _svc("drive", "v3")


def _err(e: Exception) -> str:
    if isinstance(e, HttpError):
        try:
            body = json.loads(e.content.decode("utf-8"))
            msg = body.get("error", {}).get("message", str(e))
        except Exception:
            msg = str(e)
        return f"Google API error {e.resp.status}: {msg}"
    return f"{type(e).__name__}: {e}"


# ----------------------------------------------------------------- Docs helpers
def _walk_text(elements: list[dict]) -> str:
    """Flatten Docs structural elements to plain text (paragraphs, tables, TOC)."""
    out: list[str] = []
    for el in elements:
        if "paragraph" in el:
            for pe in el["paragraph"].get("elements", []):
                tr = pe.get("textRun")
                if tr:
                    out.append(tr.get("content", ""))
        elif "table" in el:
            for row in el["table"].get("tableRows", []):
                cells = []
                for cell in row.get("tableCells", []):
                    cells.append(_walk_text(cell.get("content", [])).strip())
                out.append(" | ".join(cells) + "\n")
        elif "tableOfContents" in el:
            out.append(_walk_text(el["tableOfContents"].get("content", [])))
    return "".join(out)


def _doc_end_index(doc: dict) -> int:
    content = doc.get("body", {}).get("content", [])
    return (content[-1].get("endIndex", 1) if content else 1) - 1


# ----------------------------------------------------------------- Docs tools
@mcp.tool()
def docs_get(doc_id: str) -> str:
    """Return the full plain text of a Google Doc (paragraphs and tables flattened)."""
    try:
        doc = _docs().documents().get(documentId=doc_id).execute()
        return _walk_text(doc.get("body", {}).get("content", []))
    except Exception as e:
        return _err(e)


@mcp.tool()
def docs_outline(doc_id: str, max_preview: int = 80) -> str:
    """Return the doc's paragraphs as JSON [{start,end,style,preview}] — use the indexes to target docs_insert / docs_delete_range."""
    try:
        doc = _docs().documents().get(documentId=doc_id).execute()
        rows = []
        for el in doc.get("body", {}).get("content", []):
            if "paragraph" not in el:
                kind = "table" if "table" in el else "section"
                rows.append({"start": el.get("startIndex"), "end": el.get("endIndex"), "style": kind, "preview": ""})
                continue
            p = el["paragraph"]
            text = "".join(pe.get("textRun", {}).get("content", "") for pe in p.get("elements", []))
            rows.append({
                "start": el.get("startIndex"),
                "end": el.get("endIndex"),
                "style": p.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT"),
                "preview": text.strip()[:max_preview],
            })
        return json.dumps({"title": doc.get("title"), "end_index": _doc_end_index(doc), "paragraphs": rows}, ensure_ascii=False)
    except Exception as e:
        return _err(e)


@mcp.tool()
def docs_replace(doc_id: str, find: str, replace: str, match_case: bool = True) -> str:
    """Replace ALL occurrences of `find` with `replace` in a Google Doc (in place). Returns the number of occurrences changed."""
    try:
        body = {"requests": [{"replaceAllText": {"containsText": {"text": find, "matchCase": match_case}, "replaceText": replace}}]}
        r = _docs().documents().batchUpdate(documentId=doc_id, body=body).execute()
        n = r.get("replies", [{}])[0].get("replaceAllText", {}).get("occurrencesChanged", 0)
        return f"replaced {n} occurrence(s)"
    except Exception as e:
        return _err(e)


@mcp.tool()
def docs_insert(doc_id: str, text: str, index: int | None = None) -> str:
    """Insert text at a character index (see docs_outline). Omit index to append at the end of the body."""
    try:
        if index is None:
            doc = _docs().documents().get(documentId=doc_id).execute()
            index = _doc_end_index(doc)
        body = {"requests": [{"insertText": {"location": {"index": index}, "text": text}}]}
        _docs().documents().batchUpdate(documentId=doc_id, body=body).execute()
        return f"inserted {len(text)} chars at index {index}"
    except Exception as e:
        return _err(e)


@mcp.tool()
def docs_delete_range(doc_id: str, start: int, end: int) -> str:
    """Delete content between character indexes [start, end) — get them from docs_outline."""
    try:
        body = {"requests": [{"deleteContentRange": {"range": {"startIndex": start, "endIndex": end}}}]}
        _docs().documents().batchUpdate(documentId=doc_id, body=body).execute()
        return f"deleted range [{start}, {end})"
    except Exception as e:
        return _err(e)


@mcp.tool()
def docs_batch_update(doc_id: str, requests: list[dict[str, Any]]) -> str:
    """Escape hatch: raw Docs API batchUpdate requests (styles, tables, images, named ranges…). See developers.google.com/docs/api/reference/rest/v1/documents/request."""
    try:
        r = _docs().documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()
        return json.dumps(r.get("replies", []), ensure_ascii=False)[:4000]
    except Exception as e:
        return _err(e)


@mcp.tool()
def docs_create(title: str, text: str = "") -> str:
    """Create a new Google Doc (optionally with initial text). Returns id and URL."""
    try:
        doc = _docs().documents().create(body={"title": title}).execute()
        did = doc["documentId"]
        if text:
            _docs().documents().batchUpdate(documentId=did, body={"requests": [{"insertText": {"location": {"index": 1}, "text": text}}]}).execute()
        return json.dumps({"id": did, "url": f"https://docs.google.com/document/d/{did}/edit"})
    except Exception as e:
        return _err(e)


_EXPORT_MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain",
    "html": "text/html",
    "md": "text/markdown",
    "odt": "application/vnd.oasis.opendocument.text",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
}


@mcp.tool()
def drive_export(file_id: str, fmt: str, out_path: str) -> str:
    """Export a Google Doc/Sheet to a local file. fmt: pdf|docx|txt|html|md|odt (Docs) or xlsx|csv|pdf (Sheets). Writes out_path, returns size."""
    try:
        mime = _EXPORT_MIME.get(fmt.lower())
        if not mime:
            return f"unsupported fmt '{fmt}'; use one of {sorted(_EXPORT_MIME)}"
        req = _drive().files().export_media(fileId=file_id, mimeType=mime)
        p = Path(out_path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("wb") as fh:
            dl = MediaIoBaseDownload(fh, req)
            done = False
            while not done:
                _, done = dl.next_chunk()
        return f"wrote {p} ({p.stat().st_size} bytes)"
    except Exception as e:
        return _err(e)


# ----------------------------------------------------------------- Sheets tools
@mcp.tool()
def sheets_info(spreadsheet_id: str) -> str:
    """Spreadsheet title + tabs (title, sheetId, rows, cols) as JSON."""
    try:
        ss = _sheets().spreadsheets().get(spreadsheetId=spreadsheet_id, fields="properties.title,sheets.properties").execute()
        tabs = [{
            "title": s["properties"]["title"],
            "sheetId": s["properties"]["sheetId"],
            "rows": s["properties"].get("gridProperties", {}).get("rowCount"),
            "cols": s["properties"].get("gridProperties", {}).get("columnCount"),
        } for s in ss.get("sheets", [])]
        return json.dumps({"title": ss["properties"]["title"], "sheets": tabs}, ensure_ascii=False)
    except Exception as e:
        return _err(e)


@mcp.tool()
def sheets_read(spreadsheet_id: str, range: str, render: str = "FORMATTED_VALUE") -> str:
    """Read a range (A1 notation, e.g. 'Sheet1!A1:F50' or just 'Sheet1'). Returns JSON rows. render: FORMATTED_VALUE|UNFORMATTED_VALUE|FORMULA."""
    try:
        r = _sheets().spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=range, valueRenderOption=render).execute()
        return json.dumps({"range": r.get("range"), "values": r.get("values", [])}, ensure_ascii=False)
    except Exception as e:
        return _err(e)


@mcp.tool()
def sheets_write(spreadsheet_id: str, range: str, values: list[list[Any]], value_input: str = "USER_ENTERED") -> str:
    """Overwrite a range with a 2-D array (in place). value_input: USER_ENTERED (parses formulas/dates) or RAW."""
    try:
        r = _sheets().spreadsheets().values().update(
            spreadsheetId=spreadsheet_id, range=range, valueInputOption=value_input, body={"values": values}
        ).execute()
        return f"updated {r.get('updatedRange')}: {r.get('updatedRows')} rows x {r.get('updatedColumns')} cols"
    except Exception as e:
        return _err(e)


@mcp.tool()
def sheets_append(spreadsheet_id: str, range: str, values: list[list[Any]], value_input: str = "USER_ENTERED") -> str:
    """Append rows after the last data row of the table that contains `range` (e.g. 'Track A!A1'). Returns the written range."""
    try:
        r = _sheets().spreadsheets().values().append(
            spreadsheetId=spreadsheet_id, range=range, valueInputOption=value_input,
            insertDataOption="INSERT_ROWS", body={"values": values},
        ).execute()
        u = r.get("updates", {})
        return f"appended {u.get('updatedRows')} row(s) at {u.get('updatedRange')}"
    except Exception as e:
        return _err(e)


@mcp.tool()
def sheets_clear(spreadsheet_id: str, range: str) -> str:
    """Clear values in a range (formatting kept)."""
    try:
        r = _sheets().spreadsheets().values().clear(spreadsheetId=spreadsheet_id, range=range, body={}).execute()
        return f"cleared {r.get('clearedRange')}"
    except Exception as e:
        return _err(e)


@mcp.tool()
def sheets_find(spreadsheet_id: str, query: str, sheet: str | None = None, match_case: bool = False) -> str:
    """Find cells whose text contains `query`. Returns JSON [{sheet, cell, value}]. Optionally limit to one tab."""
    try:
        meta = _sheets().spreadsheets().get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title").execute()
        titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
        if sheet:
            titles = [t for t in titles if t == sheet]
        q = query if match_case else query.lower()
        hits = []
        for t in titles:
            vals = _sheets().spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=t).execute().get("values", [])
            for ri, row in enumerate(vals, start=1):
                for ci, v in enumerate(row):
                    s = str(v)
                    if q in (s if match_case else s.lower()):
                        hits.append({"sheet": t, "cell": f"{_col(ci)}{ri}", "value": s[:200]})
        return json.dumps(hits, ensure_ascii=False)
    except Exception as e:
        return _err(e)


def _col(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


@mcp.tool()
def sheets_add_sheet(spreadsheet_id: str, title: str) -> str:
    """Add a new tab to an existing spreadsheet."""
    try:
        r = _sheets().spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": [{"addSheet": {"properties": {"title": title}}}]}).execute()
        sid = r["replies"][0]["addSheet"]["properties"]["sheetId"]
        return f"added sheet '{title}' (sheetId {sid})"
    except Exception as e:
        return _err(e)


@mcp.tool()
def sheets_batch_update(spreadsheet_id: str, requests: list[dict[str, Any]]) -> str:
    """Escape hatch: raw Sheets API batchUpdate requests (formatting, merges, conditional formats, delete rows…). See developers.google.com/sheets/api/reference/rest/v4/spreadsheets/request."""
    try:
        r = _sheets().spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests}).execute()
        return json.dumps(r.get("replies", []), ensure_ascii=False)[:4000]
    except Exception as e:
        return _err(e)


@mcp.tool()
def sheets_create(title: str, sheet_titles: list[str] | None = None) -> str:
    """Create a new spreadsheet (optionally with named tabs). Returns id and URL."""
    try:
        body: dict[str, Any] = {"properties": {"title": title}}
        if sheet_titles:
            body["sheets"] = [{"properties": {"title": t}} for t in sheet_titles]
        ss = _sheets().spreadsheets().create(body=body, fields="spreadsheetId,spreadsheetUrl").execute()
        return json.dumps({"id": ss["spreadsheetId"], "url": ss["spreadsheetUrl"]})
    except Exception as e:
        return _err(e)


# ----------------------------------------------------------------- Drive (read-only)
@mcp.tool()
def drive_search(query: str, page_size: int = 20, mime_type: str | None = None) -> str:
    """Search Drive. `query` may be Drive query syntax (e.g. \"name contains 'Resume'\") or plain words (matched with fullText contains). mime_type filter e.g. application/vnd.google-apps.spreadsheet."""
    try:
        q = query if any(op in query for op in (" contains ", "=", "mimeType", "modifiedTime", "'")) else f"fullText contains '{query}'"
        if mime_type:
            q = f"({q}) and mimeType = '{mime_type}'"
        q = f"({q}) and trashed = false"
        r = _drive().files().list(q=q, pageSize=page_size, orderBy="modifiedTime desc",
                                  fields="files(id,name,mimeType,modifiedTime,webViewLink,owners(emailAddress))").execute()
        return json.dumps(r.get("files", []), ensure_ascii=False)
    except Exception as e:
        return _err(e)


@mcp.tool()
def drive_get(file_id: str) -> str:
    """Metadata for one Drive file (name, mimeType, modifiedTime, size, owners, webViewLink)."""
    try:
        r = _drive().files().get(fileId=file_id, fields="id,name,mimeType,modifiedTime,size,owners(emailAddress),webViewLink,parents").execute()
        return json.dumps(r, ensure_ascii=False)
    except Exception as e:
        return _err(e)


# ----------------------------------------------------------------- CLI
def _doctor() -> int:
    ok = True
    print(f"client secret: {auth.CLIENT_SECRET} -> {'present' if auth.CLIENT_SECRET.exists() else 'MISSING'}")
    print(f"token file:    {auth.TOKEN_FILE} -> {'present' if auth.TOKEN_FILE.exists() else 'missing (run: gws-mcp auth)'}")
    print(f"scopes:        {', '.join(s.rsplit('/', 1)[-1] for s in auth.SCOPES)}")
    if auth.TOKEN_FILE.exists():
        try:
            print(f"authorized as: {auth.whoami()}")
            for name, ver in (("docs", "v1"), ("sheets", "v4")):
                build(name, ver, credentials=auth.load_credentials(), cache_discovery=False)
                print(f"api {name} {ver}: reachable")
        except Exception as e:
            ok = False
            print(f"PROBLEM: {_err(e)}")
    return 0 if ok else 1


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "serve"
    if cmd == "auth":
        email = auth.run_auth(open_browser="--no-browser" not in args)
        print(f"authorized as {email}; token saved to {auth.TOKEN_FILE}")
    elif cmd == "whoami":
        print(auth.whoami())
    elif cmd == "revoke":
        auth.revoke()
    elif cmd == "doctor":
        raise SystemExit(_doctor())
    elif cmd in ("serve", "stdio"):
        mcp.run(transport="stdio")
    else:
        print(__doc__)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
