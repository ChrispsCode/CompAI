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

def generate(template_path, prompt, out_path, width=1024, height=1024,
             seed=None, steps=None, enhance=False, host=DEFAULT_HOST,
             should_cancel=None, log=print):
    """Run one generation end to end. Returns a dict describing the result."""
    client = ComfyClient(host)
    client.ping()

    wf, seed = build_qwen_t2i(load_template(template_path), prompt,
                              width, height, seed=seed, steps=steps,
                              enhance=enhance)
    t0 = time.time()
    pid = client.submit(wf)
    log(f"Queued {pid}  ({width}x{height}, seed {seed})")

    entry = client.wait(pid, should_cancel=should_cancel)
    refs = output_images(entry, wf)
    if not refs:
        raise ComfyError(f"Job finished but no images found. Outputs: "
                         f"{list(entry.get('outputs', {}))}")

    client.download(refs[0], out_path)
    preview_id = find_node_by_class(wf, "PreviewAny")
    used_prompt = output_text(entry, preview_id) if preview_id else None

    result = {"path": out_path, "seed": seed, "width": width, "height": height,
              "prompt": prompt, "used_prompt": used_prompt, "enhance": enhance,
              "prompt_id": pid, "seconds": round(time.time() - t0, 1)}

    # Provenance sidecar next to the image
    with open(os.path.splitext(out_path)[0] + ".json", "w", encoding="utf-8") as f:
        json.dump({"result": result, "workflow": wf}, f, indent=2)

    log(f"Done in {result['seconds']}s -> {out_path}")
    return result


def main():
    p = argparse.ArgumentParser(description="Run a ComfyUI API template.")
    p.add_argument("template")
    p.add_argument("--prompt", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--width", type=int, default=1024)
    p.add_argument("--height", type=int, default=1024)
    p.add_argument("--seed", type=int)
    p.add_argument("--steps", type=int)
    p.add_argument("--enhance", action="store_true")
    p.add_argument("--host", default=DEFAULT_HOST)
    a = p.parse_args()

    r = generate(a.template, a.prompt, a.out, a.width, a.height,
                 a.seed, a.steps, a.enhance, a.host)
    if r["used_prompt"] and a.enhance:
        print("\nEnhanced prompt:\n" + r["used_prompt"])


if __name__ == "__main__":
    main()
