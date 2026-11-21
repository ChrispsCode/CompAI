"""
crop.py - CompAI Crop and CompAI Uncrop, for working on part of a plate.

    plate -> CompAI Crop -> (image node) -> CompAI Uncrop -> Merge over plate

The Crop cuts out a box whose sides are multiples of a model's grid, so the
image can go to the model as it is. It never scales: put a Transform above
it for that. The Uncrop puts a result back where the box came from, pixel
for pixel, on the plate's format, ready to merge over it.

Both are Groups. All the geometry is knob expressions: the crop box is
computed from the drawn box by expressions on the Crop, and the Uncrop links
to those. Python only fills in the model list, the grid and the info lines,
so a saved script renders without compai and stays right on undo and when
the plate changes.
"""

import nuke

from . import grid

CROP_TAB = "compai_crop"          # tab knobs double as the "what is this" marker
UNCROP_TAB = "compai_uncrop"
TILE_COLOR = 0x4F8A8BFF
CROP_CALL = "import compai; compai.%s(nuke.thisNode())"

_last_model = None                # new crops start on the last model picked


# --------------------------------------------------------------------------
# Expressions
# --------------------------------------------------------------------------

# On the Crop group itself. snap is read as its index in grid.SNAP_MODES.
# Each must match grid.snap_box(); the headless tests check that they do.
_STEPS = "(box.{hi} - box.{lo}) / multiple"
_SIDE = ("max(1, snap == 0 ? ceil({s} - 1e-6) : snap == 1 ? floor({s} + 0.5)"
         " : floor({s} + 1e-6)) * multiple")
_CENTRED = "floor((box.{lo} + box.{hi} - {side}) / 2 + 0.5)"
# this.Input1 is the group's own Input node: unlike input.width it never
# errors, and with nothing connected it gives the project format.
_PLACED = ("keep_inside && {side} <= this.Input1.{dim} ? "
           "clamp({c}, 0, this.Input1.{dim} - {side}) : {c}")


def _side_expr(lo, hi):
    return _SIDE.format(s=_STEPS.format(lo=lo, hi=hi))


def _pos_expr(lo, hi, side, dim):
    c = _CENTRED.format(lo=lo, hi=hi, side=side)
    return _PLACED.format(side=side, dim=dim, c=c)


CROP_EXPRESSIONS = {
    "crop_w": _side_expr("x", "r"),
    "crop_h": _side_expr("y", "t"),
    "crop_x": _pos_expr("x", "r", "crop_w", "width"),
    "crop_y": _pos_expr("y", "t", "crop_h", "height"),
    "plate_w": "this.Input1.width",
    "plate_h": "this.Input1.height",
}

# Uncrop knob -> Crop knob it follows
UNCROP_LINKS = (("x", "crop_x"), ("y", "crop_y"), ("w", "crop_w"),
                ("h", "crop_h"), ("plate_w", "plate_w"), ("plate_h", "plate_h"))


# --------------------------------------------------------------------------
# CompAI Crop
# --------------------------------------------------------------------------

def create_crop():
    """Create a CompAI Crop below the selected node, boxing the middle of
    its image at the model's default size."""
    nuke.Undo.begin("Create CompAI Crop")
    try:
        node = _create_group()
        node.setName("CompAICrop1")
        node["tile_color"].setValue(TILE_COLOR)
        node.begin()
        inp = nuke.nodes.Input(name="Input1")
        cut = nuke.nodes.Crop(name="CutOut", inputs=[inp])
        cut["reformat"].setValue(True)    # output format = the box, at 0,0
        cut["crop"].setValue(False)       # no black edge: bbox = the box
        for i, expr in enumerate(("parent.crop_x", "parent.crop_y",
                                  "parent.crop_x + parent.crop_w",
                                  "parent.crop_y + parent.crop_h")):
            cut["box"].setExpression(expr, i)
        nuke.nodes.Output(inputs=[cut])
        node.end()

        for knob in _crop_knobs():
            node.addKnob(knob)
        for name, expr in CROP_EXPRESSIONS.items():
            node[name].setExpression(expr)

        _fill_models(node, _last_model)
        _apply_model(node)
        source = node.input(0)
        fmt = source.format() if source is not None else nuke.root().format()
        wf = _workflow(node)
        node["box"].setValue(list(grid.default_box(
            fmt.width(), fmt.height(), node["multiple"].value(),
            wf.megapixels if wf is not None else 1.0)))
        node["knobChanged"].setValue("import compai; compai.on_crop_changed()")
        _update_crop_info(node)
    finally:
        nuke.Undo.end()
    if nuke.GUI:
        nuke.show(node)        # the box handles only show with the panel open
    return node


