from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import automation
import config_store
import database
import diagnostics


class DiagnosticIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        diagnostics.start(cls.root / "logs", level="DEBUG")
        cls.old_db = database.DB_PATH
        database.DB_PATH = cls.root / "videos.db"

    @classmethod
    def tearDownClass(cls):
        diagnostics.close()
        database.DB_PATH = cls.old_db
        cls.tmp.cleanup()

    def events(self):
        diagnostics._queue.join()
        return [json.loads(line) for line in (self.root / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_database_lifecycle_and_interrupted_record(self):
        database.initialize()
        record_id = database.add_candidate({"source_url": "https://www.tiktok.com/@private/video/123?token=SECRET",
                                            "title": "private caption", "likes": 600000})
        database.update_video_status(record_id, "正在下载", 20)
        database.initialize()
        row = database.query_videos()[0]
        self.assertEqual(row["status"], "失败")
        entries = self.events()
        self.assertTrue(any(e["event"] == "database.interrupted_recovery" and e["record_count"] >= 1 for e in entries))
        self.assertTrue(any(e["event"] == "video.status_changed" and e["record_id"] == record_id for e in entries))
        raw = json.dumps(entries, ensure_ascii=False)
        self.assertNotIn("SECRET", raw)
        self.assertNotIn("private caption", raw)

    def test_config_corrupt_and_save_failure(self):
        folder = self.root / "config"
        folder.mkdir(exist_ok=True)
        (folder / "config.json").write_text("{invalid", encoding="utf-8")
        with patch.object(config_store, "app_data_dir", return_value=folder):
            cfg = config_store.AppConfig.load()
            self.assertEqual(cfg.min_likes, 500000)
            with patch.object(Path, "write_text", side_effect=PermissionError(13, "denied")):
                with self.assertRaises(PermissionError):
                    cfg.save()
        entries = self.events()
        self.assertTrue(any(e["event"] == "config.load_fallback" and e["error_type"] == "JSONDecodeError" for e in entries))
        self.assertTrue(any(e["event"] == "config.save_failed" and e["error_type"] == "PermissionError" for e in entries))

    def test_missing_edge_worker_terminal(self):
        events = queue.Queue()
        with patch.object(automation, "edge_executable", return_value=None):
            worker = automation.ManualDownloadWorker(config_store.AppConfig(download_dir=str(self.root)), events,
                                                     "https://www.tiktok.com/@private/video/456?token=SECRET")
            worker.run()
        entries = [e for e in self.events() if e.get("operation_id") == worker.operation_id]
        self.assertEqual(entries[0]["event"], "task.started")
        end = next(e for e in entries if e["event"] == "task.finished")
        self.assertEqual(end["outcome"], "failed")
        self.assertEqual(end["error_type"], "RuntimeError")
        self.assertTrue(end["stack"])
        self.assertEqual(events.get()["kind"], "manual_error")

    def test_network_timeout_and_http_error(self):
        for error in (TimeoutError("request timeout"), RuntimeError("HTTP 503")):
            transfer = automation.RemoteUrlDownload("https://media.example/video.mp4?token=SECRET", {}, "parent", "task")
            with patch.object(automation, "urlopen", side_effect=error):
                with self.assertRaises(type(error)):
                    transfer._stream_to_file(str(self.root / "not-created.mp4"))
            entries = [e for e in self.events() if e.get("operation_id") == transfer.transfer_id]
            self.assertEqual([e["event"] for e in entries], ["media_transfer.started", "media_transfer.finished"])
            self.assertEqual(entries[-1]["outcome"], "timeout" if isinstance(error, TimeoutError) else "failed")
            self.assertNotIn("SECRET", json.dumps(entries))

    def test_download_failure_then_retry_success(self):
        record_id = database.add_candidate({"source_url": "https://www.tiktok.com/@private/video/789",
                                            "title": "private caption", "likes": 700000})
        item = {"record_id": record_id, "url": "https://www.tiktok.com/@private/video/789",
                "title": "private caption", "likes": 700000}
        worker = automation.AutomationWorker(config_store.AppConfig(download_dir=str(self.root)), queue.Queue())
        class FakeDownload:
            suggested_filename = "video.mp4"
            def __init__(self, fail):
                self.fail = fail
            async def save_as(self, path):
                if self.fail:
                    raise OSError(5, "disk error")
                Path(path).write_bytes(b"fake-media-test")
        async def scenario():
            lock = asyncio.Lock()
            first = await worker._save_prepared_download(FakeDownload(True), item, self.root, lock, False)
            second = await worker._save_prepared_download(FakeDownload(False), item, self.root, lock, False)
            return first, second
        # Synthetic bytes are not an MP4; media parsing is outside this log test.
        with patch.object(worker,"_duration",return_value=12):
            self.assertEqual(asyncio.run(scenario()), (False, True))
        self.assertEqual(database.query_videos({"title": "private caption"})[0]["status"], "已完成")
        entries = [e for e in self.events() if e["event"] == "download.finished" and e.get("record_id") == record_id]
        self.assertEqual([e["outcome"] for e in entries], ["failed", "success"])
        self.assertEqual(entries[0]["error_type"], "OSError")
        self.assertTrue(entries[0]["stack"])

    def test_download_cancel_has_terminal(self):
        record_id = database.add_candidate({"source_url": "https://www.tiktok.com/@private/video/790",
                                            "title": "private caption"})
        item = {"record_id": record_id, "url": "https://www.tiktok.com/@private/video/790", "title": "private caption"}
        worker = automation.AutomationWorker(config_store.AppConfig(download_dir=str(self.root)), queue.Queue())
        worker._skip_record_ids.add(record_id)
        class SlowDownload:
            suggested_filename = "video.mp4"
            async def save_as(self, path):
                await asyncio.sleep(5)
        result = asyncio.run(worker._save_prepared_download(SlowDownload(), item, self.root, asyncio.Lock(), False))
        self.assertFalse(result)
        entries = [e for e in self.events() if e["event"] == "download.finished" and e.get("record_id") == record_id]
        self.assertEqual(entries[-1]["outcome"], "cancelled")

    def test_background_retry_then_success_is_not_final_failure(self):
        worker = automation.AutomationWorker(config_store.AppConfig(download_dir=str(self.root)), queue.Queue())
        attempts = 0
        async def flaky():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionError("temporary network failure")
        with patch.object(worker, "_run", side_effect=flaky), patch.object(automation.time, "sleep", return_value=None):
            worker.run()
        entries = [e for e in self.events() if e.get("operation_id") == worker.operation_id]
        self.assertEqual(attempts, 2)
        self.assertTrue(any(e["event"] == "task.retry" and e["level"] == "WARN" for e in entries))
        self.assertEqual([e for e in entries if e["event"] == "task.finished"][-1]["outcome"], "success")
        self.assertFalse(any(e["event"] == "task.retry_exhausted" for e in entries))

    def test_unwritable_log_dir_falls_back(self):
        original = diagnostics._directory
        marker = self.root / "not-a-directory"
        marker.write_text("x", encoding="utf-8")
        try:
            diagnostics._directory = marker
            before = diagnostics.fallback_count()
            diagnostics.log("ERROR", "test", "sink.failed", "Important failure must remain observable")
            diagnostics._queue.join()
            self.assertGreater(diagnostics.fallback_count(), before)
        finally:
            diagnostics._directory = original

    def test_prior_session_open_operation_is_detected(self):
        folder = self.root / "restart"
        code = ("import sys,diagnostics;from pathlib import Path;"
                "diagnostics.start(Path(sys.argv[1]));"
                "diagnostics.log('INFO','app','app.starting','started');"
                "diagnostics.log('INFO','test','sample.started','started',operation_id='orphan-op');"
                "diagnostics.close()")
        subprocess.run([sys.executable, "-c", code, str(folder)], check=True, env={**os.environ, "PYTHONPATH": str(Path(__file__).parent)})
        # New process/session, equivalent to the next launch after an abrupt exit.
        code2 = ("import sys,diagnostics;from pathlib import Path;"
                 "diagnostics.start(Path(sys.argv[1]));diagnostics.close()")
        subprocess.run([sys.executable, "-c", code2, str(folder)], check=True, env={**os.environ, "PYTHONPATH": str(Path(__file__).parent)})
        records = [json.loads(s) for s in (folder / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(e["event"] == "operation.interrupted" and e["operation_id"] == "orphan-op" for e in records))
        self.assertTrue(any(e["event"] == "session.previous_unclosed" for e in records))

    def test_rotation_and_retention(self):
        folder = self.root / "rotation"
        folder.mkdir()
        stale = folder / "events.1.jsonl"
        stale.write_text("old", encoding="utf-8")
        age = time.time() - 31 * 86400
        os.utime(stale, (age, age))
        code = ("import sys,diagnostics;from pathlib import Path;"
                "diagnostics.start(Path(sys.argv[1]));diagnostics.close()")
        subprocess.run([sys.executable, "-c", code, str(folder)], check=True,
                       env={**os.environ, "PYTHONPATH": str(Path(__file__).parent)})
        self.assertFalse(stale.exists())
        with patch.object(diagnostics, "_directory", folder), patch.object(diagnostics, "MAX_BYTES", 100):
            for index in range(15):
                diagnostics._write_line({"event": "size-test", "index": index, "padding": "x" * 150})
        self.assertLessEqual(len(list(folder.glob("events*.jsonl"))), diagnostics.BACKUPS + 1)

    def test_export_contains_only_redacted_events(self):
        diagnostics.log("ERROR", "test", "secret.sample", "token=SECRET and https://example.com/a?cookie=SECRET",
                        title="private caption", url="https://www.tiktok.com/@person/video/123?token=SECRET")
        destination = self.root / "diagnostics.zip"
        diagnostics.export(destination)
        with zipfile.ZipFile(destination) as archive:
            self.assertTrue(archive.namelist())
            self.assertTrue(all(name.startswith("events") and name.endswith(".jsonl") for name in archive.namelist()))
            raw = b"".join(archive.read(name) for name in archive.namelist()).decode("utf-8")
        self.assertNotIn("SECRET", raw)
        self.assertNotIn("private caption", raw)
        self.assertIn('"event":"secret.sample"', raw)


if __name__ == "__main__":
    unittest.main()
