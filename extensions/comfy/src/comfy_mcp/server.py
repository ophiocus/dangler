"""comfy-mcp: generated images for any Claude session, from one local service.

The server owns no model and no GPU. It speaks HTTP to the AIProd image gateway
(`ComfyUI/runner/gateway.py` in the AIProd repo), which runs the executor notebook against
ComfyUI. Point it at 127.0.0.1 on the machine that has the GPU, or at that machine from any
other seat — the tool surface is identical, which is the point.

Environment (all optional — the server starts and lists its tools with none of them set):
  AIPROD_GATEWAY_URL         default http://127.0.0.1:8790
  AIPROD_GATEWAY_TOKEN_FILE  default ~/.aiprod/gateway.token   (copy the GPU seat's file to other seats)
  AIPROD_HOME                the AIProd checkout, ONLY on the GPU seat: lets the server start the
                             gateway when it is down, and return files in place instead of copying
  AIPROD_SAVE_DIR            where other seats download results (default ~/Pictures/aiprod)
  AIPROD_READ_ONLY=1         status and catalog only; image_generate is refused

Reaching the GPU seat from another machine — the myevery bus (no network route between the seats needed):
  AIPROD_TRANSPORT=bus       carry every gateway call over myevery's piped transport (rendezvous, nothing
                             stored on the bus) instead of HTTP straight to the gateway
  MYEVERY_URL                the bus base URL, e.g. https://api.myevery.tecnocratica.com.co
  MYEVERY_HEADER_FILE        where the bus bearer lives (default ~/.dangler/myevery.headers, the same file
                             dangler's `myevery` fleet entry references) — read by reference, never copied.
                             Or MYEVERY_TOKEN. MYEVERY_MCP_JSON still reads it out of a Claude config
                             (default ~/.claude.json) for a seat whose direct registration is not retired yet.
                             The gateway token is NOT needed on a bus seat.
  AIPROD_GPU_SEAT            handle of the seat that owns the GPU (default desky)
  AIPROD_SEAT                this seat's own handle (default: the host name, lowercased)

Logging goes to stderr; stdout is the MCP transport.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

URL = os.environ.get("AIPROD_GATEWAY_URL", "http://127.0.0.1:8790").rstrip("/")
TOKEN_FILE = Path(os.environ.get("AIPROD_GATEWAY_TOKEN_FILE", Path.home() / ".aiprod" / "gateway.token")).expanduser()
HOME = os.environ.get("AIPROD_HOME", "")
SAVE_DIR = Path(os.environ.get("AIPROD_SAVE_DIR", Path.home() / "Pictures" / "aiprod")).expanduser()
READ_ONLY = os.environ.get("AIPROD_READ_ONLY", "") not in ("", "0", "false")
DEFAULT_WORKFLOW = "image/z-image-turbo-t2i.json"
TRANSPORT = os.environ.get("AIPROD_TRANSPORT", "http").lower()
BUS_URL = os.environ.get("MYEVERY_URL", "").rstrip("/").removesuffix("/mcp")
GPU_SEAT = os.environ.get("AIPROD_GPU_SEAT", "desky")
SEAT = os.environ.get("AIPROD_SEAT") or (os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "seat").lower()

SETUP = ("image gateway not reachable at {url}. On the GPU seat start it with "
         "`.venv-runner/Scripts/python ComfyUI/runner/gateway.py` in the AIProd repo (or set AIPROD_HOME so this "
         "server starts it). On another seat, set AIPROD_GATEWAY_URL to the GPU seat (LAN address, or an SSH "
         "tunnel to its port 8790) and copy its ~/.aiprod/gateway.token to AIPROD_GATEWAY_TOKEN_FILE.")


def log(*a: Any) -> None:
    print("[comfy-mcp]", *a, file=sys.stderr, flush=True)


class GatewayError(RuntimeError):
    pass


def _find_bus_token(node, url):
    """Walk any Claude config (.mcp.json, ~/.claude.json with per-project blocks) for the bearer token of the
    server registered at `url`. Placeholders like ${MYEVERY_TOKEN} are skipped."""
    if isinstance(node, dict):
        if str(node.get("url", "")).startswith(url):
            tok = str((node.get("headers") or {}).get("Authorization", "")).removeprefix("Bearer ").strip()
            if tok and "${" not in tok:
                return tok
        for v in node.values():
            found = _find_bus_token(v, url)
            if found:
                return found
    return None


def _bus_token() -> str:
    """The bearer, by reference, in the order a migrated seat should find it.

    The canonical home is dangler's header file — the same one the `myevery` fleet entry
    names, so one file serves both the bus's own MCP tools and this transport. The
    ~/.claude.json scan below is the pre-ingestion path: it still works for a seat whose
    direct registration has not been retired yet, and it is deliberately last, because a
    client registration is exactly what the wrap exists to remove."""
    if os.environ.get("MYEVERY_TOKEN"):
        return os.environ["MYEVERY_TOKEN"]
    hdr = Path(os.environ.get("MYEVERY_HEADER_FILE", Path.home() / ".dangler" / "myevery.headers")).expanduser()
    if hdr.is_file():
        for line in hdr.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition(":")
            if k.strip().lower() == "authorization":
                tok = v.strip().removeprefix("Bearer ").strip()
                if tok and "${" not in tok:
                    return tok
    src = Path(os.environ.get("MYEVERY_MCP_JSON", Path.home() / ".claude.json")).expanduser()
    if src.is_file():
        tok = _find_bus_token(json.loads(src.read_text(encoding="utf-8")), BUS_URL)
        if tok:
            return tok
    raise GatewayError(f"bus transport needs the myevery bearer token: write `Authorization: Bearer <token>` into "
                       f"{hdr} (the file dangler's `myevery` fleet entry already references), or set MYEVERY_TOKEN.")


def _pipe(method: str, channel: str, body: bytes | None = None, timeout: int = 75) -> tuple[int, bytes]:
    if not BUS_URL:
        raise GatewayError("AIPROD_TRANSPORT=bus but MYEVERY_URL is not set")
    req = urllib.request.Request(f"{BUS_URL}/pipe/{channel}", data=body, method=method,
                                 headers={"Authorization": "Bearer " + _bus_token(), "X-Pipe-From": SEAT,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, OSError) as e:
        raise GatewayError(f"myevery bus unreachable at {BUS_URL}: {e}") from None


def _call_over_bus(path: str, body: dict | None, raw: bool, timeout: int):
    """One gateway call as a piped round trip: the request is spliced through the bus to the GPU seat's
    listener, the reply comes back on a one-time channel. Nothing is stored on the bus — if the GPU seat
    is not listening, the send times out and we say so."""
    reply = "comfy.reply." + uuid.uuid4().hex
    msg = json.dumps({"reply": reply, "from": SEAT, "method": "POST" if body is not None else "GET",
                      "path": path, "body": body}).encode()
    for attempt in range(4):
        code, data = _pipe("POST", f"comfy.{GPU_SEAT}", msg)
        if code == 200:
            break
        if code == 409:                                   # another asker is mid-handshake on the channel
            time.sleep(0.5 + attempt)
            continue
        if code == 504:
            raise GatewayError(f"GPU seat '{GPU_SEAT}' is not listening on the bus (nobody receiving on "
                               f"comfy.{GPU_SEAT}). Its gateway must be running with ~/.aiprod/bus.json configured.")
        raise GatewayError(f"bus refused the request ({code}): {data[:200]!r}")
    else:
        raise GatewayError("bus channel stayed busy (409) — try again")
    deadline = time.time() + timeout + 180
    while time.time() < deadline:
        code, data = _pipe("GET", reply)
        if code == 204:
            continue                                       # GPU seat still working; ask again
        if code != 200:
            raise GatewayError(f"bus reply failed ({code}): {data[:200]!r}")
        head, _, payload = data.partition(b"\n")
        status = json.loads(head)["status"]
        if status >= 400:
            try:
                detail = json.loads(payload).get("error", payload[:300])
            except ValueError:
                detail = payload[:300]
            raise GatewayError(f"gateway refused ({status}): {detail}")
        return payload if raw else json.loads(payload)
    raise GatewayError(f"no reply from GPU seat '{GPU_SEAT}' in time")


def _call(path: str, body: dict | None = None, raw: bool = False, timeout: int = 60):
    if TRANSPORT == "bus":
        return _call_over_bus(path, body, raw, timeout)
    if not TOKEN_FILE.is_file():
        raise GatewayError(f"no gateway token at {TOKEN_FILE}. " + SETUP.format(url=URL))
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + TOKEN_FILE.read_text(encoding="utf-8").strip(),
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            return data if raw else json.loads(data)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("error", detail)
        except ValueError:
            pass
        raise GatewayError(f"gateway refused ({e.code}): {detail}") from None
    except (urllib.error.URLError, OSError) as e:
        raise ConnectionError(str(e)) from None


def _spawn_detached_windows(cmd: list[str], cwd: str) -> None:
    """Start a process that outlives this one on Windows.

    MCP clients hold their servers in a job object that kills every descendant and forbids
    breakaway, so re-parenting tricks are not enough: a gateway started as our child dies with the
    session. WMI creates the process from its own provider host — outside our job and our tree —
    and ShowWindow=0 keeps the console hidden."""
    q = lambda v: "'" + v.replace("'", "''") + "'"
    ps = ("$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}; "
          f"$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
          f"@{{CommandLine={q(subprocess.list2cmdline(cmd))}; CurrentDirectory={q(cwd)}; ProcessStartupInformation=$si}}; "
          "if ($r.ReturnValue -ne 0) { throw \"Win32_Process.Create returned $($r.ReturnValue)\" }")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if r.returncode != 0:
        raise GatewayError("could not start the gateway: " + (r.stderr or r.stdout).strip()[:400])


def _ensure_gateway() -> None:
    """Reach the gateway; on the GPU seat (AIPROD_HOME set, loopback URL) start it if it is down."""
    try:
        _call("/health", timeout=5)
        return
    except ConnectionError:
        pass
    local = any(h in URL for h in ("127.0.0.1", "localhost"))
    py = next((p for p in (Path(HOME) / ".venv-runner" / "Scripts" / "python.exe",
                           Path(HOME) / ".venv-runner" / "bin" / "python") if HOME and p.is_file()), None)
    if not (local and py):
        raise GatewayError(SETUP.format(url=URL))
    log("starting gateway from", HOME)
    gw_log = str(Path(HOME) / "ComfyUI" / "runs" / "gateway.log")
    cmd = [str(py), str(Path(HOME) / "ComfyUI" / "runner" / "gateway.py"), "--log", gw_log]
    if os.name == "nt":
        _spawn_detached_windows(cmd, str(Path(HOME)))
    else:
        subprocess.Popen(cmd, cwd=HOME, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(20):
        time.sleep(0.5)
        try:
            _call("/health", timeout=3)
            return
        except ConnectionError:
            continue
    raise GatewayError("started the gateway but it is not answering — see ComfyUI/runs/gateway.log in the AIProd repo")


def _collect(job: dict, save_to: str | None) -> list[str]:
    """Files in place on the GPU seat; downloaded everywhere else (or when save_to is given)."""
    if HOME and not save_to:
        return list(job.get("paths", {}).values())
    dest = Path(save_to).expanduser() if save_to else SAVE_DIR / job["id"]
    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for name in job["files"]:
        target = dest / name
        target.write_bytes(_call(f"/jobs/{job['id']}/files/{name}", raw=True, timeout=600))
        out.append(str(target))
    return out


def _report(job: dict, save_to: str | None) -> str:
    if job["state"] in ("queued", "running"):
        return (f"job {job['id']} is {job['state']} — the GPU serves one batch at a time and flagship workflows take "
                f"minutes per image. Call image_job with this id to collect the result.")
    if job["state"] == "failed":
        return f"job {job['id']} FAILED: {json.dumps(job.get('error'))[:1500]}"
    files = _collect(job, save_to)
    secs = (job.get("finished") or 0) - (job.get("started") or 0)
    return f"job {job['id']} done in {secs:.0f}s — {len(files)} image(s):\n" + "\n".join(files)


def _wait(jid: str, seconds: float) -> dict:
    end = time.time() + seconds
    while True:
        job = _call(f"/jobs/{jid}")
        if job["state"] in ("done", "failed") or time.time() >= end:
            return job
        time.sleep(4 if TRANSPORT == "bus" else 2)


# ------------------------------------------------------------------ tools

def t_status(_: dict) -> str:
    _ensure_gateway()
    h = _call("/health")
    where = f"GPU seat '{GPU_SEAT}' over the myevery bus ({BUS_URL}, piped)" if TRANSPORT == "bus" else f"gateway {URL}"
    return (f"{where} on host {h.get('host') or '?'}: ok; ComfyUI {h['comfyui']}; "
            f"{h['active_jobs']} active job(s); {'READ-ONLY; ' if READ_ONLY else ''}"
            f"results {'returned in place' if HOME else f'downloaded to {SAVE_DIR}'}")


def t_workflows(_: dict) -> str:
    _ensure_gateway()
    rows = _call("/workflows")["workflows"]
    lines = [f"- {w['workflow']}  [{w['family']}]  params: {', '.join(w['params'])}"
             f"{'  (needs control_image)' if w['needs_image'] else ''}{'  (LoRAs ok)' if w['loras'] else ''}\n    {w['title']}"
             for w in rows]
    return "\n".join(lines)


SPEC_KEYS = ("prompt", "workflow", "width", "height", "steps", "negative", "strength", "model",
             "control_image", "seed", "seeds", "params", "loras", "name")


def _spec_to_jobs(m: dict, index: int, many: bool, files: dict, catalog: dict | None = None) -> list[dict]:
    """One job spec → the gateway's job entries (one per seed).

    Uploads any control image into the shared `files` map, so several specs in
    one request can reference different drawings without colliding.

    `catalog` maps a workflow to the parameters it declares. The tool's own
    implicit parameters (prompt, seed, prefix, size…) go only to a workflow that
    takes them, so utility workflows with no sampler (vectorize, alpha-cutout,
    upscale) are callable here. Explicit `params` always pass: a typo still
    fails loudly in the runner.
    """
    declared = (catalog or {}).get(m.get("workflow") or DEFAULT_WORKFLOW)
    takes = (lambda k: True) if declared is None else (lambda k: k in declared)
    if takes("prompt") and not m.get("prompt"):
        raise GatewayError(f"job {index}: 'prompt' is required")
    params = {k: m[k] for k in ("width", "height", "steps", "negative", "strength", "model")
              if m.get(k) is not None and takes(k)}
    params.update(m.get("params") or {})
    if takes("prompt"):
        params["prompt"] = m["prompt"]
    inputs = {}
    if m.get("control_image"):
        src = Path(m["control_image"]).expanduser()
        if not src.is_file():
            raise GatewayError(f"job {index}: control_image not found: {src}")
        files[src.name] = base64.b64encode(src.read_bytes()).decode()
        inputs["image"] = src.name
    seeds = m.get("seeds") or [m.get("seed", -1)]
    name = m.get("name") or (f"job{index:02d}" if many else "image")
    def per_seed(s: int) -> dict:
        p = dict(params)
        if takes("seed"):
            p["seed"] = s
        if takes("prefix"):
            p["prefix"] = f"{name}_s{s}" if s != -1 else name
        return p

    return [{"name": f"{name}-s{s}" if len(seeds) > 1 else name,
             "workflow": m.get("workflow") or DEFAULT_WORKFLOW,
             "params": per_seed(s),
             "inputs": inputs, "loras": m.get("loras") or []} for s in seeds]


def t_generate(a: dict) -> str:
    if READ_ONLY:
        raise GatewayError("AIPROD_READ_ONLY is set — generation is disabled on this seat")
    _ensure_gateway()
    # One call may carry several job specs. Top-level fields are the defaults a
    # spec overrides, so a study of N subjects across M workflows is ONE request
    # and lands in ONE run folder with one run.json — the same shape the
    # executor notebook's JOBS list produces, which the gateway already accepted.
    specs = a.get("jobs") or [a]
    if not isinstance(specs, list) or not specs:
        raise GatewayError("'jobs' must be a non-empty list of job specs")
    defaults = {k: a[k] for k in SPEC_KEYS if k in a}
    files: dict[str, str] = {}
    jobs: list[dict] = []
    catalog = {w["workflow"]: set(w["params"]) for w in _call("/workflows")["workflows"]}
    for i, spec in enumerate(specs):
        merged = {**defaults, **{k: v for k, v in spec.items() if k in SPEC_KEYS}} if spec is not a else dict(a)
        jobs += _spec_to_jobs(merged, i, len(specs) > 1, files, catalog)
    res = _call("/jobs", {"jobs": jobs, "output_dir": a.get("output_dir"), "private": bool(a.get("private")), "files": files})
    return _report(_wait(res["id"], float(a.get("wait_seconds", 240))), a.get("save_to"))


def t_job(a: dict) -> str:
    _ensure_gateway()
    return _report(_wait(a["job_id"], float(a.get("wait_seconds", 0))), a.get("save_to"))


TOOLS: dict[str, tuple[Any, types.Tool]] = {
    "image_status": (t_status, types.Tool(
        name="image_status",
        description="Is the local image service up? Reports the gateway, the GPU seat's host name, ComfyUI state and "
                    "queue depth. Call this first when generation misbehaves; it also starts the gateway on the GPU seat.",
        inputSchema={"type": "object", "properties": {}})),
    "image_workflows": (t_workflows, types.Tool(
        name="image_workflows",
        description="List the workflows the service can run, with the parameter names each accepts. Fast drafts: "
                    "image/z-image-turbo-t2i.json (~20 s, Apache-2.0). Structure control from a drawing: "
                    "image/z-image-turbo-control.json (fast) or image/qwen-2512-control.json (minutes, most faithful). "
                    "image/flux2-dev-reference.json is the most photoreal but NON-COMMERCIAL.",
        inputSchema={"type": "object", "properties": {}})),
    "image_generate": (t_generate, types.Tool(
        name="image_generate",
        description="Generate image(s) on the local ComfyUI service and return the file path(s). This is the default way "
                    "to obtain a generated image — prefer it over any cloud image API. Write the prompt as full "
                    "descriptive sentences (subject, setting, light, lens, medium); state what you want, never 'no X' "
                    "(negations backfire on CFG-1 models). For exact scale, proportion or layout, draw a control image "
                    "and use a *-control workflow instead of describing geometry. Pass several seeds to get variations in "
                    "one batch, or a `jobs` list to put SEVERAL DIFFERENT prompts, workflows and control images in ONE "
                    "run — a visual study of many subjects across models lands in a single run folder with one run.json, "
                    "which is what makes the candidates comparable. Blocks up to wait_seconds, then returns a job id for "
                    "image_job.",
        inputSchema={"type": "object", "properties": {
            "jobs": {"type": "array", "description": "Several job specs in one run. Each may carry its own prompt, "
                                                     "workflow, control_image, seed(s), negative, size, model, loras and "
                                                     "name; anything it omits falls back to the top-level value. Use this "
                                                     "for a study: one subject per spec, or one model per spec.",
                     "items": {"type": "object", "properties": {
                         "prompt": {"type": "string"}, "workflow": {"type": "string"}, "name": {"type": "string"},
                         "control_image": {"type": "string"}, "negative": {"type": "string"},
                         "seed": {"type": "integer"}, "seeds": {"type": "array", "items": {"type": "integer"}},
                         "width": {"type": "integer"}, "height": {"type": "integer"}, "steps": {"type": "integer"},
                         "strength": {"type": "number"}, "model": {"type": "string"},
                         "params": {"type": "object"}, "loras": {"type": "array", "items": {"type": "object"}}}}},
            "prompt": {"type": "string", "description": "What to render, in full sentences. Required unless `jobs` is given, "
                                                       "or the workflow takes no prompt (image/vectorize*.json, "
                                                       "image/alpha-cutout.json, image/upscale-model.json)."},
            "workflow": {"type": "string", "description": f"From image_workflows. Default {DEFAULT_WORKFLOW}."},
            "width": {"type": "integer", "description": "Multiple of 16. Z-Image: 1024x1024, 832x1216, 1216x832, 1344x768."},
            "height": {"type": "integer"},
            "seed": {"type": "integer", "description": "-1 (default) = random."},
            "seeds": {"type": "array", "items": {"type": "integer"}, "description": "Several seeds = one batch of variations."},
            "steps": {"type": "integer"},
            "negative": {"type": "string", "description": "Only evaluated by real-CFG workflows (Qwen, SDXL)."},
            "control_image": {"type": "string", "description": "Local path of a drawing; required by *-control / *-reference workflows. Uploaded with the job."},
            "strength": {"type": "number", "description": "ControlNet strength, 0.6-0.9."},
            "model": {"type": "string", "description": "Swap the checkpoint within the workflow's family."},
            "loras": {"type": "array", "items": {"type": "object", "required": ["name"], "properties": {
                "name": {"type": "string"}, "strength": {"type": "number"}}}},
            "params": {"type": "object", "description": "Any other parameter the workflow lists."},
            "name": {"type": "string", "description": "Short slug used in file names."},
            "output_dir": {"type": "string", "description": "Folder inside the AIProd repo on the GPU seat (e.g. 'moar'). Default ComfyUI/runs/gateway-out."},
            "save_to": {"type": "string", "description": "Local folder to copy results into (other seats download here; default ~/Pictures/aiprod/<job>)."},
            "private": {"type": "boolean", "description": "Keep prompt text and images out of the run record and notebook."},
            "wait_seconds": {"type": "number", "description": "How long to block before returning a job id. Default 240."}}})),
    "image_job": (t_job, types.Tool(
        name="image_job",
        description="Check a generation job and collect its files once done. Use after image_generate returned a job id.",
        inputSchema={"type": "object", "required": ["job_id"], "properties": {
            "job_id": {"type": "string"}, "wait_seconds": {"type": "number", "description": "Block up to this long. Default 0."},
            "save_to": {"type": "string"}}})),
}

server = Server("comfy-mcp")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [tool for _, tool in TOOLS.values()]


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    fn = TOOLS[name][0]
    try:
        text = await asyncio.to_thread(fn, arguments or {})
    except (GatewayError, ConnectionError, FileNotFoundError, KeyError) as e:
        text = f"ERROR: {e}"
    return [types.TextContent(type="text", text=text)]


def main() -> None:
    async def run() -> None:
        async with stdio_server() as (r, w):
            await server.run(r, w, server.create_initialization_options())
    asyncio.run(run())


if __name__ == "__main__":
    main()
