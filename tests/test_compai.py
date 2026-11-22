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
from compai import node as cnode
from compai.utility import grid
from fake_comfy import FakeComfy, IMAGE_BYTES

UNET_INFO = {"input": {"required": {"unet_name": [
    ["qwen_image_2.1.safetensors", "z_image_turbo_int8_convrot.safetensors",
     "z_image_base.safetensors"], {}]}}}


def dangling(graph):
    return [(nid, k) for nid, n in graph.items() for k, v in n["inputs"].items()
            if isinstance(v, list) and len(v) == 2 and v[0] not in graph]


def rendered():
    """(file, first, last) for every render, by the name it is moved to."""
    return [(p.replace("_writing", ""), a, b) for p, a, b in nuke.executed]


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

    def test_every_workflow_has_a_model_dropdown(self):
        for name in workflows.names():
            wf = workflows.load(name)
            knobs = {k["name"]: k for k in wf.knobs}
            model = knobs["model"]
            self.assertEqual((model["type"], model["options"]), ("choice", "server"))
            self.assertEqual(workflows.server_class(wf, model), "UNETLoader", name)
            default = workflows.knob_default(wf, model)
            self.assertRegex(default, model["match"], name)   # the filter keeps it
            if "video_vae" in knobs:
                vae = knobs["video_vae"]
                self.assertEqual(workflows.server_class(wf, vae), "VAELoader")
                self.assertRegex(workflows.knob_default(wf, vae), vae["match"])
        self.assertIn("video_vae", {k["name"] for k in
                                    workflows.load("minimax_h3_fl2va").knobs})

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

    def test_minimax_edit_sends_only_the_images_plugged_in(self):
        wf = workflows.load("minimax_h3_image_edit")
        self.assertEqual(wf.max_images, 16)
        for n in (1, 3, 16):
            imgs = ["in%d.png" % i for i in range(1, n + 1)]
            g = workflows.build(wf, dict(prompt="p", width=2368, height=1760), imgs)
            self.assertEqual(dangling(g), [])
            self.assertEqual(len(cc.image_slots(g)), n)
            cond = titled(g, "nuke_conditioning")["inputs"]
            text = titled(g, "nuke_generate_text")["inputs"]
            self.assertEqual(sorted(k for k in cond if k.startswith("images.")),
                             sorted("images.image_%d" % i for i in range(1, n + 1)))
            self.assertEqual(sorted(k for k in text if k.startswith("images.")),
                             sorted("images.image%d" % i for i in range(1, n + 1)))
            for i in range(1, n + 1):    # image N reaches both nodes from slot N
                slot = cc.find_node(g, "nuke_input_%d" % i)
                self.assertEqual(cond["images.image_%d" % i], [slot, 0])
                self.assertEqual(text["images.image%d" % i], [slot, 0])
                self.assertEqual(g[slot]["inputs"]["image"], imgs[i - 1])
            # Nuke sizes img1; nothing in Comfy may rescale or crop it, or the
            # reference and the output differ in shape (the 2%-taller bug)
            self.assertEqual([v["class_type"] for v in g.values() if v["class_type"] in
                              ("ResizeImageMaskNode", "ImageScaleToTotalPixels",
                               "ImageScale", "GetImageSize")], [])
            self.assertEqual((cond["width"], cond["height"]), (2368, 1760))
        with self.assertRaises(cc.ComfyError):
            workflows.build(wf, dict(prompt="p"), [])         # an edit needs img1

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

    def aspect_workflow(self, **spec_changes):
        """z_image with a nuke_aspect string primitive bound to "aspect"."""
        wf = workflows.load("z_image_turbo_t2i")
        graph = copy.deepcopy(wf.graph)
        graph["999"] = {"class_type": "PrimitiveString", "inputs": {"value": "1:1"},
                        "_meta": {"title": "nuke_aspect"}}
        spec = copy.deepcopy(wf.spec)
        spec["bind"]["aspect"] = [["nuke_aspect", "value"]]
        spec["aspect_ratios"] = ["1:1", "4:3", "3:4", "3:2", "2:3", "16:9", "9:16"]
        spec.update(spec_changes)
        return workflows.Workflow("aspect_test", spec, graph)

    def test_nearest_aspect(self):
        ratios = ["1:1", "4:3", "3:4", "3:2", "2:3", "16:9", "9:16"]
        for (w, h), want in {(2368, 1760): "4:3", (1760, 2368): "3:4",
                             (2912, 2912): "1:1", (3872, 2176): "16:9",
                             (3552, 2368): "3:2", (2048, 858): "16:9",   # 2.39:1
                             (1024, 1088): "1:1"}.items():
            self.assertEqual(workflows.nearest_aspect(w, h, ratios), want, (w, h))
        self.assertEqual(workflows.nearest_aspect(10, 10, ["4:3", "3:4"]), "4:3")  # tie

    def test_aspect_follows_the_generation_size(self):
        wf = self.aspect_workflow()
        self.assertEqual(workflows.validate(wf), [])
        g = workflows.build(wf, dict(prompt="p", width=2368, height=1760))
        self.assertEqual(titled(g, "nuke_aspect")["inputs"]["value"], "4:3")
        g = workflows.build(wf, dict(prompt="p", width=1536, height=2720))
        self.assertEqual(titled(g, "nuke_aspect")["inputs"]["value"], "9:16")
        g = workflows.build(wf, dict(prompt="p", width=2368, height=1760, aspect="1:1"))
        self.assertEqual(titled(g, "nuke_aspect")["inputs"]["value"], "1:1")  # given wins
        g = workflows.build(wf, dict(prompt="p"))
        self.assertEqual(titled(g, "nuke_aspect")["inputs"]["value"], "1:1")  # no size: export's

    def test_aspect_needs_ratios_and_a_size(self):
        for ratios in (None, [], ["4x3"], ["0:1"], "16:9"):
            problems = workflows.validate(self.aspect_workflow(aspect_ratios=ratios))
            self.assertTrue(any("aspect_ratios" in p for p in problems), ratios)
        wf = self.aspect_workflow()
        del wf.spec["bind"]["width"], wf.spec["bind"]["height"]
        self.assertTrue(any("width and height must be bound too" in p
                            for p in workflows.validate(wf)))

    def test_choice_options(self):
        wf = workflows.load("z_image_turbo_t2i")
        knob = wf.knobs[0]
        default = "z_image_turbo_int8_convrot.safetensors"
        self.assertEqual(workflows.choice_options(wf, knob), [default])
        self.assertEqual(
            workflows.choice_options(wf, knob, {"UNETLoader": UNET_INFO}),
            [default, "z_image_base.safetensors"])       # "match" filters qwen
        renamed = {"UNETLoader": {"input": {"required": {"unet_name": [
            ["Z-Image-Turbo-FP16.safetensors", "qwen-image-2.1.safetensors"], {}]}}}}
        self.assertEqual(workflows.choice_options(wf, knob, renamed),
                         [default, "Z-Image-Turbo-FP16.safetensors"])   # - vs _, case
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

    def test_p_h3_aspect_reaches_the_upsampler_and_the_sidecar(self):
        r = self.run_wf("p_h3_image_t2i", values=dict(
            prompt="a ramen shop", seed=5, width=3872, height=2176))
        g = self.submitted()
        self.assertEqual(titled(g, "nuke_aspect")["inputs"]["value"], "16:9")
        self.assertEqual(r["aspect"], "16:9")
        fmt = g[g[cc.find_node(g, "Upsampler input")]["inputs"]["values.b"][0]]
        self.assertEqual(fmt["_meta"]["title"], "nuke_aspect")
        switch = titled(g, "nuke_enhance_switch")["inputs"]
        self.assertEqual(g[switch["on_false"][0]]["_meta"]["title"], "nuke_prompt")

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


