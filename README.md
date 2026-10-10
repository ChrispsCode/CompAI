# Comfy ↔ Nuke bridge

Run ComfyUI workflows from Nuke nodes. You set the prompt, seed, format and inputs on a Nuke node and press **Generate**. ComfyUI renders the image in the background, and the result comes back into the comp as a normal plate.

> **Status: early and fluid.** Two node types work today (text-to-image and image edit). The overall structure is deliberately *not* settled yet. See [Open questions](#7-open-questions--ideas-not-decisions).

---

# Part 1 — User guide

## What you need

- **ComfyUI** running on the same machine. The default address is `127.0.0.1:8188`; set the `COMFY_HOST` env var or the node's *Settings* tab if yours is different.
- The models used by the templates installed in ComfyUI. For the current templates that's Qwen Image 2.1: the diffusion model, the Qwen3-VL text encoder, the VAE, and the Qwen 3.5 prompt-enhancer encoders.
- Nuke. No extra Python packages are needed on the Nuke side.

## Install

1. **Nuke side:** add the `nuke_plugin/` folder to Nuke's plugin path, either on `NUKE_PATH` or with `nuke.pluginAddPath("/path/to/CompAI/nuke_plugin")` in `~/.nuke/init.py`. The nodes appear under **Comfy** in the node menu.
2. **Comfy side (recommended):** copy or symlink `comfyui_custom_nodes/comfy_nuke_nodes/` into `ComfyUI/custom_nodes/` and restart ComfyUI. This lets Comfy read Nuke's images straight from disk instead of uploading copies.

## ComfyGenerate (text → image)

1. Create **Comfy › ComfyGenerate**.
2. Type a prompt. Tick **enhance prompt** to have an LLM expand it first. This is slower, and the rewritten prompt appears on the *Results* tab.
3. Set the **target format**. The image is generated at about **megapixels** (default 1 MP), at the same aspect ratio, then reformatted to the target. The *generate at* line shows both sizes.
4. Set **output dir** and **name**, then press **Generate**. Nuke stays usable while it runs. **Cancel** stops the job.
5. The result loads into the node and is held on every frame.

## ComfyEdit (image edit / multi-reference)

1. Create **Comfy › ComfyEdit** and connect 1–4 images. Unconnected inputs are skipped, and the connected ones are numbered in order.
2. Write an instruction, e.g. *"replace the sky with a stormy sunset"*. With several inputs you can refer to them by number: *"put the jacket from image 2 on the person in image 1"*.
3. By default the output matches the first input's format (**match first input's format**).
4. Press **Generate**. Each input is grabbed at the **current frame**.

## Where things go

Each run writes versioned files to the output dir:

| File | What |
|---|---|
| `name_v003.png` | the result |
| `name_v003.json` | provenance: seed, prompts, timing, and the exact workflow that ran |
| `name_v003_in1.png` … | (Edit only) the downsized sRGB inputs that were sent to Comfy |

To recreate a result, use the seed on the *Results* tab and press **Reuse last seed**, or untick **new seed each run**.

If the output dir doesn't resolve to an absolute path (e.g. an unset env var), files go to `~/comfy_nuke`.

## Troubleshooting

- **"ComfyUI not reachable"**: Comfy isn't running, or the host is wrong (*Settings* tab).
- **"No node titled …"**: the template file doesn't match what the node expects. See the template rules in Part 2.
- **"Workflow template not found"**: the path on the *Settings* tab doesn't exist. If only the folder is wrong (the tool was moved), a template with the same file name in this install's `templates/` is used automatically.
- **Generate is greyed out with nothing running**: close and reopen the node's panel.
- **Colours look off**: set **PNG colorspace** on the Edit node's *Settings* tab to your OCIO config's sRGB name.
- **Errors from Comfy** show in a popup. The ComfyUI console has the full traceback.

---

# Part 2 — Developer guide (for Claude / future maintainers)

This section is meant to bring a fresh session fully up to speed. Read it all before changing code.

