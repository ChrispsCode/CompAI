"""
Offline tests: workflow definitions, patching, the client against a fake
Comfy server, and the Nuke nodes against a fake nuke module.

    python3 -m unittest discover -s tests

None of this replaces a run in real Nuke against real Comfy. For the Nuke
API itself, test headless in real Nuke (see README).
"""

import copy
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, os.pardir, "nuke_plugin")
sys.path[:0] = [os.path.join(HERE, "nuke_stub"), PLUGIN, HERE]

import nuke                      # the stub
import compai
from compai import cli, comfy_client as cc, jobs, workflows
from fake_comfy import FakeComfy, IMAGE_BYTES

UNET_INFO = {"input": {"required": {"unet_name": [
    ["qwen_image_2.1.safetensors", "z_image_turbo_int8_convrot.safetensors",
     "z_image_base.safetensors"], {}]}}}


def dangling(graph):
    return [(nid, k) for nid, n in graph.items() for k, v in n["inputs"].items()
            if isinstance(v, list) and len(v) == 2 and v[0] not in graph]


def titled(graph, title):
    return graph[cc.find_node(graph, title)]


class InstalledWorkflows(unittest.TestCase):
    """Every workflow that ships must load and pass its own checks."""

    def test_all_valid(self):
        self.assertIn("z_image_turbo_t2i", workflows.names())
        for name in workflows.names():
            wf = workflows.load(name)
            self.assertEqual(workflows.validate(wf), [], name)
            self.assertTrue(wf.label and wf.node_name, name)

    def test_all_build_without_dangling_links(self):
        for name in workflows.names():
            wf = workflows.load(name)
            images = ["/x/in%d.png" % i for i in range(wf.max_images)] or None
            graph = workflows.build(
                wf, dict(prompt="p", seed=7, width=1408, height=736), images)
            self.assertEqual(dangling(graph), [], name)
            self.assertEqual(graph[cc.find_node(graph, wf.output)]
                             ["inputs"]["filename_prefix"], "nuke/" + name)
            self.assertEqual(wf.graph, workflows.load(name).graph)  # untouched


class Patching(unittest.TestCase):
    def test_generation_size(self):
        self.assertEqual(cc.generation_size(1024, 1024), (1024, 1024))
        w, h = cc.generation_size(2048, 1080)
        self.assertEqual((w % 16, h % 16), (0, 0))
        self.assertAlmostEqual(w / h, 2048 / 1080, delta=0.05)

    def test_generation_size_keeps_a_size_already_at_budget(self):
        for mp in (0.5, 1.0, 1.7, 2.5):
            for fmt in ((1920, 1080), (2048, 858), (1080, 1920), (4096, 3112),
                        (1234, 700), (1000, 1000)):
                size = cc.generation_size(fmt[0], fmt[1], mp)
                self.assertEqual(cc.generation_size(size[0], size[1], mp), size)
        self.assertEqual(cc.generation_size(1024, 1040), (1024, 1040))  # not squared up
        self.assertNotEqual(cc.generation_size(1920, 1088), (1920, 1088))  # over budget
        self.assertNotEqual(cc.generation_size(512, 512), (512, 512))      # under budget

    def test_z_image(self):
        wf = workflows.load("z_image_turbo_t2i")
        g = workflows.build(wf, dict(prompt="a ring", seed=7, width=1408,
                                     height=736, steps="6", model="other.sft"))
        self.assertEqual(titled(g, "nuke_prompt")["inputs"]["value"], "a ring")
        self.assertEqual(titled(g, "nuke_ksampler")["inputs"]["seed"], 7)
        self.assertEqual(titled(g, "nuke_ksampler")["inputs"]["steps"], 6)
        self.assertEqual(titled(g, "nuke_latent")["inputs"]["width"], 1408)
        self.assertEqual(titled(g, "nuke_load_diffusion_model")
                         ["inputs"]["unet_name"], "other.sft")

    def test_missing_values_keep_the_export(self):
        wf = workflows.load("z_image_turbo_t2i")
        g = workflows.build(wf, dict(prompt="p"))
        self.assertEqual(titled(g, "nuke_ksampler")["inputs"]["steps"], 8)
        self.assertEqual(titled(g, "nuke_load_diffusion_model")["inputs"]
                         ["unet_name"], "z_image_turbo_int8_convrot.safetensors")

    def test_qwen_seed_goes_to_both_nodes(self):
        g = workflows.build(workflows.load("qwen_t2i"),
                            dict(prompt="p", seed=9, enhance=1))
        self.assertEqual(titled(g, "nuke_ksampler")["inputs"]["seed"], 9)
        self.assertEqual(titled(g, "nuke_generate_text")
                         ["inputs"]["sampling_mode.seed"], 9)
        self.assertIs(titled(g, "nuke_enhance_switch")["inputs"]["switch"], True)

    def test_edit_prunes_unused_slots(self):
        wf = workflows.load("qwen_edit")
        for n in (1, 2, 4):
            for mode in ("upload", "path"):
                imgs = ["/x/in%d.png" % i for i in range(n)]
                g = workflows.build(wf, dict(prompt="p", width=1024, height=1024),
                                    imgs, image_mode=mode)
                self.assertEqual(len(cc.image_slots(g)), n)
                self.assertEqual(dangling(g), [])
                first = titled(g, "nuke_input_1")
                if mode == "path":
                    self.assertEqual(first["class_type"], cc.PATH_LOADER)
                    self.assertEqual(first["inputs"], {"path": imgs[0]})
                else:
                    self.assertEqual(first["inputs"]["image"], imgs[0])
                self.assertIs(titled(g, "nuke_size_switch")["inputs"]["switch"],
                              True)

    def test_image_count_errors(self):
        with self.assertRaises(cc.ComfyError):
            workflows.build(workflows.load("qwen_edit"), dict(prompt="p"))
        with self.assertRaises(cc.ComfyError):
            workflows.build(workflows.load("qwen_t2i"), dict(prompt="p"), ["/a"])

    def test_choice_options(self):
        wf = workflows.load("z_image_turbo_t2i")
        knob = wf.knobs[0]
        default = "z_image_turbo_int8_convrot.safetensors"
        self.assertEqual(workflows.choice_options(wf, knob), [default])
        self.assertEqual(
            workflows.choice_options(wf, knob, {"UNETLoader": UNET_INFO}),
            [default, "z_image_base.safetensors"])       # "match" filters qwen
        new_shape = {"UNETLoader": {"input": {"required": {"unet_name": [
            "COMBO", {"options": ["z_image_x.safetensors"]}]}}}}
        self.assertEqual(workflows.choice_options(wf, knob, new_shape),
                         [default, "z_image_x.safetensors"])


