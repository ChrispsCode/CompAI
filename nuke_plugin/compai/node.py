"""
node.py - builds the Nuke node for an installed workflow.

Every node is a Group with the same standard parts: prompt, seed, target
format and megapixels, output dir and versioning, Generate / Cancel, a
Results tab, and an internal Read -> Reformat -> Output so the result flows
downstream like a plate. Workflows that take images also get inputs that
are written out as sRGB PNGs for Comfy.

Nothing is ever stretched. The result comes out at the size it was generated
unless 'reformat result' is on, and anything that must change shape (an
input going to Comfy, a result going to the format) is scaled to fill and
cropped. An image already at the generation size passes through untouched.

Between the prompt and the seed sits the workflow's own knob block, built
from the "knobs" list in its node.json. Nothing in here is specific to one
model: to add a model, add a workflow folder (see README).
"""

import os
import random
import re

import nuke

from . import comfy_client, jobs, workflows
from .comfy_client import ComfyError

DEFAULT_HOST = os.environ.get("COMFY_HOST", comfy_client.DEFAULT_HOST)
CALL = "import compai; compai.%s(nuke.thisNode())"

# Tried in order against the Write/Read colorspace knob. Covers nuke-default,
# ACES 1.x and OCIO v2 / ACES 1.3 style configs. Override on the Settings tab.
SRGB_CANDIDATES = ["sRGB", "Output - sRGB", "sRGB - Display",
                   "sRGB Encoded Rec.709 (sRGB)", "sRGB - Texture",
                   "Utility - sRGB - Texture", "srgb_tx"]


# --------------------------------------------------------------------------
# Menu and node creation
# --------------------------------------------------------------------------

def add_menu():
    """One entry per installed workflow, under Comfy in the node menu."""
    icon = os.path.join(os.path.dirname(__file__), "claude_icon.png")
    menu = nuke.menu("Nodes").addMenu("Comfy", icon=icon)
    for name in workflows.names():
        try:
            label = workflows.load(name).label
        except ComfyError as e:
            print(">> compai: skipping workflow '%s': %s" % (name, e))
            continue
        menu.addCommand(label, "import compai; compai.create(%r)" % name)


def create(name):
    try:
        wf = _load_checked(name)
    except ComfyError as e:
        nuke.message(str(e))
        return None

    g = nuke.createNode("Group", inpanel=False)
    g.setName(wf.node_name + "1")
    if wf.spec.get("tile_color"):
        g["tile_color"].setValue(int(wf.spec["tile_color"], 16))

    g.begin()
    for i in range(1, wf.max_images + 1):
        _add_input_chain(i)
    read = _add_output_chain()
    if wf.max_images:
        _set_colorspace(read, "")
    g.end()
    g["knobChanged"].setValue("import compai; compai.on_knob_changed()")

    knobs = [nuke.Tab_Knob("comfy", "Comfy")]
    knobs += _prompt_knobs(wf)
    knobs += _workflow_knobs(wf, _server_info(wf, DEFAULT_HOST))
    knobs += _seed_knobs()
    knobs += _size_knobs(wf)
    knobs += _output_knobs(wf)
    knobs += _action_knobs()
    knobs += _results_knobs()
    knobs += _settings_knobs(wf)
    for knob in knobs:
        g.addKnob(knob)

    _refresh(g)
    return g


def _load_checked(name):
    wf = workflows.load(name)
    problems = workflows.validate(wf)
    if problems:
        raise ComfyError("Workflow '%s' is not usable:\n- %s"
                         % (name, "\n- ".join(problems)))
    return wf


# --------------------------------------------------------------------------
# Internals of the group
# --------------------------------------------------------------------------

def _add_output_chain():
    """Read -> Reformat -> Output. Returns the Read. The Reformat only acts
    when 'reformat result' is on; otherwise the result passes through raw."""
    read = nuke.nodes.Read(name="ComfyRead")
    read["before"].setValue("hold")
    read["after"].setValue("hold")
    read["on_error"].setValue("black")
    reformat = nuke.nodes.Reformat(name="ToTarget", inputs=[read])
    reformat["resize"].setValue("fill")
    reformat["filter"].setValue("Lanczos4")
    reformat["black_outside"].setValue(True)
    reformat["format"].setExpression("parent.target_format")
    reformat["pbb"].setValue(True)
    reformat["disable"].setExpression("!parent.reformat_output")
    nuke.nodes.Output(inputs=[reformat])
    return read


