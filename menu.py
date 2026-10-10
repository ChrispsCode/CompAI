try:
    import comfy_nuke
    nuke.menu("Nodes").addCommand("Comfy/ComfyGenerate", comfy_nuke.create)
    print(">> Comfy Nuke module imported successfully.")
finally:
    import traceback
    print('Failed it import comfy_nuke:')
    traceback.print_exc()
