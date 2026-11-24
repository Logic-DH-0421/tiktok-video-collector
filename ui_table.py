"""Canvas presentation of video rows; the existing Treeview remains the selection model."""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont, messagebox
from ui_controls import rounded_rect, bind_size_redraw
from ui_icons import icon as load_icon
from ui_thumbnails import thumbnail
from feed_metrics import display_metric


ROW_HEIGHT = 48
HEADER_HEIGHT = 43
ORDER = ("check", "number", "title", "status", "progress", "category",
         "duration", "size", "downloaded", "source_url")
HEADINGS = {"number": "#", "title": "标题", "status": "状态", "progress": "进度",
            "category": "类别", "duration": "时长", "size": "大小",
            "downloaded": "下载时间", "source_url": "原视频链接"}
CATEGORY_COLORS = {"美食": ("#f59e58", "#573421"), "宠物": ("#73a7ef", "#263f61"),
                   "搞笑": ("#f4a544", "#5a3a1e"), "风光": ("#4ad7a3", "#204e44"),
                   "美女": ("#c190ed", "#493059"), "其他": ("#a3abc1", "#353d50")}
STATUS_COLORS = {"已完成": "#35d295", "正在下载": "#ff3786", "排队中": "#aab5c8",
                 "等待人工处理": "#f3be66", "失败": "#f47579", "已跳过": "#aab5c8"}


def build_visual_table(w, parent, palette: dict) -> None:
    host = tk.Frame(parent, bg=palette["surface"])
    host.pack(fill="both", expand=True, padx=1, pady=(0, 1))
    w.table_header_canvas = tk.Canvas(host, height=HEADER_HEIGHT, bg=palette["raised"],
                                      highlightthickness=0, bd=0)
    w.table_header_canvas.pack(fill="x")
    area = tk.Frame(host, bg=palette["surface"])
    area.pack(fill="both", expand=True)
    w.table_canvas = tk.Canvas(area, bg=palette["surface"], height=60,
                               highlightthickness=0, bd=0, takefocus=1)
    scrollbar = tk.Canvas(area, width=9, bg=palette["surface"], highlightthickness=0, bd=0)
    scrollbar.bind("<Button-1>", lambda event: _scrollbar_seek(w, event))
    scrollbar.bind("<B1-Motion>", lambda event: _scrollbar_seek(w, event))
    w.table_canvas.pack(side="left", fill="both", expand=True)
    w.table_scrollbar = scrollbar
    w.visual_rows = []
    bind_size_redraw(w.table_canvas, lambda: redraw(w))
    bind_size_redraw(w.table_header_canvas, lambda: draw_header(w))
    w.table_header_canvas.bind("<Button-1>", lambda event: _header_click(w, event))
    w.table_canvas.bind("<Button-1>", lambda event: _click(w, event))
    w.table_canvas.bind("<Double-1>", lambda event: _double_click(w, event))
    w.table_canvas.bind("<Button-3>", lambda event: _right_click(w, event))
    w.table_canvas.bind("<MouseWheel>", lambda event: _wheel(w, event))
    w.table_canvas.bind("<Control-a>", lambda _event: (w.select_all_rows(), redraw(w), "break")[-1])
    w.table_canvas.bind("<Escape>", lambda _event: (w.tree.selection_remove(*w.tree.selection()), redraw(w)))
    w.table_canvas.bind("<Return>", w.open_selected)
    w.table_canvas.bind("<Delete>", lambda _event: w.delete_selected())


def _layout(width: int) -> dict[str, tuple[int, int]]:
    fixed = {"check": 42, "number": 40, "status": 111, "progress": 155,
             "category": 90, "duration": 72, "size": 73, "downloaded": 147}
    if width < 1240:
        fixed = {"check": 34, "number": 30, "status": 84, "progress": 112,
                 "category": 74, "duration": 58, "size": 62, "downloaded": 119}
    flexible = max(140, width - sum(fixed.values()) - 2)
    widths = {**fixed, "title": int(flexible * .54), "source_url": flexible - int(flexible * .54)}
    x = 0
    result = {}
    for name in ORDER:
        result[name] = (x, x + widths[name])
        x += widths[name]
    return result


def _font(w, size: int = 10, bold: bool = False):
    if not hasattr(w, "_table_fonts"):
        w._table_fonts = {}
    key = (size, bold)
    if key not in w._table_fonts:
        w._table_fonts[key] = tkfont.Font(root=w, family="Microsoft YaHei UI", size=size,
                                         weight="bold" if bold else "normal")
    return w._table_fonts[key]


def _trim(text: object, width: int, font) -> str:
    value = str(text or "")
    if font.measure(value) <= width:
        return value
    while value and font.measure(value + "…") > width:
        value = value[:-1]
    return value + "…"


def _label(canvas, x: int, y: int, text: object, *, width: int, font, color: str,
           anchor: str = "w", **kwargs) -> None:
    canvas.create_text(x, y, text=_trim(text, width, font), anchor=anchor,
                       fill=color, font=font, **kwargs)


