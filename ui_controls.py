"""Small native Tk controls for the mockup's rounded dark surfaces."""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from ui_icons import icon as load_icon


def bind_size_redraw(widget, callback):
    """Ignore position-only Configure events during top-level movement."""
    previous = None
    def changed(event):
        nonlocal previous
        if event.widget is not widget:
            return
        size = (event.width, event.height)
        if size != previous:
            previous = size
            callback()
    tk.Misc.bind(widget, "<Configure>", changed)
    return changed


def rounded_rect(canvas, x0, y0, x1, y1, radius=7, **options):
    radius = min(radius, (x1-x0)/2, (y1-y0)/2)
    return canvas.create_polygon(
        x0+radius, y0, x1-radius, y0, x1, y0, x1, y0+radius,
        x1, y1-radius, x1, y1, x1-radius, y1, x0+radius, y1,
        x0, y1, x0, y1-radius, x0, y0+radius, x0, y0,
        smooth=True, splinesteps=20, **options,
    )


class RoundedButton(tk.Canvas):
    def __init__(self, parent, *, text, command, bg, fg, activebackground,
                 activeforeground, font, padx=16, pady=9, outline="", **_unused):
        self._values = dict(text=text, command=command, bg=bg, fg=fg,
                            activebackground=activebackground, activeforeground=activeforeground,
                            font=font, padx=padx, pady=pady, anchor="center", state="normal",
                            highlightbackground=outline, highlightthickness=1 if outline else 0,
                            icon=None, trailing_icon=None)
        self._hovered = False
        super().__init__(parent, bg=parent.cget("bg"), bd=0, highlightthickness=0,
                         takefocus=1, cursor="hand2")
        self._text_font = tkfont.Font(root=self, font=font)
        self._measure()
        bind_size_redraw(self, self._draw)
        self.bind("<Enter>", lambda _event: self._hover(True))
        self.bind("<Leave>", lambda _event: self._hover(False))
        self.bind("<ButtonRelease-1>", lambda _event: self.invoke())
        self.bind("<Return>", lambda _event: self.invoke())
        self.bind("<space>", lambda _event: self.invoke())

    def _measure(self):
        self._text_font.configure(**tkfont.Font(root=self, font=self._values["font"]).actual())
        icon_width = 30 if self._values["icon"] else 0
        trailing_width = 25 if self._values["trailing_icon"] else 0
        super().configure(width=self._text_font.measure(self._values["text"])+2*int(self._values["padx"])+icon_width+trailing_width,
                          height=self._text_font.metrics("linespace")+2*int(self._values["pady"]))

    def configure(self, cnf=None, **kwargs):
        if cnf:
            kwargs.update(cnf)
        changed = False
        for name in list(kwargs):
            alias = {"background": "bg", "foreground": "fg"}.get(name, name)
            if alias in self._values:
                self._values[alias] = kwargs.pop(name)
                changed = True
        for ignored in ("relief", "borderwidth", "bd"):
            kwargs.pop(ignored, None)
        if kwargs:
            super().configure(**kwargs)
        if changed:
            self._measure()
            self._draw()

    config = configure

    def cget(self, name):
        alias = {"background": "bg", "foreground": "fg"}.get(name, name)
        return self._values[alias] if alias in self._values else super().cget(name)

    def invoke(self):
        if self._values["state"] != "disabled" and self._values["command"]:
            return self._values["command"]()

    def _hover(self, entered):
        self._hovered = entered
        self._draw()

    def _draw(self):
        if not hasattr(self, "_text_font"):
            return
        self.delete("all")
        width = max(self.winfo_width(), self.winfo_reqwidth()) if self.winfo_width() <= 1 else self.winfo_width()
        height = max(self.winfo_height(), self.winfo_reqheight()) if self.winfo_height() <= 1 else self.winfo_height()
        disabled = self._values["state"] == "disabled"
        bg = "#233044" if disabled else self._values["activebackground"] if self._hovered else self._values["bg"]
        fg = "#77849a" if disabled else self._values["activeforeground"] if self._hovered else self._values["fg"]
        border = self._values["highlightbackground"] if self._values["highlightthickness"] else ""
        rounded_rect(self, 1, 1, width-1, height-1, radius=7, fill=bg, outline=border, width=1)
        anchor = self._values["anchor"]
        text_width = self._text_font.measure(self._values["text"])
        group_width = text_width + (30 if self._values["icon"] else 0)
        x = int(self._values["padx"]) if anchor == "w" else (width-group_width)/2
        tone = "pink" if fg.lower() == "#fe2c73" else "muted" if disabled or fg.lower() == "#a0acc0" else "normal"
        if self._values["icon"]:
            self.create_image(x+11,height/2,image=load_icon(self,self._values["icon"],24,tone))
            x += 30
        self.create_text(x, height/2, text=self._values["text"], fill=fg,
                         font=self._text_font, anchor="w")
        if self._values["trailing_icon"]:
            self.create_image(width-18,height/2,image=load_icon(self,self._values["trailing_icon"],16,"muted"))


