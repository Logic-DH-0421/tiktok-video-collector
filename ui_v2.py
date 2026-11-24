"""Dark desktop presentation for the collector; business callbacks live in app.py."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from classifier import CATEGORIES
from ui_table import CATEGORY_COLORS, build_visual_table, redraw as redraw_visual_table, show_metrics
from ui_controls import RoundedButton, RoundedEntry, RoundedProgress, round_card_corners
from ui_icons import icon as load_icon
from ui_thumbnails import thumbnail as video_thumbnail


def format_timeout_seconds(value):
    return f"{value:g}"


def _frame(parent, bg: str, **kwargs):
    return tk.Frame(parent, bg=bg, **kwargs)


def _label(parent, text=None, *, variable=None, color="#d6d8e1", size=10, bold=False, bg=None, **kwargs):
    options = dict(bg=bg or parent.cget("bg"), fg=color, font=("Microsoft YaHei UI", size, "bold" if bold else "normal"), **kwargs)
    if variable is not None:
        options["textvariable"] = variable
    else:
        options["text"] = text
    return tk.Label(parent, **options)


def _button(parent, text, command, p, *, primary=False, compact=False, icon=None):
    bg = p["primary"] if primary else p["raised"]
    button = RoundedButton(parent, text=text, command=command, bg=bg,
                     fg="#ffffff" if primary else p["text"],
                     activebackground=p["primary_dark"] if primary else p["selected"],
                     activeforeground="#ffffff", relief="flat", borderwidth=0,
                     cursor="hand2", padx=12 if compact else 16,
                     pady=7 if compact else 9,
                     font=("Microsoft YaHei UI", 10, "bold" if primary else "normal"),
                     outline="" if primary else p["border"])
    if icon: button.configure(icon=icon)
    return button


def _entry(parent, variable, p, *, width=20, height=24, icon=None):
    return RoundedEntry(parent,variable,p,width=width,height=height,icon=icon)


def _icon_label(parent, name, p, *, tone="normal", size=24):
    return tk.Label(parent,image=load_icon(parent,name,size,tone),bg=parent.cget("bg"),bd=0)


def _select(parent, variable, values, p):
    menu = tk.Menu(parent,tearoff=False,bg=p["raised"],fg=p["title"],
                   activebackground=p["selected"],activeforeground=p["title"],
                   font=("Microsoft YaHei UI",10))
    button = _button(parent,variable.get(),lambda:menu.tk_popup(button.winfo_rootx(),button.winfo_rooty()+button.winfo_height()),p,compact=True)
    button.configure(anchor="w",trailing_icon="chevron",pady=7)
    for value in values:
        menu.add_command(label=value,command=lambda selected=value:variable.set(selected))
    variable.trace_add("write",lambda *_args:button.configure(text=variable.get()))
    return button


def _date_range_dialog(w, p):
    dialog = tk.Toplevel(w)
    dialog.title("选择时间范围")
    dialog.configure(bg=p["surface"])
    dialog.geometry("460x220")
    dialog.transient(w)
    for title,key in (("开始日期","date_from"),("结束日期","date_to")):
        row = _frame(dialog,p["surface"])
        row.pack(fill="x",padx=22,pady=12)
        _label(row,title,color=p["text"],size=10,width=9).pack(side="left")
        _entry(row,w.filter_vars[key],p).pack(side="left",fill="x",expand=True,ipady=6)
    _label(dialog,"日期格式：YYYY-MM-DD；留空表示不限",color=p["muted"],size=9).pack()
    _button(dialog,"确定",dialog.destroy,p,primary=True,compact=True).pack(pady=10)


def _card(parent, p):
    card = _frame(parent, p["surface"], highlightthickness=0)
    round_card_corners(card, p)
    return card


def build_ui(w) -> None:
    p = w.palette
    w.filter_vars = {key: tk.StringVar() for key in ["title", "min_size", "max_size", "min_duration", "max_duration", "date_from", "date_to"]}
    w.category_filter = tk.StringVar(value="全部")
    w.status_filter = tk.StringVar(value="全部")
    w.path_var = tk.StringVar(value=w.config_data.download_dir)
    w.likes_var = tk.StringVar(value=str(w.config_data.min_likes))
    w.comments_var = tk.StringVar(value=str(w.config_data.min_comments))
    w.favorites_var = tk.StringVar(value=str(w.config_data.min_favorites))
    w.duration_var = tk.StringVar(value=str(w.config_data.min_duration_seconds))
    w.max_duration_var = tk.StringVar(value=str(w.config_data.max_duration_seconds))
    w.checkpoint_var = tk.StringVar(value=str(w.config_data.checkpoint_every))
    w.concurrent_downloads_var = tk.StringVar(value=str(w.config_data.max_concurrent_downloads))
    w.metric_timeout_var = tk.StringVar(value=format_timeout_seconds(w.config_data.metric_timeout_seconds))
    w.download_timeout_var=tk.StringVar(value=str(w.config_data.download_timeout_seconds))
    w.download_idle_timeout_var=tk.StringVar(value=str(w.config_data.download_idle_timeout_seconds))
    w.max_file_size_var=tk.StringVar(value=str(w.config_data.max_file_size_mb))
    w.manual_url_var = tk.StringVar()
    w.author_url_var = tk.StringVar()

    header = _frame(w, p["bg"], height=66)
    header.pack(fill="x")
    header.pack_propagate(False)
    title_label = _label(header, "TikTok 视频采集器", color=p["title"], size=16, bold=True)
    title_label.pack(side="left", padx=(24, 0))
    for drag_surface in (header, title_label):
        drag_surface.bind("<ButtonPress-1>", w.begin_window_drag)
        drag_surface.bind("<B1-Motion>", w.drag_window)
        drag_surface.bind("<ButtonRelease-1>", w.end_window_drag)
        drag_surface.bind("<Double-Button-1>", lambda _event: w.toggle_maximize())
    for symbol, callback in [("close", w._on_close), ("maximize", w.toggle_maximize), ("minimize", w.iconify)]:
        control = _button(header, "", callback, p, compact=True,icon=symbol)
        control.configure(bg=p["bg"], highlightthickness=0, font=("Segoe UI", 12), padx=13, pady=5)
        control.pack(side="right", padx=2)
    _label(header, "│", color=p["border"], size=11).pack(side="right", padx=9)
    status_group = _frame(header, p["bg"])
    status_group.pack(side="right", padx=7)
    _label(status_group, "●", color=p["success"], size=8).pack(side="left", padx=(0, 6))
    _label(status_group, variable=w.status_var, color=p["text"], size=9).pack(side="left")

    w.operation_height = 320
    body = _frame(w, p["bg"], height=w.operation_height+18)
    body.pack(fill="x")
    body.pack_propagate(False)
    w.main_body = body
    sidebar = _frame(body, p["bg"], width=198)
    sidebar.pack(side="left", fill="y")
    sidebar.pack_propagate(False)
    navigation = _frame(sidebar,p["bg"],height=w.operation_height)
    navigation.pack(fill="x",padx=12,pady=(8,0))
    navigation.pack_propagate(False)
    navigation.grid_propagate(False)
    navigation.grid_columnconfigure(0,weight=1)
    w.navigation_stack=navigation
    content = _frame(body, p["bg"])
    content.pack(side="left", fill="both", expand=True)
    w.main_content=content
    w.nav_buttons = {}
    for index,(name, title, icon) in enumerate([
        ("dashboard", "自动采集", "home"), ("manual", "单链接下载", "link"),
        ("author", "作者主页", "user"), ("library", "视频库", "video"),
        ("settings", "下载设置", "settings"),
        ("logs","运行日志","logs"),
    ]):
        callback=w.export_diagnostics if name=="logs" else lambda n=name:show_page(w,n)
        b = _button(navigation,title,callback,p,icon=icon)
        b.configure(anchor="w", bg=p["bg"], font=("Microsoft YaHei UI", 11), pady=8,
                    highlightthickness=0)
        navigation.grid_rowconfigure(index,weight=1,uniform="navigation")
        b.grid(row=index,column=0,sticky="nsew",pady=1)
        w.nav_buttons[name] = b

    w.pages = {}
    dashboard = _frame(content, p["bg"])
    w.pages["dashboard"] = dashboard
    _build_dashboard(w, dashboard, p)
    manual = _frame(content, p["bg"])
    w.pages["manual"] = manual
    _build_link_page(w, manual, p, author=False)
    author = _frame(content, p["bg"])
    w.pages["author"] = author
    _build_link_page(w, author, p, author=True)
    show_page(w, "dashboard")


def show_page(w, name: str, *, selected: str | None = None) -> None:
    page_name = "dashboard" if name == "settings" else name
    for page in w.pages.values():
        page.pack_forget()
    w.table_card.pack_forget()
    w.dashboard_footer.pack_forget()
    if page_name == "dashboard":
        w.pages["dashboard"].pack(fill="both",expand=True)
        w.main_body.pack_configure(fill="x", expand=False)
        w.table_card.pack(in_=w,fill="both", expand=True, padx=18, pady=(0, 8))
        w.dashboard_footer.pack(fill="x", padx=18)
        _show_scan_settings(w,name=="settings")
    elif name == "library":
        w.main_body.pack_configure(fill="both",expand=True)
        w.table_card.pack(in_=w.main_content,fill="both",expand=True,padx=17,pady=(8,0))
        w.dashboard_footer.pack(fill="x",padx=18)
    else:
        w.pages[page_name].pack(fill="both",expand=True)
        w.main_body.pack_configure(fill="both", expand=True)
    w.active_page=name
    for key, button in w.nav_buttons.items():
        active = key == (selected or name)
        button.configure(bg=w.palette["selected"] if active else w.palette["bg"],
                         fg=w.palette["primary"] if active else w.palette["text"])
    if name == "library" and hasattr(w, "tree"):
        w.table_canvas.focus_set()


def _build_dashboard(w, parent, p) -> None:
    cards = _frame(parent, p["bg"], height=w.operation_height)
    cards.pack(fill="x", padx=17, pady=(8, 10))
    cards.pack_propagate(False)
    cards.grid_propagate(False)
    cards.grid_columnconfigure(0, weight=10, uniform="card")
    cards.grid_columnconfigure(1, weight=10, uniform="card")
    cards.grid_columnconfigure(2, weight=8, uniform="card")
    cards.grid_rowconfigure(0, weight=1)
    scan = _card(cards, p)
    scan.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
    queue = _card(cards, p)
    queue.grid(row=0, column=1, sticky="nsew", padx=(0, 10))
    filters = _card(cards, p)
    filters.grid(row=0, column=2, sticky="nsew")
    w.scan_card, w.queue_card, w.filter_card = scan, queue, filters
    _build_scan_card(w, scan, p)
    _build_queue_card(w, queue, p)
    _build_filter_card(w, filters, p)

    table_card = _card(w, p)
    table_card.pack(fill="both", expand=True, padx=18, pady=(0, 8))
    w.table_card = table_card
    table_header = _frame(table_card, p["surface"], height=55)
    table_header.pack(fill="x")
    table_header.pack_propagate(False)
    _icon_label(table_header,"list",p).pack(side="left",padx=(20,10))
    _label(table_header, variable=w.table_heading_var, color=p["title"], size=12, bold=True).pack(side="left")
    _button(table_header, "更多操作  ⋯", lambda: w.table_actions_menu.tk_popup(
        table_header.winfo_rootx() + table_header.winfo_width() - 180,
        table_header.winfo_rooty() + 40), p, compact=True).pack(side="right", padx=10, pady=5)
    _button(table_header, "按下载时间排序", lambda: w.sort_tree("downloaded", True), p, compact=True,icon="sort").pack(side="right", pady=8)
    _build_tree(w, table_card, p)

    footer = _frame(w, p["bg"], height=61)
    w.dashboard_footer = footer
    footer.pack(fill="x", padx=18)
    footer.pack_propagate(False)
    _label(footer, variable=w.total_var, color=p["muted"], size=9).pack(side="left", pady=11)
    _label(footer, variable=w.footer_stats_var, color=p["muted"], size=9).pack(side="left", padx=15, pady=11)
    _label(footer, "Design by Logic", color=p["muted"], size=9).pack(side="right", pady=11)


def _focus_section(w, name: str) -> None:
    show_page(w, "library" if name == "library" else "dashboard", selected="library" if name == "library" else "dashboard")
    target = {"dashboard": w.scan_card, "queue": w.queue_card, "library": w.table_card}[name]
    target.focus_set()


def _build_scan_card(w, card, p) -> None:
    header = _frame(card, p["surface"])
    w.scan_header=header
    header.pack(fill="x", padx=18, pady=(17, 16))
    _icon_label(header,"rotate",p,tone="pink").pack(side="left",padx=(0,10))
    w.scan_heading_var = tk.StringVar(value="采集待命")
    _label(header, variable=w.scan_heading_var, color=p["title"], size=12, bold=True).pack(side="left")
    w.start_btn = _button(header, "开始采集", w.start, p, primary=True, compact=True)
    w.start_btn.pack(side="right")
    w.pause_btn = _button(header, "暂停采集", w.pause_resume, p, primary=True, compact=True,icon="pause")
    w.pause_btn.configure(state="disabled")
    w.scan_summary=_frame(card,p["surface"])
    w.scan_summary.pack(fill="both",expand=True)
    w.settings_result_var=tk.StringVar(value="开始任务时自动保存")
    _build_rule_fields(w,w.scan_summary,p)
    details = _frame(w.scan_summary, p["surface"])
    details.pack(fill="x", padx=18,pady=(5,0))
    for name, value in [
        ("已采集视频", w.batch_candidates_var), ("运行时长", w.run_time_var),
    ]:
        row = _frame(details, p["surface"])
        row.pack(fill="x", pady=0)
        _label(row, name, color=p["muted"], size=9, width=13, anchor="w").pack(side="left")
        _label(row, value if isinstance(value, str) else None,
               variable=None if isinstance(value, str) else value,
               color=p["text"], size=9).pack(side="left")
    tk.Frame(w.scan_summary, bg=p["border"], height=1).pack(fill="x", padx=18, pady=(6, 6))
    progress_caption = _frame(w.scan_summary,p["surface"])
    progress_caption.pack(fill="x",padx=18)
    _label(progress_caption, variable=w.batch_var, color=p["text"], size=10).pack(side="left")
    percent_var=tk.StringVar(value="0%")
    _label(progress_caption,variable=percent_var,color=p["muted"],size=10).pack(side="right")
    w.batch_progress_var.trace_add("write",lambda *_args:percent_var.set(f"{w.batch_progress_var.get()/max(1,w.config_data.checkpoint_every):.0%}"))
    w.batch_bar = RoundedProgress(w.scan_summary,p,maximum=w.config_data.checkpoint_every,
                                  variable=w.batch_progress_var,height=14)
    w.batch_bar.pack(fill="x", padx=18, pady=(7, 10))
    rule_footer=_frame(w.scan_summary,p["surface"])
    rule_footer.pack(fill="x",padx=18)
    w.scan_rule_save_btn=_button(rule_footer,"保存参数",lambda:_save_inline_settings(w),p,primary=True,compact=True)
    w.scan_rule_save_btn.pack(side="right")
    w.current_bar = RoundedProgress(card,p,maximum=100,variable=w.current_progress_var,height=8)
    w.stop_btn = _button(rule_footer, "停止", w.stop, p, compact=True)
    w.stop_btn.configure(state="disabled")
    w.stop_btn.pack(side="left", padx=(0,5))
    w.skip_btn = _button(rule_footer, "跳过", w.skip_current, p, compact=True)
    w.skip_btn.configure(state="disabled")
    w.skip_btn.pack(side="left")
    _build_inline_settings(w,card,p)
    w.scan_settings_visible=False


def _show_scan_settings(w,visible:bool) -> None:
    w.scan_summary.pack_forget()
    w.scan_settings_panel.pack_forget()
    panel=w.scan_settings_panel if visible else w.scan_summary
    panel.pack(fill="both",expand=True)
    w.scan_settings_visible=visible


def _build_rule_fields(w,parent,p) -> None:
    fields=_frame(parent,p["surface"])
    fields.pack(fill="x",padx=18)
    w.scan_rule_entries={}
    for index,(key,title,variable) in enumerate([
        ("likes","最低点赞",w.likes_var),("comments","最低评论",w.comments_var),
        ("favorites","最低收藏",w.favorites_var),("min_duration","最短秒数",w.duration_var),
        ("max_duration","最长秒数",w.max_duration_var),
        ("checkpoint","单轮暂停数",w.checkpoint_var),
    ]):
        cell=_frame(fields,p["surface"])
        cell.grid(row=index//2,column=index%2,sticky="ew",padx=(0,7),pady=2)
        _label(cell,title,size=9,width=8,anchor="w").pack(side="left")
        entry=_entry(cell,variable,p,width=6)
        entry.pack(side="left",fill="x",expand=True)
        w.scan_rule_entries[key]=entry
    for column in (0,1):fields.grid_columnconfigure(column,weight=1,uniform="rule")


def _build_inline_settings(w,card,p) -> None:
    panel=_frame(card,p["surface"])
    w.scan_settings_panel=panel
    path=_frame(panel,p["surface"])
    path.pack(fill="x",padx=14,pady=(0,6))
    _label(path,"保存至",size=9,width=6,anchor="w").pack(side="left")
    _entry(path,w.path_var,p,width=12).pack(side="left",fill="x",expand=True,ipady=2,padx=(0,5))
    _button(path,"…",w.choose_path,p,compact=True).pack(side="right")
    fields=_frame(panel,p["surface"])
    fields.pack(fill="x",padx=14)
    for index,(title,variable) in enumerate([
        ("并行下载",w.concurrent_downloads_var),("指标超时",w.metric_timeout_var),
        ("总时限秒",w.download_timeout_var),("无进展秒",w.download_idle_timeout_var),
        ("最大MB(0不限)",w.max_file_size_var),
    ]):
        cell=_frame(fields,p["surface"])
        cell.grid(row=index//2,column=index%2,sticky="ew",padx=(0,7),pady=3)
        _label(cell,title,size=9,width=8,anchor="w").pack(side="left")
        _entry(cell,variable,p,width=6).pack(side="left",fill="x",expand=True,ipady=1)
    for column in (0,1):fields.grid_columnconfigure(column,weight=1,uniform="setting")
    footer=_frame(panel,p["surface"])
    footer.pack(fill="x",padx=14,pady=(6,4))
    _label(footer,variable=w.settings_result_var,color=p["muted"],size=8).pack(side="left")
    _button(footer,"保存",lambda:_save_inline_settings(w),p,primary=True,compact=True).pack(side="right")


def _save_inline_settings(w) -> None:
    config=w._parse_config()
    if config is None:return
    if not (w.worker and w.worker.is_alive()):
        w.batch_bar.configure(maximum=config.checkpoint_every)
        w.batch_var.set(f"本批次进度：{w.batch_progress_var.get()} / {config.checkpoint_every}")
        w.batch_candidates_var.set(f"{len(w._batch_candidate_ids)} / {config.checkpoint_every}")
    w.settings_result_var.set("设置已保存")


def _build_queue_card(w, card, p) -> None:
    header = _frame(card, p["surface"])
    header.pack(fill="x", padx=17, pady=(16, 11))
    w.queue_heading_var = tk.StringVar(value="下载队列 (0)")
    _icon_label(header,"download",p).pack(side="left",padx=(0,12))
    _label(header, variable=w.queue_heading_var, color=p["title"], size=12, bold=True).pack(side="left")
    _button(header, "查看全部", lambda: _focus_section(w, "library"), p, compact=True).pack(side="right")
    w.queue_rows = []
    for index in range(5):
        row = _frame(card, p["surface"], height=49)
        row.pack(fill="x", padx=16)
        row.pack_propagate(False)
        if index:
            tk.Frame(row, bg=p["border"], height=1).pack(fill="x")
        badge = _label(row, f"{index + 1}", color=p["muted"], size=9, width=2)
        badge.pack(side="left", padx=(0, 8))
        thumbnail = _label(row, "·", color=p["muted"], size=12, bold=True,
                           bg=p["raised"], width=3, height=1, pady=4)
        thumbnail.pack(side="left", padx=(0, 9))
        title = _label(row, "—", color=p["text"], size=10, anchor="w")
        title.pack(side="left", fill="x", expand=True)
        status = _label(row, "", color=p["primary"], size=8)
        status.pack(side="right", padx=3)
        progress = _label(row, "", color=p["muted"], size=8)
        progress.pack(side="right", padx=4)
        bar = RoundedProgress(row,p,maximum=100,length=110,height=8)
        bar.pack(side="right", padx=4)
        w.queue_rows.append((title, progress, status, bar, thumbnail))


def render_queue(w, rows) -> None:
    if not hasattr(w, "queue_rows"):
        return
    w.queue_heading_var.set(f"下载队列 ({len(rows)})")
    for widgets, row in zip(w.queue_rows, [*rows[:5], *([None] * 5)]):
        title, progress, status, bar, thumbnail = widgets
        title.configure(text=(str(row["title"] or "（无标题）")[:22] if row else "—"))
        progress.configure(text=(f"{row['progress']}%" if row else ""))
        status.configure(text=(str(row["status"]) if row else ""))
        bar.configure(value=int(row["progress"]) if row else 0)
        category = str(row["category"]) if row and "category" in row.keys() else "其他"
        fg, bg = CATEGORY_COLORS.get(category, CATEGORY_COLORS["其他"])
        preview=video_thumbnail(w,row["file_path"]) if row and "file_path" in row.keys() else None
        thumbnail.configure(text="",image=preview if preview else load_icon(w,"video",24,"muted"),
                            bg=bg if row else w.palette["raised"],width=34,height=34,pady=0)


def _build_filter_card(w, card, p) -> None:
    filter_header = _frame(card, p["surface"])
    filter_header.pack(fill="x", padx=17, pady=(12, 8))
    _icon_label(filter_header,"filter",p).pack(side="left",padx=(0,10))
    _label(filter_header, "筛选与操作", color=p["title"], size=12, bold=True).pack(side="left")
    more = _label(filter_header, "⋯", color=p["muted"], size=12, cursor="hand2")
    more.pack(side="right")
    more.bind("<Button-1>", lambda _event: _advanced_filters(w, p))
    keyword_row = _frame(card, p["surface"])
    keyword_row.pack(fill="x", padx=17, pady=(0, 6))
    _label(keyword_row, "关键词", color=p["muted"], size=9, width=7, anchor="w").pack(side="left")
    title = _entry(keyword_row, w.filter_vars["title"], p)
    title.pack(side="left", fill="x", expand=True, ipady=7)
    title.bind("<Return>", lambda _event: w.refresh_table())
    category_row = _frame(card, p["surface"])
    category_row.pack(fill="x", padx=17, pady=(0, 6))
    _label(category_row, "类别", color=p["muted"], size=9, width=7, anchor="w").pack(side="left", anchor="n", pady=7)
    chips = _frame(category_row, p["surface"])
    chips.pack(side="left", fill="x", expand=True)
    w.category_chips = {}
    for i, name in enumerate(["全部", *CATEGORIES]):
        chip = _button(chips, name, lambda c=name: w.category_filter.set(c), p, compact=True)
        chip.configure(font=("Microsoft YaHei UI", 10), padx=4, pady=4,
                       highlightthickness=1, highlightbackground=p["border"])
        chip.grid(row=i // 4, column=i % 4, padx=3, pady=3, sticky="ew")
        w.category_chips[name] = chip
    for col in range(4):
        chips.grid_columnconfigure(col, weight=1)
    def _color_chips(*_args):
        for name, chip in w.category_chips.items():
            selected = name == w.category_filter.get()
            chip.configure(bg=p["selected"] if selected else p["surface"],
                           fg=p["title"] if selected else p["text"],
                           highlightbackground=p["primary"] if selected else p["border"])
    w.category_filter.trace_add("write", _color_chips)
    _color_chips()
    row = _frame(card, p["surface"])
    row.pack(fill="x", padx=17, pady=(0, 6))
    _label(row, "状态", color=p["muted"], size=9, width=7, anchor="w").pack(side="left")
    _select(row,w.status_filter,["全部","排队中","正在下载","等待人工处理","已完成","失败","已跳过"],p).pack(side="left",fill="x",expand=True)
    dates_row = _frame(card, p["surface"])
    dates_row.pack(fill="x", padx=17, pady=(0, 7))
    _label(dates_row, "时间范围", color=p["muted"], size=9, width=7, anchor="w").pack(side="left")
    date_button = _button(dates_row,"选择时间范围",lambda:_date_range_dialog(w,p),p,compact=True,icon="calendar")
    date_button.configure(anchor="w",trailing_icon="chevron")
    date_button.pack(side="left",fill="x",expand=True)
    def _date_caption(*_args):
        start,end=w.filter_vars["date_from"].get(),w.filter_vars["date_to"].get()
        date_button.configure(text=f"{start or '不限'} — {end or '不限'}" if start or end else "选择时间范围")
    for key in ("date_from","date_to"): w.filter_vars[key].trace_add("write",_date_caption)
    actions = _frame(card, p["surface"])
    actions.pack(fill="x", padx=17, pady=(2,8))
    _button(actions, "筛选", w.refresh_table, p, primary=True, compact=True,icon="search").pack(side="left", fill="x", expand=True, padx=(0, 6))
    _button(actions, "重置", w.reset_filters, p, compact=True,icon="reset").pack(side="left", fill="x", expand=True)


def _advanced_filters(w, p) -> None:
    dialog = tk.Toplevel(w)
    dialog.title("更多筛选条件")
    dialog.geometry("440x355")
    dialog.configure(bg=p["surface"])
    dialog.transient(w)
    for i, (label, key) in enumerate([
        ("大小 ≥ MB", "min_size"), ("大小 ≤ MB", "max_size"),
        ("时长 ≥ 秒", "min_duration"), ("时长 ≤ 秒", "max_duration"),
    ]):
        row = _frame(dialog, p["surface"])
        row.pack(fill="x", padx=18, pady=5)
        _label(row, label, color=p["text"], size=9, width=12, anchor="w").pack(side="left")
        _entry(row, w.filter_vars[key], p).pack(side="left", fill="x", expand=True, ipady=5)
    _button(dialog, "应用筛选", lambda: (w.refresh_table(), dialog.destroy()), p, primary=True).pack(pady=10)


def _build_tree(w, card, p) -> None:
    table = _frame(card, p["surface"])
    columns = ("number", "title", "status", "progress", "source_url", "size", "duration", "category", "downloaded", "likes", "comments", "favorites", "actions")
    w.tree = ttk.Treeview(table, columns=columns, show="tree headings", selectmode="extended", style="Dark.Treeview")
    w.tree.heading("#0", text="□", command=w.select_all_rows)
    w.tree.column("#0", width=64, minwidth=64, stretch=False, anchor="center")
    headings = {"number": "#", "title": "标题", "status": "状态", "progress": "进度", "source_url": "原视频链接", "size": "大小", "duration": "时长", "category": "类别", "downloaded": "下载时间", "likes": "点赞", "comments": "评论", "favorites": "收藏", "actions": "操作"}
    widths = {"number": 38, "title": 255, "status": 90, "progress": 112, "source_url": 240, "size": 70, "duration": 65, "category": 72, "downloaded": 138, "likes": 78, "comments": 72, "favorites": 72, "actions": 110}
    for column in columns:
        heading_options = {"text": headings[column]}
        if column != "actions":
            heading_options["command"] = lambda c=column: w.sort_tree(c, False)
        w.tree.heading(column, **heading_options)
        w.tree.column(column, width=widths[column], minwidth=55, stretch=False,
                      anchor="w" if column in {"title", "source_url"} else "center")
    w.tree.column("number", minwidth=38)
    w.tree.configure(displaycolumns=("number", "title", "status", "progress", "category", "duration", "downloaded", "source_url", "actions", "size", "likes", "comments", "favorites"))
    # Keep Treeview as the existing selection/sort model. A canvas renders the
    # visible rows so progress, category badges and actions can match the mockup.
    build_visual_table(w, card, p)
    w.category_icons = {}
    for category, color in {"美食": "#f59e58", "宠物": "#75aaff", "搞笑": "#e6a046",
                            "风光": "#4adea7", "美女": "#bc8cff", "其他": "#8b91a5"}.items():
        icon = tk.PhotoImage(width=20, height=20, master=w)
        icon.put(color, to=(2, 2, 18, 18))
        icon.put(p["surface"], to=(4, 4, 16, 16))
        icon.put(color, to=(6, 6, 14, 14))
        w.category_icons[category] = icon
    w.tree.bind("<<TreeviewSelect>>", lambda _event: redraw_visual_table(w))
    w.tree.bind("<Button-1>", lambda event: _tree_click(w, event))
    w.tree.bind("<Double-1>", lambda event: _tree_open(w, event))
    w.tree.bind("<Button-3>", w.show_context_menu)
    for status, color in {"排队中": "#c4a1ff", "正在下载": "#ff6f9f", "等待人工处理": "#fbbf72",
                          "已完成": "#4adea7", "失败": "#f87171", "已跳过": "#989ba9"}.items():
        w.tree.tag_configure(status, foreground=color)
    w.menu = tk.Menu(w, tearoff=False, background=p["raised"], foreground=p["title"],
                     activebackground=p["selected"], activeforeground=p["title"])
    w.category_menu = tk.Menu(w.menu, tearoff=False, background=p["raised"], foreground=p["title"],
                              activebackground=p["selected"], activeforeground=p["title"])
    for category in CATEGORIES:
        w.category_menu.add_command(label=category, command=lambda c=category: w.change_category(c))
    w.menu.add_cascade(label="批量修改类别", menu=w.category_menu)
    w.menu.add_command(label="全选", command=w.select_all_rows)
    w.menu.add_command(label="取消选择", command=lambda: w.tree.selection_remove(*w.tree.selection()))
    w.menu.add_separator()
    w.menu.add_command(label="打开视频", command=w.open_selected)
    w.menu.add_command(label="打开原视频链接", command=w.open_source_url)
    w.menu.add_command(label="打开所在文件夹", command=w.reveal_selected)
    w.menu.add_command(label="查看互动指标", command=lambda: show_metrics(w))
    w.menu.add_separator()
    w.menu.add_command(label="删除选中记录及文件", command=w.delete_selected)
    w.table_actions_menu = tk.Menu(w, tearoff=False, background=p["raised"], foreground=p["title"],
                                   activebackground=p["selected"], activeforeground=p["title"])
    for name, callback in [("打开保存文件夹", w.open_folder), ("清理丢失记录", w.clean_missing),
                           ("清空列表（保留文件）", w.clear_list), ("导出诊断日志", w.export_diagnostics)]:
        w.table_actions_menu.add_command(label=name, command=callback)


def _sync_checks(w) -> None:
    selected = set(w.tree.selection())
    for iid in w.tree.get_children(""):
        mark = "☑" if iid in selected else "□"
        if w.tree.item(iid, "text") != mark:
            w.tree.item(iid, text=mark)


def _tree_click(w, event):
    iid = w.tree.identify_row(event.y)
    if not iid:
        return None
    column = w.tree.identify_column(event.x)
    if column == "#0":
        if iid in w.tree.selection():
            w.tree.selection_remove(iid)
        else:
            w.tree.selection_add(iid)
        w.tree.focus(iid)
        return "break"
    display = tuple(w.tree.cget("displaycolumns"))
    index = int(column[1:]) - 1 if column.startswith("#") else -1
    if 0 <= index < len(display) and display[index] == "actions":
        w.tree.selection_set(iid)
        box = w.tree.bbox(iid, "actions")
        offset = event.x - box[0] if box else 0
        if offset < 37:
            w.open_selected()
        elif offset < 74:
            w.reveal_selected()
        else:
            w.show_context_menu(event)
        return "break"
    return None


def _tree_open(w, event):
    iid = w.tree.identify_row(event.y)
    if not iid:
        return
    w.tree.selection_set(iid)
    column = w.tree.identify_column(event.x)
    display = tuple(w.tree.cget("displaycolumns"))
    index = int(column[1:]) - 1 if column.startswith("#") else -1
    if 0 <= index < len(display) and display[index] == "source_url":
        w.open_source_url()
    else:
        w.open_selected()


def _build_link_page(w, page, p, *, author: bool) -> None:
    card = _card(page, p)
    card.pack(fill="x", padx=22, pady=22)
    title = "作者主页批量下载" if author else "单链接下载"
    _label(card, title, color=p["title"], size=17, bold=True).pack(anchor="w", padx=25, pady=(24, 6))
    explanation = "读取主页当前可见的公开视频并加入下载队列。" if author else "直接下载指定公开视频，不受自动采集筛选条件限制。"
    _label(card, explanation, color=p["muted"], size=10).pack(anchor="w", padx=25)
    row = _frame(card, p["surface"])
    row.pack(fill="x", padx=25, pady=22)
    variable = w.author_url_var if author else w.manual_url_var
    entry = _entry(row, variable, p, width=70)
    entry.pack(side="left", fill="x", expand=True, ipady=11, padx=(0, 10))
    action = w.start_author_download if author else w.start_manual_download
    entry.bind("<Return>", lambda _event: action())
    button = _button(row, "下载作者全部视频" if author else "单独下载", action, p, primary=True)
    button.pack(side="right")
    if author:
        w.author_download_btn = button
    else:
        w.manual_download_btn = button


