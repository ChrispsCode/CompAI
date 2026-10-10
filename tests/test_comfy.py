"""
Offline tests: template patching, the client against a fake Comfy server,
and the Nuke nodes against a fake nuke module.

    python3 -m unittest discover -s tests -v

None of this replaces a run in real Nuke against real Comfy.
"""

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
import comfy_client as cc
import comfy_nuke
import comfy_nuke_edit
from fake_comfy import FakeComfy, IMAGE_BYTES

T2I = os.path.join(PLUGIN, "templates", "basic_t2i_qwen_api_v002.json")
EDIT = os.path.join(PLUGIN, "templates", "qwen_i2i_template_v002.json")


def dangling(wf):
    return [(nid, k) for nid, n in wf.items() for k, v in n["inputs"].items()
            if isinstance(v, list) and len(v) == 2 and v[0] not in wf]


def titled(wf, title):
    return wf[cc.find_node(wf, title)]


class TemplatePatching(unittest.TestCase):
    def test_generation_size(self):
        self.assertEqual(cc.generation_size(1024, 1024), (1024, 1024))
        w, h = cc.generation_size(2048, 1080)
        self.assertEqual((w % 16, h % 16), (0, 0))
        self.assertAlmostEqual(w / h, 2048 / 1080, delta=0.05)
        self.assertAlmostEqual(w * h, 1024 * 1024, delta=1024 * 1024 * 0.05)

    def test_t2i(self):
        tmpl = cc.load_template(T2I)
        wf, seed = cc.build_qwen_t2i(tmpl, "a ring", 1408, 736, seed=7,
                                     steps=12, enhance=True)
        self.assertEqual(seed, 7)
        self.assertEqual(titled(wf, "nuke_prompt")["inputs"]["value"], "a ring")
        self.assertEqual(titled(wf, "nuke_ksampler")["inputs"]["seed"], 7)
        self.assertEqual(titled(wf, "nuke_ksampler")["inputs"]["steps"], 12)
        self.assertEqual(
            titled(wf, "nuke_generate_text")["inputs"]["sampling_mode.seed"], 7)
        self.assertIs(titled(wf, "nuke_enhance_switch")["inputs"]["switch"], True)
        self.assertEqual(titled(wf, "nuke_latent")["inputs"]["width"], 1408)
        self.assertEqual(titled(wf, "nuke_prompt")["inputs"]["value"], "a ring")
        self.assertEqual(tmpl, cc.load_template(T2I))      # template untouched

    def test_edit_prunes_unused_slots(self):
        tmpl = cc.load_template(EDIT)
        self.assertEqual(len(cc.image_slots(tmpl)), 10)
        for n in (1, 2, 4, 10):
            for mode in ("upload", "path"):
                imgs = ["/x/in%d.png" % i for i in range(n)]
                wf, _ = cc.build_qwen_edit(tmpl, "p", imgs, 1024, 1024,
                                           image_mode=mode)
                self.assertEqual(len(cc.image_slots(wf)), n)
                self.assertEqual(dangling(wf), [])
                first = titled(wf, "nuke_input_1")
                if mode == "path":
                    self.assertEqual(first["class_type"], cc.PATH_LOADER)
                    self.assertEqual(first["inputs"], {"path": imgs[0]})
                else:
                    self.assertEqual(first["inputs"]["image"], imgs[0])

    def test_edit_size_switch(self):
        tmpl = cc.load_template(EDIT)
        wf, _ = cc.build_qwen_edit(tmpl, "p", ["a"])
        self.assertIs(titled(wf, "nuke_size_switch")["inputs"]["switch"], False)
        wf, _ = cc.build_qwen_edit(tmpl, "p", ["a"], 1408, 736)
        self.assertIs(titled(wf, "nuke_size_switch")["inputs"]["switch"], True)
        self.assertEqual(titled(wf, "nuke_latent")["inputs"]["height"], 736)

    def test_edit_errors(self):
        tmpl = cc.load_template(EDIT)
        with self.assertRaises(cc.ComfyError):
            cc.build_qwen_edit(tmpl, "p", [])
        with self.assertRaises(cc.ComfyError):
            cc.build_qwen_edit(tmpl, "p", ["x"] * 11)


class WithFakeComfy(unittest.TestCase):
    def setUp(self):
        self.comfy = FakeComfy()
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self.comfy.close)
        self.addCleanup(shutil.rmtree, self.tmp)

    def plate(self, name="plate.png"):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(b"plate bytes")
        return path


