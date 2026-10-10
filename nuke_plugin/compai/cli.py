"""
cli.py - command line for working with workflows, no Nuke needed.
Run it through workflow.py in the repo root:

    python3 workflow.py check inbox/my_export.json     is this export usable?
    python3 workflow.py scaffold inbox/my_export.json my_name
    python3 workflow.py check my_name                  is the install usable?
    python3 workflow.py list
    python3 workflow.py run my_name --prompt "..." --out ./test_v001.png
"""

import argparse
import json
import os
import shutil
import sys

from . import comfy_client, workflows
from .comfy_client import ComfyError


def _server_info(host):
    """/object_info, or None (with a note) if Comfy isn't running."""
    try:
        return comfy_client.ComfyClient(host).object_info(timeout=60)
    except (OSError, ValueError):
        print("  (ComfyUI not reachable at %s: server checks skipped)" % host)
        return None


def _report(problems):
    for p in problems:
        print("  x " + p)
    return not problems


def _describe(graph):
    """List what an export offers to drive: its nuke_* nodes and inputs."""
    for nid, node in graph.items():
        title = node.get("_meta", {}).get("title", "")
        if not title.startswith(workflows.TITLE_PREFIX):
            continue
        print("  %s  (%s)" % (title, node["class_type"]))
        for key, value in node["inputs"].items():
            if not workflows._is_link(value):
                text = json.dumps(value)
                print("      %s = %s" % (key, text if len(text) < 60
                                         else text[:57] + "..."))


def cmd_check(args):
    if os.path.isfile(args.target):
        print("Export: %s" % args.target)
        try:
            graph = comfy_client.load_template(args.target)
        except ValueError as e:
            print("  x Not valid JSON: %s" % e)
            return 1
        problems, output = workflows.lint_graph(graph), None
    else:
        wf = workflows.load(args.target)
        print("Installed workflow: %s (%s)" % (wf.name, wf.label))
        graph, problems, output = wf.graph, workflows.validate(wf), wf.output
    ok = _report(problems)
    if ok:
        info = _server_info(args.host)
        if info is not None:
            ok = _report(workflows.check_server(graph, info, output))
        print("Drivable nodes:")
        _describe(graph)
    print("PASS" if ok else "REJECTED")
    return 0 if ok else 1


def cmd_scaffold(args):
    graph = comfy_client.load_template(args.export)
    problems = workflows.lint_graph(graph)
    info = _server_info(args.host)
    if info is not None:
        problems += workflows.check_server(graph, info)
    if not _report(problems):
        print("REJECTED: fix the export in ComfyUI and re-export.")
        return 1
    folder = os.path.join(workflows.WORKFLOWS_DIR, args.name)
    if os.path.exists(folder):
        print("  x %s already exists." % folder)
        return 1
    os.makedirs(folder)
    shutil.move(args.export, os.path.join(folder, "workflow.json"))
    with open(os.path.join(folder, "node.json"), "w", encoding="utf-8") as f:
        json.dump(workflows.scaffold(graph, info), f, indent=2)
        f.write("\n")
    print("Created %s\nNow edit node.json: set label and node_name, prune "
          "the knobs, then run: check %s" % (folder, args.name))
    return 0


def cmd_list(args):
    for name in workflows.names():
        try:
            wf = workflows.load(name)
            problems = workflows.validate(wf)
        except ComfyError as e:
            print("%-24s BROKEN  %s" % (name, e))
            continue
        kind = "%d image input(s)" % wf.max_images if wf.max_images else "text only"
        print("%-24s %-20s %-18s knobs: %s%s"
              % (name, wf.label, kind,
                 ", ".join(k["name"] for k in wf.knobs) or "-",
                 "   [%d PROBLEM(S)]" % len(problems) if problems else ""))
    return 0


def cmd_run(args):
    values = {"prompt": args.prompt, "seed": args.seed,
              "width": args.width, "height": args.height}
    for item in args.set:
        key, _, value = item.partition("=")
        values[key] = value
    wf = workflows.load(args.name)
    for knob in wf.knobs:              # "true"/"0" etc. for bool knobs
        if knob["type"] == "bool" and isinstance(values.get(knob["name"]), str):
            values[knob["name"]] = values[knob["name"]].lower() in ("1", "true", "yes", "on")
    result = workflows.generate(args.name, values, args.out,
                                images=args.image or None, host=args.host,
                                input_mode=args.input_mode)
    if result.get("used_prompt") and result["used_prompt"] != args.prompt:
        print("\nPrompt used:\n" + result["used_prompt"])
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="CompAI workflow tools.")
    p.add_argument("--host", default=os.environ.get("COMFY_HOST",
                                                    comfy_client.DEFAULT_HOST))
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("check", help="lint an export file, or validate an "
                                     "installed workflow by name")
    c.add_argument("target")
    c.set_defaults(fn=cmd_check)

    c = sub.add_parser("scaffold", help="install an export as a new workflow "
                                        "with a draft node.json")
    c.add_argument("export")
    c.add_argument("name")
    c.set_defaults(fn=cmd_scaffold)

    c = sub.add_parser("list", help="installed workflows")
    c.set_defaults(fn=cmd_list)

    c = sub.add_parser("run", help="run an installed workflow on ComfyUI")
    c.add_argument("name")
    c.add_argument("--prompt", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--width", type=int, default=1024)
    c.add_argument("--height", type=int, default=1024)
    c.add_argument("--seed", type=int)
    c.add_argument("--set", action="append", default=[], metavar="KNOB=VALUE",
                   help="a workflow knob, e.g. --set steps=6 (repeatable)")
    c.add_argument("--image", action="append", default=[],
                   help="input image (repeatable)")
    c.add_argument("--input-mode", default="auto",
                   choices=["auto", "path", "upload"])
    c.set_defaults(fn=cmd_run)

    args = p.parse_args(argv)
    try:
        return args.fn(args)
    except ComfyError as e:
        print("Error: %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