def draw_header(w) -> None:
    canvas = w.table_header_canvas
    width = canvas.winfo_width()
    if width < 100:
        return
    p = w.palette
    canvas.delete("all")
    cols = _layout(width)
    canvas.create_rectangle(0, 0, width, HEADER_HEIGHT, fill=p["raised"], outline="")
    font = _font(w, 10)
    for name in ORDER:
        x0, x1 = cols[name]
        if name == "check":
            _check(canvas, x0 + 16, 15, False, p)
        else:
            _label(canvas, x0 + (14 if name in {"title", "source_url"} else 10), HEADER_HEIGHT // 2,
                   HEADINGS[name], width=x1-x0-20, font=font, color=p["text"])
        if name != ORDER[-1]:
            canvas.create_line(x1, 12, x1, HEADER_HEIGHT-12, fill=p["border"])
    canvas.create_line(0, HEADER_HEIGHT-1, width, HEADER_HEIGHT-1, fill=p["border"])


def _check(canvas, x: int, y: int, selected: bool, p: dict) -> None:
    canvas.create_rectangle(x, y, x+15, y+15, outline=p["primary"] if selected else p["muted"],
                            fill=p["primary"] if selected else p["surface"], width=1)
    if selected:
        canvas.create_line(x+3, y+7, x+6, y+10, x+12, y+4, fill="#ffffff", width=2)


