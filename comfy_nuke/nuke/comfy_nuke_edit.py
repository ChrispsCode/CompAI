"""
comfy_nuke_edit.py - "ComfyEdit" node: image edit / multi-reference
generation through a Comfy template with nuke_input_1..N LoadImage slots.

Each connected Nuke input is reformatted to ~megapixels (keeping its own
aspect), written as an sRGB PNG next to the output (gen_v003_in1.png), and
handed to Comfy - by path if the NukeLoadImagePath custom node is installed,
otherwise uploaded. Inputs are packed in order: connect img1 and img3 and
they become slots 1 and 2.

menu.py:
    import comfy_nuke_edit
    nuke.menu("Nodes").addCommand("AI/ComfyEdit", comfy_nuke_edit.create)
"""

import os
import random

import nuke

import comfy_client
import comfy_nuke as base

DEFAULT_TEMPLATE = os.path.join(base.HERE, "templates", "qwen_i2i_template_v002.json")
NUM_INPUTS = 4

# Tried in order against the Write/Read colorspace knob. Covers nuke-default,
# ACES 1.x and OCIO v2 / ACES 1.3 style configs. Override on the Settings tab.
SRGB_CANDIDATES = ["sRGB", "Output - sRGB", "sRGB - Display",
                   "sRGB Encoded Rec.709 (sRGB)", "sRGB - Texture",
                   "Utility - sRGB - Texture", "srgb_tx"]


# --------------------------------------------------------------------------
# Node creation
# --------------------------------------------------------------------------

