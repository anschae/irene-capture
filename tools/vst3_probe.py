"""Diagnostics for plugins that pedalboard can't load: python tools/vst3_probe.py <plugin path> [name]"""

import glob
import os
import sys
import traceback

import pedalboard

path = sys.argv[1]
name = sys.argv[2] if len(sys.argv) > 2 else None
print("pedalboard", pedalboard.__version__, "| path exists:", os.path.exists(path))
try:
    print("names:", pedalboard.VST3Plugin.get_plugin_names_for_file(path) if path.endswith(".vst3") else
          pedalboard.AudioUnitPlugin.get_plugin_names_for_file(path))
except Exception as e:
    print("names failed:", e)
try:
    p = pedalboard.load_plugin(path, **({"plugin_name": name} if name else {}))
    print("load OK:", p.name, "| params:", len(p.parameters))
except Exception:
    traceback.print_exc()
if sys.platform == "win32":
    import ctypes

    for dll in glob.glob(os.path.join(path, "Contents", "*", "*.vst3")):
        print("inner module:", dll)
        try:
            h = ctypes.WinDLL(dll)
            print("  LoadLibrary OK; GetPluginFactory exported:", hasattr(h, "GetPluginFactory"))
        except OSError as e:
            print("  LoadLibrary FAILED:", e)
        try:
            print("  load inner file:", pedalboard.load_plugin(dll).name)
        except Exception as e:
            print("  load inner file failed:", e)