> ### ⚡ Real Nuke can be run headless from the shell. Use it.
>
> ```bash
> /opt/Nuke15.2v9/Nuke15.2 -ti my_test.py 2>&1 | grep -v "^$"
> ```
>
> - Runs `my_test.py` in real Nuke 15.2 with no GUI, in about 10 seconds. `-i` uses Harry's interactive licence, so it works while his GUI session is open. Nuke 17.1 is at `/opt/Nuke17.1v1`.
> - Harry's `~/.nuke/init.py` runs, so `import comfy_nuke, comfy_nuke_edit` just works.
> - **Good for:** creating nodes, checking knobs exist and take the values we set, expressions, formats, colourspace names, executing Writes, loading a result into the Read. Check every Nuke API assumption here before handing anything to Harry.
> - **Not covered:** `menu.py` (not run in terminal mode), anything visual, panel behaviour, and the background-thread job flow. Harry tests those in the GUI.
> - Don't override `HOME`: the licence lives there and Nuke won't start.
> - Don't queue real generations on Harry's GPU from a test without asking.
> - Put test scripts and their output in a scratch directory, not the repo.

## 1. Context and goals

- **Owner:** Harry, a lead compositor who builds Nuke pipeline tools professionally and knows Nuke Python/TCL well. He is newer to AI generation and is learning ComfyUI in parallel.
- **Goal:** Nuke-level pipeline integration (knobs, versioning, plates in, plates out) with Comfy-level AI capability. Longer term: more workflow types (video included), and possibly Foundry Griptape as a professional path.
- **Machine:** Kubuntu 26.04, RTX 4070 12 GB for ComfyUI, and a separate GTX 1650 that drives the displays and Nuke. ComfyUI runs locally on the same box, so a shared filesystem is assumed but not required.
- **How Harry likes to work:** explain the *why* before implementing, fix one issue at a time, and test from the CLI before Nuke. He iterates with short, precise feedback and often patches the code himself; respect his edits (see §6).

## 2. Architecture (and why)

```
Nuke node (knobs) ──HTTP──▶ ComfyUI server ──▶ GPU
      ▲                         │
      └──── PNG on disk ◀───────┘  (downloaded via /view, read by an internal Read)
```

- **Nuke is a thin client and never imports torch.** Running models in Nuke's Python would fight its bundled libraries, freeze the UI, risk a CUDA crash taking the comp down, and reload the model for every job. ComfyUI keeps models warm, handles VRAM offloading, and could later live on another machine or a farm.
- **Comfy is used as a render service (option A).** The alternative (option B) is a standalone HF `diffusers` backend with no Comfy dependency. It's still on the table as a later "backend" behind the same interface; it's not started.
- **Workflow JSON is the source of truth.** There is no "Python behind the workflow" to extract. Workflows are exported from Comfy with **Save (API)** and patched in Python before submission.

## 3. Files

```
nuke_plugin/                 everything Nuke loads; this folder goes on the plugin path
  menu.py                    registers the Comfy menu
  comfy_client.py
  comfy_nuke.py
  comfy_nuke_edit.py
  templates/                 the workflows the nodes run
comfyui_custom_nodes/        installed into ComfyUI, not Nuke
  comfy_nuke_nodes/
reference_comfy_workflows/   Harry's Comfy exports, for reference only (not loaded)
tests/                       offline tests, see §9
```

| File | Imports nuke? | Role |
|---|---|---|
| `comfy_client.py` | no (stdlib only) | HTTP client, template patching, image-slot binding, `generate()` / `generate_edit()`, CLI |
| `comfy_nuke.py` | yes | **ComfyGenerate** (T2I) node, plus everything the nodes share: knob groups (`prompt_knobs`, `seed_knobs`, `size_knobs`, `output_knobs`, `action_knobs`, `results_knobs`, `settings_knobs`), `add_output_chain`, the job machinery (`common_params`, `next_seed`, `start_job`, `_worker`, `_on_done`, `_status`, `_JOBS`) and helpers (`generation_size`, `output_dir`, `next_output_path`) |
| `comfy_nuke_edit.py` | yes | **ComfyEdit** node: writes Nuke inputs to PNG and reuses `comfy_nuke`'s shared parts |
| `comfy_nuke_nodes/__init__.py` | — (ComfyUI custom node) | `NukeLoadImagePath`: LoadImage from an absolute path |
| `templates/basic_t2i_qwen_api_v002.json` | — | Qwen Image 2.1 text-to-image |
| `templates/qwen_i2i_template_v002.json` | — | Qwen Image 2.1 edit, 10 image slots |