class CropGrid(unittest.TestCase):
    """The snapping rule behind CompAI Crop. The node does the same with knob
    expressions; a headless Nuke test checks the two agree."""

    def test_snap_side(self):
        self.assertEqual(grid.snap_side(748, 32, "grow"), 768)
        self.assertEqual(grid.snap_side(748, 32, "nearest"), 736)
        self.assertEqual(grid.snap_side(748, 32, "shrink"), 736)
        self.assertEqual(grid.snap_side(752, 32, "nearest"), 768)   # half up
        for mode in grid.SNAP_MODES:
            self.assertEqual(grid.snap_side(800, 32, mode), 800)     # already on it
            self.assertEqual(grid.snap_side(3, 32, mode), 32)        # never zero

    def test_snap_box_keeps_centre_then_fits_inside(self):
        # Harry's 748 x 612 crop of a 4K plate
        self.assertEqual(grid.snap_box((2308, 876, 3056, 1488), 32),
                         (2298, 862, 768, 640))
        # a box already on the grid is left exactly where it is
        self.assertEqual(grid.snap_box((2298, 862, 3066, 1502), 32),
                         (2298, 862, 768, 640))
        # grown past the right edge: slid back in, or not if bounds aren't given
        self.assertEqual(grid.snap_box((4000, 0, 4090, 90), 32, "grow", (4096, 2160)),
                         (3997, 0, 96, 96))
        self.assertEqual(grid.snap_box((4050, 0, 4096, 90), 32, "grow", (4096, 2160)),
                         (4032, 0, 64, 96))
        self.assertEqual(grid.snap_box((4050, 0, 4096, 90), 32),
                         (4041, -3, 64, 96))
        # bigger than the image: left centred rather than clamped
        self.assertEqual(grid.snap_box((0, 0, 5000, 100), 32, "grow", (4096, 2160))[0],
                         -12)

    def test_default_box_centred_at_model_budget(self):
        x, y, r, t = grid.default_box(4096, 2160, 32, 1.0)
        self.assertEqual((r - x) % 32, 0)
        self.assertEqual((t - y) % 32, 0)
        self.assertAlmostEqual((r - x) * (t - y) / 1024.0 ** 2, 1.0, delta=0.1)
        self.assertEqual((x + r) // 2, 2048)
        self.assertEqual(grid.default_box(300, 200, 32, 1.0), (6, 4, 294, 196))

    def test_targets_are_workflows_with_a_grid(self):
        names = [wf.name for wf in grid.targets()]
        self.assertIn("qwen_edit", names)
        self.assertIn("minimax_h3_image_edit", names)
        self.assertNotIn("qwen_t2i", names)               # no images
        self.assertIn("minimax_h3_fl2va", names)          # video
        self.assertIn("minimax_h3_ref2va", names)         # output on its grid

    def test_describe_warns_outside_the_model_range(self):
        wf = workflows.load("qwen_edit")
        out, model = grid.describe(800, 640, wf)
        self.assertEqual(out, "800 \u00d7 640 px \u00b7 0.49 MP")
        self.assertIn("Qwen Image Edit", model)
        out, _ = grid.describe(256, 256, wf)
        self.assertIn("outside", out)
        out, model = grid.describe(800, 640, None, 50)
        self.assertEqual(model, "manual \u00b7 50 px grid")


class Colorspace(unittest.TestCase):
    """_set_colorspace against the menu entries real configs give."""

    # Copied from a Write in Nuke 17.1 with the ACES 1.3 / OCIO v2.4 config.
    OCIO_V2 = [
        "default",
        "texture_paint\ttexture_paint (sRGB Encoded Rec.709 (sRGB))",
        "ACEScg\tColorspaces/ACES/ACEScg\t\tace_acescg,lin_ap1",
        "sRGB Encoded Rec.709 (sRGB)\tColorspaces/Utility/sRGB Encoded "
        "Rec.709 (sRGB)\t\tsrgb_encoded_rec709_srgb,Utility - sRGB - "
        "Texture,srgb_texture,sRGB - Texture,srgb_tx",
    ]

    def pick(self, items, preferred=""):
        from compai import node
        n = type("N", (), {})()
        knob = nuke.Enumeration_Knob("colorspace", "", items)
        n.knobs = lambda: {"colorspace": knob}
        node._set_colorspace(n, preferred)
        return knob.value()

    def test_ocio_v2_entries(self):
        self.assertEqual(self.pick(self.OCIO_V2), "sRGB Encoded Rec.709 (sRGB)")

    def test_preferred_by_alias(self):
        self.assertEqual(self.pick(self.OCIO_V2, "lin_ap1"), "ACEScg")

    def test_plain_and_path_entries(self):
        self.assertEqual(self.pick(["linear", "sRGB"]), "sRGB")
        self.assertEqual(self.pick(["default", "Output/Output - sRGB"]),
                         "Output/Output - sRGB")


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
                         ["Minimax_H3_Image", "Minimax_H3_Image_Edit", "Minimax_H3_fl2va", "Minimax_H3_ref2va", "P-H3-Image",
                          "Qwen Image",
                          "Qwen Image Edit", "Utility/CompAI Crop",
                          "Utility/CompAI Uncrop", "Z-Image Turbo"])
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
            wf = workflows.load(name)
            edit = bool(wf.max_images and wf.match_format
                        and not wf.spec.get("length"))
            # # an edit goes back over its input; everything else comes out raw
            self.assertIs(g["reformat_output"].value(), edit, name)
            self.assertEqual(g["size_mode"].value(),
                             cnode.SIZE_INPUT if edit else
                             cnode.SIZE_INPUT_MP if wf.max_images and wf.match_format
                             else cnode.SIZE_FORMAT_MP, name)
            self.assertEqual(g["size_mode"].values()[0] == cnode.SIZE_INPUT,
                             bool(wf.max_images or wf.max_videos), name)
            self.assertEqual(g.knob("input_fit") is not None,
                             bool(wf.max_videos or (wf.max_images and wf.resize_inputs)),
                             name)
            self.assertEqual(g.children["ToTarget"]["disable"].expression,
                             "!parent.reformat_output")
            g["reformat_output"].setValue(False)
            g["size_mode"].setValue(cnode.SIZE_FORMAT_MP)
            nuke.fire_knob_changed(g, "size_mode")
            w, h = cc.generation_size(2048, 1080, g["megapixels"].value(), wf.multiple)
            size = "%d \u00d7 %d px \u00b7 %.2f MP" % (w, h, w * h / 1024.0 ** 2)
            self.assertEqual(g["gen_size"].value(), size)
            self.assertTrue(g["megapixels"].enabled())
            g["reformat_output"].setValue(True)
            nuke.fire_knob_changed(g, "reformat_output")
            self.assertEqual(g["gen_size"].value(),
                             size + "  \u2192  result 2048 \u00d7 1080")
            self.assertEqual(g.children["ToTarget"]["resize"].value(), "fill")
            g["size_mode"].setValue(cnode.SIZE_FORMAT)
            nuke.fire_knob_changed(g, "size_mode")
            self.assertFalse(g["megapixels"].enabled())
            self.assertTrue(g["gen_size"].value().startswith(
                "%d \u00d7 %d px" % cc.snap_size(2048, 1080, wf.multiple)))

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
        g.setInput(1, None)                      # pull the middle one:
        self.assertEqual(inputs(), ["img1", "img2", "img3"])     # the rest move down
        self.assertEqual([g.input(i) for i in range(3)], [other, plate, None])
        g.setInput(0, None)                      # and again
        self.assertEqual(inputs(), ["img1", "img2"])
        self.assertEqual([g.input(i) for i in range(2)], [plate, None])
        self.assertIn("img1", g["inputs_info"].value())
        self.assertEqual(g["target_format"].value().name(), "2K_DCP")
        plate._format = nuke.Format(1234, 700, 2.0, "")      # unnamed format
        nuke.fire_knob_changed(g, "inputChange")
        self.assertEqual(g["target_format"].value().width(), 1234)
        self.assertEqual(g["target_format"].value().pixelAspect(), 2.0)

        g["generate"].execute()
        self.wait_idle(g)
        self.assertEqual(nuke.messages[1:], [])
        self.assertTrue(os.path.isfile(os.path.join(
            self.tmp, "edit_in1_plate_f%d_2464x704.png" % nuke.frame())))
        self.assertEqual(g["last_file"].value(), "edit_v001.png")
        self.assertTrue(g.children["write1"]["disable"].value())
        self.assertEqual(g.children["prep1"]["resize"].value(), "fill")
        self.assertFalse(g.children["prep1"]["disable"].value())   # resized

        self.assertEqual(g["size_mode"].value(), "first input")     # its own size
        self.assertEqual((g.children["prep1"]["box_width"].value(),   # squared up,
                          g.children["prep1"]["box_height"].value()), (2464, 704))
        self.assertNotIn("above", g["gen_size"].value())           # 1.65 MP is in range
        self.assertTrue(g["reformat_output"].value())               # back over it
        self.assertEqual(g.children["ToTarget"]["resize"].value(), "fit")  # undoes crop

        plate._format = nuke.Format(1376, 768, 1.0, "")   # on Qwen's 32 grid
        g["generate"].execute()
        self.wait_idle(g)
        self.assertTrue(g.children["prep1"]["disable"].value())    # untouched
        self.assertEqual(titled(self.submitted(), "nuke_latent")["inputs"]["width"], 1376)
        sent = self.submitted()
        self.assertEqual(len(cc.image_slots(sent)), 1)
        self.assertEqual(titled(sent, "nuke_input_1")["inputs"]["image"],
                         "nuke/QwenEdit1_in1.png")
        self.assertIs(titled(sent, "nuke_enhance_switch")["inputs"]["switch"], True)

        g["input_fit"].setValue("stretch")
        nuke.fire_knob_changed(g, "input_fit")
        self.assertEqual(g.children["ToTarget"]["resize"].value(), "distort")
        plate._format = nuke.Format(748, 612, 1.0, "")      # an arbitrary crop
        other._format = nuke.Format(1000, 500, 1.0, "")
        g.setInput(1, other)
        g["generate"].execute()
        self.wait_idle(g)
        prep = g.children["prep1"]
        self.assertEqual((prep["resize"].value(), prep["box_width"].value(),
                          prep["box_height"].value()), ("distort", 736, 608))
        self.assertEqual(g.children["prep2"]["resize"].value(), "distort")
        w, h = (g.children["prep2"][k].value() for k in ("box_width", "box_height"))
        self.assertEqual((w, h), cc.generation_size(1000, 500, 736 * 608 / 1024.0 ** 2, 32))
        self.assertEqual(g["gen_size"].value(),
                         "736 \u00d7 608 px \u00b7 0.43 MP  \u2192  result 748 \u00d7 612")
        g.setInput(1, None)

        plate._format = nuke.Format(4096, 2160, 1.0, "")    # a whole 4K plate
        nuke.fire_knob_changed(g, "inputChange")
        self.assertIn("above Qwen Image Edit's", g["gen_size"].value())
        nuke.answers.append(False)               # "generate anyway?" no
        g["generate"].execute()
        self.assertEqual(nuke.answers, [])       # it asked
        self.assertNotIn(g.fullName(), jobs._JOBS)               # and didn't queue
        self.assertEqual(g["last_file"].value(), "edit_v003.png")

        g.setInput(0, None)                      # last one removed: shrink back
        self.assertEqual(inputs(), ["img1"])
        self.assertIsNone(g.children.get("write2"))

    def test_video_node_with_first_and_last_inputs(self):
        g = self.make("minimax_h3_fl2va")
        def inputs():
            return sorted(n.name() for n in g.children.values() if n.cls == "Input")
        self.assertEqual(inputs(), ["first", "last"])
        self.assertEqual(g["megapixels"].value(), 0.4)
        self.assertEqual(g["size_mode"].value(), "first input at megapixels")
        self.assertFalse(g["reformat_output"].value())           # a clip comes out raw
        self.assertTrue(g["gen_size"].value().startswith(        # 32 grid; no input yet
            "896 \u00d7 480 px \u00b7 0.41 MP \u00b7 no input"))
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
        self.assertTrue(os.path.isfile(os.path.join(
            self.tmp, "video_last_plate_f%d_896x480.png" % nuke.frame())))
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
            written.append(write["file"].value().replace("_writing", ""))
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
        self.assertEqual(os.path.dirname(written[0]), cnode._temp_inputs_dir(g))
        self.addCleanup(shutil.rmtree, os.path.dirname(written[0]), True)
        self.assertEqual(sorted(os.listdir(self.tmp)),
                         ["edit_v001.json", "edit_v001.png"])

        g["instant_cleanup"].setValue(True)                      # now delete at once
        g["generate"].execute()
        self.wait_idle(g)
        self.assertEqual(len(written), 2)                        # off: rendered again
        self.assertFalse(os.path.exists(written[1]))
        self.assertIn(b"fake png", self.comfy.uploads[0])        # Comfy still got it

    def test_reuse_existing_input_images(self):
        g = self.make("qwen_edit")
        self.assertIs(g["reuse_inputs"].value(), False)          # off by default
        plate = nuke.nodes.Read(name="plate")
        plate._format = nuke.Format(768, 640, 1.0, "")
        g.setInput(0, plate)
        written = []
        real = nuke.execute
        def execute(write, first, last):
            written.append(write["file"].value().replace("_writing", ""))
            real(write, first, last)
        nuke.execute = execute
        self.addCleanup(setattr, nuke, "execute", real)

        def run():
            g["generate"].execute()
            self.wait_idle(g)
            self.assertEqual(nuke.messages, [])
            return titled(self.submitted(), "nuke_input_1")["inputs"]

        name = os.path.join(self.tmp, "edit_in1_plate_f%d_768x640.png" % nuke.frame())
        run()
        with open(name, "wb") as f:                              # off: rendered again,
            f.write(b"old")                                      # replacing the file
        run()
        self.assertEqual(written, [name, name])
        with open(name, "rb") as f:
            self.assertEqual(f.read(), b"fake png")
        self.assertFalse([f for f in os.listdir(self.tmp) if "_writing" in f])

        g["reuse_inputs"].setValue(True)                         # on: the one made
        run()                                                    # with it off is
        run()                                                    # sent again
        self.assertEqual(len(written), 2)
        self.assertTrue(g["status"].value().endswith("(1 of 1 input reused)"),
                        g["status"].value())
        plate._format = nuke.Format(1024, 512, 1.0, "")         # other size: new file
        run()
        self.assertEqual(os.path.basename(written[-1]),
                         "edit_in1_plate_f%d_1024x512.png" % nuke.frame())
        g.setInput(0, nuke.nodes.Read(name="other"))            # other node: new file
        run()
        self.assertIn("_in1_other_", written[-1])
        self.assertEqual(len(written), 4)

        g["temp_inputs"].setValue(True)                         # reuse in temp:
        g["instant_cleanup"].setValue(True)                     # one stable folder,
        nuke.fire_knob_changed(g, "temp_inputs")                # never cleaned
        self.assertFalse(g["instant_cleanup"].enabled())
        run()
        run()
        self.assertEqual(len(written), 5)
        folder = os.path.dirname(written[-1])
        self.addCleanup(shutil.rmtree, folder, True)
        self.assertEqual(folder, cnode._temp_inputs_dir(g))
        self.assertTrue(os.path.isfile(written[-1]))

        real_write = cnode._write_input                          # a render that dies
        def dies(node, n, index, path, frame):                   # leaves nothing
            open(path, "wb").close()                             # to be reused
            raise RuntimeError("render failed")
        cnode._write_input = dies
        self.addCleanup(setattr, cnode, "_write_input", real_write)
        g.setInput(0, nuke.nodes.Read(name="third"))
        g["generate"].execute()
        self.assertIn("render failed", nuke.messages[-1])
        self.assertFalse([f for f in os.listdir(folder) if "third" in f])

    def test_open_folder(self):
        g = self.make("qwen_t2i")
        opened = []
        real = cnode._open_in_file_browser
        cnode._open_in_file_browser = opened.append
        self.addCleanup(setattr, cnode, "_open_in_file_browser", real)
        g["output_dir"].setValue(os.path.join(self.tmp, "not_yet"))
        g["open_folder"].execute()
        self.assertEqual(opened, [])
        self.assertIn("doesn't exist yet", nuke.messages[-1])
        g["output_dir"].setValue(self.tmp)
        compai.on_open_folder(g)
        self.assertEqual(opened, [self.tmp])

    def test_temporary_inputs_are_deleted_when_the_run_fails(self):
        g = self.make("qwen_edit")
        g.setInput(0, nuke.nodes.Read(name="plate"))
        g["temp_inputs"].setValue(True)
        g["instant_cleanup"].setValue(True)
        g["host"].setValue("127.0.0.1:9")
        folder = cnode._temp_inputs_dir(g)
        self.addCleanup(shutil.rmtree, folder, True)
        g["generate"].execute()
        self.wait_idle(g)
        self.assertIn("failed", nuke.messages[-1])
        self.assertTrue(rendered())
        self.assertEqual(os.listdir(folder), [])

    def test_reference_video_node(self):
        g = self.make("minimax_h3_ref2va")
        self.assertEqual(g.name(), "MiniMaxRefVideo1")
        self.assertEqual(g["size_mode"].value(), "format at megapixels")  # refs don't set it
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
        self.assertEqual(g["video_range"].value(), "input's own range")
        self.assertEqual(g["video_info"].value(), "vid1: frames 1001-1072 (72)")
        g["video_range"].setValue("input's range, capped to output length")
        nuke.fire_knob_changed(g, "video_range")           # 2 s x 24 fps = 48
        self.assertEqual(g["video_info"].value(), "vid1: frames 1001-1048 (48)")
        g["duration"].setValue(1)
        nuke.fire_knob_changed(g, "duration")
        self.assertEqual(g["video_info"].value(), "vid1: frames 1001-1024 (24)")
        g["duration"].setValue(10)                         # longer than the clip
        nuke.fire_knob_changed(g, "duration")
        self.assertEqual(g["video_info"].value(), "vid1: frames 1001-1072 (72)")
        g["video_range"].setValue("input's own range")
        g["duration"].setValue(2)

        g["generate"].execute()
        self.wait_idle(g)
        self.assertEqual(nuke.messages, [])
        mov = os.path.join(self.tmp, "video_vid1_clip_f1001-1072_864x480_24fps.mov")
        self.assertIn((mov, 1001, 1072), rendered())            # the input's range
        self.assertTrue(os.path.isfile(mov))
        self.assertIn((os.path.join(self.tmp, "video_in1_still_f1001_2048x1080.png"),
                       1001, 1001), rendered())
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
        self.assertIn((os.path.join(
            self.tmp, "video_vid1_clip_f1010-1033_864x480_24fps.mov"), 1010, 1033),
            rendered())

        g["video_range"].setValue("project range")       # 1001-1100 in the stub
        g["generate"].execute()
        self.wait_idle(g)
        self.assertIn((os.path.join(
            self.tmp, "video_vid1_clip_f1001-1100_864x480_24fps.mov"), 1001, 1100),
            rendered())
        g["reuse_inputs"].setValue(True)                 # same range: not rendered
        nuke.executed[:] = []
        g["generate"].execute()
        self.wait_idle(g)
        self.assertEqual(rendered(), [])
        self.assertEqual(titled(self.submitted(), "nuke_video_1")["inputs"]["file"],
                         "nuke/MiniMaxRefVideo1_vid1.mov")
        self.assertIn(b"fake png", self.comfy.uploads[-1])      # the kept file went

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
        g.setInput(2, b)                                 # img2 again, fed by b
        g.setInput(0, None)                              # pull img1: img2 -> img1,
        self.assertEqual(pipes(), [("img1", True), ("vid1", True),  # video untouched
                                   ("img2", False), ("vid2", False)])
        self.assertIs(g.input(0), b)
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
