# CompAI

Run AI image generation from Nuke nodes. You set the prompt, seed, format and inputs on a Nuke node and press **Generate**. The image is made in the background and comes back into the comp as a normal plate.

- **Local models** run through ComfyUI. This is what exists today.
- **Closed models** (Nano Banana and the like) will run through Foundry Griptape. Not started.

> **Status: early.** Five workflows are installed: Qwen Image, Qwen Image Edit, Z-Image Turbo, MiniMax H3 first/last frame to video, and MiniMax H3 reference to video (never yet generated).

---

# Part 1 — User guide

## What you need

- **ComfyUI** running on the same machine. The default address is `127.0.0.1:8188`; set the `COMFY_HOST` env var or the node's *Settings* tab if yours is different.
- The models each workflow uses, installed in ComfyUI.
- Nuke. No extra Python packages are needed on the Nuke side.

## Install

1. **Nuke side:** add the `nuke_plugin/` folder to Nuke's plugin path, either on `NUKE_PATH` or with `nuke.pluginAddPath("/path/to/CompAI/nuke_plugin")` in `~/.nuke/init.py`. The nodes appear under **Comfy** in the node menu, one per installed workflow.
2. **Comfy side (recommended):** copy or symlink `comfyui_custom_nodes/comfy_nuke_nodes/` into `ComfyUI/custom_nodes/` and restart ComfyUI. This lets Comfy read Nuke's images straight from disk instead of uploading copies.

## Using a node

Every node works the same way; only the block of model settings under the prompt differs.

1. Create a node from the **Comfy** menu and type a prompt.
2. Set the model settings (see the table below).
3. Set the **format**. It gives the aspect ratio; the image is generated at about **megapixels** (default 1 MP) at that aspect, and comes out at that raw size. The *generate at* line shows it. Tick **reformat result to format** if you want the result resized to the format itself (e.g. back up to HD).
4. Set **output dir** and **name**, then press **Generate**. Nuke stays usable while it runs. **Cancel** stops the job.
5. The result loads into the node and is held on every frame.
6. **Create Read** drops a normal Read of the current result next to the node, so that version stays in the script when you generate again. It reads the raw generated file.

| Node | Takes images | Model settings |
|---|---|---|
| **Qwen Image** | no | *enhance prompt* (an LLM expands the prompt first; slower), *steps* |
| **Qwen Image Edit** | 1–10 | *enhance prompt*, *steps* |
| **Z-Image Turbo** | no | *model* (dropdown of the Z-Image models in ComfyUI), *steps* |
| **Minimax_H3_fl2va** | *first* and/or *last* | *duration (s)*, *fps*, *steps*, *lightning LoRA*, *lightning steps*, *codec*. Makes a video |

**Model dropdowns** are filled from ComfyUI when the node is created. Press **refresh** next to one after installing a model, or if ComfyUI wasn't running when you made the node.

**Nodes that take images:** the node starts with one input and grows a new free one each time you connect the last, like Merge, up to what the workflow accepts. Unconnected inputs are skipped and the rest are numbered in order, so you can write *"put the jacket from image 2 on the person in image 1"*. By default the format follows the first input. Each input is grabbed at the **current frame**, scaled to fill the generation size and cropped; it is never stretched. An input that is already the right size is sent untouched, so a Qwen Image result fed into Qwen Image Edit at the same megapixels comes back at exactly the same resolution with no resizing anywhere.

| **Minimax_H3_ref2va** | 0–9 reference images, 0–3 reference videos | *duration (s)*, *fps*, *reference size*, *steps*, *lightning LoRA*, *lightning steps*. Makes a video |

**Video nodes** work the same way, with these differences:

- On **Minimax_H3_fl2va** the inputs are named (*first*, *last*) and fixed. Connect either or both; at least one.
- On **Minimax_H3_ref2va** the inputs are references, all optional, and they don't set the node's format. There are two kinds, each growing on its own as you connect them, so they end up interleaved on the node; go by the label on the pipe:
  - **img1, img2 …** (up to nine): reference images, grabbed at the current frame and sent at their own resolution.
  - **vid1, vid2 …** (up to three): reference videos. Each is rendered out of Nuke as a ProRes movie, scaled to the megapixel budget, and uploaded. **video frames** chooses the range: each input's own range (default), the project range, or two numbers you type. Generate asks before sending more than 400 frames. Rendering the movie blocks Nuke until it's written.