The three modules are flat, top-level modules on purpose: knob callbacks are saved into scripts as strings (`import comfy_nuke; comfy_nuke.on_generate(...)`), so renaming or packaging them breaks every existing node.

**Layering rule:** keep everything that doesn't need Nuke in `comfy_client.py`, or other nuke-free modules, so it can be tested from a shell. Nuke modules handle knobs, node graph and threading only.

## 4. Template contract

A template is any **Save (API)** export that follows these rules:

1. **Patch points are found by `_meta.title`, never by node ID.** IDs change on re-export, and grouped nodes get flattened to IDs like `459_458`. Titles use the `nuke_` prefix and must be unique, because `find_node` raises on zero or multiple matches.
2. **There is exactly one output node, titled `nuke_save`**, wired to the final image. Comfy only executes nodes that feed an output node; a graph without one runs nothing.
3. **One source for the prompt.** It's a `PrimitiveString`/`PrimitiveStringMultiline` titled `nuke_prompt`, wired to every consumer (the enhancer *and* the switch's `on_false`). Never leave the literal prompt duplicated in two places.
4. **Image inputs are `LoadImage` nodes titled `nuke_input_1 … nuke_input_N`, contiguous.** The client fills them in order and **prunes** unused ones by deleting the node and every link to it. This is legal because the consumers use Comfy's auto-grow inputs (`images.image_1…`, `images.image0…`), which are optional. Never leave a gap in the numbering.
5. In API format **every widget is an input**. Any key under `inputs` can be overwritten with a literal, which also replaces a link.
6. A `PreviewAny` ("Preview as Text") on the prompt path lets the client read back the prompt that was actually used (`output_text`).

Current titles:

| Title | Input(s) patched | T2I | Edit |
|---|---|---|---|
| `nuke_prompt` | `value` | ✓ | ✓ |
| `nuke_enhance_switch` | `switch` (true = LLM rewrite) | ✓ | ✓ |
| `nuke_generate_text` | `sampling_mode.seed` (same seed as sampler) | ✓ | ✓ |
| `nuke_ksampler` | `seed`, `steps` | ✓ | ✓ |
| `nuke_latent` | `width`, `height` (ints) | ✓ | ✓ |
| `nuke_size_switch` | `switch`: true = `nuke_latent` size, false = size of reference image 1 | | ✓ |
| `nuke_save` | `filename_prefix`; outputs read from history | ✓ | ✓ |
| `nuke_input_1..10` | `image` (upload mode) or swapped node (path mode) | | ✓ |

Known template quirk: in the edit template, **Batch Images (feeding the prompt enhancer) is only wired to inputs 1–2**. The enhancer sees two refs, while the image model sees all of them. Fix it in Comfy if needed.

## 5. Behaviour reference

**Sizing (`comfy_client.generation_size`)**: keep the target aspect, use a pixel budget of `megapixels × 1024²`, and snap each side to a multiple of 16 (Qwen/Flux use 8× VAE plus 2×2 patches). Anamorphic formats are squared up with `width × pixelAspect`. In Nuke, an internal `Reformat` (`fill`, Lanczos4, `pbb`) takes the result back to the target format. Resizing is deliberately done in Nuke, not Comfy: generation resolution and delivery resolution are separate concerns.

**Job flow (`run_workflow`)**: POST `/prompt` → poll `/history/<id>` (an error status raises `ComfyError`) → find image refs under the `nuke_save` node's outputs (any list of dicts that have `filename`) → GET `/view` → write to `out_path` → write a `<stem>.json` sidecar containing the result dict *and* the patched workflow.

**Image transfer (edit)**: `generate_edit(..., input_mode=)`
- `path`: each `nuke_input_N` node is swapped at submit time for `NukeLoadImagePath{path}`, which has the same outputs (IMAGE, MASK). Nothing is copied. Templates still use stock LoadImage, so they keep working in the Comfy UI.
- `upload`: POST multipart `/upload/image` into `input/nuke/`, with stable per-node names (`<NodeName>_in<N>.png`) so repeat runs overwrite. Comfy hashes contents to detect changes.
- `auto` (default): path if `GET /object_info/NukeLoadImagePath` shows the node, else upload.
- Security note: the path loader lets anyone who can queue prompts read files the Comfy process can read. Fine for localhost; don't expose the server.

