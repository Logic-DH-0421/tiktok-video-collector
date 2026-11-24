from pathlib import Path
import tkinter as tk


def icon(master, name, size=20, tone="normal"):
    root = master._root()
    if not hasattr(root, "_ui_icon_images"):
        root._ui_icon_images = {}
    key = (name, size, tone)
    if key not in root._ui_icon_images:
        root._ui_icon_images[key] = tk.PhotoImage(master=root,
            file=str(Path(__file__).parent / "assets" / "icons" / f"{name}-{tone}-{size}.png"))
    return root._ui_icon_images[key]
