# Inbox: new ComfyUI workflows go here

Drop a ComfyUI export in this folder and ask Claude to "ingest the inbox". It gets checked, and either rejected with a list of what to fix in ComfyUI, or installed as a Nuke node.

## Preparing the export in ComfyUI

1. Get the workflow working in ComfyUI first.
2. Rename (title) every node Nuke should drive so it starts with `nuke_`. Each title must be unique.

   | Title | Node | Why |
   |---|---|---|
   | `nuke_prompt` | a String primitive wired to everything that needs the prompt | the prompt knob |
   | `nuke_latent` | the Empty Latent node | generation width and height |
   | `nuke_ksampler` | the sampler | seed, and steps etc. if you want them as knobs |
   | `nuke_save` (or `nuke_save_anything`) | the one node that saves the final image | where Nuke picks up the result |
   | `nuke_input_1`, `nuke_input_2`, … | stock Load Image nodes, numbered with no gaps | images coming from Nuke |
   | `nuke_whatever_you_like` | any other node with a setting you want as a knob | e.g. `nuke_load_diffusion_model` for a model dropdown |

3. Export with **Save (API)**, not the normal save, and put the `.json` here.

You don't need to say which settings become knobs in the export. Mention it when you ask for the ingest ("steps and the model dropdown"), or leave it to Claude to propose.

## What gets an export rejected

- It's a normal save, not **Save (API)**.
- No save node titled `nuke_save…`, or more than one.
- Two nodes share a `nuke_` title.
- A Load Image node that isn't titled `nuke_input_N` (its file would be baked in), or gaps in the numbering.
- A node type or model file that the running ComfyUI doesn't have.

Check one yourself any time: `python3 workflow.py check inbox/my_export.json`