def _add_input_chain(i):
    """imgN -> prepN (scale to fill the size for Comfy, crop the overflow)
    -> writeN (sRGB PNG, run on demand)."""
    inp = nuke.nodes.Input(name="img%d" % i)
    prep = nuke.nodes.Reformat(name="prep%d" % i, inputs=[inp])
    prep["type"].setValue("to box")
    prep["box_fixed"].setValue(True)
    prep["resize"].setValue("fill")
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


# --------------------------------------------------------------------------
# Standard knobs: every node gets these, whatever the workflow
# --------------------------------------------------------------------------

def _prompt_knobs(wf):
    prompt = nuke.Multiline_Eval_String_Knob("prompt", "prompt")
    if wf.max_images:
        prompt.setTooltip("Edit instruction. With several inputs you can "
                          "refer to them in order, e.g. 'put the jacket from "
                          "image 2 on the person in image 1'.")
    return [prompt]


def _seed_knobs():
    seed = nuke.Int_Knob("seed", "seed")
    seed.setValue(1)
    rand = nuke.Boolean_Knob("random_seed", "new seed each run")
    rand.setValue(True)
    rand.clearFlag(nuke.STARTLINE)
    return [nuke.Text_Knob("div_seed", ""), seed, rand]


def _size_knobs(wf):
    knobs = []
    if wf.max_images:
        match = nuke.Boolean_Knob("match_input", "match first input's format")
        match.setValue(True)
        match.setFlag(nuke.STARTLINE)
        knobs.append(match)
    mp = nuke.Double_Knob("megapixels", "megapixels")
    mp.setRange(0.25, 2.5)
    mp.setValue(1.0)
    mp.setTooltip("Generation budget. The image is generated at about this "
                  "many megapixels, at the format's aspect, with both sides "
                  "multiples of 16. A format that is already such a size at "
                  "this budget is used exactly."
                  + (" Each input image is sent at this budget too, at its "
                     "own aspect: scaled to fill and cropped, never stretched, "
                     "and untouched if it is already the right size."
                     if wf.max_images else ""))
    fmt = nuke.Format_Knob("target_format", "format")
    fmt.setTooltip("Sets the aspect ratio of the generation. The pixel size "
                   "comes from megapixels, unless 'reformat result' is on.")
    reformat = nuke.Boolean_Knob("reformat_output", "reformat result to format")
    reformat.setTooltip("Off: the result comes out raw, at the size it was "
                        "generated.\nOn: it is resized to the format above "
                        "(scaled to fill, then cropped).")
    reformat.setFlag(nuke.STARTLINE)
    knobs += [fmt, mp, nuke.Text_Knob("gen_size", "generate at", ""), reformat]
    if wf.max_images:
        knobs.append(nuke.Text_Knob("inputs_info", "inputs", ""))
    return knobs


def _output_knobs(wf):
    out = nuke.File_Knob("output_dir", "output dir")
    out.setValue("[getenv SHOT_ROOT]/gen")
    name = nuke.String_Knob("out_name", "name")
    name.setValue("edit" if wf.max_images else "gen")
    return [nuke.Text_Knob("div_output", ""), out, name]


def _action_knobs():
    gen = nuke.PyScript_Knob("generate", "  Generate  ", CALL % "on_generate")
    gen.setFlag(nuke.STARTLINE)
    return [nuke.Text_Knob("div_actions", ""), gen,
            nuke.PyScript_Knob("cancel", "Cancel", CALL % "on_cancel"),
            nuke.PyScript_Knob("reuse_seed", "Reuse last seed",
                               CALL % "on_reuse_seed"),
            nuke.Text_Knob("status", "status", "idle")]


def _results_knobs():
    used = nuke.Multiline_Eval_String_Knob("used_prompt", "used prompt")
    used.setTooltip("The prompt that actually reached the image model.")
    return [nuke.Tab_Knob("results", "Results"),
            nuke.Text_Knob("last_file", "last file", ""),
            nuke.Int_Knob("last_seed", "last seed"), used]


def _settings_knobs(wf):
    name = nuke.String_Knob("comfy_workflow", "workflow")
    name.setValue(wf.name)
    name.setTooltip("The installed workflow this node runs (a folder in "
                    "nuke_plugin/workflows). Set when the node is created.")
    name.setEnabled(False)
    host = nuke.String_Knob("host", "comfy host")
    host.setValue(DEFAULT_HOST)
    knobs = [nuke.Tab_Knob("settings", "Settings"), name, host]
    if wf.max_images:
        mode = nuke.Enumeration_Knob("input_mode", "image transfer",
                                     ["auto", "path", "upload"])
        mode.setTooltip("path: Comfy reads the PNGs where Nuke wrote them (needs "
                        "the NukeLoadImagePath custom node and a shared disk).\n"
                        "upload: copy them into Comfy's input dir.\n"
                        "auto: path if the custom node is installed.")
        cs = nuke.String_Knob("png_colorspace", "PNG colorspace", "")
        cs.setTooltip("Colorspace for the PNGs sent to Comfy and the result read "
                      "back. Empty = first sRGB match in the current OCIO config.")
        knobs += [mode, cs]
    return knobs