**Nuke threading rules (important):**
- Read all knobs and execute all Writes on the **main thread** in `on_generate`. The worker thread only calls `comfy_client` functions.
- The worker reports back via `nuke.executeInMainThread` (`_status`, `_on_done`, `_on_error`).
- `_JOBS[node.fullName()] = threading.Event` is both the busy flag and the cancel flag (cancel → `/interrupt`).
- `_on_done` / `_on_error` assume the node has these knobs: `generate`, `status`, `last_file`, `last_seed`, `used_prompt`, plus an internal `ComfyRead`. Any new node type that uses `start_job` must provide them; building it from the shared knob groups and `add_output_chain` does.
- `start_job` disables the Generate button, and Nuke saves that state with the script. `release_stale_lock` re-enables it on `showPanel` when no job is running, so a script saved mid-job doesn't stay locked.
- If the node is deleted mid-job, `nuke.toNode(key)` returns None and the result is dropped quietly.

**Edit node specifics**: each input `imgN → prepN (Reformat to box, distort) → writeN (png, 8 bit, rgb, disabled except while executing)`.
- **Packing:** connected inputs are packed into slots 1..k.
- **Ref sizing:** each ref is sized to the megapixel budget at its *own* aspect.
- **Target format:** "match first input's format" copies the first connected input's format onto `target_format`.
- **Recursion guard:** `_IN_REFRESH` stops `knobChanged` from recursing when code sets knobs.
- **Colourspace:** `_set_colorspace` tries `SRGB_CANDIDATES` against the Read/Write `colorspace` knob values, or uses the *PNG colorspace* override.

**Versioning**: `next_output_path` scans for `<name>_vNNN.png` and takes max+1. Input PNGs (`_in1`) and sidecars don't match the pattern, so they don't affect numbering.

**Output dir**: `output_dir` falls back to `~/comfy_nuke` only when the evaluated path is empty or not absolute, or when it's the `root.name` default in an unsaved script. An earlier version ignored user paths in unsaved scripts; that was a bug, now fixed.

**Template path**: the knob stores an absolute path. `resolve_template` uses it if it exists, else a same-named file in this install's `templates/`, so nodes created before the tool was moved keep working.

## 6. Nuke API notes learned the hard way (from Harry's fixes)

- `node.setName(name)` has **no** `uncollisionable` argument.
- `Format_Knob.setValue()` only accepts the **name** of a registered format. Passing a `Format` object does nothing except log "not in named format list", and raises no exception. For an unnamed format, register it with `nuke.addFormat("w h pa name")` first (adding the same one twice is harmless). See `comfy_nuke_edit._set_format`.
- Don't rely on a chained `init.py`: with `nuke.pluginAddPath("/…/CompAI/")` in `~/.nuke/init.py`, Nuke 15.2 added the path but never ran `CompAI/init.py` (it did run when the same folder was on `NUKE_PATH`; cause not found). Pointing the plugin path straight at `nuke_plugin/` needs no `init.py`.
- Format knobs **can** be expression-linked: `reformat["format"].setExpression("parent.target_format")`. Use this rather than syncing in `knobChanged`.
- Harry's preferred default output dir is `[getenv SHOT_ROOT]/gen`.
- Callbacks are stored as strings like `"import comfy_nuke; comfy_nuke.on_generate(nuke.thisNode())"`, so existing nodes pick up code changes after `importlib.reload`.
- **Treat Harry's versions of files as canonical.** He tests in real Nuke and patches things. Diff against his copy before rewriting.

## 7. Open questions / ideas (not decisions)

Harry wants the structure to stay fluid until a few more workflow types exist. Don't refactor toward any of these without discussing first.

