"""
comfy_nuke.py - Nuke front end for comfy_client.

Creates a "ComfyGenerate" Group: knobs for prompt / seed / format etc.,
a Generate button that runs the job on a background thread, and an internal
Read -> Reformat -> Output so the result flows downstream like a plate.

Also holds what every Comfy node shares: the common knob groups, the
background job runner and output versioning. comfy_nuke_edit builds on these.

Install: see README. Nodes are registered in menu.py.
"""

import os
import random
import re
import threading
import traceback

import nuke

import comfy_client

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(HERE, "templates")
DEFAULT_TEMPLATE = os.path.join(TEMPLATE_DIR, "basic_t2i_qwen_api_v002.json")
DEFAULT_HOST = os.environ.get("COMFY_HOST", comfy_client.DEFAULT_HOST)

# Jobs in flight, keyed by node fullName -> threading.Event (cancel flag)
_JOBS = {}


# --------------------------------------------------------------------------
# Node creation
# --------------------------------------------------------------------------

def create():
    g = nuke.createNode("Group", inpanel=False)
    g.setName("ComfyGenerate1")
    g["tile_color"].setValue(0x6A4C93FF)

    g.begin()
    add_output_chain()
    g.end()
    g["knobChanged"].setValue("import comfy_nuke; comfy_nuke.on_knob_changed()")

    k = [nuke.Tab_Knob("comfy", "Comfy")]
    k += prompt_knobs(
        enhance_tip="Rewrite the prompt with the Qwen 3.5 LLM before generating. "
                    "Slower; the rewritten text appears in 'used prompt'.")
    k += seed_knobs()
    k += size_knobs(
        megapixels_tip="Generation budget. The image is generated at this size "
                       "(same aspect, multiples of 16) then reformatted to target.")
    k += output_knobs("gen")
    k += action_knobs("comfy_nuke")
    k += results_knobs("The prompt that actually reached the image model.")
    k += settings_knobs(DEFAULT_TEMPLATE)
    for knob in k:
        g.addKnob(knob)

    update_gen_size(g)
    return g


# --------------------------------------------------------------------------
# Shared node parts. Knob names here are relied on by the job machinery
# below (generate, status, last_file, last_seed, used_prompt) and by the
# internal node name ComfyRead - keep them when adding a new node type.
# --------------------------------------------------------------------------

def add_output_chain():
    """Read -> Reformat -> Output. Call inside the group. Returns the Read."""
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
    nuke.nodes.Output(inputs=[reformat])
    return read


def prompt_knobs(prompt_tip=None, enhance_tip=None):
    prompt = nuke.Multiline_Eval_String_Knob("prompt", "prompt")
    if prompt_tip:
        prompt.setTooltip(prompt_tip)
    enhance = nuke.Boolean_Knob("enhance", "enhance prompt")
    if enhance_tip:
        enhance.setTooltip(enhance_tip)
    enhance.setFlag(nuke.STARTLINE)
    return [prompt, enhance]


def seed_knobs():
    seed = nuke.Int_Knob("seed", "seed")
    seed.setValue(1)
    rand = nuke.Boolean_Knob("random_seed", "new seed each run")
    rand.setValue(True)
    rand.clearFlag(nuke.STARTLINE)
    steps = nuke.Int_Knob("steps", "steps")
    steps.setValue(25)
    return [nuke.Text_Knob("div1", ""), seed, rand, steps]


def size_knobs(megapixels_tip):
    fmt = nuke.Format_Knob("target_format", "target format")
    mp = nuke.Double_Knob("megapixels", "megapixels")
    mp.setRange(0.25, 2.5)
    mp.setValue(1.0)
    mp.setTooltip(megapixels_tip)
    return [fmt, mp, nuke.Text_Knob("gen_size", "generate at", "")]


