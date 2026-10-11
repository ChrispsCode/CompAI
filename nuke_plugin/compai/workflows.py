"""
workflows.py - installed workflows: finding, checking, patching and running.

Never imports nuke. Each workflow is a folder in nuke_plugin/workflows/:

    <name>/workflow.json   the ComfyUI "Save (API)" export, unedited
    <name>/node.json       how Nuke drives it: which knob feeds which input

node.json keys (see README "Workflow definitions" for the full reference):

    label, node_name, tile_color   how the Nuke node is presented
    output                         title of the save node
    bind                           standard values -> [[title, input], ...]
                                   (prompt, seed, width, height)
    set                            constants: [[title, input, value], ...]
    images                         {"max": N} for numbered nuke_input_N slots,
                                   or {"inputs": [{name, title}, ...]} for
                                   named ones such as first / last frame
    size                           {"multiple", "megapixels", "megapixels_range"}
    timeout                        seconds to wait for a run (default 900)
    knobs                          the workflow-specific knob block
"""

import copy
import json
import os
import random
import re

from . import comfy_client
from .comfy_client import ComfyError

WORKFLOWS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workflows")

STANDARD = ("prompt", "seed", "width", "height")
KNOB_TYPES = ("int", "float", "bool", "string", "choice")
TITLE_PREFIX = "nuke_"
SAVE_PREFIX = "nuke_save"
INPUT_PREFIX = "nuke_input_"

# Knob names a workflow can't use: the standard knobs every node gets, and
# the ones Nuke puts on every node.
RESERVED = set("""
    prompt seed random_seed match_input target_format megapixels gen_size
    reformat_output inputs_info output_dir out_name generate cancel reuse_seed status
    last_file last_seed used_prompt comfy_workflow host input_mode
    png_colorspace temp_inputs instant_cleanup refresh_choices create_read timeout
    name label disable selected xpos ypos tile_color note_font
    note_font_size note_font_color hide_input cached postage_stamp
    lifetimeStart lifetimeEnd useLifetime knobChanged onCreate onDestroy
    updateUI autolabel icon indicators help
""".split())


class Workflow(object):
    """An installed workflow: its definition (spec) and its graph."""

    def __init__(self, name, spec, graph):
        self.name, self.spec, self.graph = name, spec, graph

    label = property(lambda self: self.spec.get("label", self.name))
    node_name = property(lambda self: self.spec.get("node_name", "Comfy"))
    output = property(lambda self: self.spec.get("output", ""))
    bind = property(lambda self: self.spec.get("bind", {}))
    knobs = property(lambda self: self.spec.get("knobs", []))
    constants = property(lambda self: self.spec.get("set", []))

    @property
    def image_inputs(self):
        """Named image inputs ([{name, title}, ...]), each with its own
        LoadImage node, or None if the workflow uses numbered slots."""
        return self.spec.get("images", {}).get("inputs")

    @property
    def max_images(self):
        if self.image_inputs is not None:
            return len(self.image_inputs)
        return int(self.spec.get("images", {}).get("max", 0))

    @property
    def multiple(self):
        """Width and height are snapped to a multiple of this."""
        return int(self.spec.get("size", {}).get("multiple", 16))

    @property
    def megapixels(self):
        return float(self.spec.get("size", {}).get("megapixels", 1.0))

    @property
    def megapixels_range(self):
        return self.spec.get("size", {}).get("megapixels_range", [0.25, 2.5])

    @property
    def timeout(self):
        return int(self.spec.get("timeout", 900))

    @property
    def image_prefix(self):
        return self.spec.get("images", {}).get("prefix", INPUT_PREFIX)


# --------------------------------------------------------------------------
# Finding and loading
# --------------------------------------------------------------------------

def names(root=None):
    """Installed workflow names (folders that have a node.json), sorted."""
    root = root or WORKFLOWS_DIR
    if not os.path.isdir(root):
        return []
    return sorted(d for d in os.listdir(root)
                  if os.path.isfile(os.path.join(root, d, "node.json")))


_CACHE = {}


def load_cached(name):
    """load(), but only re-reading the files when they change. For callers
    that run on every knob change."""
    folder = os.path.join(WORKFLOWS_DIR, name)
    try:
        stamp = tuple(os.path.getmtime(os.path.join(folder, f))
                      for f in ("node.json", "workflow.json"))
    except OSError as e:
        raise ComfyError("Can't load workflow '%s': %s" % (name, e))
    if _CACHE.get(name, (None,))[0] != stamp:
        _CACHE[name] = (stamp, load(name))
    return _CACHE[name][1]