def _crop_knobs():
    model = nuke.Enumeration_Knob("model", "for model", [grid.MANUAL])
    model.setTooltip(
        "The workflow this crop feeds. Its image inputs must be multiples "
        "of its grid (node.json size.multiple), and the crop snaps to that. "
        "manual: set the grid yourself.")
    guess = nuke.PyScript_Knob("guess", "guess", CROP_CALL % "on_crop_guess")
    guess.setTooltip("Pick the model from the first CompAI node below this one.")
    refresh = nuke.PyScript_Knob("refresh", "refresh",
                                 CROP_CALL % "on_crop_refresh")
    refresh.setTooltip("Rescan the installed workflows.")
    multiple = nuke.Int_Knob("multiple", "grid")
    multiple.setRange(1, 128)
    multiple.setValue(grid.DEFAULT_MULTIPLE)
    multiple.setTooltip("Both sides of the crop are snapped to multiples of "
                        "this many pixels.")
    multiple.setFlag(nuke.STARTLINE)

    box = nuke.BBox_Knob("box", "area")
    box.setTooltip("Draw the area you want to work on. The crop is this box "
                   "resized about its centre to the grid (see 'crop box').")
    snap = nuke.Enumeration_Knob("snap", "snap", list(grid.SNAP_MODES))
    snap.setTooltip("grow: round each side up, so everything in the area is "
                    "kept.\nnearest: round to the nearest multiple.\n"
                    "shrink: round down.")
    inside = nuke.Boolean_Knob("keep_inside", "keep inside image", True)
    inside.setTooltip("Slide the crop back inside the image when snapping "
                      "pushes it over an edge (if it fits).")
    inside.clearFlag(nuke.STARTLINE)

    crop_knobs = []
    for name, label in (("crop_x", "crop box"), ("crop_y", "y"),
                        ("crop_w", "w"), ("crop_h", "h")):
        k = nuke.Int_Knob(name, label)
        k.setEnabled(False)
        k.setTooltip("The box actually cut out, in plate pixels: position "
                     "and size. CompAI Uncrop links to these.")
        if name != "crop_x":
            k.clearFlag(nuke.STARTLINE)
        crop_knobs.append(k)
    plate = []
    for name in ("plate_w", "plate_h"):
        k = nuke.Int_Knob(name, name)
        k.setFlag(nuke.INVISIBLE)
        plate.append(k)

    make = nuke.PyScript_Knob("make_uncrop", "Create Uncrop",
                              CROP_CALL % "on_crop_make_uncrop")
    make.setTooltip("Make a CompAI Uncrop linked to this crop, below the "
                    "CompAI node this crop feeds if there is one.")
    make.setFlag(nuke.STARTLINE)

    return ([nuke.Tab_Knob(CROP_TAB, "CompAI Crop"), model, guess, refresh,
             multiple, box, snap, inside, nuke.Text_Knob("div_crop", ""),
             *crop_knobs, *plate,
             nuke.Text_Knob("output_info", "output", ""),
             nuke.Text_Knob("model_info", "model", ""),
             nuke.Text_Knob("div_uncrop", ""), make])


def on_crop_changed():
    node, knob = nuke.thisNode(), nuke.thisKnob()
    if not _alive(node):
        return
    name = knob.name()
    if name == "model":
        _apply_model(node)
    if name in ("model", "multiple", "box", "snap", "keep_inside",
                "inputChange", "showPanel"):
        _update_crop_info(node)


def on_crop_refresh(node):
    _fill_models(node, node["model"].value())
    _apply_model(node)
    _update_crop_info(node)


def on_crop_guess(node):
    below = _workflow_node_below(node)
    if below is None:
        nuke.message("No CompAI node found below %s." % node.name())
        return
    name = below["comfy_workflow"].value()
    if name not in node["model"].values():
        _fill_models(node, node["model"].value())
    if name not in node["model"].values():
        nuke.message("%s runs '%s', which takes no images, so there is "
                     "nothing to crop for." % (below.name(), name))
        return
    node["model"].setValue(name)
    _apply_model(node)
    _update_crop_info(node)


def on_crop_make_uncrop(node):
    below = _workflow_node_below(node)
    with node.parent():
        uncrop = _build_uncrop(nuke.nodes.Group())
        if below is not None:
            uncrop.setInput(0, below)
            uncrop.setXYpos(below.xpos(), below.ypos() + 80)
        else:
            uncrop.setXYpos(node.xpos() + 150, node.ypos())
    link_uncrop(uncrop, node)
    return uncrop