class Checking(unittest.TestCase):
    """The gate: a flawed export or definition must be rejected with a
    reason, not turned into a node."""

    def graph(self):
        return copy.deepcopy(workflows.load("qwen_edit").graph)

    def assertProblem(self, problems, text):
        self.assertTrue(any(text in p for p in problems), problems)

    def test_ui_format_rejected(self):
        self.assertProblem(workflows.lint_graph({"nodes": [], "links": []}),
                           "Save (API)")

    def test_dangling_link(self):
        g = self.graph()
        del g[cc.find_node(g, "nuke_latent")]
        self.assertProblem(workflows.lint_graph(g), "links to missing node")

    def test_duplicate_title(self):
        g = self.graph()
        titled(g, "nuke_latent")["_meta"]["title"] = "nuke_prompt"
        self.assertProblem(workflows.lint_graph(g), "must be unique")

    def test_no_save_node(self):
        g = self.graph()
        titled(g, "nuke_save")["_meta"]["title"] = "Save Image"
        self.assertProblem(workflows.lint_graph(g), "No save node")

    def test_gap_in_image_slots(self):
        g = self.graph()
        titled(g, "nuke_input_2")["_meta"]["title"] = "nuke_input_12"
        self.assertProblem(workflows.lint_graph(g), "no gaps")

    def test_baked_in_load_image(self):
        g = self.graph()
        titled(g, "nuke_input_10")["_meta"]["title"] = "Load Image"
        self.assertProblem(workflows.lint_graph(g), "baked in")

    def test_definition_problems(self):
        wf = workflows.load("z_image_turbo_t2i")
        spec = copy.deepcopy(wf.spec)
        spec["output"] = "nuke_save"
        spec["bind"]["seed"] = [["nuke_ksampler", "sead"]]
        spec["bind"]["cfg"] = [["nuke_ksampler", "cfg"]]
        spec["knobs"] += [{"name": "seed", "type": "int", "bind": [["nuke_ksampler", "cfg"]]},
                          {"name": "mode", "type": "choice", "bind": [["nuke_ksampler", "scheduler"]]},
                          {"name": "x", "type": "slider", "bind": []}]
        problems = workflows.validate(workflows.Workflow("t", spec, wf.graph))
        for text in ("No node titled 'nuke_save'", "no input 'sead'",
                     "not a standard value", "taken by a standard knob",
                     "a choice needs", "type must be one of", "needs \"bind\""):
            self.assertProblem(problems, text)

    def test_server_check(self):
        wf = workflows.load("z_image_turbo_t2i")
        info = {n["class_type"]: {"input": {"required": dict.fromkeys(n["inputs"], ["X"])}}
                for n in wf.graph.values()}
        info["SaveImageAdvanced"]["output_node"] = True
        self.assertEqual(workflows.check_server(wf.graph, info, wf.output), [])
        info["UNETLoader"]["input"]["required"]["unet_name"] = [["other.sft"], {}]
        del info["ModelSamplingAuraFlow"]
        info["SaveImageAdvanced"]["output_node"] = False
        problems = workflows.check_server(wf.graph, info, wf.output)
        self.assertProblem(problems, "is not available on this server")
        self.assertProblem(problems, "is not installed")
        self.assertProblem(problems, "not an output node")

    def test_scaffold(self):
        wf = workflows.load("z_image_turbo_t2i")
        draft = workflows.scaffold(wf.graph, {"UNETLoader": UNET_INFO})
        self.assertEqual(draft["output"], "nuke_save_image")
        self.assertEqual(draft["bind"], wf.spec["bind"])
        knobs = {k["name"]: k for k in draft["knobs"]}
        self.assertEqual(knobs["unet_name"]["type"], "choice")
        self.assertEqual(knobs["steps"]["type"], "int")
        edit = workflows.scaffold(workflows.load("qwen_edit").graph)
        self.assertEqual(edit["images"], {"max": 4})
        self.assertEqual(len(edit["bind"]["seed"]), 2)