def load(name, root=None):
    folder = os.path.join(root or WORKFLOWS_DIR, name)
    try:
        with open(os.path.join(folder, "node.json"), "r", encoding="utf-8") as f:
            spec = json.load(f)
        graph = comfy_client.load_template(os.path.join(folder, "workflow.json"))
    except (OSError, ValueError) as e:
        raise ComfyError("Can't load workflow '%s': %s" % (name, e))
    return Workflow(name, spec, graph)


# --------------------------------------------------------------------------
# Checking. Each function returns a list of problems; empty means fine.
# --------------------------------------------------------------------------

def _title(node):
    return node.get("_meta", {}).get("title", "")


def _is_link(value):
    return isinstance(value, list) and len(value) == 2 \
        and isinstance(value[0], str)


def lint_graph(graph):
    """Problems with a workflow export on its own, before any definition
    exists. This is the gate for accepting a new export."""
    if not isinstance(graph, dict) or not graph:
        return ["Not a workflow: expected a JSON object of nodes."]
    if isinstance(graph.get("nodes"), list):
        return ["This is a normal (UI) save. Re-export it from ComfyUI with "
                "'Save (API)'."]
    bad = [nid for nid, n in graph.items()
           if not isinstance(n, dict) or "class_type" not in n
           or not isinstance(n.get("inputs"), dict)]
    if bad:
        return ["Not an API-format export: node(s) %s have no class_type/"
                "inputs. Re-export with 'Save (API)'." % ", ".join(bad[:5])]

    problems = []
    for nid, node in graph.items():
        for key, value in node["inputs"].items():
            if _is_link(value) and value[0] not in graph:
                problems.append("Node %s (%s) input '%s' links to missing "
                                "node %s." % (nid, _title(node), key, value[0]))

    by_title = {}
    for nid, node in graph.items():
        if _title(node).startswith(TITLE_PREFIX):
            by_title.setdefault(_title(node), []).append(nid)
    for title, ids in sorted(by_title.items()):
        if len(ids) > 1:
            problems.append("Title '%s' is used by %d nodes (%s); titles "
                            "starting %s must be unique."
                            % (title, len(ids), ", ".join(ids), TITLE_PREFIX))

    saves = sorted(t for t in by_title if t.startswith(SAVE_PREFIX))
    if not saves:
        problems.append("No save node titled '%s...'. Title the node that "
                        "saves the final image '%s'." % (SAVE_PREFIX, SAVE_PREFIX))
    elif len(saves) > 1:
        problems.append("More than one save node (%s); Nuke reads back one "
                        "image, so keep one." % ", ".join(saves))

    slots = sorted(int(t[len(INPUT_PREFIX):]) for t in by_title
                   if t.startswith(INPUT_PREFIX)
                   and t[len(INPUT_PREFIX):].isdigit())
    if slots and slots != list(range(1, len(slots) + 1)):
        problems.append("Image inputs must be numbered %s1..N with no gaps; "
                        "found %s." % (INPUT_PREFIX, slots))
    for nid, node in graph.items():
        is_slot = _title(node).startswith(INPUT_PREFIX)
        if node["class_type"] == "LoadImage" \
                and not _title(node).startswith(TITLE_PREFIX):
            problems.append("LoadImage node %s ('%s') has a file baked in. "
                            "Give it a %s title so Nuke supplies the image "
                            "(%sN, or a name like nuke_first_frame), or "
                            "remove it." % (nid, _title(node), TITLE_PREFIX,
                                            INPUT_PREFIX))
        if is_slot and node["class_type"] != "LoadImage":
            problems.append("'%s' must be a stock LoadImage node, not %s."
                            % (_title(node), node["class_type"]))
    return problems