- The result is a movie file (`.mov` ProRes 422 by default), loaded into the node so that it **starts at the script's first frame** as it was when the result came back. Before and after the clip it holds.
- **fps** follows the project's frame rate through an expression. The frame count is duration × fps, rounded up to the model's grid (5, 22, 39, 56 … frames), so 2 s gives 56 frames.
- The movie has an audio track, which Nuke's Read ignores. Open the file elsewhere to hear it.
- Megapixels defaults much lower (0.4), because every frame costs memory.
- It is slow. The node gives up after **timeout (min)** on the *Settings* tab: an hour by default here, 15 minutes on the stills nodes. Raise it before starting a long run; it can't be changed for a job already running.

## Where things go

Each run writes versioned files to the output dir:

| File | What |
|---|---|
| `name_v003.png` | the result (`.mp4` / `.mov` from a video node) |
| `name_v003.json` | provenance: every setting used, timing, and the exact workflow that ran |
| `name_v003_in1.png` … | (nodes with inputs) the sRGB inputs that were sent to Comfy; `_first.png` / `_last.png` on nodes with named inputs, `_vid1.mov` … for video inputs. Tick **temporary input images** on the *Settings* tab to write these to the system temp folder instead: they stay for the session, so a run can still be checked in ComfyUI, and the system clears them later. Add **instant cleanup** to delete them the moment the run ends |

To recreate a result, use the seed on the *Results* tab and press **Reuse last seed**, or untick **new seed each run**.

If the output dir doesn't resolve to an absolute path (e.g. an unset env var), files go to `~/comfy_nuke`.

## Adding a workflow

Export it from ComfyUI and drop it in `inbox/`. See [inbox/README.md](inbox/README.md).

## Troubleshooting

- **"ComfyUI not reachable"**: Comfy isn't running, or the host is wrong (*Settings* tab).
- **"Workflow '…' is not usable"**: the installed workflow fails its checks. Run `python3 workflow.py check <name>` for the reasons.
- **A model dropdown only shows one entry**: ComfyUI wasn't reachable when the node was made (press **refresh**), or only one matching model is installed.
- **Generate is greyed out with nothing running**: close and reopen the node's panel.
- **Colours look off**: set **PNG colorspace** on the *Settings* tab to your OCIO config's sRGB name.
- **Errors from Comfy** show in a popup. The ComfyUI console has the full traceback.

---

# Part 2 — Developer guide (for Claude / future maintainers)

This section is meant to bring a fresh session fully up to speed. Read it all before changing code. `CLAUDE.md` has the short version and the step-by-step for ingesting a workflow.

> ### ⚡ Real Nuke can be run headless from the shell. Use it.
>
> ```bash
> /opt/Nuke15.2v9/Nuke15.2 -ti my_test.py 2>&1 | grep -v "^$"
> ```
>
> - Runs `my_test.py` in real Nuke 15.2 with no GUI, in about 10 seconds. `-i` uses Harry's interactive licence, so it works while his GUI session is open. Nuke 17.1 is at `/opt/Nuke17.1v1`.
> - Harry's `~/.nuke/init.py` runs, so `import compai` just works.
> - **Good for:** creating nodes, checking knobs exist and take the values we set, expressions, formats, colourspace names, executing Writes, loading a result into the Read, saving and reopening a script. Check every Nuke API assumption here before handing anything to Harry.
> - **Not covered:** `menu.py` (not run in terminal mode), anything visual, panel behaviour, and the background-thread job flow. Harry tests those in the GUI.
> - Don't override `HOME`: the licence lives there and Nuke won't start.
> - Don't queue real generations on Harry's GPU from a test without asking.
> - Put test scripts and their output in a scratch directory, not the repo.

## 1. Context and goals