# --------------------------------------------------------------------------
# Workflow knobs: the block that differs per workflow, from node.json
# --------------------------------------------------------------------------

def _workflow_knobs(wf, info):
    if not wf.knobs:
        return []
    knobs = [nuke.Text_Knob("div_workflow", "")]
    last_server_choice = None
    for spec in wf.knobs:
        knob = _workflow_knob(wf, spec, info)
        if spec.get("tooltip"):
            knob.setTooltip(spec["tooltip"])
        knobs.append(knob)
        if spec.get("options") == "server":
            last_server_choice = knob
    if last_server_choice is not None:
        refresh = nuke.PyScript_Knob("refresh_choices", "refresh",
                                     CALL % "on_refresh_choices")
        refresh.setTooltip("Re-read the available options from ComfyUI, "
                           "e.g. after installing a model.")
        knobs.insert(knobs.index(last_server_choice) + 1, refresh)
    return knobs


def _workflow_knob(wf, spec, info):
    name, kind = spec["name"], spec["type"]
    label = spec.get("label", name.replace("_", " "))
    default = workflows.knob_default(wf, spec)
    if kind == "bool":
        knob = nuke.Boolean_Knob(name, label)
        knob.setFlag(nuke.STARTLINE)
        knob.setValue(bool(default))
    elif kind == "int":
        knob = nuke.Int_Knob(name, label)
        knob.setValue(int(default))
    elif kind == "float":
        knob = nuke.Double_Knob(name, label)
        if spec.get("range"):
            knob.setRange(*spec["range"])
        knob.setValue(float(default))
    elif kind == "choice":
        knob = nuke.Enumeration_Knob(name, label,
                                     workflows.choice_options(wf, spec, info))
        knob.setValue(str(default))
    else:
        knob = nuke.String_Knob(name, label)
        knob.setValue(str(default))
    return knob


def _server_info(wf, host):
    """/object_info for the classes behind this workflow's server-driven
    choice knobs. Empty if there are none or Comfy isn't reachable; the
    knobs then offer just their default until 'refresh' is pressed."""
    info = {}
    client = comfy_client.ComfyClient(host)
    for spec in wf.knobs:
        if spec.get("options") != "server":
            continue
        cls = workflows.server_class(wf, spec)
        if cls not in info:
            try:
                info.update(client.object_info(cls, timeout=3))
            except (OSError, ValueError):
                return {}
    return info


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
                "reformat_output", "inputChange", "showPanel"):
        _refresh(node)
    if name == "showPanel":
        jobs.release_stale_lock(node)


def on_reuse_seed(node):
    node["seed"].setValue(node["last_seed"].value())
    node["random_seed"].setValue(False)


def on_refresh_choices(node):
    try:
        wf = _load_checked(node["comfy_workflow"].value())
    except ComfyError as e:
        nuke.message(str(e))
        return
    info = _server_info(wf, _host(node))
    if not info:
        nuke.message("ComfyUI not reachable at %s." % _host(node))
        return
    for spec in wf.knobs:
        knob = node.knob(spec["name"])
        if spec.get("options") != "server" or knob is None:
            continue
        current = knob.value()
        options = workflows.choice_options(wf, spec, info)
        if current not in options:      # keep what the script was saved with
            options.insert(0, current)
        knob.setValues(options)
        knob.setValue(current)


