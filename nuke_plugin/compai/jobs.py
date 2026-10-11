"""
jobs.py - runs a generation on a background thread for a Nuke node.

Knobs are only touched on the main thread: the worker calls nuke-free code
and reports back through nuke.executeInMainThread.

A node using this needs the knobs generate, status, last_file, last_seed and
used_prompt, and an internal Read named ComfyRead. node.py provides them.
"""

import os
import shutil
import threading
import traceback

import nuke

# Jobs in flight, keyed by node fullName -> threading.Event (cancel flag)
_JOBS = {}


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


def start(node, fn, params, summary, cleanup=None):
    """Run fn(should_cancel=..., log=..., **params) in the background. fn
    returns a dict with at least path, seconds, and seed/prompt if used.
    cleanup is a directory to delete once the job ends, however it ends."""
    key = node.fullName()
    cancel = threading.Event()
    _JOBS[key] = cancel
    node["generate"].setEnabled(False)
    _status(key, summary)
    threading.Thread(target=_worker, args=(key, fn, params, cancel, cleanup),
                     name="ComfyJob:" + key, daemon=True).start()


def on_cancel(node):
    ev = _JOBS.get(node.fullName())
    if ev:
        ev.set()
        _status(node.fullName(), "cancelling...")


def _worker(key, fn, params, cancel, cleanup=None):
    try:
        try:
            result = fn(should_cancel=cancel.is_set,
                        log=lambda msg: _status(key, msg), **params)
        finally:
            if cleanup:                # before reporting, so it's gone by then
                shutil.rmtree(cleanup, ignore_errors=True)
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
    # Ask before entering the group: inside it, nuke.root() is the group.
    script_first = int(nuke.root()["first_frame"].value())
    with node:
        read = nuke.toNode("ComfyRead")
        colorspace = read["colorspace"].value()
        read["file"].fromUserText(result["path"])   # sets the range of a video
        read["colorspace"].setValue(colorspace)     # a new file type resets it
        if read["last"].value() > read["first"].value():
            # A clip starts at the script's first frame. Set as a number:
            # an expression in this knob is not evaluated.
            read["frame_mode"].setValue("start at")
            read["frame"].setValue(str(script_first))
        else:
            read["frame_mode"].setValue("expression")
            read["frame"].setValue("")
        read["reload"].execute()
    node["last_file"].setValue(os.path.basename(result["path"]))
    if result.get("seed") is not None:
        node["last_seed"].setValue(int(result["seed"]))
    node["used_prompt"].setValue(result.get("used_prompt")
                                 or result.get("prompt") or "")
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
