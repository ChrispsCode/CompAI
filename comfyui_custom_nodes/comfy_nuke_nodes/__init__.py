"""
comfy_nuke_nodes - ComfyUI custom nodes for the Nuke bridge.

Install: copy this folder to ComfyUI/custom_nodes/ and restart Comfy.

NukeLoadImagePath loads an image from an absolute path, so Nuke can point a
workflow at files on disk instead of uploading copies into Comfy's input dir.
Outputs match the stock LoadImage (IMAGE, MASK), so the client can swap it in
for any LoadImage node at submit time.

Note: this lets anything that can queue prompts on this server read any image
the Comfy process can read. Fine for a local box; think twice before exposing
the server on a network.
"""

import hashlib
import os

import numpy as np
import torch
from PIL import Image, ImageOps


class NukeLoadImagePath:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"path": ("STRING", {"default": "", "multiline": False})}}

    RETURN_TYPES = ("IMAGE", "MASK")
    FUNCTION = "load"
    CATEGORY = "image/nuke"

    def load(self, path):
        img = ImageOps.exif_transpose(Image.open(path))
        rgb = np.array(img.convert("RGB")).astype(np.float32) / 255.0
        image = torch.from_numpy(rgb)[None, ]                    # [1, H, W, 3]
        if "A" in img.getbands():
            alpha = np.array(img.getchannel("A")).astype(np.float32) / 255.0
            mask = 1.0 - torch.from_numpy(alpha)                 # LoadImage convention
        else:
            mask = torch.zeros(rgb.shape[:2], dtype=torch.float32)
        return (image, mask.unsqueeze(0))

    @classmethod
    def IS_CHANGED(cls, path):
        # Re-run when the file's contents change, even if the path doesn't.
        if not os.path.isfile(path):
            return ""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    @classmethod
    def VALIDATE_INPUTS(cls, path):
        if not os.path.isfile(path):
            return "File not found: %s" % path
        return True


NODE_CLASS_MAPPINGS = {"NukeLoadImagePath": NukeLoadImagePath}
NODE_DISPLAY_NAME_MAPPINGS = {"NukeLoadImagePath": "Load Image (Path, Nuke)"}