class WithFakeComfy(unittest.TestCase):
    def setUp(self):
        self.comfy = FakeComfy()
        self.comfy.object_info["UNETLoader"] = UNET_INFO
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self.comfy.close)
        self.addCleanup(shutil.rmtree, self.tmp)

    def plate(self, name="plate.png"):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(b"plate bytes")
        return path

    def submitted(self):
        return list(self.comfy.prompts.values())[-1]


class Running(WithFakeComfy):
    def run_wf(self, name, **kw):
        kw.setdefault("values", dict(prompt="a ring", seed=3, width=1408, height=736))
        return workflows.generate(name, kw.pop("values"),
                                  os.path.join(self.tmp, "sub", "out_v001.png"),
                                  host=self.comfy.host, log=lambda m: None, **kw)

    def test_generate_writes_image_and_sidecar(self):
        r = self.run_wf("z_image_turbo_t2i")
        with open(r["path"], "rb") as f:
            self.assertEqual(f.read(), IMAGE_BYTES)
        self.assertEqual((r["seed"], r["workflow"]), (3, "z_image_turbo_t2i"))
        with open(os.path.join(self.tmp, "sub", "out_v001.json")) as f:
            side = json.load(f)
        self.assertEqual(side["result"]["seed"], 3)
        self.assertIn("workflow", side)

    def test_seed_is_rolled_when_missing(self):
        r = self.run_wf("z_image_turbo_t2i", values=dict(prompt="p"))
        self.assertIsInstance(r["seed"], int)
        self.assertEqual(titled(self.submitted(), "nuke_ksampler")["inputs"]["seed"],
                         r["seed"])

    def test_used_prompt_read_back(self):
        self.assertEqual(self.run_wf("qwen_t2i")["used_prompt"],
                         "the prompt that was used")

    def test_unreachable(self):
        with self.assertRaises(cc.ComfyError):
            workflows.generate("qwen_t2i", dict(prompt="p"),
                               os.path.join(self.tmp, "x.png"),
                               host="127.0.0.1:9", log=lambda m: None)

    def test_edit_auto_uploads_without_custom_node(self):
        r = self.run_wf("qwen_edit", images=[self.plate()],
                        upload_names=["N_in1.png"])
        self.assertEqual(r["image_mode"], "upload")
        self.assertIn(b"plate bytes", self.comfy.uploads[0])
        self.assertEqual(titled(self.submitted(), "nuke_input_1")["inputs"]["image"],
                         "nuke/N_in1.png")

    def test_edit_auto_uses_path_with_custom_node(self):
        self.comfy.node_classes.add(cc.PATH_LOADER)
        plate = self.plate()
        r = self.run_wf("qwen_edit", images=[plate])
        self.assertEqual(r["image_mode"], "path")
        self.assertEqual(self.comfy.uploads, [])
        self.assertEqual(titled(self.submitted(), "nuke_input_1")["inputs"]["path"],
                         plate)

    def test_edit_path_mode_needs_custom_node(self):
        with self.assertRaises(cc.ComfyError):
            self.run_wf("qwen_edit", images=[self.plate()], input_mode="path")

    def test_cancel_interrupts(self):
        self.comfy.hold = True
        with self.assertRaises(cc.ComfyError) as ctx:
            self.run_wf("qwen_t2i", should_cancel=lambda: True)
        self.assertEqual(str(ctx.exception), "Cancelled")
        self.assertTrue(self.comfy.interrupted)

    def test_cli_run_and_list(self):
        out = os.path.join(self.tmp, "cli_v001.png")
        code = cli.main(["--host", self.comfy.host, "run", "z_image_turbo_t2i",
                         "--prompt", "p", "--out", out, "--set", "steps=5"])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(out))
        self.assertEqual(titled(self.submitted(), "nuke_ksampler")["inputs"]["steps"], 5)
        self.assertEqual(cli.main(["list"]), 0)


