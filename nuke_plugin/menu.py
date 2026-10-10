import traceback

import nuke

try:
    import comfy_nuke
    import comfy_nuke_edit
    menu = nuke.menu("Nodes").addMenu("Comfy")
    menu.addCommand("ComfyGenerate", comfy_nuke.create)
    menu.addCommand("ComfyEdit", comfy_nuke_edit.create)
    print("\033[34m>> >> Comfy Nuke module imported successfully.\033[0m")
except Exception:
    print("\033[31m>> Failed to import comfy_nuke:")
    traceback.print_exc()
    print("\033[0m")
