import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import ui_thumbnails as thumbnails


class ThumbnailIsolationTests(unittest.TestCase):
    def test_native_worker_failure_is_not_main_process_failure(self):
        with patch.object(thumbnails.subprocess, "run", return_value=SimpleNamespace(returncode=0xC0000409)), \
             patch.object(thumbnails, "shell_thumbnail") as native, \
             patch.object(thumbnails.diagnostics, "log") as log:
            self.assertIsNone(thumbnails.isolated_thumbnail("video.mp4"))
            native.assert_not_called()
            self.assertEqual(log.call_args.args[2], "thumbnail.worker_failed")

    def test_worker_timeout_falls_back(self):
        with patch.object(thumbnails.subprocess, "run", side_effect=subprocess.TimeoutExpired("worker", 8)), \
             patch.object(thumbnails.diagnostics, "log") as log:
            self.assertIsNone(thumbnails.isolated_thumbnail("video.mp4"))
            self.assertEqual(log.call_args.args[2], "thumbnail.worker_timeout")

    def test_worker_launch_failure_falls_back(self):
        with patch.object(thumbnails.subprocess, "run", side_effect=OSError("unavailable")), \
             patch.object(thumbnails.diagnostics, "log") as log:
            self.assertIsNone(thumbnails.isolated_thumbnail("video.mp4"))
            self.assertEqual(log.call_args.args[2], "thumbnail.worker_unavailable")
