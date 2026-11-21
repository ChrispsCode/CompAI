"""
grid.py - the maths behind CompAI Crop, kept free of nuke so it can be
tested from a shell.

The node itself snaps with knob expressions (crop.py), so it keeps working on
undo, when the plate upstream changes, and on the farm without compai.
snap_box() is the same rule in Python: it places new boxes, and the headless
tests check the two agree.
"""

import math

from .. import comfy_client, workflows
from ..comfy_client import ComfyError

MANUAL = "manual"
# Order matters: the node's expressions read the knob as an index.
SNAP_MODES = ("grow", "nearest", "shrink")
DEFAULT_MULTIPLE = 16
_EPS = 1e-6          # so a side that is already a multiple stays put


def snap_side(length, multiple, mode="grow"):
    """length rounded to a multiple of `multiple`, never below one step.
    grow rounds up (keeps everything you framed), shrink rounds down,
    nearest rounds half up."""
    steps = length / float(multiple)
    if mode == "grow":
        k = math.ceil(steps - _EPS)
    elif mode == "shrink":
        k = math.floor(steps + _EPS)
    else:
        k = math.floor(steps + 0.5)
    return max(1, int(k)) * multiple


def snap_box(box, multiple, mode="grow", bounds=None):
    """(x, y, w, h) in whole pixels: the box (x, y, r, t) resized about its
    centre so both sides are multiples, then moved inside bounds (width,
    height) if given and it fits."""
    x, y, r, t = box
    w = snap_side(r - x, multiple, mode)
    h = snap_side(t - y, multiple, mode)
    left = int(math.floor((x + r - w) / 2.0 + 0.5))
    bottom = int(math.floor((y + t - h) / 2.0 + 0.5))
    if bounds:
        bw, bh = bounds
        if w <= bw:
            left = min(max(left, 0), bw - w)
        if h <= bh:
            bottom = min(max(bottom, 0), bh - h)
    return left, bottom, w, h


def default_box(width, height, multiple, megapixels):
    """Starting box (x, y, r, t) for a new crop on a width x height image:
    centred, at about `megapixels` at the image's aspect, on the grid. Shrunk
    to fit if the image is smaller than that."""
    w, h = comfy_client.generation_size(width, height, megapixels, multiple)
    if w > width:
        w = snap_side(width, multiple, "shrink")
    if h > height:
        h = snap_side(height, multiple, "shrink")
    x, y = (width - w) // 2, (height - h) // 2
    return x, y, x + w, y + h


def targets():
    """Installed workflows a crop can be made for: any that takes images or
    videos, video ones included. Text-to-image workflows have no input to
    crop. Reference workflows send their inputs unresized, but their output
    is still on the model's grid, which is what the uncrop puts back."""
    out = []
    for name in workflows.names():
        try:
            wf = workflows.load_cached(name)
        except ComfyError:
            continue
        if wf.max_images or wf.max_videos:
            out.append(wf)
    return out


def describe(w, h, wf=None, multiple=None):
    """The two info lines under the crop: (output, model). The output line
    warns when the size is outside the model's megapixel range."""
    mp = w * h / float(1024 * 1024)
    output = "%d × %d px · %.2f MP" % (w, h, mp)
    if wf is None:
        return output, "manual · %d px grid" % (multiple or DEFAULT_MULTIPLE)
    lo, hi = wf.megapixels_range
    model = ("%s · %d px grid · %g–%g MP, default %g"
             % (wf.label, wf.multiple, lo, hi, wf.megapixels))
    if mp < lo or mp > hi:
        output += (" · <font color='#e8a33d'>outside %s's %g–%g MP</font>"
                   % (wf.label, lo, hi))
    return output, model