def _alive(node):
    """False while Nuke tears the node down: closing a script fires
    inputChange on nodes that are already gone (17.1)."""
    try:
        node.name()
    except ValueError:
        return False
    return True


def _create_group():
    """A Group below the selected node, like any node from the menu.
    createNode connects it in the GUI but not in terminal mode, so connect
    it here too."""
    selected = nuke.selectedNodes()
    node = nuke.createNode("Group", inpanel=False)
    if node.input(0) is None and len(selected) == 1:
        node.setInput(0, selected[0])
    return node


def is_crop(node):
    return node is not None and node.knob(CROP_TAB) is not None


def _fill_models(node, keep=None):
    """Model list: the installed workflows with a grid, then manual. A model
    the script was saved with stays listed even if it's gone, so its grid
    (kept in the multiple knob) isn't lost."""
    names = [wf.name for wf in grid.targets()]
    if keep and keep != grid.MANUAL and keep not in names:
        names.insert(0, keep)
    names.append(grid.MANUAL)
    node["model"].setValues(names)
    node["model"].setValue(keep if keep in names else names[0])


def _workflow(node):
    """The crop's workflow, or None for manual or one that can't load."""
    name = node["model"].value()
    if name == grid.MANUAL:
        return None
    for wf in grid.targets():
        if wf.name == name:
            return wf
    return None


def _apply_model(node):
    """Take the grid from the model; show the grid knob only for manual."""
    global _last_model
    name = node["model"].value()
    _last_model = name
    wf = _workflow(node)
    if wf is not None:
        node["multiple"].setValue(wf.multiple)
    node["multiple"].setVisible(name == grid.MANUAL)


def _update_crop_info(node):
    nuke.Undo.disable()            # derived text, not a user change
    try:
        if node.input(0) is None:
            output = "no input"
            model = grid.describe(1, 1, _workflow(node),
                                  node["multiple"].value())[1]
        else:
            output, model = grid.describe(
                node["crop_w"].value(), node["crop_h"].value(),
                _workflow(node), node["multiple"].value())
        node["output_info"].setValue(output)
        node["model_info"].setValue(model)
    finally:
        nuke.Undo.enable()


def _workflow_node_below(node, limit=200):
    """First CompAI workflow node downstream, breadth first."""
    seen, queue = set(), [node]
    with node.parent():
        while queue and len(seen) < limit:
            for n in queue.pop(0).dependent(nuke.INPUTS | nuke.HIDDEN_INPUTS,
                                            forceEvaluate=False):
                if n.fullName() in seen:
                    continue
                seen.add(n.fullName())
                if n.knob("comfy_workflow") is not None:
                    return n
                queue.append(n)
    return None


# --------------------------------------------------------------------------
# CompAI Uncrop
# --------------------------------------------------------------------------

def create_uncrop():
    """Create a CompAI Uncrop below the selected node, linked to the first
    CompAI Crop above it."""
    nuke.Undo.begin("Create CompAI Uncrop")
    try:
        uncrop = _build_uncrop(_create_group())
        crop = _crop_above(uncrop)
        if crop is not None:
            link_uncrop(uncrop, crop)
        else:                      # something sensible until it's linked
            fmt = nuke.root().format()
            uncrop["plate_w"].setValue(fmt.width())
            uncrop["plate_h"].setValue(fmt.height())
            source = uncrop.input(0)
            if source is not None:
                uncrop["w"].setValue(source.width())
                uncrop["h"].setValue(source.height())
        _update_uncrop_info(uncrop)
    finally:
        nuke.Undo.end()
    return uncrop


def link_uncrop(uncrop, crop):
    """Link the Uncrop's placement to a Crop by expressions, so moving the
    crop moves the uncrop. Type over a value to break its link."""
    for knob, source in UNCROP_LINKS:
        uncrop[knob].setExpression("%s.%s" % (crop.name(), source))
    _update_uncrop_info(uncrop)