def validate(wf):
    """Problems with a definition against its own graph."""
    problems = lint_graph(wf.graph)
    spec = wf.spec

    def check_target(what, title, key):
        try:
            nid = comfy_client.find_node(wf.graph, title)
        except ComfyError as e:
            problems.append("%s: %s." % (what, e))
            return
        if key is not None and key not in wf.graph[nid]["inputs"]:
            problems.append("%s: node '%s' has no input '%s' (has: %s)."
                            % (what, title, key,
                               ", ".join(sorted(wf.graph[nid]["inputs"]))))

    if "/" in wf.label:
        problems.append("label: '/' would split the menu entry into a "
                        "submenu; use another character.")
    if not spec.get("output"):
        problems.append("node.json needs \"output\": the save node's title.")
    else:
        check_target("output", spec["output"], "filename_prefix")

    for name, targets in wf.bind.items():
        if name not in STANDARD:
            problems.append("bind: '%s' is not a standard value (%s)."
                            % (name, ", ".join(STANDARD)))
        for title, key in targets:
            check_target("bind.%s" % name, title, key)
    if ("width" in wf.bind) != ("height" in wf.bind):
        problems.append("bind: width and height must be bound together.")
    for title, key, _value in wf.constants:
        check_target("set", title, key)

    seen = set()
    for knob in wf.knobs:
        name = knob.get("name", "")
        what = "knob '%s'" % name
        if not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", name):
            problems.append("%s: names must be letters, digits and "
                            "underscores, starting with a letter." % what)
        if name in RESERVED:
            problems.append("%s: that name is taken by a standard knob." % what)
        if name in seen:
            problems.append("%s: defined twice." % what)
        seen.add(name)
        if knob.get("type") not in KNOB_TYPES:
            problems.append("%s: type must be one of %s."
                            % (what, ", ".join(KNOB_TYPES)))
        if not knob.get("bind"):
            problems.append("%s: needs \"bind\": [[title, input], ...]." % what)
        for title, key in knob.get("bind", []):
            check_target(what, title, key)
        if knob.get("type") == "choice":
            options = knob.get("options")
            if options != "server" and not (isinstance(options, list) and options):
                problems.append("%s: a choice needs \"options\": a list, or "
                                "\"server\" to ask ComfyUI." % what)

    claimed = set()
    if wf.image_inputs is not None:
        names = [i.get("name", "") for i in wf.image_inputs]
        if len(set(names)) != len(names) or not all(
                re.match(r"^[A-Za-z][A-Za-z0-9_]*$", n) for n in names):
            problems.append("images.inputs: each needs a unique \"name\" "
                            "(letters, digits, underscores).")
        for item in wf.image_inputs:
            check_target("image input '%s'" % item.get("name"),
                         item.get("title", ""), "image")
            claimed.add(item.get("title"))
    elif wf.max_images:
        slots = comfy_client.image_slots(wf.graph, wf.image_prefix)
        claimed.update(_title(wf.graph[nid]) for nid in slots)
        if len(slots) < wf.max_images:
            problems.append("images.max is %d but the workflow only has %d "
                            "'%sN' slot(s)." % (wf.max_images, len(slots),
                                                wf.image_prefix))
    for nid, node in wf.graph.items():
        if node["class_type"] == "LoadImage" and _title(node) not in claimed:
            problems.append("LoadImage '%s' is not declared under \"images\" "
                            "in node.json, so its file would be baked in."
                            % _title(node))
    return problems


def _input_specs(class_spec):
    specs = {}
    for section in ("required", "optional"):
        specs.update(class_spec.get("input", {}).get(section, {}))
    return specs


def combo_options(input_spec):
    """The option list of a combo input from /object_info, or None if the
    input isn't a plain combo. Handles both the old and the new spec shape."""
    if not isinstance(input_spec, list) or not input_spec:
        return None
    options = None
    if isinstance(input_spec[0], list):
        options = input_spec[0]
    elif input_spec[0] == "COMBO" and len(input_spec) > 1:
        options = input_spec[1].get("options")
    if not isinstance(options, list):
        return None
    return [o for o in options if isinstance(o, (str, int, float))]


def check_server(graph, info, output=None):
    """Problems running this graph on the server described by info (the
    /object_info reply): missing node classes, unknown inputs, and combo
    values such as model files that the server doesn't have."""
    problems = []
    for nid, node in graph.items():
        cls, title = node["class_type"], _title(node)
        if cls not in info:
            problems.append("Node class '%s' (node %s, '%s') is not installed "
                            "in ComfyUI." % (cls, nid, title))
            continue
        specs = _input_specs(info[cls])
        for key, value in node["inputs"].items():
            if "." in key:             # sub-input of a dynamic combo
                continue
            if key not in specs:
                problems.append("%s '%s' has no input '%s' on this server."
                                % (cls, title, key))
                continue
            options = combo_options(specs[key])
            is_slot = cls == "LoadImage" and title.startswith(TITLE_PREFIX)
            if options is not None and not _is_link(value) and not is_slot \
                    and value not in options:
                problems.append("%s '%s': %s = '%s' is not available on this "
                                "server." % (cls, title, key, value))
        if output and title == output and not info[cls].get("output_node"):
            problems.append("'%s' (%s) is not an output node, so ComfyUI "
                            "would run nothing." % (title, cls))
    return problems


