"""Cancellation, fail-closed behavior and task terminal regression tests."""
import asyncio
import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
import automation
import database
from config_store import AppConfig
from download_handles import NativeDownload


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.db=patch.object(database,'DB_PATH',self.root/'test.db');self.db.start();database.initialize()
        self.worker=automation.AutomationWorker(AppConfig(),queue.Queue())
        self.item=dict(url='https://www.tiktok.com/@fixture/video/1234567890123456789')
        self.item['record_id']=database.add_candidate(dict(source_url=self.item['url']))
        async def blocked(path):await asyncio.Event().wait()
        self.download=SimpleNamespace(suggested_filename='video.mp4',save_as=AsyncMock(side_effect=blocked),cancel=AsyncMock())
    async def asyncTearDown(self):self.db.stop();self.temp.cleanup()
    async def save(self):
        return await self.worker._save_prepared_download(self.download,self.item,self.root,
            asyncio.Lock(),count_toward_batch=False)
    async def test_stop_cancels_native_download(self):
        task=asyncio.create_task(self.save())
        await asyncio.sleep(.01);self.worker.stop_event.set()
        with self.assertRaises(asyncio.CancelledError):await task
        self.download.cancel.assert_awaited_once()
        self.assertEqual(database.query_videos()[0]['status'],'已跳过')
    async def test_skip_cancels_native_download(self):
        self.worker._skip_record_ids.add(self.item['record_id'])
        self.assertFalse(await self.save());self.download.cancel.assert_awaited_once()
    async def test_known_oversize_cancel_before_save(self):
        self.worker.config.max_file_size_mb=1
        self.download.expected_size=2*1024*1024
        self.assertFalse(await self.save())
        self.download.save_as.assert_not_awaited();self.download.cancel.assert_awaited_once()
    async def test_cancel_failure_stops_further_admission(self):
        self.worker._skip_record_ids.add(self.item['record_id'])
        self.download.cancel.side_effect=RuntimeError('fixture cancel failure')
        self.assertFalse(await self.save());self.assertTrue(self.worker.stop_event.is_set())
    async def test_invalid_media_never_counts_success(self):
        async def write(path):Path(path).write_bytes(b'not a movie')
        self.download.save_as.side_effect=write
        with patch.object(self.worker,'_duration',return_value=0):
            self.assertFalse(await self.save())
        self.assertEqual(database.query_videos()[0]['status'],'失败')
        self.assertEqual(list(self.root.glob('*.mp4')),[])
    async def test_actual_fractional_duration_rechecks_upper_bound(self):
        self.worker.config.max_duration_seconds=15
        async def write(path):Path(path).write_bytes(b'fixture')
        self.download.save_as.side_effect=write
        with patch.object(self.worker,'_duration',return_value=15.022):
            self.assertFalse(await self.worker._save_prepared_download(self.download,self.item,self.root,asyncio.Lock()))
        self.assertEqual(self.worker.completed,0)
        self.assertEqual(database.query_videos()[0]['status'],'失败')
    async def test_observable_stall_cancels(self):
        self.download.bytes_received=0
        self.worker.config.download_idle_timeout_seconds=1
        ticks=iter((0,0,0,2))
        with patch.object(automation,'time',SimpleNamespace(monotonic=lambda:next(ticks,2))):
            self.assertFalse(await self.save())
        self.download.cancel.assert_awaited_once()
    async def test_unknown_progress_is_not_false_stall(self):
        self.worker.config.download_idle_timeout_seconds=.01
        async def write(path):
            await asyncio.sleep(.05);Path(path).write_bytes(b'fixture')
        self.download.save_as.side_effect=write
        with patch.object(self.worker,'_duration',return_value=12):self.assertTrue(await self.save())
        self.download.cancel.assert_not_awaited()
    async def test_prepare_failure_has_correlated_terminal(self):
        with patch.object(self.worker,'_prepare_savesora_download',new=AsyncMock(side_effect=RuntimeError('fixture'))), \
             patch.object(automation,'log') as start,patch.object(automation,'log_finish') as end:
            with self.assertRaises(RuntimeError):await self.worker._prepare_download_logged(None,self.item)
        self.assertEqual(start.call_args.kwargs['operation_id'],end.call_args.args[2])
        self.assertEqual(end.call_args.args[4],'failed')
    async def test_stop_during_navigation_is_cancelled_not_retry(self):
        async def run():
            self.worker.stop();raise RuntimeError('fixture target closed')
        with patch.object(self.worker,'_run',new=run),patch.object(automation,'log') as logs:
            await asyncio.to_thread(self.worker.run)
        self.assertEqual(self.worker._terminal_outcome,'cancelled')
        self.assertNotIn('task.retry_exhausted',[call.args[2] for call in logs.call_args_list])
    async def test_native_adapter_does_not_start_duplicate_fallback(self):
        native=SimpleNamespace(suggested_filename='video.mp4',save_as=AsyncMock(side_effect=RuntimeError('fixture')),
                               cancel=AsyncMock())
        adapter=NativeDownload(native)
        with self.assertRaises(RuntimeError):await adapter.save_as('unused')
        await adapter.cancel();native.cancel.assert_awaited_once()
    async def test_video_identity_ignores_author_and_tracking_query(self):
        other='https://www.tiktok.com/@renamed/video/1234567890123456789?tracking=1'
        self.assertEqual(self.worker._video_key(self.item['url']),self.worker._video_key(other))
        database.update_video_status(self.item['record_id'],'已完成',100)
        self.assertTrue(database.source_exists(other))


class ConfigTests(unittest.TestCase):
    def test_loaded_config_rejects_nonfinite_or_invalid_limits(self):
        for key,value in (('metric_timeout_seconds',float('nan')),('download_timeout_seconds',float('inf')),
            ('download_idle_timeout_seconds',0),('max_file_size_mb',-1),('max_concurrent_downloads',5)):
            with self.subTest(key=key),self.assertRaises(ValueError):AppConfig(**{key:value})
