"""
comfy_nuke_edit.py - "ComfyEdit" node: image edit / multi-reference
generation through a Comfy template with nuke_input_1..N LoadImage slots.

Each connected Nuke input is reformatted to ~megapixels (keeping its own
aspect), written as an sRGB PNG next to the output (gen_v003_in1.png), and
handed to Comfy - by path if the NukeLoadImagePath custom node is installed,
otherwise uploaded. Inputs are packed in order: connect img1 and img3 and
they become slots 1 and 2.

Install: see README. Nodes are registered in menu.py.
"""

import os

import nuke

import comfy_client
import comfy_nuke as base

DEFAULT_TEMPLATE = os.path.join(base.TEMPLATE_DIR, "qwen_i2i_template_v002.json")
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

    _set_colorspace(base.add_output_chain(), "")
    g.end()
    g["knobChanged"].setValue("import comfy_nuke_edit; comfy_nuke_edit.on_knob_changed()")

    mi = nuke.Boolean_Knob("match_input", "match first input's format")
    mi.setValue(True)
    mi.setFlag(nuke.STARTLINE)
    mode = nuke.Enumeration_Knob("input_mode", "image transfer",
                                 ["auto", "path", "upload"])
    mode.setTooltip("path: Comfy reads the PNGs where Nuke wrote them (needs "
                    "the NukeLoadImagePath custom node and a shared disk).\n"
                    "upload: copy them into Comfy's input dir.\n"
                    "auto: path if the custom node is installed.")
    cs = nuke.String_Knob("png_colorspace", "PNG colorspace", "")
    cs.setTooltip("Colorspace for the PNGs sent to Comfy and the result read "
                  "back. Empty = first sRGB match in the current OCIO config.")

    k = [nuke.Tab_Knob("comfy", "Comfy")]
    k += base.prompt_knobs(
        prompt_tip="Edit instruction. With several inputs you can refer to "
                   "them in order, e.g. 'put the jacket from image 2 on the "
                   "person in image 1'.",
        enhance_tip="Rewrite the instruction with the Qwen 3.5 LLM, which also "
                    "looks at the input images. Slower.")
    k += base.seed_knobs()
    k += [mi]
    k += base.size_knobs(
        megapixels_tip="Budget for the generated image and for each reference "
                       "image sent to Comfy (each keeps its own aspect).")
    k += [nuke.Text_Knob("inputs_info", "inputs", "")]
    k += base.output_knobs("edit")
    k += base.action_knobs("comfy_nuke_edit")
    k += base.results_knobs("The instruction that actually reached the image model.")
    k += base.settings_knobs(DEFAULT_TEMPLATE)
    k += [mode, cs]
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
    if name == "showPanel":
        base.release_stale_lock(node)


def on_generate(node):
    if base.is_busy(node):
        return
    params = base.common_params(node, "Enter an edit instruction first.")
    if params is None:
        return

    connected = _connected_inputs(node)
    if not connected:
        nuke.message("Connect at least one image input.")
        return
    try:
        slots = len(comfy_client.image_slots(
            comfy_client.load_template(params["template_path"])))
    except Exception as e:
        nuke.message("Couldn't read template:\n%s" % e)
        return
    if len(connected) > slots:
        nuke.message("%d inputs connected but the template only has %d image "
                     "slots." % (len(connected), slots))
        return

    _refresh(node)
    seed = base.next_seed(node)
    w, h = base.generation_size(node)
    out_path = base.next_output_path(node)
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

    params.update(images=images, out_path=out_path, width=w, height=h,
                  seed=seed, input_mode=node["input_mode"].value(),
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
        _set_format(node["target_format"], node.input(connected[0]).format())
    node["target_format"].setEnabled(not node["match_input"].value())
    base.update_gen_size(node)
    if connected:
        node["inputs_info"].setValue(
            ", ".join("img%d" % (i + 1) for i in connected)
            + "  ->  slot(s) 1-%d" % len(connected))
    else:
        node["inputs_info"].setValue("<font color=#e0a96c>none connected</font>")


def _set_format(knob, fmt):
    """Point a Format_Knob at fmt. The knob only accepts the name of a
    registered format (a Format object is ignored with a warning), so an
    unnamed format, e.g. from a Crop or a to-box Reformat, is registered."""
    name = fmt.name()
    if not name:
        name = "comfy_%dx%d" % (fmt.width(), fmt.height())
        if fmt.pixelAspect() != 1:
            name += "_pa%g" % fmt.pixelAspect()
        nuke.addFormat("%d %d %g %s" % (fmt.width(), fmt.height(),
                                        fmt.pixelAspect(), name))
    knob.setValue(name)


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