class RoundedEntry(tk.Frame):
    def __init__(self, parent, variable, palette, width=20, height=24, icon=None):
        self.palette = palette
        self.leading_icon = icon
        super().__init__(parent, bg=parent.cget("bg"), width=width*8, height=height, bd=0)
        self.pack_propagate(False)
        self.canvas = tk.Canvas(self, bg=parent.cget("bg"), bd=0, highlightthickness=0)
        self.canvas.place(x=0,y=0,relwidth=1,relheight=1)
        self.entry = tk.Entry(self, textvariable=variable, bg=palette["raised"], fg=palette["title"],
                              insertbackground=palette["title"], selectbackground=palette["primary_dark"],
                              relief="flat", bd=0, highlightthickness=0,
                              font=("Microsoft YaHei UI",10))
        left = 36 if icon else 10
        self.entry.place(x=left,y=5,relwidth=1,width=-left-10,relheight=1,height=-10)
        bind_size_redraw(self, self._draw)
        self.entry.bind("<FocusIn>",lambda _event:self._draw(True),add=True)
        self.entry.bind("<FocusOut>",lambda _event:self._draw(False),add=True)

    def _draw(self, focused=False):
        self.canvas.delete("all")
        rounded_rect(self.canvas,1,1,self.winfo_width()-1,self.winfo_height()-1,7,
                     fill=self.palette["raised"],outline=self.palette["primary"] if focused else self.palette["border"])
        if self.leading_icon:
            self.canvas.create_image(19,self.winfo_height()/2,image=load_icon(self,self.leading_icon,20,"muted"))

    def bind(self, sequence=None, func=None, add=None):
        return self.entry.bind(sequence,func,add)

    def configure(self, cnf=None, **kwargs):
        if cnf: kwargs.update(cnf)
        forwarded = {key:kwargs.pop(key) for key in list(kwargs) if key in {"fg","foreground","insertbackground","font"}}
        if forwarded: self.entry.configure(**forwarded)
        if kwargs: super().configure(**kwargs)

    config = configure

    def focus_set(self): self.entry.focus_set()


class RoundedProgress(tk.Canvas):
    def __init__(self,parent,palette,*,maximum=100,variable=None,length=100,height=8):
        self.palette=palette
        self._maximum=maximum
        self._variable=variable
        self._value=0
        super().__init__(parent,width=length,height=height,bg=parent.cget("bg"),bd=0,highlightthickness=0)
        bind_size_redraw(self, self._draw)
        if variable is not None: variable.trace_add("write",lambda *_args:self._draw())

    def configure(self,cnf=None,**kwargs):
        if cnf: kwargs.update(cnf)
        if "maximum" in kwargs: self._maximum=kwargs.pop("maximum")
        if "value" in kwargs: self._value=kwargs.pop("value")
        if kwargs: super().configure(**kwargs)
        self._draw()

    config=configure

    def cget(self,name):
        if name=="value": return self._variable.get() if self._variable is not None else self._value
        if name=="maximum": return self._maximum
        return super().cget(name)

    def _draw(self):
        self.delete("all")
        width,height=self.winfo_width(),self.winfo_height()
        if width<2 or height<2:return
        rounded_rect(self,0,0,width,height,height/2,fill=self.palette["track"],outline="")
        fraction=max(0,min(1,float(self.cget("value"))/max(1,float(self._maximum))))
        if fraction: rounded_rect(self,0,0,width*fraction,height,height/2,fill=self.palette["primary"],outline="")


def round_card_corners(card, palette, radius=9):
    corners = []
    for options in (
        dict(x=radius, y=0, relwidth=1, width=-2*radius, height=1),
        dict(x=radius, rely=1, y=-1, relwidth=1, width=-2*radius, height=1),
        dict(x=0, y=radius, width=1, relheight=1, height=-2*radius),
        dict(relx=1, x=-1, y=radius, width=1, relheight=1, height=-2*radius),
    ):
        edge = tk.Frame(card, bg=palette["border"], bd=0)
        edge.place(**options)
        corners.append(edge)
    for relx, rely, dx, dy, oval in (
        (0, 0, 0, 0, (0, 0, radius*2, radius*2)),
        (1, 0, -radius, 0, (-radius, 0, radius, radius*2)),
        (0, 1, 0, -radius, (0, -radius, radius*2, radius)),
        (1, 1, -radius, -radius, (-radius, -radius, radius, radius)),
    ):
        corner = tk.Canvas(card, width=radius, height=radius, bd=0, highlightthickness=0,
                           bg=palette["bg"])
        corner.create_oval(*oval, fill=palette["surface"], outline=palette["border"], width=1)
        corner.place(relx=relx, rely=rely, x=dx, y=dy)
        corners.append(corner)
    bind_size_redraw(card, lambda: [tk.Misc.tkraise(corner) for corner in corners])
    card.after_idle(lambda: [tk.Misc.tkraise(corner) for corner in corners])
