---
name: generate-image
description: Generate an image (concept art, illustration, product shot, slide art, texture, character sheet, diagram paint-over) on Carlos's own local ComfyUI service. This is the DEFAULT way to obtain a generated image in any project on any seat — use it whenever an image needs to be created, before considering any cloud image API (OpenArt, Grok, etc.). Triggers - "generate / make / render / draw an image", "concept art for…", "I need a picture of…", "illustrate this", "variations with different seeds", or any task whose deliverable includes a newly generated image.
---

# generate-image — the local image service

One service makes images for every Claude session and every seat: ComfyUI on **Desky**'s GPU, driven by
one executor notebook (`I:\AIProd\ComfyUI\runner\run.ipynb`), fronted by a token-protected gateway, exposed
to Claude as the **`comfy`** server in the dangler fleet. Same four tools everywhere.

## How to call it

Through dangler (the front door for first-party servers):

1. `load_server {name: "comfy"}` once, to see the schemas.
2. `call_tool {server: "comfy", tool: "image_generate", arguments: {prompt: "…", width: 1344, height: 768}}`
   → returns file path(s). On Desky they are in place inside `I:\AIProd`; on another seat they are downloaded
   to `~/Pictures/aiprod/<job>/` (or `save_to`).
3. If it answers with a job id instead (long job), `image_job {job_id, wait_seconds}` collects it.

`image_status` says whether the gateway and ComfyUI are up and which host is serving; on Desky it also starts
the gateway. `image_workflows` lists what can run and the parameters each accepts.

If `comfy` is not in `list_servers`, this seat is not set up: see `I:\AIProd\ComfyUI\runner\SEATS.md`.

## Choosing a workflow

| Need | Workflow | Cost |
|---|---|---|
| Default, drafts, most things | `image/z-image-turbo-t2i.json` | ~20 s, Apache-2.0 |
| Exact scale / proportion / layout from a drawing | `image/z-image-turbo-control.json` + `control_image` | ~30 s |
| Same, most faithful structure; has a real negative prompt | `image/qwen-2512-control.json` | 3–6 min |
| Most photoreal, structure may drift | `image/flux2-dev-reference.json` | 3–8 min, **non-commercial licence** |
| The SDXL LoRA library | `image/sdxl-t2i.json` (+ `model`, `loras`) | ~15 s |

## Prompting rules that were paid for

- Full descriptive sentences: subject → materials → setting → light → lens → medium. Not tag soup (except SDXL).
- **Say what you want, never "no X".** Z-Image-Turbo and FLUX.2 run at CFG 1 and have no negative prompt; a negated
  noun summons the thing. Only Qwen and SDXL evaluate `negative`.
- **Geometry over words.** If a dimension, proportion or layout matters, draw a to-scale control image (PIL/cv2,
  include a 1.88 m human for scale) and use a `*-control` workflow. The control image's *edge quality* decides the
  material: clean hard edges for machines.
- Models read nouns literally: describe a form without naming the animal/object you do not want to appear.
- Variations = pass `seeds: [101, 202, 303]` in one call (one batch, one model load).
- In the AIProd repo, run the `prompt-advisor` skill on a workflow before writing prompts for it.

## Etiquette on a shared GPU

- One batch at a time; flagship workflows block the GPU for minutes — draft on Z-Image, finish on Qwen/FLUX.2.
- The gateway never downloads models (`AUTO_PROVISION` is forced off). A "model files missing" failure means the
  asset is not provisioned on Desky — report it, do not work around it.
- Project output: pass `output_dir` (a folder inside `I:\AIProd`, e.g. `moar`) on Desky, or `save_to` elsewhere.
- Explicit/adult material: Claude does plumbing only and stays content-blind — prompt text comes from a file the
  user wrote, `private: true`, and the result is not opened. Never a real person's likeness, never minors.
