"""
comfy_client.py - minimal ComfyUI API client.

Standard library only, so it runs unchanged inside Nuke's Python.

Workflow templates are API-format exports ("Save (API)") whose patchable
nodes carry `nuke_*` titles. Nodes are found by title, never by ID, so
re-exporting the template doesn't break anything.

CLI test:
    python comfy_client.py basic_t2i_qwen_api_v002.json \
        --prompt "An engagement ring in a ring box. Plain grey background." \
        --width 1408 --height 736 --out ./ring_v001.png
"""

import argparse
import copy
import json
import math
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_HOST = "127.0.0.1:8188"
PATH_LOADER = "NukeLoadImagePath"   # custom node in comfy_nuke_nodes/


class ComfyError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Template handling
# --------------------------------------------------------------------------

def load_template(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def find_node(workflow, title):
    """Return the node ID whose _meta.title == title. Exactly one must match."""
    matches = [nid for nid, node in workflow.items()
               if node.get("_meta", {}).get("title") == title]
    if not matches:
        raise ComfyError(f"No node titled '{title}' in template")
    if len(matches) > 1:
        raise ComfyError(f"Multiple nodes titled '{title}': {matches}")
    return matches[0]


def set_input(workflow, title, key, value):
    """Set an input on a titled node. Replaces a link if one was there."""
    nid = find_node(workflow, title)
    inputs = workflow[nid]["inputs"]
    if key not in inputs:
        raise ComfyError(f"Node '{title}' has no input '{key}' "
                         f"(has: {sorted(inputs)})")
    inputs[key] = value


def remove_node(workflow, node_id):
    """Delete a node and every link that points at it."""
    workflow.pop(node_id, None)
    for node in workflow.values():
        inputs = node.get("inputs", {})
        for key in [k for k, v in inputs.items()
                    if isinstance(v, list) and len(v) == 2 and v[0] == node_id]:
            del inputs[key]


def image_slots(workflow, prefix="nuke_input_"):
    """Node IDs of nuke_input_1, nuke_input_2, ... in order (stops at first gap)."""
    slots, i = [], 1
    while True:
        try:
            slots.append(find_node(workflow, "%s%d" % (prefix, i)))
        except ComfyError:
            return slots
        i += 1


def bind_images(workflow, images, mode="upload", prefix="nuke_input_"):
    """Fill nuke_input_1..N in order and prune the unused slots (node + links),
    so a template with N image slots accepts 1..N images.

    mode "upload": images are names returned by ComfyClient.upload_image.
    mode "path":   images are absolute paths; each slot's LoadImage is swapped
                   for the NukeLoadImagePath custom node (same outputs), so
                   nothing is copied into Comfy's input dir.
    """
    slots = image_slots(workflow, prefix)
    if not slots:
        raise ComfyError("Template has no '%s1' image input" % prefix)
    if len(images) > len(slots):
        raise ComfyError("Got %d images but template only has %d slots"
                         % (len(images), len(slots)))
    for nid, value in zip(slots, images):
        if mode == "path":
            workflow[nid] = {"inputs": {"path": value}, "class_type": PATH_LOADER,
                             "_meta": workflow[nid].get("_meta", {})}
        else:
            workflow[nid]["inputs"]["image"] = value
    for nid in slots[len(images):]:
        remove_node(workflow, nid)
    return len(slots)


def generation_size(target_w, target_h, megapixels=1.0, multiple=16):
    """Same aspect as the target, scaled to a megapixel budget, snapped."""
    aspect = target_w / float(target_h)
    h = math.sqrt(megapixels * 1024 * 1024 / aspect)
    w = h * aspect
    snap = lambda v: max(multiple, int(round(v / multiple)) * multiple)
    return snap(w), snap(h)


def build_qwen_t2i(template, prompt, width, height, seed=None, steps=None,
                   enhance=False, filename_prefix="nuke/qwen"):
    """Patch a copy of the Qwen t2i template. Returns (workflow, seed)."""
    wf = copy.deepcopy(template)
    if seed is None:
        seed = random.randint(0, 2**32 - 1)

    set_input(wf, "nuke_prompt", "value", prompt)
    set_input(wf, "nuke_enhance_switch", "switch", bool(enhance))
    set_input(wf, "nuke_ksampler", "seed", int(seed))
    set_input(wf, "nuke_generate_text", "sampling_mode.seed", int(seed))
    if steps is not None:
        set_input(wf, "nuke_ksampler", "steps", int(steps))
    set_input(wf, "nuke_latent", "width", int(width))
    set_input(wf, "nuke_latent", "height", int(height))
    set_input(wf, "nuke_save", "filename_prefix", filename_prefix)
    return wf, seed


def build_qwen_edit(template, prompt, images, width=None, height=None,
                    seed=None, steps=None, enhance=False, image_mode="upload",
                    filename_prefix="nuke/qwen_edit"):
    """Patch a copy of the Qwen edit template. images are upload names or
    absolute paths (see bind_images / image_mode), in slot order. If width/height are
    given, nuke_size_switch routes the Empty Latent in; otherwise the output
    follows reference image 1's size."""
    if not images:
        raise ComfyError("Edit template needs at least one input image")
    wf = copy.deepcopy(template)
    if seed is None:
        seed = random.randint(0, 2**32 - 1)

    set_input(wf, "nuke_prompt", "value", prompt)
    set_input(wf, "nuke_enhance_switch", "switch", bool(enhance))
    set_input(wf, "nuke_ksampler", "seed", int(seed))
    set_input(wf, "nuke_generate_text", "sampling_mode.seed", int(seed))
    if steps is not None:
        set_input(wf, "nuke_ksampler", "steps", int(steps))
    explicit = width is not None and height is not None
    set_input(wf, "nuke_size_switch", "switch", explicit)
    if explicit:
        set_input(wf, "nuke_latent", "width", int(width))
        set_input(wf, "nuke_latent", "height", int(height))
    set_input(wf, "nuke_save", "filename_prefix", filename_prefix)
    bind_images(wf, images, mode=image_mode)
    return wf, seed


# --------------------------------------------------------------------------
# Server communication
# --------------------------------------------------------------------------

class ComfyClient:
    def __init__(self, host=DEFAULT_HOST):
        self.base = f"http://{host}"
        self.client_id = str(uuid.uuid4())

    def _get_json(self, path):
        with urllib.request.urlopen(self.base + path, timeout=30) as r:
            return json.loads(r.read())

    def ping(self):
        """Raise ComfyError if the server isn't reachable."""
        try:
            self._get_json("/system_stats")
        except (urllib.error.URLError, OSError) as e:
            raise ComfyError(f"ComfyUI not reachable at {self.base}: {e}")

    def submit(self, workflow):
        """Queue a workflow. Returns prompt_id."""
        body = json.dumps({"prompt": workflow,
                           "client_id": self.client_id}).encode("utf-8")
        req = urllib.request.Request(self.base + "/prompt", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())["prompt_id"]
        except urllib.error.HTTPError as e:
            # Validation errors come back as 400 with node_errors in the body
            detail = e.read().decode("utf-8", "replace")
            raise ComfyError(f"Comfy rejected the workflow ({e.code}):\n{detail}")

    def wait(self, prompt_id, timeout=900, poll=1.0, should_cancel=None):
        """Poll /history until the job finishes. Returns its history entry."""
        start = time.time()
        while True:
            if should_cancel and should_cancel():
                self.interrupt()
                raise ComfyError("Cancelled")
            hist = self._get_json(f"/history/{prompt_id}")
            if prompt_id in hist:
                entry = hist[prompt_id]
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    msgs = [m for m in status.get("messages", [])
                            if m[0] == "execution_error"]
                    raise ComfyError(f"Execution failed: {msgs or status}")
                if status.get("completed", True):
                    return entry
            if time.time() - start > timeout:
                raise ComfyError(f"Timed out after {timeout}s")
            time.sleep(poll)

    def interrupt(self):
        req = urllib.request.Request(self.base + "/interrupt", data=b"",
                                     method="POST")
        try:
            urllib.request.urlopen(req, timeout=10).close()
        except urllib.error.URLError:
            pass

    def has_node(self, class_type):
        """True if the server has this node class installed."""
        try:
            info = self._get_json("/object_info/" + urllib.parse.quote(class_type))
            return class_type in info
        except (urllib.error.URLError, ValueError):
            return False

    def upload_image(self, path, subfolder="nuke", name=None, overwrite=True):
        """Upload a local image to Comfy's input dir. Returns the value a
        LoadImage node expects, e.g. "nuke/ComfyEdit1_in1.png"."""
        name = name or os.path.basename(path)
        with open(path, "rb") as f:
            data = f.read()
        boundary = "----comfynuke" + uuid.uuid4().hex
        parts = []
        def field(k, v):
            parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                          % (boundary, k, v)).encode("utf-8"))
        field("subfolder", subfolder)
        field("type", "input")
        field("overwrite", "true" if overwrite else "false")
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"image\"; "
                      "filename=\"%s\"\r\nContent-Type: application/octet-stream\r\n\r\n"
                      % (boundary, name)).encode("utf-8") + data + b"\r\n")
        parts.append(("--%s--\r\n" % boundary).encode("utf-8"))
        req = urllib.request.Request(
            self.base + "/upload/image", data=b"".join(parts),
            headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                info = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise ComfyError("Upload failed (%s): %s"
                             % (e.code, e.read().decode("utf-8", "replace")))
        sub = info.get("subfolder", "")
        return (sub + "/" if sub else "") + info["name"]

    def download(self, image_ref, dest_path):
        """Fetch an output image via /view and write it to dest_path."""
        query = urllib.parse.urlencode({
            "filename": image_ref["filename"],
            "subfolder": image_ref.get("subfolder", ""),
            "type": image_ref.get("type", "output"),
        })
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        with urllib.request.urlopen(f"{self.base}/view?{query}", timeout=60) as r, \
                open(dest_path, "wb") as f:
            f.write(r.read())
        return dest_path


# --------------------------------------------------------------------------
# Reading results
# --------------------------------------------------------------------------

def output_images(entry, workflow, save_title="nuke_save"):
    """Image refs ({filename, subfolder, type}) produced by the titled save node."""
    node_out = entry.get("outputs", {}).get(find_node(workflow, save_title), {})
    # SaveImage reports under "images"; scan all keys in case an advanced
    # save node uses a different one.
    refs = []
    for value in node_out.values():
        if isinstance(value, list):
            refs += [v for v in value if isinstance(v, dict) and "filename" in v]
    return refs


def output_text(entry, node_id):
    """Text shown by a PreviewAny node (e.g. the prompt actually used)."""
    node_out = entry.get("outputs", {}).get(node_id, {})
    for value in node_out.values():
        if isinstance(value, list) and value and isinstance(value[0], str):
            return "".join(value)
    return None


def find_node_by_class(workflow, class_type):
    for nid, node in workflow.items():
        if node.get("class_type") == class_type:
            return nid
    return None


# --------------------------------------------------------------------------
# One-call convenience + CLI
# --------------------------------------------------------------------------

def run_workflow(client, wf, out_path, info, should_cancel=None, log=print):
    """Submit an already-patched workflow, wait, download, write sidecar."""
    t0 = time.time()
    pid = client.submit(wf)
    log("Queued %s" % pid)
    entry = client.wait(pid, should_cancel=should_cancel)
    refs = output_images(entry, wf)
    if not refs:
        raise ComfyError("Job finished but no images found. Outputs: %s"
                         % list(entry.get("outputs", {})))
    client.download(refs[0], out_path)
    preview_id = find_node_by_class(wf, "PreviewAny")
    used_prompt = output_text(entry, preview_id) if preview_id else None

    result = dict(info, path=out_path, used_prompt=used_prompt, prompt_id=pid,
                  seconds=round(time.time() - t0, 1))
    with open(os.path.splitext(out_path)[0] + ".json", "w", encoding="utf-8") as f:
        json.dump({"result": result, "workflow": wf}, f, indent=2)
    log("Done in %ss -> %s" % (result["seconds"], out_path))
    return result


def generate(template_path, prompt, out_path, width=1024, height=1024,
             seed=None, steps=None, enhance=False, host=DEFAULT_HOST,
             should_cancel=None, log=print):
    """Text to image, end to end."""
    client = ComfyClient(host)
    client.ping()
    wf, seed = build_qwen_t2i(load_template(template_path), prompt, width, height,
                              seed=seed, steps=steps, enhance=enhance)
    log("Submitting %dx%d, seed %d" % (width, height, seed))
    info = dict(prompt=prompt, seed=seed, width=width, height=height,
                enhance=enhance)
    return run_workflow(client, wf, out_path, info, should_cancel, log)


def generate_edit(template_path, prompt, images, out_path, width=None,
                  height=None, seed=None, steps=None, enhance=False,
                  host=DEFAULT_HOST, input_mode="auto", upload_names=None,
                  should_cancel=None, log=print):
    """Image edit, end to end. images = local file paths in slot order.

    input_mode: "path"   - Comfy reads the files where they are (needs the
                           NukeLoadImagePath custom node and a shared disk)
                "upload" - copy them into Comfy's input dir via /upload/image
                "auto"   - path if the custom node is installed, else upload
    """
    client = ComfyClient(host)
    client.ping()

    mode = input_mode
    has_loader = client.has_node(PATH_LOADER)
    if mode == "auto":
        mode = "path" if has_loader else "upload"
    if mode == "path" and not has_loader:
        raise ComfyError("input_mode 'path' needs the %s custom node in "
                         "ComfyUI/custom_nodes (restart Comfy after installing)"
                         % PATH_LOADER)

    if mode == "path":
        refs = [os.path.abspath(p) for p in images]
    else:
        refs = []
        for i, path in enumerate(images):
            name = upload_names[i] if upload_names else None
            log("Uploading %s" % os.path.basename(path))
            refs.append(client.upload_image(path, name=name))

    wf, seed = build_qwen_edit(load_template(template_path), prompt, refs,
                               width, height, seed=seed, steps=steps,
                               enhance=enhance, image_mode=mode)
    log("Submitting %d image(s) via %s, seed %d" % (len(refs), mode, seed))
    info = dict(prompt=prompt, seed=seed, width=width, height=height,
                enhance=enhance, images=[os.path.abspath(p) for p in images],
                image_mode=mode)
    return run_workflow(client, wf, out_path, info, should_cancel, log)


def main():
    p = argparse.ArgumentParser(description="Run a ComfyUI API template.")
    p.add_argument("template")
    p.add_argument("--prompt", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--width", type=int)
    p.add_argument("--height", type=int)
    p.add_argument("--seed", type=int)
    p.add_argument("--steps", type=int)
    p.add_argument("--enhance", action="store_true")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--image", action="append", default=[],
                   help="Input image (repeatable) - switches to edit mode")
    p.add_argument("--input-mode", default="auto", choices=["auto", "path", "upload"])
    a = p.parse_args()

    if not a.image and (a.width is None or a.height is None):
        a.width, a.height = a.width or 1024, a.height or 1024
    if a.image:
        r = generate_edit(a.template, a.prompt, a.image, a.out,
                          a.width, a.height, a.seed, a.steps, a.enhance, a.host,
                          input_mode=a.input_mode)
    else:
        r = generate(a.template, a.prompt, a.out, a.width, a.height,
                     a.seed, a.steps, a.enhance, a.host)
    if r["used_prompt"] and a.enhance:
        print("\nEnhanced prompt:\n" + r["used_prompt"])


if __name__ == "__main__":
    main()
