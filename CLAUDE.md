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

1. `python3 workflow.py check inbox/<file>.json`. If it says REJECTED, do not work around it and do not edit the export: tell Harry exactly what to change in ComfyUI and re-export. Also reject, with the reason, anything the linter can't see but that makes the workflow unsound: the prompt typed into two nodes instead of one `nuke_prompt` feeding both, a `nuke_` node with nothing drivable on it, a seed that isn't on a `nuke_` node.
2. `python3 workflow.py scaffold inbox/<file>.json <name>`. Name it `<model>_<task>`, lowercase, e.g. `z_image_turbo_t2i`. This moves the export to `nuke_plugin/workflows/<name>/workflow.json` and drafts `node.json`.
3. Edit `node.json`: set `label`, `node_name` and `tile_color`; prune `knobs` down to what an artist would actually change (the draft lists every literal input); add `label`/`tooltip`; give server-driven model choices a `match` regex so only compatible files show. Never edit `workflow.json`. README §4 is the reference.
4. `python3 workflow.py check <name>` must PASS, and `python3 -m unittest discover -s tests` must pass (the suite checks every installed workflow, and the menu test lists labels: add the new one).
5. Create the node in headless Nuke (`import compai; compai.create("<name>")`) and confirm the knobs and their defaults.
6. Add the workflow to the table in README §3, then tell Harry what the node exposes and what he should test in the GUI.

A new workflow should need no Python. If it does, the standard node is missing a capability: raise that with Harry instead of special-casing a model in `node.py`.