def create(num_inputs=NUM_INPUTS):
    g = nuke.createNode("Group", inpanel=False)
    g.setName("ComfyEdit1")
    g["tile_color"].setValue(0x3E7CB1FF)

    g.begin()
    for i in range(1, num_inputs + 1):
        inp = nuke.nodes.Input(name="img%d" % i)
        prep = nuke.nodes.Reformat(name="prep%d" % i, inputs=[inp])
        prep["type"].setValue("to box")
        prep["box_fixed"].setValue(True)
        prep["resize"].setValue("distort")
        prep["filter"].setValue("Lanczos4")
        prep["black_outside"].setValue(True)
        w = nuke.nodes.Write(name="write%d" % i, inputs=[prep])
        w["file_type"].setValue("png")
        w["channels"].setValue("rgb")
        if "datatype" in w.knobs():
            w["datatype"].setValue("8 bit")
        if "create_directories" in w.knobs():
            w["create_directories"].setValue(True)
        w["disable"].setValue(True)        # only enabled while we execute it
        _set_colorspace(w, "")

    read = nuke.nodes.Read(name="ComfyRead")
    read["before"].setValue("hold")
    read["after"].setValue("hold")
    read["on_error"].setValue("black")
    _set_colorspace(read, "")
    reformat = nuke.nodes.Reformat(name="ToTarget", inputs=[read])
    reformat["resize"].setValue("fill")
    reformat["filter"].setValue("Lanczos4")
    reformat["black_outside"].setValue(True)
    reformat["format"].setExpression("parent.target_format")
    reformat["pbb"].setValue(True)
    nuke.nodes.Output(inputs=[reformat])
    g.end()
    g["knobChanged"].setValue("import comfy_nuke_edit; comfy_nuke_edit.on_knob_changed()")

    k = []
    k.append(nuke.Tab_Knob("comfy", "Comfy"))
    prompt = nuke.Multiline_Eval_String_Knob("prompt", "prompt")
    prompt.setTooltip("Edit instruction. With several inputs you can refer to "
                      "them in order, e.g. 'put the jacket from image 2 on the "
                      "person in image 1'.")
    k.append(prompt)
    en = nuke.Boolean_Knob("enhance", "enhance prompt")
    en.setTooltip("Rewrite the instruction with the Qwen 3.5 LLM, which also "
                  "looks at the input images. Slower.")
    en.setFlag(nuke.STARTLINE)
    k.append(en)

    k.append(nuke.Text_Knob("div1", ""))
    seed = nuke.Int_Knob("seed", "seed")
    seed.setValue(1)
    k.append(seed)
    rs = nuke.Boolean_Knob("random_seed", "new seed each run")
    rs.setValue(True)
    rs.clearFlag(nuke.STARTLINE)
    k.append(rs)
    steps = nuke.Int_Knob("steps", "steps")
    steps.setValue(25)
    k.append(steps)

    mi = nuke.Boolean_Knob("match_input", "match first input's format")
    mi.setValue(True)
    mi.setFlag(nuke.STARTLINE)
    k.append(mi)
    fmt = nuke.Format_Knob("target_format", "target format")
    k.append(fmt)
    mp = nuke.Double_Knob("megapixels", "megapixels")
    mp.setRange(0.25, 2.5)
    mp.setValue(1.0)
    mp.setTooltip("Budget for the generated image and for each reference "
                  "image sent to Comfy (each keeps its own aspect).")
    k.append(mp)
    k.append(nuke.Text_Knob("gen_size", "generate at", ""))
    k.append(nuke.Text_Knob("inputs_info", "inputs", ""))

    k.append(nuke.Text_Knob("div2", ""))
    out = nuke.File_Knob("output_dir", "output dir")
    out.setValue("[getenv SHOT_ROOT]/gen")
    k.append(out)
    name = nuke.String_Knob("out_name", "name")
    name.setValue("edit")
    k.append(name)

    k.append(nuke.Text_Knob("div3", ""))
    gen = nuke.PyScript_Knob("generate", "  Generate  ",
                             "import comfy_nuke_edit; comfy_nuke_edit.on_generate(nuke.thisNode())")
    gen.setFlag(nuke.STARTLINE)
    k.append(gen)
    k.append(nuke.PyScript_Knob("cancel", "Cancel",
                                "import comfy_nuke; comfy_nuke.on_cancel(nuke.thisNode())"))
    k.append(nuke.PyScript_Knob("reuse_seed", "Reuse last seed",
                                "import comfy_nuke; comfy_nuke.on_reuse_seed(nuke.thisNode())"))
    k.append(nuke.Text_Knob("status", "status", "idle"))

    k.append(nuke.Tab_Knob("results", "Results"))
    k.append(nuke.Text_Knob("last_file", "last file", ""))
    k.append(nuke.Int_Knob("last_seed", "last seed"))
    used = nuke.Multiline_Eval_String_Knob("used_prompt", "used prompt")
    used.setTooltip("The instruction that actually reached the image model.")
    k.append(used)

    k.append(nuke.Tab_Knob("settings", "Settings"))
    tmpl = nuke.File_Knob("template", "workflow template")
    tmpl.setValue(DEFAULT_TEMPLATE)
    k.append(tmpl)
    host = nuke.String_Knob("host", "comfy host")
    host.setValue(base.DEFAULT_HOST)
    k.append(host)
    mode = nuke.Enumeration_Knob("input_mode", "image transfer",
                                 ["auto", "path", "upload"])
    mode.setTooltip("path: Comfy reads the PNGs where Nuke wrote them (needs "
                    "the NukeLoadImagePath custom node and a shared disk).\n"
                    "upload: copy them into Comfy's input dir.\n"
                    "auto: path if the custom node is installed.")
    k.append(mode)
    cs = nuke.String_Knob("png_colorspace", "PNG colorspace", "")
    cs.setTooltip("Colorspace for the PNGs sent to Comfy and the result read "
                  "back. Empty = first sRGB match in the current OCIO config.")
    k.append(cs)

    for knob in k:
        g.addKnob(knob)

    _refresh(g)
    return g


# --------------------------------------------------------------------------
# Callbacks
# --------------------------------------------------------------------------

_IN_REFRESH = False


def on_knob_changed():
    node, knob = nuke.thisNode(), nuke.thisKnob()
    name = knob.name()
    if _IN_REFRESH:            # ignore knobChanged fired by our own setValue
        return
    if name == "png_colorspace":
        with node:
            for n in nuke.allNodes("Write") + nuke.allNodes("Read"):
                _set_colorspace(n, knob.value().strip())
    if name in ("target_format", "megapixels", "match_input",
                "inputChange", "showPanel"):
        _refresh(node)