class NukeNodes(WithFakeComfy):
    def setUp(self):
        WithFakeComfy.setUp(self)
        nuke.reset()
        jobs._JOBS.clear()
        from compai import node
        self.node = node
        node.DEFAULT_HOST = self.comfy.host

    def wait_idle(self, n):
        end = time.time() + 10
        while n.fullName() in jobs._JOBS:
            self.assertLess(time.time(), end, "job never finished")
            time.sleep(0.02)

    def make(self, name):
        g = compai.create(name)
        g["prompt"].setValue("a ring in a box")
        g["output_dir"].setValue(self.tmp)
        return g

    def test_menu_lists_every_workflow(self):
        import importlib
        import menu
        importlib.reload(menu)
        commands = nuke.menu("Nodes").commands
        self.assertEqual(sorted(commands),
                         ["Qwen Image", "Qwen Image Edit", "Z-Image Turbo"])
        nuke.menu("Nodes").run("Z-Image Turbo")
        self.assertIn("ZImageTurbo1", [n.name() for n in nuke.allNodes()])

    def test_standard_knobs_on_every_node(self):
        for name in workflows.names():
            g = compai.create(name)
            for knob in ("prompt seed random_seed target_format megapixels "
                         "gen_size output_dir out_name generate cancel "
                         "reuse_seed status last_file last_seed used_prompt "
                         "comfy_workflow host").split():
                self.assertIsNotNone(g.knob(knob), "%s.%s" % (name, knob))
            self.assertEqual(g["comfy_workflow"].value(), name)
            self.assertIs(g["reformat_output"].value(), False)     # raw by default
            self.assertEqual(g.children["ToTarget"]["disable"].expression,
                             "!parent.reformat_output")
            self.assertEqual(g["gen_size"].value(), "1408 x 736")
            g["reformat_output"].setValue(True)
            nuke.fire_knob_changed(g, "reformat_output")
            self.assertEqual(g["gen_size"].value(),
                             "1408 x 736  ->  reformat to 2048 x 1080")
            self.assertEqual(g.knob("match_input") is not None,
                             bool(workflows.load(name).max_images))

    def test_z_image_node(self):
        g = self.make("z_image_turbo_t2i")
        self.assertEqual(g.name(), "ZImageTurbo1")
        self.assertEqual(g["steps"].value(), 8)
        self.assertEqual(g["model"].values(),          # from the server, filtered
                         ["z_image_turbo_int8_convrot.safetensors",
                          "z_image_base.safetensors"])
        g["model"].setValue("z_image_base.safetensors")
        g["steps"].setValue(6)
        g["generate"].execute()
        self.wait_idle(g)
        self.assertEqual(nuke.messages, [])
        out = os.path.join(self.tmp, "gen_v001.png")
        self.assertEqual(g.children["ComfyRead"]["file"].value(), out)
        self.assertEqual(g["last_seed"].value(), g["seed"].value())
        self.assertTrue(g["status"].value().startswith("done in"))
        sent = self.submitted()
        self.assertEqual(titled(sent, "nuke_load_diffusion_model")["inputs"]["unet_name"],
                         "z_image_base.safetensors")
        self.assertEqual(titled(sent, "nuke_ksampler")["inputs"]["steps"], 6)
        self.assertEqual(titled(sent, "nuke_prompt")["inputs"]["value"],
                         "a ring in a box")

        g["generate"].execute()                      # versions up
        self.wait_idle(g)
        self.assertEqual(g["last_file"].value(), "gen_v002.png")
        g["reuse_seed"].execute()
        self.assertIs(g["random_seed"].value(), False)

    def test_choices_without_comfy_then_refresh(self):
        self.node.DEFAULT_HOST = "127.0.0.1:9"
        g = self.make("z_image_turbo_t2i")
        default = "z_image_turbo_int8_convrot.safetensors"
        self.assertEqual(g["model"].values(), [default])
        g["refresh_choices"].execute()
        self.assertIn("not reachable", nuke.messages[-1])
        g["host"].setValue(self.comfy.host)
        g["refresh_choices"].execute()
        self.assertEqual(g["model"].values(), [default, "z_image_base.safetensors"])
        self.assertEqual(g["model"].value(), default)

    def test_refresh_keeps_a_model_the_server_lost(self):
        g = self.make("z_image_turbo_t2i")
        g["model"].setValues(["gone.sft"])
        g["model"].setValue("gone.sft")
        g["refresh_choices"].execute()
        self.assertEqual(g["model"].value(), "gone.sft")
        self.assertIn("z_image_base.safetensors", g["model"].values())

    def test_validation_messages(self):
        g = compai.create("qwen_t2i")
        g["generate"].execute()
        self.assertEqual(nuke.messages, ["Enter a prompt first."])
        g["comfy_workflow"].setValue("no_such_workflow")
        g["generate"].execute()
        self.assertIn("Can't load workflow", nuke.messages[-1])
        self.assertIsNone(compai.create("no_such_workflow"))

    def test_error_is_reported_and_node_unlocks(self):
        g = self.make("qwen_t2i")
        g["host"].setValue("127.0.0.1:9")
        g["generate"].execute()
        self.wait_idle(g)
        self.assertIn("QwenImage1 failed", nuke.messages[-1])
        self.assertTrue(g["generate"].enabled())

    def test_cancel(self):
        self.comfy.hold = True
        g = self.make("qwen_t2i")
        g["generate"].execute()
        self.assertFalse(g["generate"].enabled())
        g["generate"].execute()
        self.assertIn("already running", nuke.messages[-1])
        g["cancel"].execute()
        self.wait_idle(g)
        self.assertEqual(g["status"].value(), "cancelled")
        self.assertTrue(self.comfy.interrupted)

    def test_stale_lock_released_on_panel_open(self):
        g = compai.create("qwen_t2i")
        g["generate"].setEnabled(False)          # as loaded from a saved script
        nuke.fire_knob_changed(g, "showPanel")
        self.assertTrue(g["generate"].enabled())

    def test_edit_node(self):
        g = self.make("qwen_edit")
        self.assertEqual(g.name(), "QwenEdit1")
        g["enhance"].setValue(True)
        g["generate"].execute()
        self.assertEqual(nuke.messages, ["Connect at least one image input."])

        plate = nuke.nodes.Read(name="plate")
        g.setInput(0, None)
        g.setInput(2, plate)                     # img3 only -> slot 1
        nuke.fire_knob_changed(g, "inputChange")
        self.assertIn("img3", g["inputs_info"].value())
        self.assertEqual(g["target_format"].value().name(), "2K_DCP")
        plate._format = nuke.Format(1234, 700, 2.0, "")      # unnamed format
        nuke.fire_knob_changed(g, "inputChange")
        self.assertEqual(g["target_format"].value().width(), 1234)
        self.assertEqual(g["target_format"].value().pixelAspect(), 2.0)

        g["generate"].execute()
        self.wait_idle(g)
        self.assertEqual(nuke.messages[1:], [])
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "edit_v001_in1.png")))
        self.assertEqual(g["last_file"].value(), "edit_v001.png")
        self.assertTrue(g.children["write3"]["disable"].value())
        self.assertEqual(g.children["prep3"]["resize"].value(), "fill")
        self.assertFalse(g.children["prep3"]["disable"].value())   # resized

        plate._format = nuke.Format(1360, 768, 1.0, "")   # already at budget
        g["generate"].execute()
        self.wait_idle(g)
        self.assertTrue(g.children["prep3"]["disable"].value())    # untouched
        self.assertEqual(titled(self.submitted(), "nuke_latent")["inputs"]["width"], 1360)
        sent = self.submitted()
        self.assertEqual(len(cc.image_slots(sent)), 1)
        self.assertEqual(titled(sent, "nuke_input_1")["inputs"]["image"],
                         "nuke/QwenEdit1_in1.png")
        self.assertIs(titled(sent, "nuke_enhance_switch")["inputs"]["switch"], True)


if __name__ == "__main__":
    unittest.main()
