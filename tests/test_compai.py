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
            self.assertEqual(wf.spec.get("label", "").count("/"), 0, name)
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
        for n in (1, 2, 4, 10):
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

    def test_named_inputs_are_each_optional(self):
        wf = workflows.load("minimax_h3_fl2va")
        values = dict(prompt="p", seed=4, width=864, height=480, duration=3)
        for images, kept in ((["/a", "/b"], ["first_frame", "last_frame"]),
                             (["/a", None], ["first_frame"]),
                             ([None, "/b"], ["last_frame"]),
                             (["/a"], ["first_frame"])):
            g = workflows.build(wf, values, images, image_mode="path")
            i2v = titled(g, "nuke_MiniMaxH3_i2v")["inputs"]
            self.assertEqual([k for k in ("first_frame", "last_frame") if k in i2v],
                             kept)
            self.assertEqual(dangling(g), [])
            self.assertEqual((i2v["width"], i2v["height"]), (864, 480))
            self.assertEqual(titled(g, "nuke_duration")["inputs"]["value"], 3.0)
        with self.assertRaises(cc.ComfyError):
            workflows.build(wf, values, [None, None])

    def test_reference_images_are_all_optional(self):
        wf = workflows.load("minimax_h3_ref2va")
        self.assertEqual((wf.max_images, wf.min_images, wf.match_format,
                          wf.resize_inputs), (9, 0, False, False))
        values = dict(prompt="p", seed=4, width=864, height=480, fps=25)
        for n in (0, 1, 2, 9):
            imgs = ["/x/r%d.png" % i for i in range(n)]
            g = workflows.build(wf, values, imgs or None, image_mode="path")
            node = titled(g, "nuke_ref_to_video")["inputs"]
            self.assertEqual(sorted(k for k in node if k.startswith("ref_images.")),
                             ["ref_images.ref_image_%d" % i for i in range(n)])
            self.assertEqual(dangling(g), [])
            self.assertEqual(len(cc.image_slots(g)), n)
        self.assertEqual(titled(g, "nuke_fps")["inputs"]["value"], 25.0)
        with self.assertRaises(cc.ComfyError):
            workflows.build(wf, values, ["/x"] * 10)
        self.assertTrue(workflows.load("qwen_edit").match_format)
        self.assertEqual(workflows.load("qwen_edit").min_images, 1)

    def test_size_settings_per_workflow(self):
        video = workflows.load("minimax_h3_fl2va")
        self.assertEqual((video.multiple, video.megapixels, video.timeout),
                         (32, 0.4, 3600))
        knobs = {k["name"]: k for k in video.knobs}
        self.assertEqual(workflows.knob_default(video, knobs["duration"]), 2)
        self.assertEqual(workflows.knob_default(video, knobs["steps"]), 20)
        self.assertEqual(workflows.knob_default(video, knobs["lightning_steps"]), 8)
        self.assertTrue(workflows.knob_default(video, knobs["codec"])
                        .startswith("ProRes_422_"))
        still = workflows.load_cached("qwen_t2i")
        self.assertEqual((still.multiple, still.megapixels, still.timeout),
                         (16, 1.0, 900))
        self.assertEqual(cc.generation_size(1920, 1080, 0.4, 32), (864, 480))

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

    def test_undeclared_load_image(self):
        wf = workflows.load("minimax_h3_fl2va")
        spec = copy.deepcopy(wf.spec)
        spec["images"]["inputs"].pop()               # forget the last frame
        problems = workflows.validate(workflows.Workflow("t", spec, wf.graph))
        self.assertProblem(problems, "'nuke_last_frame' is not declared")

    def test_numeric_combo_options(self):
        info = {"CreateVideo": {"input": {"optional": {"bit_depth": [
            "COMBO", {"options": ["auto", 8, 10]}]}}}}
        graph = {"1": {"class_type": "CreateVideo", "inputs": {"bit_depth": 8},
                       "_meta": {"title": "x"}}}
        self.assertEqual(workflows.check_server(graph, info), [])

    def test_definition_problems(self):
        wf = workflows.load("z_image_turbo_t2i")
        spec = copy.deepcopy(wf.spec)
        spec["output"] = "nuke_save"
        spec["label"] = "First/Last"
        spec["bind"]["seed"] = [["nuke_ksampler", "sead"]]
        spec["bind"]["cfg"] = [["nuke_ksampler", "cfg"]]
        spec["knobs"] += [{"name": "seed", "type": "int", "bind": [["nuke_ksampler", "cfg"]]},
                          {"name": "mode", "type": "choice", "bind": [["nuke_ksampler", "scheduler"]]},
                          {"name": "x", "type": "slider", "bind": []}]
        problems = workflows.validate(workflows.Workflow("t", spec, wf.graph))
        for text in ("No node titled 'nuke_save'", "no input 'sead'", "submenu",
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
        video = workflows.scaffold(workflows.load("minimax_h3_fl2va").graph)
        self.assertEqual(video["bind"]["seed"], [["nuke_random_noise", "noise_seed"]])
        self.assertEqual(video["bind"]["width"], [["nuke_MiniMaxH3_i2v", "width"]])
        self.assertEqual([i["name"] for i in video["images"]["inputs"]],
                         ["first_frame", "last_frame"])
        edit = workflows.scaffold(workflows.load("qwen_edit").graph)
        self.assertEqual(edit["images"], {"max": 10})
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

    def test_video_result_keeps_its_extension(self):
        r = self.run_wf("minimax_h3_fl2va", images=[None, self.plate()],
                        upload_names=[None, "N_last.png"])
        self.assertTrue(r["path"].endswith("out_v001.mp4"), r["path"])
        self.assertTrue(os.path.isfile(r["path"]))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "sub", "out_v001.json")))
        self.assertEqual(r["images"][0], None)
        self.assertEqual(titled(self.submitted(), "nuke_last_frame")["inputs"]["image"],
                         "nuke/N_last.png")
        self.assertEqual(len(self.comfy.uploads), 1)

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
                         ["Minimax_H3_fl2va", "Minimax_H3_ref2va", "Qwen Image",
                          "Qwen Image Edit", "Z-Image Turbo"])
        nuke.menu("Nodes").run("Z-Image Turbo")
        self.assertIn("ZImageTurbo1", [n.name() for n in nuke.allNodes()])

    def test_standard_knobs_on_every_node(self):
        for name in workflows.names():
            g = compai.create(name)
            for knob in ("prompt seed random_seed target_format megapixels "
                         "gen_size output_dir out_name generate cancel "
                         "reuse_seed status create_read last_file last_seed used_prompt "
                         "comfy_workflow host").split():
                self.assertIsNotNone(g.knob(knob), "%s.%s" % (name, knob))
            self.assertEqual(g["comfy_workflow"].value(), name)
            self.assertIs(g["reformat_output"].value(), False)     # raw by default
            self.assertEqual(g.children["ToTarget"]["disable"].expression,
                             "!parent.reformat_output")
            size = "%d x %d" % cc.generation_size(
                2048, 1080, g["megapixels"].value(), workflows.load(name).multiple)
            self.assertEqual(g["gen_size"].value(), size)
            g["reformat_output"].setValue(True)
            nuke.fire_knob_changed(g, "reformat_output")
            self.assertEqual(g["gen_size"].value(),
                             size + "  ->  reformat to 2048 x 1080")
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

        g.setXYpos(40, 70)
        g["create_read"].execute()                   # keep v001 in the script
        kept = [n for n in nuke.allNodes("Read")]
        self.assertEqual(len(kept), 1)               # beside the group, not in it
        self.assertEqual(kept[0]["file"].value(), out)
        self.assertEqual((kept[0].xpos(), kept[0].ypos()), (160, 70))

        g["generate"].execute()                      # versions up
        self.wait_idle(g)
        self.assertEqual(g["last_file"].value(), "gen_v002.png")
        self.assertEqual(kept[0]["file"].value(), out)   # still v001
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
        g["create_read"].execute()
        self.assertEqual(nuke.messages.pop(), "Nothing generated yet.")
        g["generate"].execute()
        self.assertEqual(nuke.messages, ["Enter a prompt first."])
        g["comfy_workflow"].setValue("no_such_workflow")
        g["generate"].execute()
        self.assertIn("Can't load workflow", nuke.messages[-1])
        self.assertIsNone(compai.create("no_such_workflow"))

    def test_timeout_knob_is_passed_on(self):
        self.comfy.hold = True                   # the job never finishes
        g = self.make("qwen_t2i")
        self.assertEqual(g["timeout"].value(), 15)
        seen = {}
        real = cc.ComfyClient.wait
        def wait(client, pid, timeout=900, **kw):
            seen["timeout"] = timeout
            raise cc.ComfyError("Timed out after %ss" % timeout)
        cc.ComfyClient.wait = wait
        self.addCleanup(setattr, cc.ComfyClient, "wait", real)
        g["timeout"].setValue(90)
        g["generate"].execute()
        self.wait_idle(g)
        self.assertEqual(seen["timeout"], 5400)
        self.assertIn("Timed out", nuke.messages[-1])

    def test_still_result_has_no_start_frame(self):
        g = self.make("qwen_t2i")
        g["generate"].execute()
        self.wait_idle(g)
        self.assertEqual(g.children["ComfyRead"]["frame_mode"].value(), "expression")

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
        self.assertEqual(nuke.messages, ["Connect at least 1 image input."])

        def inputs():
            return sorted(n.name() for n in g.children.values() if n.cls == "Input")

        self.assertEqual(inputs(), ["img1"])     # one free input to start
        plate = nuke.nodes.Read(name="plate")
        other = nuke.nodes.Read(name="other")
        g.setInput(0, other)
        self.assertEqual(inputs(), ["img1", "img2"])
        g.setInput(1, other)
        g.setInput(2, plate)
        self.assertEqual(inputs(), ["img1", "img2", "img3", "img4"])
        g.setInput(1, None)                      # a gap in the middle: no change
        self.assertEqual(inputs(), ["img1", "img2", "img3", "img4"])
        g.setInput(0, None)                      # img3 only -> slot 1
        self.assertEqual(inputs(), ["img1", "img2", "img3", "img4"])
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

        g.setInput(2, None)                      # top one removed: shrink back
        self.assertEqual(inputs(), ["img1"])
        self.assertIsNone(g.children.get("write3"))

    def test_video_node_with_first_and_last_inputs(self):
        g = self.make("minimax_h3_fl2va")
        def inputs():
            return sorted(n.name() for n in g.children.values() if n.cls == "Input")
        self.assertEqual(inputs(), ["first", "last"])
        self.assertEqual(g["megapixels"].value(), 0.4)
        self.assertEqual(g["gen_size"].value(), "896 x 480")     # 32 grid
        self.assertEqual((g["duration"].value(), g["steps"].value(),
                          g["lightning_steps"].value()), (2.0, 20, 8))
        self.assertEqual(g["fps"].expression, "root.fps")        # follows project
        self.assertEqual(g["timeout"].value(), 60)               # minutes
        g["generate"].execute()
        self.assertEqual(nuke.messages, ["Connect at least 1 image input."])

        g.setInput(1, nuke.nodes.Read(name="plate"))             # last only
        self.assertEqual(inputs(), ["first", "last"])            # fixed, no growing
        self.assertEqual(g["inputs_info"].value(), "last")
        g["duration"].setValue(3)
        g["generate"].execute()
        self.wait_idle(g)
        self.assertEqual(nuke.messages[1:], [])
        self.assertEqual(g["last_file"].value(), "video_v001.mp4")
        self.assertTrue(g.children["ComfyRead"]["file"].value().endswith(".mp4"))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "video_v001_last.png")))
        sent = self.submitted()
        self.assertNotIn("first_frame", titled(sent, "nuke_MiniMaxH3_i2v")["inputs"])
        self.assertEqual(titled(sent, "nuke_last_frame")["inputs"]["image"],
                         "nuke/MiniMaxVideo1_last.png")
        self.assertEqual(titled(sent, "nuke_duration")["inputs"]["value"], 3.0)
        self.assertEqual(titled(sent, "nuke_fps")["inputs"]["value"], 24.0)
        self.assertTrue(titled(sent, "nuke_save_video")["inputs"]["preset"]
                        .startswith("ProRes_422_"))
        read = g.children["ComfyRead"]           # clip starts at the script's first frame
        self.assertEqual((read["frame_mode"].value(), read["frame"].value()),
                         ("start at", "1001"))
        g["create_read"].execute()
        kept = [n for n in nuke.allNodes("Read") if n.name() != "plate"][0]
        self.assertEqual((kept["frame_mode"].value(), kept["frame"].value()),
                         ("start at", "1001"))

        g["generate"].execute()                  # versions past the .mp4
        self.wait_idle(g)
        self.assertEqual(g["last_file"].value(), "video_v002.mp4")

    def test_temporary_inputs_are_deleted_after_the_run(self):
        g = self.make("qwen_edit")
        self.assertIs(g["temp_inputs"].value(), False)           # kept by default
        self.assertIsNone(compai.create("qwen_t2i").knob("temp_inputs"))
        self.assertFalse(g["instant_cleanup"].enabled())         # nothing to clean
        g.setInput(0, nuke.nodes.Read(name="plate"))
        g["temp_inputs"].setValue(True)
        nuke.fire_knob_changed(g, "temp_inputs")
        self.assertTrue(g["instant_cleanup"].enabled())
        self.assertIs(g["instant_cleanup"].value(), False)       # off by default
        written = []
        real = nuke.execute
        def execute(write, first, last):
            written.append(write["file"].value())
            real(write, first, last)
        nuke.execute = execute
        self.addCleanup(setattr, nuke, "execute", real)
        g["generate"].execute()
        self.wait_idle(g)
        self.assertEqual(nuke.messages, [])
        self.assertEqual(len(written), 1)
        self.assertFalse(written[0].startswith(self.tmp))        # not beside the result
        self.assertTrue(written[0].startswith(tempfile.gettempdir()))
        self.assertTrue(os.path.isfile(written[0]))              # still there to inspect
        self.addCleanup(shutil.rmtree, os.path.dirname(written[0]), True)
        self.assertEqual(sorted(os.listdir(self.tmp)),
                         ["edit_v001.json", "edit_v001.png"])

        g["instant_cleanup"].setValue(True)                      # now delete at once
        g["generate"].execute()
        self.wait_idle(g)
        self.assertEqual(len(written), 2)
        self.assertFalse(os.path.exists(os.path.dirname(written[1])))
        self.assertIn(b"fake png", self.comfy.uploads[0])        # Comfy still got it

    def test_temporary_inputs_are_deleted_when_the_run_fails(self):
        g = self.make("qwen_edit")
        g.setInput(0, nuke.nodes.Read(name="plate"))
        g["temp_inputs"].setValue(True)
        g["instant_cleanup"].setValue(True)
        g["host"].setValue("127.0.0.1:9")
        before = set(os.listdir(tempfile.gettempdir()))
        g["generate"].execute()
        self.wait_idle(g)
        self.assertIn("failed", nuke.messages[-1])
        left = [d for d in set(os.listdir(tempfile.gettempdir())) - before
                if d.startswith("compai_")]
        self.assertEqual(left, [])

    def test_reference_video_node(self):
        g = self.make("minimax_h3_ref2va")
        self.assertEqual(g.name(), "MiniMaxRefVideo1")
        self.assertIs(g["match_input"].value(), False)   # refs don't set the format
        self.assertEqual((g["ref_size"].values(), g["lightning_steps"].value()),
                         (["match", "max"], 4))
        g["generate"].execute()                          # no references at all
        self.wait_idle(g)
        self.assertEqual(nuke.messages, [])
        self.assertEqual(g["last_file"].value(), "video_v001.mp4")
        sent = self.submitted()
        self.assertEqual(cc.image_slots(sent), [])
        self.assertFalse(any(k.startswith("ref_images.")
                             for k in titled(sent, "nuke_ref_to_video")["inputs"]))

        def pipes():
            return [n.name() for n in sorted(
                (n for n in g.children.values() if n.cls == "Input"),
                key=lambda n: n["number"].value())]

        self.assertEqual(pipes(), ["img1", "vid1"])      # one of each to start
        plate = nuke.nodes.Read(name="plate")
        plate._format = nuke.Format(4096, 2160, 1.0, "")
        g.setInput(0, plate)                             # img1
        self.assertEqual(pipes(), ["img1", "vid1", "img2"])
        g.setInput(2, plate)                             # img2
        self.assertEqual(pipes(), ["img1", "vid1", "img2", "img3"])
        self.assertEqual(g["target_format"].value().name(), "2K_DCP")   # unchanged
        self.assertEqual(g["inputs_info"].value(), "img1, img2  ->  slot(s) 1-2")
        g["generate"].execute()
        self.wait_idle(g)
        sent = self.submitted()
        self.assertEqual(len(cc.image_slots(sent)), 2)
        self.assertTrue(g.children["prep1"]["disable"].value())   # sent unresized
        self.assertEqual(cc.image_slots(sent, "nuke_video_"), [])  # no video given
        self.assertFalse(any(n["class_type"] == "GetVideoComponents"
                             for n in sent.values()))
        self.assertEqual(dangling(sent), [])

    def test_video_inputs(self):
        g = self.make("minimax_h3_ref2va")
        def pipes():
            return [n.name() for n in sorted(
                (n for n in g.children.values() if n.cls == "Input"),
                key=lambda n: n["number"].value())]
        clip = nuke.nodes.Read(name="clip")
        clip._format = nuke.Format(1920, 1080, 1.0, "HD_1080")
        clip.first_frame, clip.last_frame = 1001, 1072
        still = nuke.nodes.Read(name="still")

        g.setInput(1, clip)                              # vid1
        self.assertEqual(pipes(), ["img1", "vid1", "vid2"])
        g.setInput(0, still)                             # img1
        self.assertEqual(pipes(), ["img1", "vid1", "vid2", "img2"])
        self.assertEqual(g["inputs_info"].value(),
                         "img1  ->  slot(s) 1-1;  vid1  ->  video slot(s) 1-1")
        self.assertFalse(g["video_first"].enabled())

        g["generate"].execute()
        self.wait_idle(g)
        self.assertEqual(nuke.messages, [])
        mov = os.path.join(self.tmp, "video_v001_vid1.mov")
        self.assertIn((mov, 1001, 1072), nuke.executed)          # the input's range
        self.assertIn((os.path.join(self.tmp, "video_v001_in1.png"), 1001, 1001),
                      nuke.executed)
        vprep = g.children["vprep1"]                             # scaled to budget
        self.assertEqual((vprep["box_width"].value(), vprep["box_height"].value()),
                         (864, 480))
        self.assertEqual(g.children["vwrite1"]["mov64_fps"].value(), 24.0)
        self.assertTrue(g.children["vwrite1"]["disable"].value())
        sent = self.submitted()
        self.assertEqual(titled(sent, "nuke_video_1")["inputs"]["file"],
                         "nuke/MiniMaxRefVideo1_vid1.mov")
        self.assertEqual(len(cc.image_slots(sent, "nuke_video_")), 1)
        self.assertEqual([k for k in titled(sent, "nuke_ref_to_video")["inputs"]
                          if k.startswith("ref_videos.")], ["ref_videos.ref_video_0"])
        self.assertEqual(sum(n["class_type"] == "GetVideoComponents"
                             for n in sent.values()), 1)
        self.assertEqual(dangling(sent), [])
        self.assertEqual(len(self.comfy.uploads), 2)             # the png and the mov

        g["video_range"].setValue("custom")
        nuke.fire_knob_changed(g, "video_range")
        self.assertTrue(g["video_first"].enabled())
        g["video_first"].setValue(1010)
        g["video_last"].setValue(1033)
        g["generate"].execute()
        self.wait_idle(g)
        self.assertIn((os.path.join(self.tmp, "video_v002_vid1.mov"), 1010, 1033),
                      nuke.executed)

        g["video_range"].setValue("project range")       # 1001-1100 in the stub
        g["generate"].execute()
        self.wait_idle(g)
        self.assertIn((os.path.join(self.tmp, "video_v003_vid1.mov"), 1001, 1100),
                      nuke.executed)

    def test_long_video_asks_first(self):
        g = self.make("minimax_h3_ref2va")
        clip = nuke.nodes.Read(name="clip")
        clip.first_frame, clip.last_frame = 1, 2000
        g.setInput(1, clip)
        nuke.answers.append(False)
        g["generate"].execute()
        self.assertIn("2000 frames", nuke.messages[-1])
        self.assertEqual(nuke.executed, [])                      # nothing rendered
        self.assertNotIn(g.fullName(), jobs._JOBS)

    def test_pipes_stay_tidy_when_an_input_is_removed(self):
        g = self.make("minimax_h3_ref2va")
        def pipes():
            return [(n.name(), g.input(n["number"].value()) is not None)
                    for n in sorted((n for n in g.children.values()
                                     if n.cls == "Input"),
                                    key=lambda n: n["number"].value())]
        a, b = nuke.nodes.Read(name="a"), nuke.nodes.Read(name="b")
        g.setInput(0, a)                                 # img1
        g.setInput(2, a)                                 # img2
        g.setInput(1, b)                                 # vid1
        self.assertEqual(pipes(), [("img1", True), ("vid1", True), ("img2", True),
                                   ("img3", False), ("vid2", False)])
        g.setInput(2, None)                              # drop img2: img3 goes,
        self.assertEqual(pipes(), [("img1", True), ("vid1", True),   # no dead pipe
                                   ("img2", False), ("vid2", False)])
        self.assertIs(g.input(1), b)                     # the video kept its feed
        g.setInput(1, None)
        self.assertEqual(pipes(), [("img1", True), ("vid1", False), ("img2", False)])

    def test_video_slots_in_the_workflow(self):
        wf = workflows.load("minimax_h3_ref2va")
        self.assertEqual((wf.max_videos, wf.min_videos), (3, 0))
        for n in (0, 1, 3):
            g = workflows.build(wf, dict(prompt="p"), None,
                                videos=["nuke/v%d.mov" % i for i in range(n)])
            self.assertEqual(len(cc.image_slots(g, "nuke_video_")), n)
            self.assertEqual(sum(x["class_type"] == "GetVideoComponents"
                                 for x in g.values()), n)
            self.assertEqual(dangling(g), [])
        with self.assertRaises(cc.ComfyError):
            workflows.build(wf, dict(prompt="p"), None, videos=["a"] * 4)
        with self.assertRaises(cc.ComfyError):
            workflows.build(workflows.load("qwen_t2i"), dict(prompt="p"),
                            videos=["a"])
        draft = workflows.scaffold(wf.graph)
        self.assertEqual(draft["videos"], {"max": 3})
        spec = copy.deepcopy(wf.spec)
        del spec["videos"]
        problems = workflows.validate(workflows.Workflow("t", spec, wf.graph))
        self.assertTrue(any("LoadVideo 'nuke_video_1' is not declared" in p
                            for p in problems), problems)

    def test_inputs_stop_at_the_workflow_limit(self):
        g = self.make("qwen_edit")
        plate = nuke.nodes.Read(name="plate")
        for i in range(10):
            g.setInput(i, plate)
        self.assertEqual(len([n for n in g.children.values() if n.cls == "Input"]), 10)


if __name__ == "__main__":
    unittest.main()