def output_knobs(default_name):
    out = nuke.File_Knob("output_dir", "output dir")
    out.setValue("[getenv SHOT_ROOT]/gen")
    name = nuke.String_Knob("out_name", "name")
    name.setValue(default_name)
    return [nuke.Text_Knob("div2", ""), out, name]


def action_knobs(module):
    """Generate / Cancel / Reuse seed / status. module is the name of the
    module whose on_generate(node) the Generate button calls."""
    gen = nuke.PyScript_Knob("generate", "  Generate  ",
                             "import %s; %s.on_generate(nuke.thisNode())"
                             % (module, module))
    gen.setFlag(nuke.STARTLINE)
    cancel = nuke.PyScript_Knob(
        "cancel", "Cancel",
        "import comfy_nuke; comfy_nuke.on_cancel(nuke.thisNode())")
    reuse = nuke.PyScript_Knob(
        "reuse_seed", "Reuse last seed",
        "import comfy_nuke; comfy_nuke.on_reuse_seed(nuke.thisNode())")
    return [nuke.Text_Knob("div3", ""), gen, cancel, reuse,
            nuke.Text_Knob("status", "status", "idle")]


def results_knobs(used_prompt_tip):
    used = nuke.Multiline_Eval_String_Knob("used_prompt", "used prompt")
    used.setTooltip(used_prompt_tip)
    return [nuke.Tab_Knob("results", "Results"),
            nuke.Text_Knob("last_file", "last file", ""),
            nuke.Int_Knob("last_seed", "last seed"),
            used]


def settings_knobs(default_template):
    tmpl = nuke.File_Knob("template", "workflow template")
    tmpl.setValue(default_template)
    host = nuke.String_Knob("host", "comfy host")
    host.setValue(DEFAULT_HOST)
    return [nuke.Tab_Knob("settings", "Settings"), tmpl, host]


# --------------------------------------------------------------------------
# Callbacks
# --------------------------------------------------------------------------

def on_knob_changed():
    node, knob = nuke.thisNode(), nuke.thisKnob()
    if knob.name() in ("target_format", "megapixels", "showPanel"):
        update_gen_size(node)
    if knob.name() == "showPanel":
        release_stale_lock(node)


def on_reuse_seed(node):
    node["seed"].setValue(node["last_seed"].value())
    node["random_seed"].setValue(False)


def on_cancel(node):
    ev = _JOBS.get(node.fullName())
    if ev:
        ev.set()
        _status(node.fullName(), "cancelling...")


def on_generate(node):
    if is_busy(node):
        return
    params = common_params(node, "Enter a prompt first.")
    if params is None:
        return
    seed = next_seed(node)
    w, h = generation_size(node)
    params.update(out_path=next_output_path(node), width=w, height=h, seed=seed)
    start_job(node, params, comfy_client.generate,
              "queued %dx%d, seed %d..." % (w, h, seed))


# --------------------------------------------------------------------------
# Shared job machinery. Knobs are only touched on the main thread; the
# worker calls comfy_client and reports back via executeInMainThread.
# --------------------------------------------------------------------------

def is_busy(node):
    """True (after telling the user) if this node already has a job running."""
    if node.fullName() in _JOBS:
        nuke.message("A generation is already running on this node.")
        return True
    return False


def release_stale_lock(node):
    """Re-enable Generate if no job is running. The disabled state is saved
    with the script, so a script saved mid-job would otherwise stay locked."""
    if node.fullName() not in _JOBS and not node["generate"].enabled():
        node["generate"].setEnabled(True)


def common_params(node, empty_prompt_message):
    """Read and validate the knobs every node shares. Returns the matching
    comfy_client keyword args, or None after telling the user what's wrong."""
    prompt = node["prompt"].value().strip()
    if not prompt:
        nuke.message(empty_prompt_message)
        return None
    template = resolve_template(node["template"].evaluate())
    if template is None:
        nuke.message("Workflow template not found:\n" + node["template"].evaluate())
        return None
    return dict(template_path=template, prompt=prompt,
                steps=int(node["steps"].value()),
                enhance=bool(node["enhance"].value()),
                host=node["host"].value().strip() or DEFAULT_HOST)