def on_generate(node):
    if jobs.is_busy(node):
        return
    try:
        wf = _load_checked(node["comfy_workflow"].value())
    except ComfyError as e:
        nuke.message(str(e))
        return

    # Gather everything on the main thread - no knob access from the worker.
    values = {"prompt": node["prompt"].value().strip()}
    if "prompt" in wf.bind and not values["prompt"]:
        nuke.message("Enter a prompt first.")
        return
    for spec in wf.knobs:              # a node made before a knob was added
        knob = node.knob(spec["name"])  # just runs with that knob's default
        if knob is not None:
            values[spec["name"]] = knob.value()

    connected = _connected_inputs(node) if wf.max_images else []
    if wf.max_images and not connected:
        nuke.message("Connect at least one image input.")
        return
    if len(connected) > wf.max_images:
        nuke.message("%d inputs connected but '%s' takes at most %d."
                     % (len(connected), wf.label, wf.max_images))
        return

    _refresh(node)
    if node["random_seed"].value():
        node["seed"].setValue(random.randint(0, 2**31 - 1))
    values["seed"] = int(node["seed"].value())
    values["width"], values["height"] = _generation_size(node)
    out_path = _next_output_path(node)
    params = dict(name=wf.name, values=values, out_path=out_path,
                  host=_host(node))
    summary = "%dx%d, seed %d..." % (values["width"], values["height"],
                                     values["seed"])

    if connected:
        # Write the reference PNGs (main thread, current frame)
        stem, frame, images = os.path.splitext(out_path)[0], nuke.frame(), []
        try:
            for slot, idx in enumerate(connected, start=1):
                path = "%s_in%d.png" % (stem, slot)
                _write_input(node, idx, path, frame)
                images.append(path)
        except Exception as e:
            nuke.message("Failed writing input images:\n%s" % e)
            return
        params.update(images=images, input_mode=node["input_mode"].value(),
                      upload_names=["%s_in%d.png" % (node.name(), s)
                                    for s in range(1, len(images) + 1)])
        summary = "%d ref(s), frame %d, %s" % (len(images), frame, summary)
    jobs.start(node, workflows.generate, params, "queued " + summary)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _host(node):
    return node["host"].value().strip() or DEFAULT_HOST


def _generation_size(node):
    f = node["target_format"].value()
    tw = f.width() * f.pixelAspect()   # square up anamorphic formats
    return comfy_client.generation_size(tw, f.height(),
                                        node["megapixels"].value())


def _output_dir(node):
    raw = node["output_dir"].value().strip()
    d = (node["output_dir"].evaluate() or "").strip()
    unsaved = nuke.root().name() in ("", "Root")
    # Only fall back when the knob can't resolve to a real absolute path
    if not d or not os.path.isabs(d) or (unsaved and "root.name" in raw):
        d = os.path.join(os.path.expanduser("~"), "comfy_nuke")
    return os.path.normpath(d)


def _next_output_path(node):
    out_dir = _output_dir(node)
    name = re.sub(r"[^\w\-]+", "_", node["out_name"].value().strip()) or "gen"
    pattern = re.compile(r"^%s_v(\d{3,})\.png$" % re.escape(name))
    existing = []
    if os.path.isdir(out_dir):
        for f in os.listdir(out_dir):
            m = pattern.match(f)
            if m:
                existing.append(int(m.group(1)))
    version = max(existing) + 1 if existing else 1
    return os.path.join(out_dir, "%s_v%03d.png" % (name, version))


def _connected_inputs(node):
    return [i for i in range(node.inputs()) if node.input(i) is not None]


def _write_input(node, input_index, path, frame):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = node.input(input_index).format()
    w, h = comfy_client.generation_size(f.width() * f.pixelAspect(), f.height(),
                                        node["megapixels"].value())
    n = input_index + 1
    exact = (f.width(), f.height(), f.pixelAspect()) == (w, h, 1)
    with node:
        prep = nuke.toNode("prep%d" % n)
        prep["box_width"].setValue(w)
        prep["box_height"].setValue(h)
        prep["disable"].setValue(exact)    # already the right size: no resample
        write = nuke.toNode("write%d" % n)
        write["file"].setValue(path.replace("\\", "/"))
        write["disable"].setValue(False)
        try:
            nuke.execute(write, frame, frame)
        finally:
            write["disable"].setValue(True)


def _refresh(node):
    """Bring the derived knobs (matched format, size readout, input list) up
    to date. Guarded so our own setValue calls don't re-enter knobChanged."""
    global _IN_REFRESH
    _IN_REFRESH = True
    try:
        _do_refresh(node)
    finally:
        _IN_REFRESH = False


def _do_refresh(node):
    if node.knob("match_input") is not None:
        connected = _connected_inputs(node)
        if node["match_input"].value() and connected:
            _set_format(node["target_format"], node.input(connected[0]).format())
        node["target_format"].setEnabled(not node["match_input"].value())
        if connected:
            node["inputs_info"].setValue(
                ", ".join("img%d" % (i + 1) for i in connected)
                + "  ->  slot(s) 1-%d" % len(connected))
        else:
            node["inputs_info"].setValue(
                "<font color=#e0a96c>none connected</font>")
    w, h = _generation_size(node)
    f = node["target_format"].value()
    text = "%d x %d" % (w, h)
    if node["reformat_output"].value():
        text += "  ->  reformat to %d x %d" % (f.width(), f.height())
    node["gen_size"].setValue(text)


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
