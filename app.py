from __future__ import annotations

import os
import ctypes
import faulthandler
import math
import queue
import re
import shutil
import subprocess
import sys
import time
import tkinter as tk
import traceback
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlsplit, urlunsplit

from automation import AutomationWorker, AuthorDownloadWorker, ManualDownloadWorker
from classifier import CATEGORIES, categorized_path
from config_store import AppConfig, app_data_dir
from database import clear_videos, delete_missing_records, delete_videos, initialize, query_videos, summarize_videos, update_category
import diagnostics
from feed_metrics import display_metric


class MainWindow(tk.Tk):
    def __init__(self) -> None:
        if sys.platform == "win32":
            try:
                ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            except (AttributeError, OSError):
                pass
        super().__init__()
        self.tk.call("tk", "scaling", 1.5)
        self.title("TikTok 视频采集器")
        self.geometry("1480x900")
        self.minsize(1180, 760)
        self.config_data = AppConfig.load()
        self.events: queue.Queue[dict] = queue.Queue()
        self.worker: AutomationWorker | None = None
        self.manual_worker: ManualDownloadWorker | None = None
        self.author_worker: AuthorDownloadWorker | None = None
        self._logger_warning_shown = False
        self._exit_requested_at: float | None = None
        self.status_var = tk.StringVar(value="就绪")
        self.total_var = tk.StringVar(value="总视频数量：0")
        self.table_heading_var = tk.StringVar(value="视频列表 (0)")
        self.footer_stats_var = tk.StringVar(value="已完成 0  |  下载中 0  |  等待中 0")
        self.batch_var = tk.StringVar(value=f"本批次进度：0 / {self.config_data.checkpoint_every}")
        self.batch_progress_var = tk.IntVar(value=0)
        self.batch_candidates_var = tk.StringVar(value=f"0 / {self.config_data.checkpoint_every}")
        self.dedup_count_var = tk.StringVar(value="0")
        self.run_time_var = tk.StringVar(value="00:00:00")
        self.rule_summary_var = tk.StringVar(value=f"点赞 ≥ {self.config_data.min_likes:,}")
        self._batch_candidate_ids: set[int] = set()
        self._scan_started_at: float | None = None
        self.current_download_var = tk.StringVar(value="当前下载：等待任务")
        self.current_progress_var = tk.IntVar(value=0)
        self._build_style()
        self._build_ui()
        self.after(150, self._enable_dark_titlebar)
        self.bind("<Map>", lambda event: self.after(100, self._enable_dark_titlebar)
                  if event.widget is self else None)
        initialize()
        self.refresh_table()
        self.after(200, self._poll_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _enable_dark_titlebar(self) -> None:
        """Use the in-app title bar while retaining the native resize/taskbar styles."""
        if sys.platform != "win32":
            return
        try:
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.GetParent.argtypes = [wintypes.HWND]
            user32.GetParent.restype = wintypes.HWND
            user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
            hwnd = user32.GetParent(self.winfo_id()) or self.winfo_id()
            current_style = user32.GetWindowLongW(hwnd, -16)
            if current_style & 0x00C00000:
                user32.SetWindowLongW(hwnd, -16, current_style & ~0x00C00000)
                user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                               ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
                user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0037)
            enabled = ctypes.c_int(1)
            set_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
            set_attribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
            set_attribute.restype = ctypes.c_long
            result = set_attribute(
                hwnd, 20, ctypes.byref(enabled), ctypes.sizeof(enabled)
            )
            if result != 0:
                set_attribute(
                    hwnd, 19, ctypes.byref(enabled), ctypes.sizeof(enabled)
                )
        except (AttributeError, OSError) as exc:
            diagnostics.log("WARN", "ui", "titlebar.dark_unavailable",
                            "Native dark title bar unavailable", error=exc)

    def toggle_maximize(self) -> None:
        self.state("normal" if self.state() == "zoomed" else "zoomed")

    def begin_window_drag(self, event) -> None:
        # Never enter a synchronous Win32 move loop from a Tcl callback.
        # SendMessage(WM_NCLBUTTONDOWN) could block Tk indefinitely.
        if self.state() == "zoomed":
            self.state("normal")
            self.update_idletasks()
        self._window_drag = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())
        return "break"

    def drag_window(self, event) -> None:
        if self.state() == "normal" and hasattr(self, "_window_drag"):
            self._pending_drag_position=(event.x_root-self._window_drag[0],event.y_root-self._window_drag[1])
            if not getattr(self,"_drag_frame_pending",False):
                self._drag_frame_pending=True
                self._drag_after_id = self.after(16,self._apply_drag_position)

    def _apply_drag_position(self) -> None:
        timer = getattr(self, "_drag_after_id", None)
        if timer is not None:
            self.after_cancel(timer)
            self._drag_after_id = None
        self._drag_frame_pending=False
        if hasattr(self,"_pending_drag_position"):
            x,y=self._pending_drag_position
            # Explicit '+' prefixes keep negative values as absolute coordinates;
            # '-200-50' instead anchors relative to the right/bottom screen edge.
            self._move_window_to(int(x), int(y))
            del self._pending_drag_position

    def _move_window_to(self, x: int, y: int) -> None:
        if sys.platform == "win32":
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.GetParent.argtypes = [wintypes.HWND]
            user32.GetParent.restype = wintypes.HWND
            move = user32.SetWindowPos
            move.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            move.restype = wintypes.BOOL
            hwnd = user32.GetParent(self.winfo_id()) or self.winfo_id()
            # NOSIZE | NOZORDER | NOACTIVATE | NOCOPYBITS: invalidate old
            # client pixels instead of copying stale Canvas contents while moving.
            if move(hwnd, None, x, y, 0, 0, 0x0115):
                return
            diagnostics.log("WARN", "ui", "window.move_fallback",
                            "Native window move failed; using Tk positioning")
        self.geometry(f"+{x}+{y}")

    def end_window_drag(self, _event=None) -> None:
        if hasattr(self, "_pending_drag_position"):
            self._apply_drag_position()
        if hasattr(self, "_window_drag"):
            del self._window_drag

    def _build_style(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        # A restrained dark palette inspired by short-video desktop navigation.
        # All business widgets keep their original variables and callbacks.
        colors = {
            "bg": "#141a27",
            "surface": "#191f2d",
            "raised": "#232a3a",
            "primary": "#fe2c73",
            "primary_dark": "#d91d5c",
            "title": "#f7f7fa",
            "text": "#dce2ee",
            "muted": "#a0acc0",
            "border": "#303847",
            "danger": "#f87171",
            "success": "#4adea7",
            "selected": "#44273e",
            "row_alt": "#151d2a",
            "track": "#3a4254",
            "link": "#9ed2ff",
        }
        self.palette = colors
        self.configure(background=colors["bg"])

        style.configure(".", font=("Microsoft YaHei UI", 9), foreground=colors["text"], background=colors["surface"])
        style.configure("TLabel", background=colors["surface"], foreground=colors["text"])
        style.configure("App.TFrame", background=colors["bg"])
        style.configure("Header.TFrame", background=colors["surface"])
        style.configure("Footer.TFrame", background=colors["surface"])
        style.configure("Side.TFrame", background=colors["surface"])
        style.configure("Card.TFrame", background=colors["surface"])
        style.configure(
            "Title.TLabel", background=colors["surface"], foreground=colors["title"],
            font=("Microsoft YaHei UI", 18, "bold"),
        )
        style.configure(
            "Subtitle.TLabel", background=colors["surface"], foreground=colors["muted"],
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "Status.TLabel", background=colors["raised"], foreground=colors["success"],
            font=("Microsoft YaHei UI", 9, "bold"), padding=(12, 6),
        )
        style.configure(
            "Card.TLabelframe", background=colors["surface"], bordercolor=colors["border"],
            borderwidth=1, relief="solid",
        )
        style.configure(
            "Card.TLabelframe.Label", background=colors["surface"], foreground=colors["title"],
            font=("Microsoft YaHei UI", 10, "bold"), padding=(2, 0, 8, 0),
        )
        style.configure("Card.TLabel", background=colors["surface"], foreground=colors["text"])
        style.configure(
            "Muted.TLabel", background=colors["surface"], foreground=colors["muted"],
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "Accent.TLabel", background=colors["surface"], foreground=colors["primary"],
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.configure(
            "Primary.TButton", background=colors["primary"], foreground="#ffffff",
            bordercolor=colors["primary"], padding=(16, 8), font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.map(
            "Primary.TButton",
            background=[("active", colors["primary_dark"]), ("disabled", colors["raised"])],
            foreground=[("disabled", colors["muted"])],
        )
        style.configure(
            "Secondary.TButton", background=colors["raised"], foreground=colors["text"],
            bordercolor=colors["border"], padding=(14, 8),
        )
        style.map(
            "Secondary.TButton",
            background=[("active", colors["selected"])],
            foreground=[("active", colors["primary"])],
            bordercolor=[("active", colors["primary"])],
        )
        style.configure(
            "Danger.TButton", background=colors["raised"], foreground=colors["danger"],
            bordercolor=colors["border"], padding=(14, 8),
        )
        style.map("Danger.TButton", background=[("active", colors["selected"])])
        style.configure("Nav.TButton", background=colors["surface"], foreground=colors["text"],
                        borderwidth=0, anchor="w", padding=(18, 13), font=("Microsoft YaHei UI", 10))
        style.map("Nav.TButton", background=[("active", colors["raised"])],
                  foreground=[("active", colors["title"])])
        style.configure("Selected.Nav.TButton", background=colors["selected"], foreground=colors["primary"],
                        borderwidth=0, anchor="w", padding=(18, 13), font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Dark.TNotebook", background=colors["bg"], borderwidth=0)
        style.configure("Dark.TNotebook.Tab", background=colors["bg"], foreground=colors["muted"],
                        padding=(20, 11), borderwidth=0)
        style.map("Dark.TNotebook.Tab", background=[("selected", colors["surface"])],
                  foreground=[("selected", colors["primary"])])
        style.configure(
            "TEntry", fieldbackground=colors["raised"], foreground=colors["title"],
            bordercolor=colors["border"], lightcolor=colors["border"], darkcolor=colors["border"],
            padding=(9, 7),
        )
        style.map("TEntry", bordercolor=[("focus", colors["primary"])])
        style.configure(
            "TCombobox", fieldbackground=colors["raised"], background=colors["raised"],
            foreground=colors["title"], bordercolor=colors["border"], padding=(8, 6),
        )
        style.map("TCombobox", bordercolor=[("focus", colors["primary"])],
                  fieldbackground=[("readonly", colors["raised"])],
                  foreground=[("readonly", colors["title"])])
        style.configure(
            "Treeview", background=colors["surface"], fieldbackground=colors["surface"],
            foreground=colors["text"], bordercolor=colors["border"], rowheight=38,
            font=("Microsoft YaHei UI", 9),
        )
        style.map(
            "Treeview", background=[("selected", colors["selected"])],
            foreground=[("selected", colors["title"])],
        )
        style.configure(
            "Treeview.Heading", background=colors["raised"], foreground=colors["title"],
            bordercolor=colors["border"], lightcolor=colors["raised"], darkcolor=colors["raised"],
            borderwidth=0, relief="flat", padding=(8, 9), font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.map("Treeview.Heading", background=[("active", colors["selected"])])
        style.configure("Dark.Treeview.Heading", background=colors["raised"], foreground=colors["title"],
                        bordercolor=colors["border"], lightcolor=colors["raised"], darkcolor=colors["raised"],
                        borderwidth=0, relief="flat", padding=(8, 9),
                        font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Dark.Treeview", background=colors["surface"], fieldbackground=colors["surface"],
                        foreground=colors["text"], borderwidth=0, relief="flat", rowheight=37,
                        font=("Microsoft YaHei UI", 9))
        style.map("Dark.Treeview", background=[("selected", colors["selected"])],
                  foreground=[("selected", colors["title"])])
        style.configure(
            "Blue.Horizontal.TProgressbar", troughcolor=colors["raised"],
            background=colors["primary"], bordercolor=colors["raised"], lightcolor=colors["primary"],
            darkcolor=colors["primary"], thickness=12,
        )
        style.configure("TScrollbar", background=colors["border"], troughcolor=colors["bg"],
                        bordercolor=colors["bg"], lightcolor=colors["border"], darkcolor=colors["border"],
                        arrowcolor=colors["muted"], borderwidth=0, relief="flat")

    def _build_ui(self) -> None:
        # UI construction is isolated; collection and download callbacks stay here.
        from ui_v2 import build_ui
        build_ui(self)

    def choose_path(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.path_var.get() or str(Path.home()))
        if selected:
            self.path_var.set(selected)

    def _parse_config(self) -> AppConfig | None:
        try:
            values = [int(self.likes_var.get()), int(self.comments_var.get()), int(self.favorites_var.get()), int(self.duration_var.get()), int(self.max_duration_var.get()), int(self.checkpoint_var.get()), int(self.concurrent_downloads_var.get())]
            metric_timeout = float(self.metric_timeout_var.get())
            extra=[float(getattr(self,key).get()) if hasattr(self,key) else getattr(self.config_data,field)
                   for key,field in (("download_timeout_var","download_timeout_seconds"),
                       ("download_idle_timeout_var","download_idle_timeout_seconds"),("max_file_size_var","max_file_size_mb"))]
            if any(value < 0 for value in values[:5]) or values[5] <= 0 or not 1 <= values[6] <= 4 or not math.isfinite(metric_timeout) or metric_timeout <= 0 or any(not math.isfinite(v) for v in extra) or extra[0]<=0 or extra[1]<=0 or extra[2]<0 or (values[4] and values[4] < values[3]):
                raise ValueError
        except ValueError:
            diagnostics.log("WARN", "ui", "settings.invalid", "Invalid download settings entered")
            messagebox.showerror("设置错误", "点赞、评论、收藏和时长必须是大于或等于 0 的整数；最长时长不能小于最短时长（0 表示不限制）；单轮暂停数和指标超时秒数必须大于 0；并行下载数为 1 至 4。")
            return None
        path = Path(self.path_var.get().strip()).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            diagnostics.log("ERROR", "ui", "download_directory.failed", "Cannot create download directory", error=exc, path=str(path))
            messagebox.showerror("路径错误", f"无法创建保存目录：\n{exc}")
            return None
        cfg = AppConfig(
            download_dir=str(path), min_likes=values[0], min_comments=values[1],
            min_favorites=values[2], min_duration_seconds=values[3], max_duration_seconds=values[4], checkpoint_every=values[5], max_concurrent_downloads=values[6],
            scroll_delay_seconds=self.config_data.scroll_delay_seconds,
            metric_timeout_seconds=metric_timeout,
            download_timeout_seconds=extra[0],download_idle_timeout_seconds=extra[1],max_file_size_mb=extra[2],
        )
        try:
            cfg.save()
        except OSError as exc:
            messagebox.showerror("设置保存失败", f"无法保存配置：\n{exc}")
            return None
        self.config_data = cfg
        self.rule_summary_var.set(f"点赞 ≥ {cfg.min_likes:,}")
        return cfg

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        op = diagnostics.operation("task", "ui", "Feed collection requested", mode="feed")
        began = time.monotonic()
        cfg = self._parse_config()
        if not cfg:
            diagnostics.finish("task", "ui", op, began, "cancelled", "Feed collection blocked by settings")
            return
        self.worker = AutomationWorker(cfg, self.events, operation_id=op)
        self._scan_started_at = time.monotonic()
        self._batch_candidate_ids.clear()
        self.batch_candidates_var.set(f"0 / {cfg.checkpoint_every}")
        self.dedup_count_var.set("0")
        self.run_time_var.set("00:00:00")
        self.batch_progress_var.set(0)
        self.batch_bar.configure(maximum=cfg.checkpoint_every)
        self.batch_var.set(f"本批次进度：0 / {cfg.checkpoint_every}")
        self.current_progress_var.set(0)
        self.current_download_var.set("当前下载：等待任务")
        try:
            self.worker.start()
        except Exception as exc:
            diagnostics.finish("task", "ui", op, began, "failed", "Worker thread failed to start", error=exc)
            messagebox.showerror("启动失败", str(exc))
            return
        diagnostics.bind(None)
        self.scan_heading_var.set("正在采集")
        self.start_btn.configure(state="disabled")
        self.start_btn.pack_forget()
        self.pause_btn.configure(state="normal", text="暂停采集")
        self.pause_btn.pack(side="right")
        self.stop_btn.configure(state="normal")
        self.skip_btn.configure(state="normal")

    def start_manual_download(self) -> None:
        if self.manual_worker and self.manual_worker.is_alive():
            messagebox.showinfo("单链接下载", "已有一个单链接下载任务正在进行，请等待其完成。")
            return
        raw_url = self.manual_url_var.get().strip()
        try:
            parts = urlsplit(raw_url)
            host = (parts.hostname or "").lower()
            if parts.scheme not in {"http", "https"} or not (host == "tiktok.com" or host.endswith(".tiktok.com")) or not parts.path:
                raise ValueError
            normalized_url = urlunsplit(("https", parts.netloc, parts.path.rstrip("/"), "", ""))
        except ValueError:
            diagnostics.log("WARN", "ui", "manual.invalid_url", "Manual download rejected: invalid TikTok URL")
            messagebox.showerror("链接错误", "请输入完整的 TikTok 视频链接，例如：https://www.tiktok.com/@用户名/video/视频ID")
            return
        cfg = self._parse_config()
        if not cfg:
            return
        self.manual_url_var.set(normalized_url)
        op = diagnostics.operation("task", "ui", "Manual download requested", mode="manual", url=normalized_url)
        self.manual_worker = ManualDownloadWorker(cfg, self.events, normalized_url, operation_id=op)
        self.manual_download_btn.configure(state="disabled")
        self.current_progress_var.set(0)
        self.current_download_var.set("当前下载：正在启动单链接下载…")
        self.status_var.set("正在启动单链接下载…")
        try:
            self.manual_worker.start()
        except Exception as exc:
            diagnostics.log("ERROR", "ui", "task.start_failed", "Manual worker failed to start", operation_id=op, error=exc)
            self.manual_download_btn.configure(state="normal")
            messagebox.showerror("启动失败", str(exc))
        finally:
            diagnostics.bind(None)

    def start_author_download(self) -> None:
        if self.author_worker and self.author_worker.is_alive():
            messagebox.showinfo("作者主页下载", "已有一个作者主页下载任务正在进行，请等待其完成。")
            return
        raw_url = self.author_url_var.get().strip()
        try:
            parts = urlsplit(raw_url)
            host = (parts.hostname or "").lower()
            path = parts.path.rstrip("/")
            if parts.scheme not in {"http", "https"} or not (host == "tiktok.com" or host.endswith(".tiktok.com")) or not re.match(r"^/@[^/]+$", path, re.I):
                raise ValueError
            normalized_url = urlunsplit(("https", parts.netloc, path, "", ""))
        except ValueError:
            diagnostics.log("WARN", "ui", "author.invalid_url", "Author download rejected: invalid profile URL")
            messagebox.showerror("链接错误", "请输入作者主页链接，例如：https://www.tiktok.com/@用户名")
            return
        cfg = self._parse_config()
        if not cfg:
            return
        self.author_url_var.set(normalized_url)
        op = diagnostics.operation("task", "ui", "Author download requested", mode="author", url=normalized_url)
        self.author_worker = AuthorDownloadWorker(cfg, self.events, normalized_url, operation_id=op)
        self.author_download_btn.configure(state="disabled")
        self.current_progress_var.set(0)
        self.current_download_var.set("当前下载：正在读取作者主页…")
        self.status_var.set("正在读取作者主页视频…")
        try:
            self.author_worker.start()
        except Exception as exc:
            diagnostics.log("ERROR", "ui", "task.start_failed", "Author worker failed to start", operation_id=op, error=exc)
            self.author_download_btn.configure(state="normal")
            messagebox.showerror("启动失败", str(exc))
        finally:
            diagnostics.bind(None)

    def skip_current(self) -> None:
        if self.worker:
            self.worker.skip_current()

    def pause_resume(self) -> None:
        if not self.worker:
            return
        if self.worker.pause_event.is_set():
            self.worker.pause()
            self.pause_btn.configure(text="继续采集")
        else:
            self.worker.resume()
            self.pause_btn.configure(text="暂停采集")

    def stop(self) -> None:
        if self.worker:
            self.worker.stop()
            self.status_var.set("正在停止…")

    def _poll_events(self) -> None:
        if diagnostics.fallback_count() and not self._logger_warning_shown:
            self._logger_warning_shown = True
            self.status_var.set("诊断日志目录不可写；程序可继续运行，但本次日志可能无法导出")
        needs_refresh = False
        deadline = time.monotonic() + 0.02
        try:
            while time.monotonic() < deadline:
                event = self.events.get_nowait()
                kind = event.get("kind")
                if kind == "status":
                    self.status_var.set(event["text"])
                elif kind == "candidate":
                    record_id = event.get("record_id")
                    if self.worker and self.worker.is_alive() and record_id is not None:
                        self._batch_candidate_ids.add(int(record_id))
                        count = len(self._batch_candidate_ids)
                        self.batch_candidates_var.set(f"{count} / {self.config_data.checkpoint_every}")
                        self.dedup_count_var.set(str(count))
                    needs_refresh = True
                elif kind == "download_progress":
                    self.current_progress_var.set(int(event.get("progress", 0)))
                    title = str(event.get("title", ""))[:38]
                    self.current_download_var.set(f"当前下载：{title}｜{event.get('status', '')}｜{event.get('message', '')}")
                    needs_refresh = True
                elif kind == "downloaded":
                    self.status_var.set(f"本轮已完成 {event['completed']} 个视频")
                    batch_count = int(event.get("batch_completed", 0))
                    self.batch_progress_var.set(batch_count)
                    self.batch_var.set(f"本批次进度：{batch_count} / {self.config_data.checkpoint_every}（本轮累计 {event['completed']}）")
                    self.current_progress_var.set(100)
                    self.current_download_var.set("当前下载：上一条已完成，等待下一条")
                    needs_refresh = True
                elif kind == "manual_downloaded":
                    self.status_var.set("单链接视频下载完成")
                    self.current_progress_var.set(100)
                    self.current_download_var.set("当前下载：单链接视频已完成")
                    needs_refresh = True
                elif kind == "author_downloaded":
                    self.status_var.set("作者主页视频下载完成一条")
                    self.current_progress_var.set(100)
                    self.current_download_var.set("当前下载：作者主页视频已完成，继续处理队列")
                    needs_refresh = True
                elif kind == "manual_status":
                    self.status_var.set(event["text"])
                elif kind == "manual_error":
                    self.status_var.set(event["text"])
                    messagebox.showerror("单链接下载失败", event["text"])
                elif kind == "manual_finished":
                    self.manual_download_btn.configure(state="normal")
                    needs_refresh = True
                elif kind == "author_status":
                    self.status_var.set(event["text"])
                elif kind == "author_error":
                    self.status_var.set(event["text"])
                    messagebox.showerror("作者主页下载失败", event["text"])
                elif kind == "author_finished":
                    self.author_download_btn.configure(state="normal")
                    needs_refresh = True
                elif kind in {"warning", "error"}:
                    self.status_var.set(event["text"])
                    if kind == "error" and "--acceptance-exit" not in sys.argv:
                        messagebox.showerror("任务停止", event["text"])
                elif kind == "attention":
                    self.status_var.set("已暂停：需要人工处理")
                    if "--acceptance-exit" in sys.argv:
                        diagnostics.log("WARN","ui","acceptance.manual_required","Unattended acceptance requires human action")
                        self.stop()
                    else:
                        messagebox.showwarning("需要人工处理", event["text"])
                    self.pause_btn.configure(text="继续采集")
                elif kind == "checkpoint":
                    if "--acceptance-exit" in sys.argv:
                        diagnostics.log("INFO","ui","acceptance.checkpoint_stop","Isolated acceptance stops at first checkpoint")
                        answer=False
                    else:
                        answer = messagebox.askyesno(
                        "批次确认",
                        f"本批次 {event.get('batch_size', self.config_data.checkpoint_every)} 条任务已处理完成，"
                        f"其中下载完成 {event.get('batch_completed', 0)} 条。\n\n是否继续读取并下载下一批？",
                    )
                    if self.worker:
                        self.worker.answer_checkpoint(answer)
                elif kind == "batch_started":
                    self._batch_candidate_ids.clear()
                    self.batch_candidates_var.set(f"0 / {event.get('target', self.config_data.checkpoint_every)}")
                    self.dedup_count_var.set("0")
                    self.batch_progress_var.set(0)
                    self.batch_var.set(f"本批次进度：0 / {event.get('target', self.config_data.checkpoint_every)}")
                elif kind == "finished":
                    self.scan_heading_var.set("已停止")
                    self.start_btn.configure(state="normal")
                    self.pause_btn.configure(state="disabled", text="暂停采集")
                    self.pause_btn.pack_forget()
                    self.start_btn.pack(side="right")
                    self.stop_btn.configure(state="disabled")
                    self.skip_btn.configure(state="disabled")
                    if not self.status_var.get().startswith("自动化任务停止"):
                        self.status_var.set("已停止")
                    if "--acceptance-exit" in sys.argv:
                        self.after(500,self._on_close)
        except queue.Empty:
            pass
        if needs_refresh:
            self.refresh_table()
        if self._scan_started_at is not None and self.worker and self.worker.is_alive():
            seconds = max(0, int(time.monotonic() - self._scan_started_at))
            self.run_time_var.set(f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}")
        self.after(200, self._poll_events)

    def _filters(self) -> dict:
        data = {key: variable.get().strip() for key, variable in self.filter_vars.items()}
        data["category"] = self.category_filter.get()
        data["status"] = self.status_filter.get()
        return data

    def refresh_table(self) -> None:
        try:
            rows = query_videos(self._filters() if hasattr(self, "filter_vars") else {})
        except (ValueError, TypeError):
            messagebox.showerror("筛选错误", "大小和时长筛选条件必须是数字。")
            return
        selected_ids = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        status_icons = {"已完成": "●", "正在下载": "◌", "排队中": "◷",
                        "等待人工处理": "!", "失败": "×", "已跳过": "–"}
        for number, row in enumerate(rows, 1):
            size = f"{row['size_bytes'] / 1024 / 1024:.1f} MB"
            duration = self._format_duration(float(row["duration_seconds"]))
            downloaded = str(row["downloaded_at"]).replace("T", " ")
            progress = max(0, min(100, int(row["progress"])))
            progress_text = "▰" * (progress // 20) + "▱" * (5 - progress // 20) + f"  {progress}%"
            self.tree.insert("", "end", iid=str(row["id"]), text="□",
                             image=self.category_icons.get(str(row["category"]), self.category_icons["其他"]),
                             values=(number, row["title"] or "（无标题）",
                                     f"{status_icons.get(row['status'], '·')} {row['status']}", progress_text,
                                     row["source_url"], size, duration, f"● {row['category']}", downloaded,
                                     display_metric(row['likes']), display_metric(row['comments']), display_metric(row['favorites']),
                                     "▶   ▣   ⋯"), tags=(row["status"],))
        remaining_selection = selected_ids.intersection(self.tree.get_children())
        if remaining_selection:
            self.tree.selection_set(*remaining_selection)
        from ui_table import update_rows
        update_rows(self, rows)
        summary = summarize_videos()
        total = summary.get("total", 0)
        self.table_heading_var.set(f"视频列表 ({len(rows)})")
        self.total_var.set(f"共 {total} 个视频" + (f"  |  筛选显示 {len(rows)} 个" if len(rows) != total else ""))
        self.footer_stats_var.set(
            f"已完成 {summary.get('已完成', 0)}  |  下载中 {summary.get('正在下载', 0)}  |  等待中 {summary.get('排队中', 0)}"
        )
        if hasattr(self, "queue_rows") and time.monotonic() - getattr(self, "_queue_card_updated_at", 0.0) >= 1.0:
            from ui_v2 import render_queue
            active = query_videos({"status": "正在下载"}) + query_videos({"status": "排队中"})
            render_queue(self, active)
            self._queue_card_updated_at = time.monotonic()

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total = max(0, int(round(seconds)))
        return f"{total // 60:02d}:{total % 60:02d}" if total else "未知"

    def reset_filters(self) -> None:
        for variable in self.filter_vars.values():
            variable.set("")
        self.category_filter.set("全部")
        self.status_filter.set("全部")
        self.refresh_table()

    def delete_selected(self) -> None:
        rows = self._selected_rows()
        if not rows:
            return
        diagnostics.log("INFO", "ui", "records.delete_requested", "Delete selected records requested", requested_count=len(rows))
        if not messagebox.askyesno("确认删除", f"确定删除选中的 {len(rows)} 条记录及已下载文件吗？\n\n此操作无法撤销。"):
            diagnostics.log("INFO", "ui", "records.delete_cancelled", "Delete selected records cancelled", requested_count=len(rows))
            return
        op = diagnostics.operation("records.delete", "ui", "Delete selected records confirmed", requested_count=len(rows))
        began = time.monotonic()
        ids = [int(row["id"]) for row in rows]
        if self.worker and self.worker.is_alive():
            self.worker.cancel_records(ids)
        file_errors: list[str] = []
        for row in rows:
            path = Path(str(row["file_path"])) if row["file_path"] else None
            if path and path.exists():
                try:
                    path.unlink()
                except OSError as exc:
                    file_errors.append(f"{path.name}：{exc}")
        deleted = delete_videos(ids)
        diagnostics.finish("records.delete", "ui", op, began, "success" if not file_errors else "failed",
                           "Selected record deletion ended", deleted_count=deleted, file_error_count=len(file_errors))
        self.refresh_table()
        if file_errors:
            messagebox.showwarning("删除完成", f"已删除 {deleted} 条记录，但有 {len(file_errors)} 个文件删除失败。\n\n" + "\n".join(file_errors[:8]))
        else:
            self.status_var.set(f"已删除 {deleted} 条记录及对应文件")

    def sort_tree(self, column: str, reverse: bool) -> None:
        raw_rows = {str(row["id"]): row for row in self.visual_rows}
        fields = {"size": "size_bytes", "duration": "duration_seconds", "downloaded": "downloaded_at"}
        items = []
        for index, iid in enumerate(self.tree.get_children("")):
            row = raw_rows.get(iid)
            value = index if column == "number" else row[fields.get(column, column)] if row and fields.get(column, column) in row.keys() else self.tree.set(iid, column)
            items.append((value, iid))
        def key(item):
            value = str(item[0]).replace(",", "").replace(" MB", "")
            try:
                return (0, float(value))
            except ValueError:
                return (1, value.casefold())
        items.sort(key=key, reverse=reverse)
        for index, (_, iid) in enumerate(items):
            self.tree.move(iid, "", index)
        self.tree.heading(column, command=lambda: self.sort_tree(column, not reverse))
        self._visual_sort = (column, reverse)
        from ui_table import redraw as redraw_visual_table
        order = {str(row["id"]): row for row in self.visual_rows}
        self.visual_rows = [order[iid] for _, iid in items if iid in order]
        redraw_visual_table(self)

    def _selected_row(self):
        rows = self._selected_rows()
        return rows[0] if rows else None

    def _selected_rows(self):
        selected = {int(iid) for iid in self.tree.selection()}
        if not selected:
            return []
        return [row for row in query_videos() if int(row["id"]) in selected]

    def select_all_rows(self) -> None:
        self.tree.selection_set(self.tree.get_children(""))

    def open_selected(self, _event=None) -> None:
        row = self._selected_row()
        if row and Path(row["file_path"]).exists():
            os.startfile(row["file_path"])

    def reveal_selected(self) -> None:
        row = self._selected_row()
        if row and Path(row["file_path"]).exists():
            subprocess.Popen(["explorer", "/select,", str(Path(row["file_path"]))])

    def open_source_url(self) -> None:
        row = self._selected_row()
        if row and row["source_url"]:
            webbrowser.open(str(row["source_url"]))

    def show_context_menu(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid:
            if iid not in self.tree.selection():
                self.tree.selection_set(iid)
            self.menu.tk_popup(event.x_root, event.y_root)

    def change_category(self, category: str) -> None:
        rows = self._selected_rows()
        if not rows:
            return
        op = diagnostics.operation("records.reclassify", "ui", "Category change requested", requested_count=len(rows), category=category)
        began = time.monotonic()
        changed = 0
        errors: list[str] = []
        for row in rows:
            old_path = Path(row["file_path"])
            if not old_path.exists():
                errors.append(f"找不到：{old_path.name}")
                continue
            new_path = categorized_path(old_path, category, str(row["title"]), float(row["duration_seconds"]))
            try:
                if new_path != old_path:
                    old_path.rename(new_path)
                update_category(int(row["id"]), category, str(new_path))
                changed += 1
            except OSError as exc:
                errors.append(f"{old_path.name}：{exc}")
        self.refresh_table()
        diagnostics.finish("records.reclassify", "ui", op, began, "success" if not errors else "failed",
                           "Category change ended", changed_count=changed, error_count=len(errors))
        if errors:
            messagebox.showwarning("批量修改完成", f"成功修改 {changed} 个；失败 {len(errors)} 个。\n\n" + "\n".join(errors[:8]))
        else:
            self.status_var.set(f"已批量修改 {changed} 个视频为“{category}”")

    def open_folder(self) -> None:
        path = Path(self.path_var.get().strip()).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def clean_missing(self) -> None:
        op = diagnostics.operation("records.clean_missing", "ui", "Missing-record cleanup requested")
        began = time.monotonic()
        count = delete_missing_records()
        diagnostics.finish("records.clean_missing", "ui", op, began, "success", "Missing-record cleanup completed", deleted_count=count)
        self.refresh_table()
        messagebox.showinfo("清理完成", f"已删除 {count} 条找不到文件的记录。")

    def clear_list(self) -> None:
        diagnostics.log("INFO", "ui", "records.clear_requested", "Clear list requested")
        running = (self.worker and self.worker.is_alive()) or (self.manual_worker and self.manual_worker.is_alive()) or (self.author_worker and self.author_worker.is_alive())
        if running:
            diagnostics.log("WARN", "ui", "records.clear_blocked", "Clear list blocked while task is running")
            messagebox.showwarning("无法清空列表", "当前仍有采集或下载任务运行，请先停止任务后再清空列表。")
            return
        count = len(query_videos())
        if not count:
            self.status_var.set("列表已经为空，下载文件未受影响")
            return
        if not messagebox.askyesno("确认清空列表", f"确定清空全部 {count} 条列表记录吗？\n\n只删除列表数据，不删除任何已下载视频文件。"):
            diagnostics.log("INFO", "ui", "records.clear_cancelled", "Clear list cancelled", previous_count=count)
            return
        op = diagnostics.operation("records.clear", "ui", "Clear list confirmed", previous_count=count)
        began = time.monotonic()
        deleted = clear_videos()
        diagnostics.finish("records.clear", "ui", op, began, "success", "List cleared; files retained", deleted_count=deleted)
        self.refresh_table()
        self.status_var.set(f"已清空 {deleted} 条列表记录，下载文件已保留")

    def export_diagnostics(self) -> None:
        destination = filedialog.asksaveasfilename(title="导出诊断日志", defaultextension=".zip",
                                                   initialfile="TikTokVideoCollector-diagnostics.zip",
                                                   filetypes=[("ZIP 文件", "*.zip")])
        if not destination:
            return
        op = diagnostics.operation("diagnostics.export", "ui", "Diagnostic export requested")
        began = time.monotonic()
        try:
            diagnostics.export(Path(destination))
            diagnostics.finish("diagnostics.export", "ui", op, began, "success", "Redacted diagnostic export created")
            messagebox.showinfo("导出完成", f"诊断日志已导出到：\n{destination}\n\n不包含浏览器资料及旧版原始日志。")
        except Exception as exc:
            diagnostics.finish("diagnostics.export", "ui", op, began, "failed", "Diagnostic export failed", error=exc)
            messagebox.showerror("导出失败", str(exc))

    def _on_close(self) -> None:
        if self._exit_requested_at is not None:
            return
        running = (self.worker and self.worker.is_alive()) or (self.manual_worker and self.manual_worker.is_alive()) or (self.author_worker and self.author_worker.is_alive())
        if running:
            if not messagebox.askyesno("退出", "下载或采集任务仍在运行，确定停止并退出吗？"):
                diagnostics.log("INFO", "ui", "app.exit_cancelled", "Exit cancelled by user")
                return
            if self.worker and self.worker.is_alive():
                self.worker.stop()
            if self.manual_worker and self.manual_worker.is_alive():
                self.manual_worker.stop()
            if self.author_worker and self.author_worker.is_alive():
                self.author_worker.stop()
        diagnostics.log("INFO", "ui", "app.exit_requested", "Application exit requested", active_task=bool(running))
        if running:
            self._exit_requested_at = time.monotonic()
            self.status_var.set("正在等待后台任务停止…")
            self.after(100, self._finish_close_when_workers_stop)
        else:
            self.destroy()

    def _finish_close_when_workers_stop(self) -> None:
        active = [worker for worker in (self.worker, self.manual_worker, self.author_worker)
                  if worker and worker.is_alive()]
        if active and self._exit_requested_at is not None and time.monotonic() - self._exit_requested_at < 15:
            self.after(200, self._finish_close_when_workers_stop)
            return
        if active:
            diagnostics.log("WARN", "app", "app.exit_worker_timeout", "Background workers did not stop within 15 seconds",
                            active_count=len(active))
        self.destroy()

    def report_callback_exception(self, exc_type, exc_value, exc_traceback) -> None:
        diagnostics.log("ERROR", "ui", "ui.callback_failed", "Unhandled UI callback exception", error=exc_value)
        diagnostics.fail_current(exc_value)
        messagebox.showerror("程序错误", f"操作失败：{exc_value}\n可通过“导出诊断日志”提供错误详情。")


def self_test() -> int:
    try:
        from playwright.async_api import async_playwright  # noqa: F401
        from mutagen.mp4 import MP4  # noqa: F401
        interpreter = tk.Tcl()
        interpreter.eval("info patchlevel")
        initialize()
        return 0
    except Exception as exc:
        diagnostics.log("ERROR", "app", "app.self_test_failed", "Startup dependency check failed", error=exc)
        return 1


if __name__ == "__main__":
    if "--thumbnail-worker" in sys.argv:
        from ui_thumbnails import thumbnail_worker
        raise SystemExit(thumbnail_worker(sys.argv[-2:]))
    diagnostics.start(app_data_dir() / "logs")
    _crash_stream = None
    try:
        # Native Tcl/codec aborts bypass Python exception hooks.
        crash_path = app_data_dir() / "logs" / f"native-crash-{diagnostics.SESSION_ID}.log"
        for old_crash in sorted(crash_path.parent.glob("native-crash-*.log"), key=lambda p: p.stat().st_mtime)[:-4]:
            old_crash.unlink(missing_ok=True)
        _crash_stream = crash_path.open("wb")
        faulthandler.enable(file=_crash_stream, all_threads=True)
    except (OSError, RuntimeError):
        pass
    executable_stat = Path(sys.executable if getattr(sys, "frozen", False) else __file__).stat()
    build_id = f"{executable_stat.st_size:x}-{int(executable_stat.st_mtime)}"
    diagnostics.log("INFO", "app", "app.starting", "Application startup began", version="1.0", build=build_id,
                    python=sys.version.split()[0], platform=sys.platform, executable=Path(sys.executable).name)
    def _unhandled(exc_type, exc_value, exc_tb):
        diagnostics.log("ERROR", "app", "app.unhandled", "Unhandled process exception", error=exc_value)
        diagnostics.close()
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    sys.excepthook = _unhandled
    if "--self-test" in sys.argv:
        code = self_test()
        diagnostics.log("INFO" if code == 0 else "ERROR", "app", "app.self_test", "Self-test finished", outcome="success" if code == 0 else "failed")
        diagnostics.close()
        raise SystemExit(code)
    try:
        window = MainWindow()
        if "--acceptance-exit" in sys.argv:
            window.withdraw()
        diagnostics.log("INFO", "app", "app.ready", "Application initialization completed")
        if "--autostart" in sys.argv:
            window.after(800, window.start)
        for arg in sys.argv:
            if arg.startswith("--acceptance-deadline=") and "--acceptance-exit" in sys.argv:
                window.after(int(float(arg.split("=",1)[1])*1000),window.stop)
        if "--ui-smoke" in sys.argv:
            # Non-destructive event-loop regression: no network or collection.
            from ui_v2 import show_page
            from types import SimpleNamespace
            def _verify_drag_and_rule_layout():
                x,y=window.winfo_x(),window.winfo_y()
                window.begin_window_drag(SimpleNamespace(x_root=x+30,y_root=y+30))
                for step in range(100):
                    window.drag_window(SimpleNamespace(x_root=x+30+step,y_root=y+30+step))
                window.end_window_drag()
                def _verify_position():
                    window.update_idletasks()
                    moved=abs(window.winfo_x()-(x+99))<=2 and abs(window.winfo_y()-(y+99))<=2
                    card_bottom=window.scan_card.winfo_rooty()+window.scan_card.winfo_height()
                    # Hidden test windows need not be mapped: validate requested
                    # geometry as well as actual bounds when widgets are shown.
                    needed=window.scan_summary.winfo_reqheight()+window.scan_header.winfo_reqheight()+33
                    layout_ok=needed<=window.operation_height and all(
                        entry.winfo_reqheight()>=20 for entry in window.scan_rule_entries.values())
                    controls_mapped=all(entry.winfo_ismapped() for entry in window.scan_rule_entries.values())
                    if controls_mapped:
                        layout_ok=layout_ok and all(entry.winfo_rooty()+entry.winfo_height()<=card_bottom
                            for entry in window.scan_rule_entries.values()) and (
                            window.scan_rule_save_btn.winfo_ismapped() and
                            window.scan_rule_save_btn.winfo_rooty()+window.scan_rule_save_btn.winfo_height()<=card_bottom)
                    diagnostics.log("INFO" if moved and layout_ok else "ERROR", "ui", "ui.smoke_verified",
                                    "Programmatic drag and inline rule layout checked", moved=bool(moved),
                                    inline_rules_fit=bool(layout_ok), outcome="success" if moved and layout_ok else "failed",
                                    card_height=window.scan_card.winfo_height(),
                                    required_height=needed,
                                    layout_check="actual" if controls_mapped else "requested_hidden_window",
                                    save_mapped=bool(window.scan_rule_save_btn.winfo_ismapped()),
                                    save_bottom_delta=window.scan_rule_save_btn.winfo_rooty()+window.scan_rule_save_btn.winfo_height()-card_bottom,
                                    rule_geometry=[{"key":key,"mapped":bool(entry.winfo_ismapped()),"height":entry.winfo_height(),
                                                    "bottom_delta":entry.winfo_rooty()+entry.winfo_height()-card_bottom}
                                                   for key,entry in window.scan_rule_entries.items()])
                window.after(100,_verify_position)
            window.after(700,_verify_drag_and_rule_layout)
            for index, page in enumerate(("library", "settings", "manual", "author", "dashboard")):
                window.after(1500 + index * 1500, lambda page=page: show_page(window, page))
            window.after(14000, window._on_close)
        window.mainloop()
        diagnostics.log("INFO", "app", "app.exited", "Application exited normally")
    except Exception as exc:
        diagnostics.log("ERROR", "app", "app.start_or_runtime_failed", "Application startup or event loop failed", error=exc)
        raise
    finally:
        if _crash_stream:
            faulthandler.disable()
            _crash_stream.close()
        diagnostics.close()