def resolve_template(path):
    """The template file to use, or None. Falls back to a same-named file in
    this install's templates/ so nodes survive the tool being moved."""
    path = (path or "").strip()
    if os.path.isfile(path):
        return path
    fallback = os.path.join(TEMPLATE_DIR, os.path.basename(path))
    return fallback if os.path.isfile(fallback) else None


def next_seed(node):
    """Roll a new seed onto the knob if 'new seed each run' is on."""
    if node["random_seed"].value():
        node["seed"].setValue(random.randint(0, 2**31 - 1))
    return int(node["seed"].value())


def start_job(node, params, fn, summary):
    """Run fn(**params) on a background thread with status/cancel/done
    handling. Shared by every ComfyGenerate-style node."""
    key = node.fullName()
    cancel = threading.Event()
    _JOBS[key] = cancel
    node["generate"].setEnabled(False)
    _status(key, summary)
    threading.Thread(target=_worker, args=(key, params, cancel, fn),
                     name="ComfyJob:" + key, daemon=True).start()


def _worker(key, params, cancel, fn):
    try:
        result = fn(
            should_cancel=cancel.is_set,
            log=lambda msg: _status(key, msg),
            **params)
        nuke.executeInMainThread(_on_done, args=(key, result))
    except Exception as e:  # report everything back to the UI
        traceback.print_exc()
        nuke.executeInMainThread(_on_error, args=(key, str(e)))


def _on_done(key, result):
    _JOBS.pop(key, None)
    node = nuke.toNode(key)
    if node is None:          # node deleted while generating
        return
    node["generate"].setEnabled(True)
    with node:
        read = nuke.toNode("ComfyRead")
        read["file"].fromUserText(result["path"])
        read["reload"].execute()
    node["last_file"].setValue(os.path.basename(result["path"]))
    node["last_seed"].setValue(result["seed"])
    node["used_prompt"].setValue(result.get("used_prompt") or result["prompt"])
    node["status"].setValue("done in %ss  ->  %s"
                            % (result["seconds"], os.path.basename(result["path"])))


def _on_error(key, message):
    _JOBS.pop(key, None)
    node = nuke.toNode(key)
    if node is None:
        return
    node["generate"].setEnabled(True)
    if message == "Cancelled":
        node["status"].setValue("cancelled")
    else:
        node["status"].setValue("<font color=#e06c6c>error</font>")
        nuke.message("%s failed:\n\n%s" % (node.name(), message[:2000]))


def _status(key, msg):
    """Thread-safe status update."""
    def apply():
        node = nuke.toNode(key)
        if node is not None:
            node["status"].setValue(msg)
    if threading.current_thread() is threading.main_thread():
        apply()
    else:
        nuke.executeInMainThread(apply)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def generation_size(node):
    f = node["target_format"].value()
    tw = f.width() * f.pixelAspect()   # square up anamorphic formats
    th = f.height()
    return comfy_client.generation_size(tw, th, node["megapixels"].value())


def update_gen_size(node):
    w, h = generation_size(node)
    f = node["target_format"].value()
    node["gen_size"].setValue("%d x %d  ->  reformat to %d x %d"
                              % (w, h, f.width(), f.height()))


def output_dir(node):
    raw = node["output_dir"].value().strip()
    d = (node["output_dir"].evaluate() or "").strip()
    unsaved = nuke.root().name() in ("", "Root")
    # Only fall back when the knob can't resolve to a real absolute path
    if not d or not os.path.isabs(d) or (unsaved and "root.name" in raw):
        d = os.path.join(os.path.expanduser("~"), "comfy_nuke")
    return os.path.normpath(d)


def next_output_path(node):
    out_dir = output_dir(node)
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