class Client(WithFakeComfy):
    def test_generate(self):
        out = os.path.join(self.tmp, "sub", "gen_v001.png")
        r = cc.generate(T2I, "a ring", out, 1408, 736, seed=3,
                        host=self.comfy.host, log=lambda m: None)
        with open(out, "rb") as f:
            self.assertEqual(f.read(), IMAGE_BYTES)
        self.assertEqual(r["seed"], 3)
        self.assertEqual(r["used_prompt"], "the prompt that was used")
        with open(os.path.join(self.tmp, "sub", "gen_v001.json")) as f:
            side = json.load(f)
        self.assertEqual(side["result"]["seed"], 3)
        self.assertIn("workflow", side)

    def test_unreachable(self):
        with self.assertRaises(cc.ComfyError):
            cc.generate(T2I, "p", os.path.join(self.tmp, "x.png"),
                        host="127.0.0.1:9", log=lambda m: None)

    def test_edit_auto_uploads_without_custom_node(self):
        out = os.path.join(self.tmp, "edit_v001.png")
        r = cc.generate_edit(EDIT, "night", [self.plate()], out,
                             host=self.comfy.host, upload_names=["N_in1.png"],
                             log=lambda m: None)
        self.assertEqual(r["image_mode"], "upload")
        self.assertEqual(len(self.comfy.uploads), 1)
        self.assertIn(b"plate bytes", self.comfy.uploads[0])
        wf = list(self.comfy.prompts.values())[0]
        self.assertEqual(titled(wf, "nuke_input_1")["inputs"]["image"],
                         "nuke/N_in1.png")

    def test_edit_auto_uses_path_with_custom_node(self):
        self.comfy.node_classes.add(cc.PATH_LOADER)
        plate = self.plate()
        r = cc.generate_edit(EDIT, "night", [plate],
                             os.path.join(self.tmp, "edit_v001.png"),
                             host=self.comfy.host, log=lambda m: None)
        self.assertEqual(r["image_mode"], "path")
        self.assertEqual(self.comfy.uploads, [])
        wf = list(self.comfy.prompts.values())[0]
        self.assertEqual(titled(wf, "nuke_input_1")["inputs"]["path"], plate)

    def test_edit_path_mode_needs_custom_node(self):
        with self.assertRaises(cc.ComfyError):
            cc.generate_edit(EDIT, "p", [self.plate()],
                             os.path.join(self.tmp, "e.png"),
                             host=self.comfy.host, input_mode="path",
                             log=lambda m: None)

    def test_cancel_interrupts(self):
        self.comfy.hold = True
        with self.assertRaises(cc.ComfyError) as ctx:
            cc.generate(T2I, "p", os.path.join(self.tmp, "x.png"),
                        host=self.comfy.host, should_cancel=lambda: True,
                        log=lambda m: None)
        self.assertEqual(str(ctx.exception), "Cancelled")
        self.assertTrue(self.comfy.interrupted)


class NukeNodes(WithFakeComfy):
    def setUp(self):
        WithFakeComfy.setUp(self)
        nuke.reset()
        comfy_nuke._JOBS.clear()

    def wait_idle(self, node):
        end = time.time() + 10
        while node.fullName() in comfy_nuke._JOBS:
            self.assertLess(time.time(), end, "job never finished")
            time.sleep(0.02)

    def configure(self, node):
        node["prompt"].setValue("a ring in a box")
        node["host"].setValue(self.comfy.host)
        node["output_dir"].setValue(self.tmp)

    def test_generate_node(self):
        g = comfy_nuke.create()
        self.assertEqual(g.name(), "ComfyGenerate1")
        self.assertIn("reformat to 2048 x 1080", g["gen_size"].value())
        self.configure(g)
        g["generate"].execute()
        self.wait_idle(g)
        self.assertEqual(nuke.messages, [])
        out = os.path.join(self.tmp, "gen_v001.png")
        self.assertEqual(g.children["ComfyRead"]["file"].value(), out)
        self.assertTrue(os.path.isfile(out))
        self.assertEqual(g["last_file"].value(), "gen_v001.png")
        self.assertEqual(g["last_seed"].value(), g["seed"].value())
        self.assertTrue(g["status"].value().startswith("done in"))
        self.assertTrue(g["generate"].enabled())

        g["generate"].execute()                      # versions up
        self.wait_idle(g)
        self.assertEqual(g["last_file"].value(), "gen_v002.png")

        g["reuse_seed"].execute()
        self.assertIs(g["random_seed"].value(), False)

    def test_generate_validation(self):
        g = comfy_nuke.create()
        g["generate"].execute()
        self.assertEqual(nuke.messages, ["Enter a prompt first."])
        self.configure(g)
        g["template"].setValue("/nowhere/missing.json")
        g["generate"].execute()
        self.assertIn("Workflow template not found", nuke.messages[-1])

    def test_template_survives_a_moved_install(self):
        g = comfy_nuke.create()
        self.configure(g)
        g["template"].setValue("/old/install/templates/" + os.path.basename(T2I))
        g["generate"].execute()
        self.wait_idle(g)
        self.assertEqual(nuke.messages, [])
        self.assertEqual(g["last_file"].value(), "gen_v001.png")

    def test_error_is_reported_and_node_unlocks(self):
        g = comfy_nuke.create()
        self.configure(g)
        g["host"].setValue("127.0.0.1:9")
        g["generate"].execute()
        self.wait_idle(g)
        self.assertIn("ComfyGenerate1 failed", nuke.messages[-1])
        self.assertTrue(g["generate"].enabled())

    def test_cancel(self):
        self.comfy.hold = True
        g = comfy_nuke.create()
        self.configure(g)
        g["generate"].execute()
        self.assertFalse(g["generate"].enabled())
        g["generate"].execute()
        self.assertIn("already running", nuke.messages[-1])
        g["cancel"].execute()
        self.wait_idle(g)
        self.assertEqual(g["status"].value(), "cancelled")
        self.assertTrue(self.comfy.interrupted)

    def test_stale_lock_released_on_panel_open(self):
        g = comfy_nuke.create()
        g["generate"].setEnabled(False)          # as loaded from a saved script
        nuke.fire_knob_changed(g, "showPanel")
        self.assertTrue(g["generate"].enabled())

    def test_edit_node(self):
        g = comfy_nuke_edit.create()
        self.assertEqual(g.name(), "ComfyEdit1")
        self.assertEqual(len(nuke.allNodes()), 1)
        self.configure(g)
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
        wf = list(self.comfy.prompts.values())[0]
        self.assertEqual(len(cc.image_slots(wf)), 1)
        self.assertEqual(titled(wf, "nuke_input_1")["inputs"]["image"],
                         "nuke/ComfyEdit1_in1.png")

    def test_menu(self):
        import importlib
        import menu
        importlib.reload(menu)
        self.assertEqual(sorted(nuke.menu("Nodes").commands),
                         ["ComfyEdit", "ComfyGenerate"])


if __name__ == "__main__":
    unittest.main()