def _build_uncrop(node):
    """Turn a new Group into an Uncrop:
    Input -> Fit (only if the result isn't the crop's size) -> Alpha ->
    ToPlate (plate format, image left at 0,0) -> Place (whole-pixel move)
    -> Edge (cut to the box, optional feather) -> Output."""
    node.setName("CompAIUncrop1")
    node["tile_color"].setValue(TILE_COLOR)
    node.begin()
    inp = nuke.nodes.Input(name="Input1")
    fit = nuke.nodes.Reformat(name="Fit", inputs=[inp])
    fit["type"].setValue("to box")
    fit["box_fixed"].setValue(True)
    fit["box_width"].setExpression("max(1, parent.w)")
    fit["box_height"].setExpression("max(1, parent.h)")
    fit["resize"].setValue("distort")
    fit["filter"].setValue("Lanczos4")
    fit["disable"].setExpression("input.width == parent.w && "
                                 "input.height == parent.h")
    alpha = nuke.nodes.Expression(name="Alpha", inputs=[fit])
    alpha["channel3"].setValue("alpha")
    alpha["expr3"].setValue("1")
    alpha["disable"].setExpression("!parent.solid_alpha")
    to_plate = nuke.nodes.Reformat(name="ToPlate", inputs=[alpha])
    to_plate["type"].setValue("to box")
    to_plate["box_fixed"].setValue(True)
    to_plate["box_width"].setExpression("max(1, parent.plate_w)")
    to_plate["box_height"].setExpression("max(1, parent.plate_h)")
    to_plate["resize"].setValue("none")
    to_plate["center"].setValue(False)
    to_plate["black_outside"].setValue(False)
    to_plate["pbb"].setValue(True)
    place = nuke.nodes.Position(name="Place", inputs=[to_plate])
    place["translate"].setExpression("parent.x", 0)
    place["translate"].setExpression("parent.y", 1)
    edge = nuke.nodes.Crop(name="Edge", inputs=[place])
    for i, expr in enumerate(("parent.x", "parent.y", "parent.x + parent.w",
                              "parent.y + parent.h")):
        edge["box"].setExpression(expr, i)
    edge["softness"].setExpression("parent.feather")
    nuke.nodes.Output(inputs=[edge])
    node.end()

    for knob in _uncrop_knobs():
        node.addKnob(knob)
    node["knobChanged"].setValue("import compai; compai.on_uncrop_changed()")
    return node


def _uncrop_knobs():
    knobs = [nuke.Tab_Knob(UNCROP_TAB, "CompAI Uncrop"),
             nuke.Text_Knob("link_info", "crop", "")]
    for name, label, tip in (
            ("x", "position", "Where the crop's bottom-left corner sits on "
                              "the plate."),
            ("y", "", ""),
            ("w", "size", "The crop's size. A result of another size is "
                          "scaled to fit it."),
            ("h", "", ""),
            ("plate_w", "plate", "The plate's size: the format the result "
                                 "is placed on."),
            ("plate_h", "", "")):
        k = nuke.Int_Knob(name, label)
        if tip:
            k.setTooltip(tip + " Linked to the crop; type a value to break "
                               "the link.")
        else:
            k.clearFlag(nuke.STARTLINE)
        knobs.append(k)
    alpha = nuke.Boolean_Knob("solid_alpha", "solid alpha", True)
    alpha.setTooltip("Give the result an alpha of 1 over the whole box, so "
                     "it covers the plate when merged over it.")
    alpha.setFlag(nuke.STARTLINE)
    feather = nuke.Double_Knob("feather", "feather")
    feather.setRange(0, 50)
    feather.setTooltip("Soften the box's edges by this many pixels, inwards.")
    knobs += [nuke.Text_Knob("div_result", ""), alpha, feather,
              nuke.Text_Knob("result_info", "result", "")]
    return knobs


def on_uncrop_changed():
    node, knob = nuke.thisNode(), nuke.thisKnob()
    if not _alive(node):
        return
    if knob.name() in ("inputChange", "showPanel", "x", "y", "w", "h",
                       "plate_w", "plate_h"):
        _update_uncrop_info(node)


def _crop_above(node, limit=200):
    """First CompAI Crop up the B pipes (input 0) from node."""
    current = node.input(0)
    for _ in range(limit):
        if current is None or is_crop(current):
            return current
        current = current.input(0)
    return None


def _linked_crop_name(node):
    knob = node["x"]
    if not knob.hasExpression():
        return None
    return knob.animation(0).expression().split(".")[0]


def _update_uncrop_info(node):
    nuke.Undo.disable()            # derived text, not a user change
    try:
        name = _linked_crop_name(node)
        node["link_info"].setValue(
            name or "<font color='#e8a33d'>not linked: use Create Uncrop on a "
                    "CompAI Crop, or set the numbers below</font>")
        w, h = node["w"].value(), node["h"].value()
        source = node.input(0)
        if source is None:
            text = "no input"
        elif (source.width(), source.height()) == (w, h):
            text = "%d × %d, placed 1:1" % (w, h)
        else:
            text = ("<font color='#e8a33d'>%d × %d, scaled to %d × %d"
                    "</font>" % (source.width(), source.height(), w, h))
        node["result_info"].setValue(text)
    finally:
        nuke.Undo.enable()