# --------------------------------------------------------------------------
# Knob values
# --------------------------------------------------------------------------

def knob_default(wf, knob):
    """A knob's default: the definition's, else whatever the export has."""
    if "default" in knob:
        return knob["default"]
    title, key = knob["bind"][0]
    return wf.graph[comfy_client.find_node(wf.graph, title)]["inputs"][key]


def coerce(knob, value):
    kind = knob["type"]
    if kind == "int":
        return int(value)
    if kind == "float":
        return float(value)
    if kind == "bool":
        return bool(value)
    return str(value)


def choice_options(wf, knob, info=None):
    """Options for a choice knob. For "options": "server" they come from
    info (an /object_info reply), filtered by the knob's "match" regex; with
    no info only the default is offered. The default is always included."""
    default = str(knob_default(wf, knob))
    options = knob.get("options")
    if options == "server":
        title, key = knob["bind"][0]
        cls = wf.graph[comfy_client.find_node(wf.graph, title)]["class_type"]
        found = combo_options(_input_specs((info or {}).get(cls, {})).get(key))
        pattern = knob.get("match")
        options = [o for o in found or []
                   if not pattern or re.search(pattern, str(o))]
    options = [str(o) for o in options]
    return options if default in options else [default] + options


def server_class(wf, knob):
    """The node class a server-driven choice knob gets its options from."""
    title, _key = knob["bind"][0]
    return wf.graph[comfy_client.find_node(wf.graph, title)]["class_type"]


# --------------------------------------------------------------------------
# Patching and running
# --------------------------------------------------------------------------

def build(wf, values, images=None, image_mode="upload", filename_prefix=None):
    """A patched copy of the workflow graph, ready to submit.

    values holds the standard values (prompt, seed, width, height) and the
    workflow's own knobs by name; anything missing keeps the export's value.
    images are upload names or absolute paths: in slot order for numbered
    slots, or one per named input (None = not supplied) for named ones.
    Unused image nodes are pruned along with their links."""
    graph = copy.deepcopy(wf.graph)
    for name, targets in wf.bind.items():
        if values.get(name) is None:
            continue
        value = values[name] if name == "prompt" else int(values[name])
        for title, key in targets:
            comfy_client.set_input(graph, title, key, value)
    for knob in wf.knobs:
        if values.get(knob["name"]) is None:
            continue
        for title, key in knob["bind"]:
            comfy_client.set_input(graph, title, key,
                                   coerce(knob, values[knob["name"]]))
    for title, key, value in wf.constants:
        comfy_client.set_input(graph, title, key, value)
    comfy_client.set_input(graph, wf.output, "filename_prefix",
                           filename_prefix or "nuke/" + wf.name)
    if wf.max_images:
        if not images or not any(images):
            raise ComfyError("'%s' needs at least one input image" % wf.name)
        if wf.image_inputs is None:
            comfy_client.bind_images(graph, images, mode=image_mode,
                                     prefix=wf.image_prefix)
        else:
            if len(images) > len(wf.image_inputs):
                raise ComfyError("'%s' takes %d image(s), got %d" % (
                    wf.name, len(wf.image_inputs), len(images)))
            images = list(images) + [None] * len(wf.image_inputs)
            for item, value in zip(wf.image_inputs, images):
                nid = comfy_client.find_node(graph, item["title"])
                if value:
                    comfy_client.set_image(graph, nid, value, image_mode)
                else:
                    comfy_client.remove_node(graph, nid)
    elif images:
        raise ComfyError("'%s' takes no input images" % wf.name)
    return graph


