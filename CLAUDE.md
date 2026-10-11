# CompAI

Runs AI image generation from Nuke nodes. Local models go through ComfyUI (built); closed models will go through Griptape (not started). Read README.md Part 2 before changing code.

## Layout

- `nuke_plugin/` is on Harry's Nuke plugin path. `compai/` is the Python package, `workflows/<name>/` holds one installed workflow each (`workflow.json` export + `node.json` definition).
- `inbox/` is where Harry drops new ComfyUI exports.
- `workflow.py` is the CLI: `check`, `scaffold`, `list`, `run`.
- `tests/` are offline tests with a fake `nuke` module and a fake Comfy server.

## Testing

- Offline: `python3 -m unittest discover -s tests`
- Real Nuke, headless (about 10 s): `/opt/Nuke15.2v9/Nuke15.2 -ti script.py`. Use this to check any Nuke API assumption before handing work to Harry. It doesn't run `menu.py`. Don't override `HOME` (licence). Keep test scripts in the scratch directory.
- ComfyUI is usually running at `127.0.0.1:8188`. Read-only calls (`check`, `/object_info`) are fine. Ask before queuing a generation on Harry's GPU.

## Ingesting a workflow from `inbox/`

Do these in order and stop at the first failure.

1. `python3 workflow.py check inbox/<file>.json`. If it says REJECTED, or the export is missing something the node needs, sort the problem into one of two kinds:
   - **Fix it yourself in the JSON** (Harry agreed on 2026-10-11): wrong or missing `nuke_` titles; a missing node or connection that is mechanical and that `/object_info` confirms exists, such as a Load Image for an optional input the model node declares, or a primitive feeding a value that was hard-coded in two places.
   - **Reject and tell Harry what to change in ComfyUI**: a node type or model file the server doesn't have, anything that changes what the workflow *does* (sampler, model, wiring between processing nodes), and anything you can't verify without running it. Also reject what the linter can't see but makes the workflow unsound: the prompt typed into two nodes instead of one `nuke_prompt` feeding both, a `nuke_` node with nothing drivable on it, a seed that isn't on a `nuke_` node.

   Once installed, **`nuke_plugin/workflows/<name>/workflow.json` is the source of truth**, not Harry's original export: he carries on from it. List every edit you made in a `"notes"` list in `node.json` and in your report to him. An edit you added has not been run until a real generation succeeds; say so.
2. `python3 workflow.py scaffold inbox/<file>.json <name>`. Name it `<model>_<task>`, lowercase, e.g. `z_image_turbo_t2i`. This moves the export to `nuke_plugin/workflows/<name>/workflow.json` and drafts `node.json`.
3. Edit `node.json`: set `label`, `node_name` and `tile_color`; prune `knobs` down to what an artist would actually change (the draft lists every literal input); add `label`/`tooltip`; give server-driven model choices a `match` regex so only compatible files show; set `size` if the model isn't a 16-pixel-grid, 1 MP model, and `timeout` for anything slow. Edit `workflow.json` only as step 1 allows. README §4 is the reference.
4. `python3 workflow.py check <name>` must PASS, and `python3 -m unittest discover -s tests` must pass (the suite checks every installed workflow, and the menu test lists labels: add the new one).
5. Create the node in headless Nuke (`import compai; compai.create("<name>")`) and confirm the knobs and their defaults.
6. Add the workflow to the table in README §3, then tell Harry what the node exposes and what he should test in the GUI.

A new workflow should need no Python. If it does, the standard node is missing a capability: raise that with Harry instead of special-casing a model in `node.py`.
