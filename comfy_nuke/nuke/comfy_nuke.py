"""
comfy_nuke.py - Nuke front end for comfy_client.

Creates a "ComfyGenerate" Group: knobs for prompt / seed / format etc.,
a Generate button that runs the job on a background thread, and an internal
Read -> Reformat -> Output so the result flows downstream like a plate.

Install: put comfy_nuke.py, comfy_client.py and templates/ on NUKE_PATH,
then in menu.py:

    import comfy_nuke
    nuke.menu("Nodes").addCommand("AI/ComfyGenerate", comfy_nuke.create)
"""

import os
import re
import threading
import traceback

import nuke

import comfy_client

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TEMPLATE = os.path.join(HERE, "templates", "basic_t2i_qwen_api_v002.json")
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

    # --- internals: Read -> Reformat -> Output ---------------------------
    g.begin()
    read = nuke.nodes.Read(name="ComfyRead")
    read["before"].setValue("hold")
    read["after"].setValue("hold")
    read["on_error"].setValue("black")
    reformat = nuke.nodes.Reformat(name="ToTarget", inputs=[read])
    reformat["resize"].setValue("fill")
    reformat["filter"].setValue("Lanczos4")
    reformat["black_outside"].setValue(True)
    reformat['format'].setExpression('parent.target_format')
    reformat['pbb'].setValue(True)
    nuke.nodes.Output(inputs=[reformat])
    g.end()
    g["knobChanged"].setValue("import comfy_nuke; comfy_nuke.on_knob_changed()")

    # --- knobs -----------------------------------------------------------
    k = []
    k.append(nuke.Tab_Knob("comfy", "Comfy"))

    prompt = nuke.Multiline_Eval_String_Knob("prompt", "prompt")
    k.append(prompt)
    k.append(nuke.Boolean_Knob("enhance", "enhance prompt"))
    k[-1].setTooltip("Rewrite the prompt with the Qwen 3.5 LLM before generating. "
                     "Slower; the rewritten text appears in 'used prompt'.")
    k[-1].setFlag(nuke.STARTLINE)

    k.append(nuke.Text_Knob("div1", ""))

    seed = nuke.Int_Knob("seed", "seed")
    seed.setValue(1)
    k.append(seed)
    k.append(nuke.Boolean_Knob("random_seed", "new seed each run"))
    k[-1].setValue(True)
    k[-1].clearFlag(nuke.STARTLINE)
    steps = nuke.Int_Knob("steps", "steps")
    steps.setValue(25)
    k.append(steps)

    fmt = nuke.Format_Knob("target_format", "target format")
    k.append(fmt)
    mp = nuke.Double_Knob("megapixels", "megapixels")
    mp.setRange(0.25, 2.5)
    mp.setValue(1.0)
    mp.setTooltip("Generation budget. The image is generated at this size "
                  "(same aspect, multiples of 16) then reformatted to target.")
    k.append(mp)
    k.append(nuke.Text_Knob("gen_size", "generate at", ""))

    k.append(nuke.Text_Knob("div2", ""))

    out = nuke.File_Knob("output_dir", "output dir")
    out.setValue("[getenv SHOT_ROOT]/gen")
    k.append(out)
    name = nuke.String_Knob("out_name", "name")
    name.setValue("gen")
    k.append(name)

    k.append(nuke.Text_Knob("div3", ""))

    gen = nuke.PyScript_Knob("generate", "  Generate  ",
                             "import comfy_nuke; comfy_nuke.on_generate(nuke.thisNode())")
    gen.setFlag(nuke.STARTLINE)
    k.append(gen)
    k.append(nuke.PyScript_Knob("cancel", "Cancel",
                                "import comfy_nuke; comfy_nuke.on_cancel(nuke.thisNode())"))
    k.append(nuke.PyScript_Knob("reuse_seed", "Reuse last seed",
                                "import comfy_nuke; comfy_nuke.on_reuse_seed(nuke.thisNode())"))
    k.append(nuke.Text_Knob("status", "status", "idle"))

    # --- results / settings tab -----------------------------------------
    k.append(nuke.Tab_Knob("results", "Results"))
    k.append(nuke.Text_Knob("last_file", "last file", ""))
    k.append(nuke.Int_Knob("last_seed", "last seed"))
    used = nuke.Multiline_Eval_String_Knob("used_prompt", "used prompt")
    used.setTooltip("The prompt that actually reached the image model.")
    k.append(used)

    k.append(nuke.Tab_Knob("settings", "Settings"))
    tmpl = nuke.File_Knob("template", "workflow template")
    tmpl.setValue(DEFAULT_TEMPLATE)
    k.append(tmpl)
    host = nuke.String_Knob("host", "comfy host")
    host.setValue(DEFAULT_HOST)
    k.append(host)

    for knob in k:
        g.addKnob(knob)

    _update_gen_size(g)
    return g


# --------------------------------------------------------------------------
# Callbacks
# --------------------------------------------------------------------------

def on_knob_changed():
    node, knob = nuke.thisNode(), nuke.thisKnob()
    if knob.name() in ("target_format", "megapixels", "showPanel"):
        _update_gen_size(node)


def on_reuse_seed(node):
    node["seed"].setValue(node["last_seed"].value())
    node["random_seed"].setValue(False)


def on_cancel(node):
    ev = _JOBS.get(node.fullName())
    if ev:
        ev.set()
        _status(node.fullName(), "cancelling...")


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


def on_generate(node):
    key = node.fullName()
    if key in _JOBS:
        nuke.message("A generation is already running on this node.")
        return

    # Gather everything on the main thread - no knob access from the worker.
    prompt = node["prompt"].value().strip()
    if not prompt:
        nuke.message("Enter a prompt first.")
        return
    template = node["template"].evaluate()
    if not os.path.isfile(template):
        nuke.message("Workflow template not found:\n" + template)
        return

    if node["random_seed"].value():
        import random
        node["seed"].setValue(random.randint(0, 2**31 - 1))
    seed = int(node["seed"].value())

    w, h = _generation_size(node)
    out_path = _next_output_path(node)

    params = dict(template_path=template, prompt=prompt, out_path=out_path,
                  width=w, height=h, seed=seed,
                  steps=int(node["steps"].value()),
                  enhance=bool(node["enhance"].value()),
                  host=node["host"].value().strip() or DEFAULT_HOST)

    cancel = threading.Event()
    _JOBS[key] = cancel
    node["generate"].setEnabled(False)
    _status(key, "queued %dx%d, seed %d..." % (w, h, seed))

    t = threading.Thread(target=_worker, args=(key, params, cancel),
                         name="ComfyGenerate:" + key, daemon=True)
    t.start()


# --------------------------------------------------------------------------
# Worker (background thread)
# --------------------------------------------------------------------------

def _worker(key, params, cancel, fn=None):
    fn = fn or comfy_client.generate
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
    node["status"].setValue("<font color=#e06c6c>error</font>")
    if message != "Cancelled":
        nuke.message("ComfyGenerate failed:\n\n" + message[:2000])
    else:
        node["status"].setValue("cancelled")


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

def _generation_size(node):
    f = node["target_format"].value()
    tw = f.width() * f.pixelAspect()   # square up anamorphic formats
    th = f.height()
    return comfy_client.generation_size(tw, th, node["megapixels"].value())


def _update_gen_size(node):
    w, h = _generation_size(node)
    f = node["target_format"].value()
    node["gen_size"].setValue("%d x %d  ->  reformat to %d x %d"
                              % (w, h, f.width(), f.height()))


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