- **Owner:** Harry, a lead compositor who builds Nuke pipeline tools professionally and knows Nuke Python/TCL well. He is newer to AI generation and is learning ComfyUI in parallel.
- **Goal:** Nuke-level pipeline integration (knobs, versioning, plates in, plates out) with Comfy-level AI capability. Longer term: many more workflows (video included).
- **Comfy is local generation, Griptape is closed generation.** Closed-weight API models are deliberately kept out of the Comfy side (a Nano Banana export didn't behave) and reserved for a later Griptape integration.
- **Machine:** Kubuntu 26.04, RTX 4070 12 GB for ComfyUI, and a separate GTX 1650 that drives the displays and Nuke. ComfyUI runs locally on the same box, so a shared filesystem is assumed but not required.
- **How Harry likes to work:** explain the *why* before implementing, fix one issue at a time, and test from the CLI before Nuke. He iterates with short, precise feedback and often patches the code himself; respect his edits (see §7).

## 2. Architecture (and why)

```
Nuke node (knobs) ──HTTP──▶ ComfyUI server ──▶ GPU
      ▲                         │
      └──── PNG on disk ◀───────┘  (downloaded via /view, read by an internal Read)
```

- **Nuke is a thin client and never imports torch.** Running models in Nuke's Python would fight its bundled libraries, freeze the UI, risk a CUDA crash taking the comp down, and reload the model for every job. ComfyUI keeps models warm and handles VRAM offloading.
- **Workflow JSON is the source of truth.** Workflows are exported from Comfy with **Save (API)** and patched in Python before submission. The export is never edited by hand.
- **One generic node, many workflows.** There is no per-model Python. Every node gets the same standard parts (Nuke I/O, sizing, versioning, job handling). What differs per workflow is a block of knobs declared in a small `node.json` next to the export, which also says which workflow input each knob drives. Adding a model means adding a folder.

## 3. Layout

```
CLAUDE.md                    short brief + the ingest procedure, auto-loaded by Claude Code
workflow.py                  CLI: check / scaffold / list / run
inbox/                       Harry drops new ComfyUI exports here
nuke_plugin/                 everything Nuke loads; this folder goes on the plugin path
  menu.py                    builds the Comfy menu from the installed workflows
  compai/                    the Python package
  workflows/<name>/          one folder per installed workflow
    workflow.json            the Save (API) export, unedited
    node.json                the definition: knobs and bindings
comfyui_custom_nodes/        installed into ComfyUI, not Nuke
reference_comfy_workflows/   Harry's older Comfy exports, for reference only (not loaded)
tests/                       offline tests, see §9
```

| Module in `compai/` | Imports nuke? | Role |
|---|---|---|
| `__init__.py` | lazily | The stable entry points knob callbacks call: `create`, `on_generate`, `on_cancel`, `on_knob_changed`, `on_reuse_seed`, `on_refresh_choices`, `on_create_read`, plus `add_menu` and `reload` |
| `comfy_client.py` | no | HTTP client, workflow-JSON primitives (`find_node`, `set_input`, `bind_images`), `generation_size`, `run_workflow` |
| `workflows.py` | no | Installed workflows: `names`, `load`, `lint_graph`, `validate`, `check_server`, `build`, `generate`, `scaffold` |
| `cli.py` | no | The commands behind `workflow.py` |
| `node.py` | yes | Builds the Nuke node: standard knobs, the workflow's knob block, image inputs, `on_generate` |
| `jobs.py` | yes | Background job runner: thread, status, cancel, loading the result |

| Installed workflow | Node | Images | Knobs | Models |
|---|---|---|---|---|
| `qwen_t2i` | Qwen Image | – | enhance, steps | Qwen Image 2.1 + Qwen3-VL encoder + Qwen 3.5 prompt enhancer |
| `qwen_edit` | Qwen Image Edit | 1–10 | enhance, steps | same, i2i enhancer |
| `minimax_h3_ref2va` | Minimax_H3_ref2va | 0–9 reference images (`min` 0, sent unresized, don't drive the format) and 0–3 reference videos | duration, fps (→ `root.fps`), ref_size, steps, lightning, lightning_steps | MiniMax H3 ref2va + the same VAEs and encoder + 4-step turbo LoRA. Output is ComfyUI's stock Save Video (`.mp4`, H.264). `workflow.json` has three groups of edits vs Harry's export, listed in its `node.json` notes |
| `z_image_turbo_t2i` | Z-Image Turbo | – | model (server list, `z_image`), steps | Z-Image Turbo, `qwen_3_4b` encoder, `ae` VAE |
| `minimax_h3_fl2va` | Minimax_H3_fl2va | first, last (named, each optional) | duration, fps (→ `root.fps`), steps, lightning, lightning_steps, codec | MiniMax H3 fl2va + video and audio VAEs + Qwen3-VL 32B encoder + turbo LoRA. Output is ProRes/H.264 with audio. `workflow.json` has four edits vs Harry's export, listed in its `node.json` notes |

**Layering rule:** anything that doesn't need Nuke stays in the nuke-free modules so it can be tested from a shell. `node.py` and `jobs.py` handle knobs, node graph and threading only.

**Entry points are a compatibility surface.** Knob callbacks are saved into scripts as strings (`import compai; compai.on_generate(nuke.thisNode())`). Renaming those functions or the package breaks every node in every saved script.

## 4. Workflow definitions (`node.json`)

```json
{
  "label": "Z-Image Turbo",
  "node_name": "ZImageTurbo",
  "tile_color": "0x2E8B6FFF",
  "output": "nuke_save_image",
  "bind": {
    "prompt": [["nuke_prompt", "value"]],
    "seed":   [["nuke_ksampler", "seed"]],
    "width":  [["nuke_latent", "width"]],
    "height": [["nuke_latent", "height"]]
  },
  "knobs": [
    {"name": "model", "type": "choice", "options": "server", "match": "z_image",
     "tooltip": "…", "bind": [["nuke_load_diffusion_model", "unet_name"]]},
    {"name": "steps", "type": "int", "bind": [["nuke_ksampler", "steps"]]}
  ]
}
```

| Key | Meaning |
|---|---|
| `label` | Menu entry and how the node is described to the user. No `/`: Nuke turns it into a submenu |
| `node_name` | Nuke node name stem (`ZImageTurbo1`) |
| `tile_color` | Hex RGBA string |
| `output` | Title of the save node. Its `filename_prefix` is set and its images are read back |
| `bind` | Where the **standard** values go: `prompt`, `seed`, `width`, `height`. Each is a list of `[title, input]`, so one value can feed several nodes (Qwen sends the seed to the sampler and the prompt enhancer). All optional; an unbound value is simply not sent |
| `set` | Constants applied on every run: `[[title, input, value], …]`. Qwen Edit uses it to switch the size source to the Empty Latent, since Nuke always supplies a size |
| `out_name` | Optional default for the node's *name* knob |
| `timeout` | Seconds to wait for a run before giving up. Default 900; the video workflow uses 3600. It becomes the default of the node's *timeout (min)* knob, which is what a run actually uses |
| `size` | Optional. `multiple` (width and height snap to it, default 16), `megapixels` (default budget, default 1.0), `megapixels_range` (slider, default `[0.25, 2.5]`). The video model needs a 32 grid and a 0.4 MP default |
| `notes` | Free text, ignored by the code. Records any edits made to `workflow.json` after export |
| `images` | **Either** `{"inputs": [{"name": "first", "title": "nuke_first_frame"}, …]}` for fixed, named inputs, each with its own Load Image node and each optional (an unused one is pruned with its links); **or** `{"max": N}`, which lets the node take up to N images, growing as they're connected (at most the number of `nuke_input_N` slots in the export; `scaffold` sets it to that). Connected inputs fill `nuke_input_1…` in order and unused slots are pruned. Optional `"prefix"` |
| `images` extras | `"min"`: images a run needs (default 1; 0 makes them all optional). `"match_format"`: whether *match first input's format* starts ticked (default true; false when inputs are references, not the frame). `"resize"`: false sends inputs at their own size instead of the megapixel budget (for references the model node sizes itself) |
| `videos` | `{"max": N}` gives the node up to N video inputs, fed into stock **Load Video** nodes titled `nuke_video_1…N` in order, unused ones pruned. Optional `"min"` (default 0) and `"prefix"` |
| `knobs` | The workflow's own block, in display order |

Knob fields:

| Field | Meaning |
|---|---|
| `name` | Nuke knob name. Must not collide with a standard knob (`workflows.RESERVED`) |
| `type` | `int`, `float`, `bool`, `string` or `choice` |
| `bind` | `[[title, input], …]`, required |
| `default` | Optional. **Leave it out** and the default is whatever the export has, which keeps the node in step with the workflow as Harry tuned it |
| `label`, `tooltip` | Optional |
| `range` | `[min, max]` for a `float` slider |
| `expression` | For a `float`: a Nuke expression the knob is created with, e.g. `"root.fps"`. The evaluated value is what gets sent |
| `options` | For `choice`: a list, or `"server"` to ask ComfyUI for the bound input's combo list (model files, samplers…) |
| `match` | For `"options": "server"`: a regex; only matching entries are offered. Use it on model lists, which hold every model of that kind |

**Rules for exports** (what `lint_graph` enforces; the user-facing version is `inbox/README.md`):

1. **Nodes are found by `_meta.title`, never by ID.** IDs change on re-export and grouped nodes get flattened to IDs like `459_458`. Titles Nuke uses start with `nuke_` and must be unique.
2. **Exactly one save node, titled `nuke_save…`**, wired to the final image. Comfy only executes nodes that feed an output node.
3. **Image inputs are stock `LoadImage` nodes with a `nuke_` title**: either `nuke_input_1 … nuke_input_N`, contiguous, or names such as `nuke_first_frame`. Pruning an unused one is only legal if the input it feeds is optional on the consuming node (auto-grow inputs, or `first_frame` / `last_frame`). `validate` requires every `LoadImage` to be declared under `images`, since an undeclared one would have its file baked in.
   Video inputs are stock `LoadVideo` nodes titled `nuke_video_1 … N`, under the same rules. Pruning one also removes any node it leaves with no inputs at all (`remove_node` cascades), which is what takes the **Get Video Components** after it away; `nuke_` nodes are never removed that way.
4. **An unconnected optional input is absent from an API export.** If Nuke should be able to feed it, it must be wired to a Load Image in ComfyUI before exporting.
5. In API format **every widget is an input**: any key under `inputs` can be overwritten with a literal, which also replaces a link. The video workflow relies on this: `width`/`height` on the model node arrive linked from a Resolution Selector, and binding them replaces the links, leaving that node orphaned and unexecuted.

Not enforced by the linter, so check by eye when ingesting: **one source for the prompt** (a String primitive titled `nuke_prompt` wired to every consumer, never the literal prompt typed into two nodes). A `PreviewAny` on the prompt path lets the client read back the prompt that was actually used (shown as *used prompt*).

`check_server` adds what only the running ComfyUI knows: missing node classes, unknown *linked* inputs (an unknown literal is a display-only widget the editor exported, such as Load Video's `video-preview`, and is ignored), combo values the server doesn't have (which is how a missing model file shows up), and whether the save node is really an output node.

Known quirk: in `qwen_edit`, **Batch Images (feeding the prompt enhancer) is only wired to inputs 1–2**. The enhancer sees two refs while the image model sees all of them. Fix in Comfy and re-export if it matters.

## 5. Adding a workflow

`CLAUDE.md` has the procedure. In short: `check` the export (reject it if flawed, never patch around it), `scaffold` it into `nuke_plugin/workflows/<name>/`, edit the drafted `node.json` down to what an artist needs, `check` again, run the tests, create the node in headless Nuke, update the table in §3.

A new workflow should need **no Python**. If one does, the standard node lacks a capability (video output, masks, a second prompt…); design that as a general feature with Harry rather than special-casing a model.

## 6. Behaviour reference

**Sizing (`comfy_client.generation_size`)**: keep the format's aspect, use a pixel budget of `megapixels × 1024²`, and snap each side to a multiple of 16. Anamorphic formats are squared up with `width × pixelAspect`. A size that is already on the 16 grid and within one snapping step of the budget is returned unchanged, which makes the function a fixed point on its own output: that is what lets a generated image go back in without resampling.

**Nothing is stretched, and raw is the default.**
- *Output:* the internal `ToTarget` Reformat (`fill`, Lanczos4, `pbb`) is disabled by the expression `!parent.reformat_output`, so by default the node outputs the PNG at the size it was generated. With **reformat result to format** on, it scales to fill the format and crops.
- *Inputs:* `prepN` is a to-box Reformat with `fill` (scale to fill, crop the overflow). When the input is already exactly the generation size with square pixels, `_write_input` disables `prepN` so the pixels aren't resampled. They still go through Nuke's sRGB decode and re-encode on the way to the 8-bit PNG, which can move a value by one code (measured in Nuke 15.2); geometry is untouched.
- The `target_format` knob (labelled *format*) is therefore mainly the **aspect** source. Its pixel size only matters when reformat is on, or when it is already a valid generation size.

**Job flow (`workflows.generate` → `comfy_client.run_workflow`)**: load and validate the workflow → `build` a patched copy → POST `/prompt` → poll `/history/<id>` (an error status raises `ComfyError`) → find file refs under the save node's outputs → GET `/view` → write to `out_path`, **swapping its extension for the one Comfy produced** (callers pass `.png` as a placeholder and must use the returned `path`) → write a `<stem>.json` sidecar with the values used *and* the patched workflow.

**The workflow is looked up by name at Generate time**, from the node's read-only `comfy_workflow` knob. So re-exporting a workflow updates every existing node. A node made before a knob was added to `node.json` simply runs with that knob's default.

**Choice knobs from the server**: filled at node creation with a 3-second `/object_info/<class>` call on the main thread. If Comfy is down the knob holds just its default. **refresh** re-reads the list and keeps the current value even if the server no longer lists it, so a saved script never silently changes model.

**Image transfer**: `generate(..., input_mode=)`
- `path`: each `nuke_input_N` node is swapped at submit time for `NukeLoadImagePath{path}`, which has the same outputs (IMAGE, MASK). Nothing is copied.
- `upload`: POST multipart `/upload/image` into `input/nuke/`, with stable per-node names (`<NodeName>_in<N>.png`) so repeat runs overwrite.
- `auto` (default): path if `GET /object_info/NukeLoadImagePath` shows the node, else upload.
- Security note: the path loader lets anyone who can queue prompts read files the Comfy process can read. Fine for localhost; don't expose the server.

**Nuke threading rules (important):**
- Read all knobs and execute all Writes on the **main thread** in `on_generate`. The worker thread only calls nuke-free code.
- The worker reports back via `nuke.executeInMainThread` (`_status`, `_on_done`, `_on_error`).
- `jobs._JOBS[node.fullName()] = threading.Event` is both the busy flag and the cancel flag (cancel → `/interrupt`).
- If the node is deleted mid-job, `nuke.toNode(key)` returns None and the result is dropped quietly.
- `jobs.start` disables the Generate button, and Nuke saves that state with the script. `release_stale_lock` re-enables it on `showPanel` when no job is running.

**Two kinds of pipe** (`node.IMG`, `node.VID`). A node can have image inputs, video inputs, or both, and each kind grows on its own, so their positions interleave (img1, vid1, img2 …). **Never assume an input's index from its slot number**: `_pipes(node, kind)` maps each slot `n` to its real index by reading the internal Input nodes' names and `number` knobs, and everything else goes through it.

**Video inputs**: each is `vidN → vprepN (to box, fill) → vwriteN (mov, ProRes 4:2:2 10-bit, disabled except while executing)`. `_write_video` renders the chosen range on the main thread (`video_range`: the input's own `firstFrame()/lastFrame()`, the root's, or `video_first/last`), at the project fps, always scaled to the megapixel budget because every frame costs memory in Comfy. Videos are always uploaded (`/upload/image` takes any file); there is no path mode for them. More than `MAX_VIDEO_FRAMES` (400) asks first.

**Image inputs**: each is `imgN → prepN (Reformat to box, fill) → writeN (png, 8 bit, rgb, disabled except while executing)`.
- **Growing inputs:** `_sync_inputs` runs on the `inputChange` knobChanged event, once per kind, and keeps exactly one free input of that kind after the highest connected one, up to the workflow's limit, by adding or deleting chains inside the group. Deleting an Input that isn't the last one leaves a hole in the group's input numbers (a dead pipe), so `_compact_inputs` renumbers the rest 0..N-1 and reconnects each to what it had. Disconnecting a middle input changes nothing; disconnecting the top one shrinks back to just above the next connected input. `inputChange` fires in terminal mode too, and it does not fire destructively on script load (checked: a saved node reopens with its inputs and connections intact).
- **Temporary inputs:** with `temp_inputs` on, the PNGs go to a fresh `tempfile.mkdtemp(prefix="compai_")` folder. On Harry's machine that is under `/tmp`, a RAM-backed tmpfs emptied on reboot and swept of files older than 10 days, so nothing is permanent. With `instant_cleanup` also on, `jobs._worker` deletes the folder when the run ends (done, failed or cancelled), before the result is reported. The sidecar records the paths either way. A node that has `temp_inputs` but no `instant_cleanup` knob (made on 2026-10-11 before the second knob existed) cleans up instantly, as it did then.
- **Packing:** connected inputs are packed into slots 1..k.
- **Ref sizing:** each ref is sized to the megapixel budget at its *own* aspect.
- **Target format:** "match first input's format" copies the first connected input's format onto `target_format`.
- **Recursion guard:** `_IN_REFRESH` stops `knobChanged` from recursing when code sets knobs.
- **Colourspace:** `_set_colorspace` tries `SRGB_CANDIDATES` against the Read/Write `colorspace` knob values, or uses the *PNG colorspace* override.

**Versioning**: `_next_output_path` scans for `<name>_vNNN.<any extension>` and takes max+1, so stills, movies and sidecars all count. Input PNGs (`_v003_in1.png`) don't match the pattern.

**Video results**: `jobs._on_done` loads the movie with `fromUserText`, which sets the Read's frame range from the file (frames 1..N). If the file has more than one frame it sets the Read to `frame_mode` "start at" with the script's first frame as a literal number (so it is fixed at load time, not live; a still resets it). Create Read copies both knobs. It then restores the colourspace, because a new file type resets it to the type's default (`Gamma2.2` for mov/mp4; we want sRGB, which is what `CreateVideo` was told the images are). Nuke 15.2 reads H.264 4:2:2 10-bit all-intra `.mp4` and ProRes `.mov`; it does **not** read H.265, so those presets aren't offered.

**Output dir**: `_output_dir` falls back to `~/comfy_nuke` only when the evaluated path is empty or not absolute, or when it's the `root.name` default in an unsaved script.

## 7. Nuke API notes learned the hard way

- `node.setName(name)` has **no** `uncollisionable` argument. Setting a taken name gets uniquified automatically.
- Format knobs **can** be expression-linked: `reformat["format"].setExpression("parent.target_format")`. (`hasExpression()` reports False for it, but it works.)
- `Format_Knob.setValue()` only accepts the **name** of a registered format. Passing a `Format` object does nothing except log "not in named format list", and raises no exception. For an unnamed format, register it with `nuke.addFormat("w h pa name")` first (adding the same one twice is harmless). See `node._set_format`.
- Deleting an Input node from a group does **not** renumber the others: `maxInputs()` still counts the hole. A new Input takes the lowest free number. External connections belong to the number, not the Input node, so renumbering means reconnecting.
- A Write set to `file_type` mov defaults to ProRes 4:2:2 HQ at 24 fps regardless of the project; set `mov_prores_codec_profile` and `mov64_fps` explicitly.
- **Inside `with node:` (a group context) `nuke.root()` is the group, not the script.** Read script settings such as `first_frame` before entering the block. The test stub now raises if you get this wrong.
- A Read's `frame` knob (for "start at") must hold a literal number: a TCL expression like `[value root.first_frame]` is stored but not evaluated. An expression on `TimeOffset.time_offset` also evaluated to 0 in terminal mode, so that route was dropped.
- A Read's `colorspace` knob falls back to the file type's default when the file changes type (PNG → MP4). Save it and set it again after `fromUserText`.
- Nuke 15.2 on Linux reads H.264 (including 10-bit 4:2:2) and ProRes; H.265 fails with "unsupported codec".
- `Enumeration_Knob.setValues(list)` replaces the options; the list and the chosen value both survive a script save and reopen.
- `setEnabled(False)` on a user knob is saved with the script.
- Don't rely on a chained `init.py`: with `nuke.pluginAddPath("/…/CompAI/")` in `~/.nuke/init.py`, Nuke 15.2 added the path but never ran `CompAI/init.py` (it did run when the same folder was on `NUKE_PATH`; cause not found). Pointing the plugin path straight at `nuke_plugin/` needs no `init.py`.
- Harry's preferred default output dir is `[getenv SHOT_ROOT]/gen`.
- After editing code with Nuke open: `import compai; compai.reload()`. Existing nodes pick up the new code; only knob layout changes need the node recreated.
- **Treat Harry's versions of files as canonical.** He tests in real Nuke and patches things. Diff against his copy before rewriting.

## 8. Status: what's actually been tested

| Piece | Status |
|---|---|
| All three nodes, end to end in the Nuke GUI | ✅ tested by Harry on 2026-10-10 on the definition-driven code, **before** the raw-output / crop-inputs change |
| Raw output, reformat toggle, fill-and-crop inputs, exact pass-through | ✅ headless Nuke 15.2, and Harry in the GUI on 2026-10-10 |
| MiniMax H3 video node | ✅ definition passes every check against live Comfy. ✅ headless Nuke 15.2 with synthetic clips: named inputs, last-only connect, 864x480 on the 32 grid, fps following the project, clip starting on the script's first frame (verified frame by frame at 1001), sRGB, versioning, Create Read, timeout knob. ⚠️ **never generated**: the added last-frame and fps nodes, the rewritten length expression, the shape of Comfy's history entry for `SaveVideoHQ`, run time and VRAM on the 4070 are all unverified |
| MiniMax H3 reference-to-video node, images | ✅ Harry generated with two references and with none on 2026-10-11 |
| Video inputs (vid1–3) | ✅ definition passes against live Comfy. ✅ headless Nuke 15.2: both kinds of pipe growing and shrinking, renumbering with connections kept, save and reopen, a 24-frame ProRes written from a clip at the budget size and project fps, the three range modes. ⚠️ **never generated**: uploading a `.mov` through `/upload/image`, whether Load Video accepts a file in the `nuke/` subfolder, and Comfy reading Nuke's ProRes are unverified. ⚠️ Adding and removing pipes while dragging in the GUI is untested for the two-kind case |
| Growing inputs (1 → 10) on Qwen Image Edit | ✅ headless Nuke 15.2: grow, middle gap, shrink, stop at 10, save and reopen, write from an added input. ⚠️ not yet tried in the GUI (adding/deleting Input nodes from a callback while dragging a pipe), and no generation with more than 4 images has been run |
| All three workflows vs live Comfy `/object_info` | ✅ node classes, input names and model files all present (ComfyUI 0.38.1) |
| All three nodes in real Nuke 15.2, headless, no generation | ✅ creation, knobs and defaults, model dropdown filled from live Comfy, refresh, format link, "match input", writing an input PNG, loading a result, save and reopen |
| Patching, checks, client flow, nodes end to end | ✅ offline only, against a fake Comfy server and a fake `nuke` module. Proves our own logic, not the real APIs |
| `upload_image` against live Comfy | ✅ implied by Harry's Qwen Edit run (the path loader isn't installed, so it uploaded) |
| `NukeLoadImagePath` custom node | ⚠️ untested and not installed on Harry's Comfy |
| Write colourspace names under Harry's OCIO config | ⚠️ `sRGB` was picked under the default config; other configs untested |

## 9. Offline tests

```bash
python3 -m unittest discover -s tests       # everything offline
python3 workflow.py list                    # installed workflows
python3 workflow.py check z_image_turbo_t2i # definition + live server checks

# a live generation from the shell (Comfy up; ask Harry first)
python3 workflow.py run z_image_turbo_t2i --prompt "a ring in a box" --width 1408 --height 736 --out ./t_v001.png
python3 workflow.py run qwen_edit --prompt "make it night" --image ./plate.png --set enhance=true --out ./e_v001.png
```

The suite checks **every installed workflow** automatically (loads, validates, builds with no dangling links, gets its standard knobs), so a new workflow is covered as soon as its folder exists.

`tests/nuke_stub/nuke.py` is a hand-written fake that models only what our code calls. A test passing there says nothing about whether real Nuke accepts the call, so anything touching knobs or the node graph needs a run in real Nuke: do it headless first (see the box at the top of Part 2). When the stub turns out to disagree with real Nuke, fix the stub too. `tests/fake_comfy.py` is the same idea for the HTTP API.

Useful Comfy endpoints: `/prompt`, `/history/<id>`, `/view`, `/upload/image`, `/interrupt`, `/system_stats`, `/object_info[/<class>]`.

## 10. Open questions / ideas (not decisions)

- **Griptape for closed models.** Decided in principle, nothing designed. The standard node parts (`node.py` knobs, `jobs.py`) are backend-neutral; `workflows.generate` is the Comfy-specific call a second backend would sit beside.
- **Standard knobs are always present.** A workflow with no prompt or no seed (an upscaler) would still show those knobs. Make them conditional on `bind` when the first such workflow arrives.
- **Input fitting is fixed at fill-and-crop** (centred). Harry floated exposing it as an option (fit with padding, distort, crop offset); not needed yet.
- **Megapixel range is global** (0.25–2.5, default 1). Models differ in what they handle; it may need to move into `node.json`.
- **Int knobs have no slider or limits.** `range` only applies to floats.
- **Menu organisation** once there are many workflows: submenus by task or by model.
- **Colour:** the pipeline is 8-bit sRGB PNG both ways. `SaveImageAdvanced` has `format.bit_depth` and colour-space options, so 16-bit output may be possible; and how display-referred AI output should sit in a scene-linear comp is open.
- **Video, still open:**
  - *Start frame is fixed when the result loads.* Changing the script's first frame later doesn't move a clip already loaded; generate again or edit the internal Read.
  - *Frame rate other than 24.* The model was trained at 24 fps. At another rate the picture is retimed by that ratio and the audio track (generated for 24) no longer matches its length.
  - *Audio* is in the file but Nuke has no audio stream to pass it down. `AudioRead` only takes WAV/AIFF, so using it would mean extracting the track with ffmpeg first. Not done.
  - *Inputs are single frames* grabbed at the current frame. A video-to-video workflow would need frame ranges written out and uploaded, which nothing supports yet.
  - *Image sequences* (EXR/PNG) would suit Nuke better than a movie, but mean many files per result; `run_workflow` downloads only the first file.
  - *Reference audio.* The ref2va model node also takes reference audio clips and each reference video's soundtrack. Nuke has no audio stream, so these aren't exposed; video inputs are sent silent.
  - *Video input length.* The model wants reference videos of about 2 to 15 s at 24 fps. Nothing enforces that beyond the 400-frame prompt.
