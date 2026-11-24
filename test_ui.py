"""Isolated UI regression checks; no browser, database, or user files."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import app
from config_store import AppConfig


class UiRegressionTests(unittest.TestCase):
    def test_integer_timeout_does_not_lose_trailing_zero(self):
        from ui_v2 import format_timeout_seconds
        for seconds in (10,20,60,10.0,20.0):
            with self.subTest(seconds=seconds):
                self.assertEqual(float(format_timeout_seconds(seconds)),float(seconds))
    @classmethod
    def setUpClass(cls):
        cls.patches = [
            patch.object(app.AppConfig, "load", return_value=AppConfig()),
            patch.object(app, "initialize"),
            patch.object(app, "query_videos", return_value=[]),
            patch.object(app, "summarize_videos", return_value={"total": 0}),
        ]
        for item in cls.patches:
            item.start()
        cls.window = app.MainWindow()
        cls.window.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.window.destroy()
        for item in reversed(cls.patches):
            item.stop()

    def test_all_original_workflows_remain_reachable(self):
        self.assertEqual(list(self.window.nav_buttons), ["dashboard", "manual", "author", "library", "settings", "logs"])
        self.assertEqual(list(self.window.pages), ["dashboard", "manual", "author"])
        for widget in (self.window.start_btn, self.window.pause_btn, self.window.stop_btn,
                       self.window.skip_btn, self.window.manual_download_btn,
                       self.window.author_download_btn):
            self.assertTrue(widget.cget("command"))
        self.assertEqual(str(self.window.tree.cget("selectmode")), "extended")
        self.assertEqual(len(self.window.tree.cget("columns")), 13)
        self.assertEqual(self.window.category_menu.index("end") + 1, len(app.CATEGORIES))
        self.assertEqual(len(self.window.queue_rows), 5)
        self.assertEqual(self.window.table_actions_menu.index("end") + 1, 4)
        self.assertEqual(tuple(self.window.tree.cget("displaycolumns"))[:3],
                         ("number", "title", "status"))

    def test_navigation_and_filters_keep_state(self):
        from ui_v2 import show_page
        self.window.manual_url_var.set("https://www.tiktok.com/@a/video/123")
        show_page(self.window, "manual")
        self.assertTrue(self.window.pages["manual"].winfo_manager())
        show_page(self.window, "settings")
        self.assertTrue(self.window.scan_settings_visible)
        self.assertTrue(self.window.scan_settings_panel.winfo_manager())
        show_page(self.window, "library")
        self.assertFalse(self.window.pages["dashboard"].winfo_manager())
        self.assertEqual(str(self.window.table_card.pack_info()["in"]),str(self.window.main_content))
        self.assertEqual(self.window.manual_url_var.get(), "https://www.tiktok.com/@a/video/123")
        self.window.filter_vars["title"].set("猫")
        self.window.category_filter.set("宠物")
        self.window.refresh_table()
        app.query_videos.assert_called_with(self.window._filters())
        self.window.reset_filters()
        self.assertEqual(self.window.filter_vars["title"].get(), "")
        self.assertEqual(self.window.category_filter.get(), "全部")
        self.assertEqual(self.window.status_filter.get(), "全部")

    def test_queue_and_separate_library_render_real_state(self):
        from ui_v2 import _focus_section, render_queue
        render_queue(self.window, [{"title": "示例视频", "progress": 47, "status": "正在下载"}])
        self.assertEqual(self.window.queue_heading_var.get(), "下载队列 (1)")
        self.assertEqual(self.window.queue_rows[0][0].cget("text"), "示例视频")
        self.assertEqual(int(float(self.window.queue_rows[0][3].cget("value"))), 47)
        _focus_section(self.window, "queue")
        self.assertEqual(self.window.active_page,"dashboard")
        _focus_section(self.window, "library")
        self.assertEqual(self.window.active_page,"library")
        self.assertFalse(self.window.pages["dashboard"].winfo_manager())

    def test_drag_coalesces_motion_and_flushes_on_release(self):
        from types import SimpleNamespace
        with patch.object(self.window,"_move_window_to") as geometry, patch.object(self.window,"after", return_value="test-timer") as after, patch.object(self.window,"after_cancel"), \
             patch.object(self.window,"state",return_value="normal"):
            self.window.begin_window_drag(SimpleNamespace(x_root=100,y_root=100))
            for index in range(1000):
                self.window.drag_window(SimpleNamespace(x_root=index,y_root=index))
            after.assert_called_once()
            geometry.assert_not_called()
            self.window.end_window_drag()
            geometry.assert_called_once()
        self.assertFalse(hasattr(self.window,"_window_drag"))

    def test_drag_supports_negative_monitor_coordinates(self):
        self.window._pending_drag_position=(-200,-50)
        with patch.object(self.window,"_move_window_to") as geometry:
            self.window._apply_drag_position()
            geometry.assert_called_once_with(-200, -50)

    def test_collection_rules_are_directly_visible_in_summary(self):
        from ui_v2 import show_page
        show_page(self.window,"dashboard")
        self.assertFalse(self.window.scan_settings_visible)
        self.assertEqual(set(self.window.scan_rule_entries),
                         {"likes","comments","favorites","min_duration","max_duration","checkpoint"})
        for entry in self.window.scan_rule_entries.values():
            self.assertEqual(entry.master.master.master,self.window.scan_summary)
            self.assertTrue(entry.winfo_manager())
        self.window.update_idletasks()
        self.assertLessEqual(self.window.scan_summary.winfo_reqheight()+
                             self.window.scan_header.winfo_reqheight()+33,self.window.operation_height)
        self.window.likes_var.set("765432")
        show_page(self.window,"settings")
        show_page(self.window,"dashboard")
        self.assertEqual(self.window.likes_var.get(),"765432")

    def test_checkpoint_is_editable_in_collection_card(self):
        from ui_v2 import show_page, _save_inline_settings
        w = self.window
        show_page(w, "dashboard")
        entry = w.scan_rule_entries["checkpoint"]
        previous = w.checkpoint_var.get()
        entry.entry.delete(0, "end")
        entry.entry.insert(0, "5")
        self.assertEqual(w.checkpoint_var.get(), "5")
        config = AppConfig(checkpoint_every=5)
        with patch.object(w, "_parse_config", return_value=config) as parse:
            _save_inline_settings(w)
            parse.assert_called_once()
            self.assertEqual(int(w.batch_bar.cget("maximum")), 5)
            self.assertIn("/ 5", w.batch_var.get())
        w.checkpoint_var.set(previous)

    def test_stop_skip_are_independent_visible_controls(self):
        from ui_v2 import show_page
        show_page(self.window, "dashboard")
        for button in (self.window.stop_btn, self.window.skip_btn):
            self.assertEqual(button.winfo_manager(), "pack")
            self.assertEqual(button.master.master, self.window.scan_summary)
        self.assertFalse(any(child.cget("text") == "⋯" for child in self.window.scan_header.winfo_children()
                             if "text" in child.keys()))

    def test_header_search_removed(self):
        self.assertFalse(hasattr(self.window, "search_var"))
        self.assertTrue(hasattr(self.window, "manual_url_var"))
        self.assertTrue(hasattr(self.window, "author_url_var"))

    def test_progress_burst_refreshes_table_once(self):
        for index in range(50):
            self.window.events.put({"kind": "download_progress", "progress": index})
        with patch.object(self.window, "refresh_table") as refresh:
            self.window._poll_events()
            refresh.assert_called_once()
        self.assertEqual(self.window.current_progress_var.get(), 49)

    def test_minimum_window_does_not_require_horizontal_resize(self):
        self.window.update_idletasks()
        self.assertLessEqual(self.window.winfo_reqwidth(), 1180)
        self.assertLessEqual(self.window.winfo_reqheight(), 760)

    def test_visual_rows_keep_selection_and_sort_numeric_data(self):
        rows = [dict(id=i, title=f"测试 {i}", source_url=f"https://www.tiktok.com/@test/video/{i}",
                     file_path="", size_bytes=i*1024, duration_seconds=i*20,
                     category="其他", downloaded_at="2026-10-22T10:00:00", likes=600000,
                     comments=100, favorites=100, status="正在下载", progress=progress)
                for i, progress in [(1, 2), (2, 100), (3, 25)]]
        with patch.object(app, "query_videos", return_value=rows):
            self.window.refresh_table()
            self.window.tree.selection_set("2")
            self.window.refresh_table()
            self.assertEqual(self.window.tree.selection(), ("2",))
            self.window.sort_tree("progress", False)
            self.assertEqual([row["id"] for row in self.window.visual_rows], [1, 3, 2])
            self.window.sort_tree("size", True)
            self.assertEqual([row["id"] for row in self.window.visual_rows], [3, 2, 1])
        self.window.refresh_table()

    def test_visual_columns_fit_supported_window_widths(self):
        from ui_table import _layout
        for width in (1144, 1464, 1800):
            layout = _layout(width)
            self.assertNotIn("actions", layout)
            self.assertLessEqual(layout["source_url"][1], width)
            self.assertTrue(all(end > start for start, end in layout.values()))

    def test_rounded_controls_preserve_commands_and_values(self):
        import tkinter as tk
        from types import SimpleNamespace
        from ui_v2 import _button, _entry, _select
        calls=[]
        button=_button(self.window,"测试",lambda:calls.append("called"),self.window.palette,icon="play")
        button.invoke()
        button.configure(state="disabled")
        button.invoke()
        self.assertEqual(calls,["called"])
        value=tk.StringVar(self.window,value="原值")
        entry=_entry(self.window,value,self.window.palette)
        entry.entry.delete(0,"end")
        entry.entry.insert(0,"修改后的值")
        self.assertEqual(value.get(),"修改后的值")
        selected=tk.StringVar(self.window,value="全部")
        selector=_select(self.window,selected,["全部","已完成"],self.window.palette)
        selected.set("已完成")
        self.assertEqual(selector.cget("text"),"已完成")
        for control in (button,entry,selector):control.destroy()

    def test_thumbnail_binary_data_reaches_tk_image(self):
        from ui_thumbnails import decode_thumbnail
        image=decode_thumbnail(self.window,b"P6\n2 1\n255\n"+bytes((255,0,0,0,255,0)))
        self.assertEqual((image.width(),image.height()),(2,1))
        self.assertEqual(image.get(0,0),(255,0,0))
        self.assertEqual(image.get(1,0),(0,255,0))

    def test_position_only_configure_does_not_redraw(self):
        import tkinter as tk
        from ui_controls import bind_size_redraw
        widget = tk.Frame(self.window)
        calls = []
        changed = bind_size_redraw(widget, lambda: calls.append(True))
        # Synthetic events also work when the test window is withdrawn.
        binding = widget.bind("<Configure>")
        self.assertTrue(binding)
        from types import SimpleNamespace
        changed(SimpleNamespace(widget=widget, width=100, height=20))
        changed(SimpleNamespace(widget=widget, width=100, height=20, x=30, y=40))
        changed(SimpleNamespace(widget=widget, width=120, height=20))
        self.assertEqual(len(calls), 2)
        widget.destroy()

    def test_drag_coalesces_and_cancels_pending_frame(self):
        from types import SimpleNamespace
        w = self.window
        w._window_drag = (10, 10)
        with patch.object(w, "_move_window_to") as move, patch.object(w, "state", return_value="normal"):
            for index in range(100):
                w.drag_window(SimpleNamespace(x_root=index, y_root=index))
            w.end_window_drag()
            move.assert_called_once_with(89, 89)
            self.assertIsNone(w._drag_after_id)
            self.assertFalse(w._drag_frame_pending)
            self.assertFalse(hasattr(w, "_window_drag"))


if __name__ == "__main__":
    unittest.main()
