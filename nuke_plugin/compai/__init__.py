"""
compai - run AI generation workflows from Nuke nodes.

The functions here are the stable entry points. Nuke saves knob callbacks
into scripts as strings such as "import compai; compai.on_generate(...)",
so these names must keep working. They import lazily, so the nuke-free
modules (comfy_client, workflows, cli) can be used from a plain shell.

    comfy_client   ComfyUI HTTP client and workflow-JSON primitives
    workflows      installed workflows: find, check, patch, run
    cli            command line: check / scaffold / list / run
    node           builds the Nuke node for a workflow       (imports nuke)
    jobs           background job runner behind Generate     (imports nuke)
    utility        CompAI Crop / Uncrop                      (crop imports nuke)
"""


def create(workflow):
    """Create the Nuke node for an installed workflow, by folder name."""
    from . import node
    return node.create(workflow)


def add_menu():
    from . import node
    node.add_menu()


def on_generate(n):
    from . import node
    node.on_generate(n)


def on_knob_changed():
    from . import node
    node.on_knob_changed()


def on_refresh_choices(n):
    from . import node
    node.on_refresh_choices(n)


def on_cancel(n):
    from . import jobs
    jobs.on_cancel(n)


def on_reuse_seed(n):
    from . import node
    node.on_reuse_seed(n)


def on_create_read(n):
    from . import node
    node.on_create_read(n)


def on_open_folder(n):
    from . import node
    node.on_open_folder(n)


# CompAI Crop / Uncrop (utility/crop.py)

def create_crop():
    from .utility import crop
    return crop.create_crop()


def create_uncrop():
    from .utility import crop
    return crop.create_uncrop()


def on_crop_changed():
    from .utility import crop
    crop.on_crop_changed()


def on_crop_guess(n):
    from .utility import crop
    crop.on_crop_guess(n)


def on_crop_refresh(n):
    from .utility import crop
    crop.on_crop_refresh(n)


def on_crop_make_uncrop(n):
    from .utility import crop
    return crop.on_crop_make_uncrop(n)


def on_uncrop_changed():
    from .utility import crop
    crop.on_uncrop_changed()


def reload():
    """Pick up code changes without restarting Nuke. Existing nodes use the
    new code straight away; nodes are only rebuilt by creating them again."""
    import importlib
    import sys
    for name in ("comfy_client", "workflows", "jobs", "node",
                 "utility.grid", "utility.crop"):
        module = sys.modules.get(__name__ + "." + name)
        if module is not None:
            importlib.reload(module)