def generate(name, values, out_path, images=None,
             host=comfy_client.DEFAULT_HOST, input_mode="auto",
             upload_names=None, timeout=None, should_cancel=None, log=print):
    """Run an installed workflow end to end. Returns the result dict that is
    also written to the sidecar: the values used, path, seed, timing.

    images are local file paths: in slot order, or one per named input with
    None for any not supplied. out_path's extension is a placeholder; the
    result's "path" has the real one (.png, .mp4 ...). timeout is in
    seconds and defaults to the workflow's own. input_mode:
        "path"   - Comfy reads the files where they are (needs the
                   NukeLoadImagePath custom node and a shared disk)
        "upload" - copy them into Comfy's input dir via /upload/image
        "auto"   - path if the custom node is installed, else upload
    """
    wf = load(name)
    problems = validate(wf)
    if problems:
        raise ComfyError("Workflow '%s' is not usable:\n- %s"
                         % (name, "\n- ".join(problems)))
    client = comfy_client.ComfyClient(host)
    client.ping()

    values = dict(values)
    if "seed" in wf.bind and values.get("seed") is None:
        values["seed"] = random.randint(0, 2**32 - 1)
    info = dict(values, workflow=name)

    refs = None
    if images and any(images):
        mode = input_mode
        has_loader = client.has_node(comfy_client.PATH_LOADER)
        if mode == "auto":
            mode = "path" if has_loader else "upload"
        if mode == "path" and not has_loader:
            raise ComfyError("input_mode 'path' needs the %s custom node in "
                             "ComfyUI/custom_nodes (restart Comfy after "
                             "installing)" % comfy_client.PATH_LOADER)
        refs = []
        for i, path in enumerate(images):
            if not path:
                refs.append(None)
            elif mode == "path":
                refs.append(os.path.abspath(path))
            else:
                log("Uploading %s" % os.path.basename(path))
                refs.append(client.upload_image(
                    path, name=upload_names[i] if upload_names else None))
        info.update(images=[os.path.abspath(p) if p else None for p in images],
                    image_mode=mode)

    graph = build(wf, values, refs, image_mode=info.get("image_mode", "upload"))
    log("Submitting %s" % wf.label)
    return comfy_client.run_workflow(client, graph, out_path, info, wf.output,
                                     should_cancel, log,
                                     timeout=timeout or wf.timeout)


# --------------------------------------------------------------------------
# Drafting a definition from a new export
# --------------------------------------------------------------------------

def scaffold(graph, info=None):
    """A first-draft node.json for an export, from its nuke_* titles.

    Standard values are bound by convention (nuke_prompt, any '...seed'
    input, a nuke_* node with width and height, nuke_save*, and LoadImage
    nodes: numbered nuke_input_N, or named ones by their title). Every other literal input on a
    nuke_* node becomes a candidate knob: prune these by hand, a node should
    only show what an artist would change. info (/object_info) gives types
    and tells combos from free text."""
    spec = {"label": "", "node_name": "", "output": "", "bind": {}, "knobs": []}
    slots, named, used = 0, [], set()
    for nid, node in graph.items():
        title = _title(node)
        if not title.startswith(TITLE_PREFIX):
            continue
        if title.startswith(SAVE_PREFIX):
            spec["output"] = title
            continue
        if title.startswith(INPUT_PREFIX):
            slots += 1
            continue
        if node["class_type"] == "LoadImage":
            named.append({"name": title[len(TITLE_PREFIX):], "title": title})
            continue
        if "width" in node["inputs"] and "height" in node["inputs"] \
                and "width" not in spec["bind"]:
            spec["bind"]["width"] = [[title, "width"]]
            spec["bind"]["height"] = [[title, "height"]]
        specs = _input_specs((info or {}).get(node["class_type"], {}))
        for key, value in node["inputs"].items():
            if _is_link(value):
                continue
            if title == "nuke_prompt" and key in ("value", "text"):
                spec["bind"].setdefault("prompt", []).append([title, key])
            elif key in ("width", "height") and [title, key] in spec["bind"].get(key, []):
                continue
            elif key.endswith("seed"):
                spec["bind"].setdefault("seed", []).append([title, key])
            elif title == "nuke_latent":
                continue
            else:
                name = re.sub(r"\W+", "_", key.split(".")[-1])
                if name in used or name in RESERVED:
                    name = "%s_%s" % (title[len(TITLE_PREFIX):], name)
                used.add(name)
                knob = {"name": name, "type": _guess_type(value, specs.get(key)),
                        "bind": [[title, key]]}
                if knob["type"] == "choice":
                    knob["options"] = "server"
                spec["knobs"].append(knob)
    if slots:
        spec["images"] = {"max": slots}
    elif named:
        spec["images"] = {"inputs": named}
    return spec


def _guess_type(value, input_spec):
    if combo_options(input_spec) is not None:
        return "choice"
    kind = input_spec[0] if isinstance(input_spec, list) and input_spec else None
    if kind == "FLOAT":
        return "float"
    if kind == "INT":
        return "int"
    if isinstance(value, bool) or kind == "BOOLEAN":
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "string"
