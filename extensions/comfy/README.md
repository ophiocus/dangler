# comfy-mcp — generated images for any Claude session, from one local service

A dangler fleet server. It owns no model and no GPU: it speaks HTTP to the **AIProd image
gateway** (`ComfyUI/runner/gateway.py` in the AIProd repo), which runs the executor notebook
(`run.ipynb`) against ComfyUI and serves the results back.

```
any Claude session ─► dangler ─► comfy-mcp ─HTTP+token─► gateway.py ─► papermill run.ipynb ─► ComfyUI ─► GPU
   (any seat)                     (this)                  (GPU seat)
```

The same server runs on every seat. On the GPU seat it points at `127.0.0.1`; on any other seat it
points at the GPU seat. The tool surface is identical, which is the point.

| Tool | Does |
|---|---|
| `image_status` | gateway + ComfyUI state, which host serves, queue depth; starts the gateway on the GPU seat |
| `image_workflows` | the workflows the service can run and the parameters each accepts |
| `image_generate` | prompt (+ size, seeds, control image, LoRAs, workflow) → file path(s); blocks up to `wait_seconds`, then hands back a job id |
| `image_job` | poll / collect a job |

## Configuration

Everything is optional — the server starts and answers `tools/list` with nothing set, and every
call that needs a missing piece fails with the sentence that names it.

| Env | Meaning |
|---|---|
| `AIPROD_GATEWAY_URL` | default `http://127.0.0.1:8790` |
| `AIPROD_GATEWAY_TOKEN_FILE` | default `~/.aiprod/gateway.token`; minted by the gateway on first start. Copy the GPU seat's file to a seat that should be allowed in |
| `AIPROD_HOME` | the AIProd checkout — **GPU seat only**. Lets the server start the gateway when it is down and return files in place instead of copying them |
| `AIPROD_SAVE_DIR` | where other seats download results (default `~/Pictures/aiprod/<job>`) |
| `AIPROD_READ_ONLY=1` | status and catalog only; `image_generate` refuses |

`dangler.toml` on the GPU seat:

```toml
[servers.comfy]
command = "uv"
args = ["run", "--directory", "I:/dangler/extensions/comfy", "comfy-mcp"]
identity = "local ComfyUI on this workstation's GPU, through the AIProd executor notebook"
setup_hint = "needs the AIProd repo with .venv-runner (papermill); AIPROD_HOME names it. Token is minted at ~/.aiprod/gateway.token on first start"
[servers.comfy.env]
AIPROD_HOME = "I:/AIProd"
```

On another seat, drop `AIPROD_HOME` and set `AIPROD_GATEWAY_URL` to the GPU seat.

`SKILL.md` in this folder is the extension's name tag — the `generate-image` skill with
the workflow table and the prompting rules. dangler installs it into `~/.claude/skills/`
at every start, so it arrives on a seat with the same `git pull` as the code.

## Reaching the GPU seat from another machine — the myevery bus

Set `AIPROD_TRANSPORT=bus` and `MYEVERY_URL`. Every gateway call then travels over myevery's **piped transport**
(`/pipe/<channel>`: sender and receiver rendezvous, the server splices the streams, nothing is stored there). The GPU
seat's gateway listens on `comfy.<seat>`; replies come back on a one-time channel. The remote seat needs only the bus
bearer token — read from the Claude config that already registers the bus (`MYEVERY_MCP_JSON`, default
`~/.claude.json`) — and no route to the GPU seat, no gateway token, no firewall rule. Needs myevery ≥ 0.4.0.
Full seat setup: `ComfyUI/runner/SEATS.md` in the AIProd repo.

## Fallback: direct HTTP to the gateway

The gateway binds `127.0.0.1` by default, and ComfyUI itself is never exposed. Two ways in:

- **SSH tunnel through a host both seats already reach** (works from anywhere, no firewall change, nothing public):
  GPU seat `ssh -N -R 127.0.0.1:18790:127.0.0.1:8790 <relay>`; other seat `ssh -N -L 8790:127.0.0.1:18790 <relay>`;
  then the default URL just works there.
- **LAN**: start the gateway with `--lan` and allow inbound TCP 8790 from the private network in the
  GPU seat's firewall (an owner decision — the server never does this for you). URL `http://<gpu-seat-ip>:8790`.

## What a remote caller cannot do

Workflows by catalog name only · inputs only from files uploaded with the job · output only inside the
AIProd repo · never a model download (`AUTO_PROVISION` is forced off) · one batch at a time.