- **One node class per workflow *type*** (T2I, Edit, later Video, Upscale…) is Harry's current leaning. The shared machinery already lives in `comfy_nuke.py`.
- **Generic, manifest-driven alternative:** a `manifest.json` next to each workflow lists `params` (knob → [(title, input), …], type, default, range) and `roles` (seed, size, inputs, output). A single generic builder would create knobs from it, and `/object_info` could validate bindings and supply ranges or enums. Floated, not adopted.
- **Qwen-specific builders:** `build_qwen_t2i` / `build_qwen_edit` are Qwen-specific in name but mostly generic (the shared part is `_patch_common`). They'll likely generalise once a non-Qwen template arrives.
- **Template library:** a library path (e.g. a `COMFY_NUKE_TEMPLATES` env var) plus menu entries generated by scanning it.
- **Validation on node creation:** check that every expected `nuke_` title and input exists, so failures show up early rather than mid-render.
- **Colour:** the pipeline is 8-bit sRGB PNG both ways for now. Worth exploring: `SaveImageAdvanced` has `format.bit_depth` and colour-space options, so 16-bit output may be possible, and how display-referred AI output should sit in a scene-linear comp.
- **Video / per-frame work:** generating frame by frame flickers. Proper video needs video-model templates, which will stretch the "single image in, single image out" assumptions.
- **Backend abstraction:** a `submit/poll/download` interface so a `diffusers` or cloud backend could slot in.

## 8. Status: what's actually been tested

| Piece | Status |
|---|---|
| `comfy_client` T2I via CLI | ✅ tested by Harry against live Comfy (before the Oct 2026 restructure) |
| ComfyGenerate node in Nuke | ✅ tested by Harry before the restructure; ⚠️ not yet re-tested since. Logic is unchanged, but knob creation and `on_generate` now go through the shared helpers |
| Output dir fix | ✅ tested by Harry |
| Template patching, pruning, path-swap | ✅ offline tests against the real templates (no dangling links for 1–10 images) |
| Both templates vs live Comfy `/object_info` | ✅ every node class and input name exists on Harry's server (ComfyUI 0.38.1). Model files not checked |
| Client flow, both nodes end to end | ✅ offline only, against a fake Comfy server and a fake `nuke` module (`tests/`). Proves our own logic, not the real APIs |
| `upload_image`, `generate_edit` against live Comfy | ⚠️ untested |
| `NukeLoadImagePath` custom node | ⚠️ untested and not installed on Harry's Comfy yet (written against ComfyUI's legacy node API) |
| Both nodes in real Nuke 15.2, headless (`-t`), no generation | ✅ creation, knobs, format expression link, "match input" for named and unnamed formats, writing an input PNG (sRGB picked under the default config), loading a result into the Read |
| ComfyEdit in the Nuke GUI, end to end with Comfy | ⚠️ untested. Still open: Write colourspace names under Harry's OCIO config |
| `menu.py` in the GUI | ⚠️ untested by Claude (terminal mode doesn't run `menu.py`); imports are confirmed |

## 9. Offline tests

```bash
# everything offline: patching, client vs a fake Comfy server, nodes vs a fake nuke
python3 -m unittest discover -s tests

# live runs (Comfy up), from nuke_plugin/
python3 comfy_client.py templates/basic_t2i_qwen_api_v002.json --prompt "a ring in a box" --width 1408 --height 736 --out ./t2i_v001.png
python3 comfy_client.py templates/qwen_i2i_template_v002.json --prompt "make it night" --image ./plate.png --width 1024 --height 1024 --input-mode path --out ./edit_v001.png
```

`tests/nuke_stub/nuke.py` is a hand-written fake that models only what our code calls. A test passing there says nothing about whether real Nuke accepts the call, so anything touching knobs or the node graph needs a run in real Nuke. Run those headless in real Nuke first (see the box at the top of Part 2). When the stub turns out to disagree with real Nuke, fix the stub too. `tests/fake_comfy.py` is the same idea for the HTTP API.

Useful Comfy endpoints: `/prompt`, `/history/<id>`, `/view`, `/upload/image`, `/interrupt`, `/system_stats`, `/object_info[/<class>]`.

## 10. Adding a new workflow type (current process)

1. Build and test it in ComfyUI.
2. Title the patch points per §4 and add `nuke_save`. Export with **Save (API)** into `templates/`.
3. Add a `build_<type>()` (and `generate_<type>()` if the flow differs) to `comfy_client.py`. Test from the CLI against live Comfy.
4. Add a Nuke node module modelled on `comfy_nuke_edit.py`: build the knobs from the shared groups in `comfy_nuke`, do main-thread prep, then `base.start_job(node, params, fn, summary)`. Register it in `menu.py`.
5. Add offline tests to `tests/test_comfy.py`, and update this README: the title table (§4), status (§8), and any new lessons (§6).
