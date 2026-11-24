"""Keep tkinter discoverable when the build Python lacks init.tcl.

The application bundles a known-good Tcl/Tk runtime explicitly in the spec,
so PyInstaller's default probe must not remove tkinter from the module graph.
"""


def pre_find_module_path(hook_api):
    return None