def redraw(w) -> None:
    canvas = w.table_canvas
    width = canvas.winfo_width()
    if width < 100:
        return
    p = w.palette
    height = canvas.winfo_height()
    total = max(height, len(w.visual_rows) * ROW_HEIGHT)
    canvas.delete("all")
    canvas.configure(scrollregion=(0, 0, width, total))
    if len(w.visual_rows) * ROW_HEIGHT > height:
        if not w.table_scrollbar.winfo_manager():
            w.table_scrollbar.pack(side="right", fill="y")
        _draw_scrollbar(w)
    elif w.table_scrollbar.winfo_manager():
        w.table_scrollbar.pack_forget()
    first = max(0, int(canvas.canvasy(0) // ROW_HEIGHT) - 1)
    last = min(len(w.visual_rows), int(canvas.canvasy(height) // ROW_HEIGHT) + 2)
    cols = _layout(width)
    normal = _font(w, 10)
    small = _font(w, 9)
    bold = _font(w, 10, True)
    selected = set(w.tree.selection())
    for index in range(first, last):
        row = w.visual_rows[index]
        iid = str(row["id"])
        y0, y1 = index * ROW_HEIGHT, (index + 1) * ROW_HEIGHT
        mid = y0 + ROW_HEIGHT // 2
        row_bg = p["selected"] if iid in selected else p["surface"] if index % 2 == 0 else p["row_alt"]
        canvas.create_rectangle(0, y0, width, y1, fill=row_bg, outline="")
        canvas.create_line(0, y1-1, width, y1-1, fill=p["border"])
        _check(canvas, cols["check"][0]+16, mid-8, iid in selected, p)
        _label(canvas, cols["number"][0]+12, mid, index+1, width=30, font=normal, color=p["muted"])
        category = str(row["category"])
        cat_fg, cat_bg = CATEGORY_COLORS.get(category, CATEGORY_COLORS["其他"])
        title_x = cols["title"][0]+12
        preview=thumbnail(w,row["file_path"])
        rounded_rect(canvas,title_x,mid-17,title_x+34,mid+17,4,fill=cat_bg,outline=p["border"])
        canvas.create_image(title_x+17,mid,image=preview if preview else load_icon(w,"video",24,"muted"))
        _label(canvas, title_x+42, mid, row["title"] or "（无标题）",
               width=cols["title"][1]-title_x-48, font=normal, color=p["title"])
        status = str(row["status"])
        status_color = STATUS_COLORS.get(status, p["text"])
        sx = cols["status"][0]+14
        status_icon = "check" if status == "已完成" else "rotate" if status == "正在下载" else "clock"
        canvas.create_image(sx+7,mid,image=load_icon(w,status_icon,18,"green" if status=="已完成" else "pink" if status=="正在下载" else "muted"))
        _label(canvas, sx+22, mid, status, width=cols["status"][1]-sx-24,
               font=small, color=status_color)
        percent = max(0, min(100, int(row["progress"])))
        px = cols["progress"][0]+12
        bar_end = cols["progress"][1]-47
        rounded_rect(canvas,px,mid-4,bar_end,mid+4,4,fill=p["track"],outline="")
        if percent:
            rounded_rect(canvas,px,mid-4,px+(bar_end-px)*percent/100,mid+4,4,fill=p["primary"],outline="")
        _label(canvas, bar_end+8, mid, f"{percent}%", width=37, font=small,
               color=p["muted"])
        cx0, cx1 = cols["category"]
        rounded_rect(canvas,cx0+9,mid-13,cx1-9,mid+13,13,fill=cat_bg,outline=cat_fg,width=1)
        _label(canvas, (cx0+cx1)//2, mid, category, width=cx1-cx0-24,
               font=small, color=cat_fg, anchor="center")
        duration = w._format_duration(float(row["duration_seconds"]))
        _label(canvas, cols["duration"][0]+9, mid, duration, width=62,
               font=normal, color=p["text"])
        _label(canvas, cols["size"][0]+8, mid, f"{row['size_bytes']/1024/1024:.1f} MB",
               width=64, font=small, color=p["muted"])
        timestamp = str(row["downloaded_at"]).replace("T", " ")[:16]
        _label(canvas, cols["downloaded"][0]+8, mid, timestamp, width=cols["downloaded"][1]-cols["downloaded"][0]-14,
               font=small, color=p["text"])
        url = str(row["source_url"])
        _label(canvas, cols["source_url"][0]+9, mid, url,
               width=cols["source_url"][1]-cols["source_url"][0]-16,
               font=small, color=p["link"])
    draw_header(w)


def update_rows(w, rows) -> None:
    position = w.table_canvas.yview()[0] if hasattr(w, "table_canvas") else 0
    w.visual_rows = list(rows)
    if hasattr(w, "table_canvas"):
        redraw(w)
        w.table_canvas.yview_moveto(position)
        redraw(w)


def _scroll(w, *args) -> None:
    w.table_canvas.yview(*args)
    redraw(w)


def _draw_scrollbar(w) -> None:
    bar = w.table_scrollbar
    height = max(1, bar.winfo_height())
    first, last = w.table_canvas.yview()
    bar.delete("all")
    bar.create_rectangle(0, 0, 9, height, fill=w.palette["surface"], outline="")
    bar.create_rectangle(2, max(2, height * first), 7, min(height-2, height * last),
                         fill=w.palette["border"], outline="")


def _scrollbar_seek(w, event) -> None:
    height = max(1, w.table_scrollbar.winfo_height())
    fraction = min(1.0, max(0.0, event.y / height))
    visible = min(1.0, w.table_canvas.winfo_height() / max(1, len(w.visual_rows)*ROW_HEIGHT))
    w.table_canvas.yview_moveto(max(0.0, fraction - visible/2))
    redraw(w)


def _wheel(w, event) -> None:
    w.table_canvas.yview_scroll(-int(event.delta / 120), "units")
    redraw(w)


def _row_at(w, event):
    index = int(w.table_canvas.canvasy(event.y) // ROW_HEIGHT)
    return (index, w.visual_rows[index]) if 0 <= index < len(w.visual_rows) else None


def _region(w, x: int) -> str:
    for name, (_, x1) in _layout(w.table_canvas.winfo_width()).items():
        if x < x1:
            return name
    return ""


def _click(w, event) -> None:
    w.table_canvas.focus_set()
    target = _row_at(w, event)
    if target is None:
        return
    index, row = target
    iid = str(row["id"])
    region = _region(w, event.x)
    if region == "check" or event.state & 0x0004:
        if iid in w.tree.selection():
            w.tree.selection_remove(iid)
        else:
            w.tree.selection_add(iid)
    elif event.state & 0x0001 and w.visual_rows:
        anchor = getattr(w, "_visual_anchor", index)
        w.tree.selection_set(*(str(item["id"]) for item in w.visual_rows[min(index, anchor):max(index, anchor)+1]))
    else:
        w.tree.selection_set(iid)
    w._visual_anchor = index
    redraw(w)
    if region == "source_url":
        w.open_source_url()


def _double_click(w, event) -> None:
    if _row_at(w, event) and _region(w, event.x) not in {"check", "source_url"}:
        w.open_selected()


def _right_click(w, event) -> None:
    target = _row_at(w, event)
    if target is None:
        return
    iid = str(target[1]["id"])
    if iid not in w.tree.selection():
        w.tree.selection_set(iid)
    redraw(w)
    w.menu.tk_popup(event.x_root, event.y_root)


def _header_click(w, event) -> None:
    for name, (x0, x1) in _layout(w.table_header_canvas.winfo_width()).items():
        if x0 <= event.x < x1:
            if name == "check":
                w.select_all_rows()
                redraw(w)
            elif name in {"number", "title", "status", "progress", "category",
                          "duration", "size", "downloaded", "source_url"}:
                previous = getattr(w, "_visual_sort", (None, True))
                w.sort_tree(name, not previous[1] if previous[0] == name else False)
            return


def show_metrics(w) -> None:
    row = w._selected_row()
    if row is None:
        return
    messagebox.showinfo("视频信息", f"点赞：{display_metric(row['likes'])}\n评论：{display_metric(row['comments'])}\n收藏：{display_metric(row['favorites'])}\n"
                        f"大小：{row['size_bytes']/1024/1024:.1f} MB\n原视频链接：{row['source_url']}")
