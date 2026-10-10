import traceback

import nuke

try:
    import compai
    compai.add_menu()
    print("\033[34m>> CompAI loaded.\033[0m")
except Exception:
    print("\033[31m>> Failed to load compai:")
    traceback.print_exc()
    print("\033[0m")