def on_generate(node):
    key = node.fullName()
    if key in base._JOBS:
        nuke.message("A generation is already running on this node.")
        return

    prompt = node["prompt"].value().strip()
    if not prompt:
        nuke.message("Enter an edit instruction first.")
        return
    template_path = node["template"].evaluate()
    if not os.path.isfile(template_path):
        nuke.message("Workflow template not found:\n" + template_path)
        return

    connected = _connected_inputs(node)
    if not connected:
        nuke.message("Connect at least one image input.")
        return
    try:
        slots = len(comfy_client.image_slots(comfy_client.load_template(template_path)))
    except Exception as e:
        nuke.message("Couldn't read template:\n%s" % e)
        return
    if len(connected) > slots:
        nuke.message("%d inputs connected but the template only has %d image "
                     "slots." % (len(connected), slots))
        return

    _refresh(node)
    if node["random_seed"].value():
        node["seed"].setValue(random.randint(0, 2**31 - 1))
    seed = int(node["seed"].value())
    w, h = base._generation_size(node)
    out_path = base._next_output_path(node)
    stem = os.path.splitext(out_path)[0]

    # Write the reference PNGs (main thread, current frame)
    frame = nuke.frame()
    images = []
    try:
        for slot, idx in enumerate(connected, start=1):
            path = "%s_in%d.png" % (stem, slot)
            _write_input(node, idx, path, frame)
            images.append(path)
    except Exception as e:
        nuke.message("Failed writing input images:\n%s" % e)
        return

    params = dict(template_path=template_path, prompt=prompt, images=images,
                  out_path=out_path, width=w, height=h, seed=seed,
                  steps=int(node["steps"].value()),
                  enhance=bool(node["enhance"].value()),
                  host=node["host"].value().strip() or base.DEFAULT_HOST,
                  input_mode=node["input_mode"].value(),
                  upload_names=["%s_in%d.png" % (node.name(), s)
                                for s in range(1, len(images) + 1)])
    base.start_job(node, params, comfy_client.generate_edit,
                   "queued %d ref(s), %dx%d, seed %d, frame %d..."
                   % (len(images), w, h, seed, frame))


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _connected_inputs(node):
    return [i for i in range(node.inputs()) if node.input(i) is not None]


def _ref_size(node, input_index):
    f = node.input(input_index).format()
    return comfy_client.generation_size(f.width() * f.pixelAspect(), f.height(),
                                        node["megapixels"].value())


def _write_input(node, input_index, path, frame):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    w, h = _ref_size(node, input_index)
    n = input_index + 1
    with node:
        prep = nuke.toNode("prep%d" % n)
        prep["box_width"].setValue(w)
        prep["box_height"].setValue(h)
        write = nuke.toNode("write%d" % n)
        write["file"].setValue(path.replace("\\", "/"))
        write["disable"].setValue(False)
        try:
            nuke.execute(write, frame, frame)
        finally:
            write["disable"].setValue(True)


def _refresh(node):
    global _IN_REFRESH
    _IN_REFRESH = True
    try:
        _do_refresh(node)
    finally:
        _IN_REFRESH = False


def _do_refresh(node):
    connected = _connected_inputs(node)
    if node["match_input"].value() and connected:
        fmt = node.input(connected[0]).format()
        try:
            node["target_format"].setValue(fmt)
        except Exception:
            if fmt.name():
                node["target_format"].setValue(fmt.name())
    node["target_format"].setEnabled(not node["match_input"].value())
    base._update_gen_size(node)
    if connected:
        node["inputs_info"].setValue(
            ", ".join("img%d" % (i + 1) for i in connected)
            + "  ->  slot(s) 1-%d" % len(connected))
    else:
        node["inputs_info"].setValue("<font color=#e0a96c>none connected</font>")


def _set_colorspace(n, preferred):
    """Set a Read/Write colorspace knob to preferred, or the first sRGB match."""
    knob = n.knobs().get("colorspace")
    if knob is None:
        return None
    values = knob.values()
    # values may be plain names or "Family/Name" paths depending on config
    def match(name):
        for v in values:
            if v == name or v.split("/")[-1] == name:
                return v
        return None
    for name in ([preferred] if preferred else []) + SRGB_CANDIDATES:
        v = match(name)
        if v:
            try:
                knob.setValue(v)
            except Exception:
                knob.setValue(v.split("/")[-1])
            return v
    return None
