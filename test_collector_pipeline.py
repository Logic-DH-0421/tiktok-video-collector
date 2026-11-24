"""Isolated five-item pipeline using synthetic media, never user records/files."""
import asyncio
import queue
import tempfile
import unittest
from pathlib import Path
from types import MethodType
from unittest.mock import patch

import automation
import database
from config_store import AppConfig


class CollectorPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.db_patch=patch.object(database,"DB_PATH",self.root/"videos.db")
        self.db_patch.start()
        database.initialize()
        self.worker=automation.AutomationWorker(AppConfig(download_dir=str(self.root),checkpoint_every=5),queue.Queue())
        namespace=dict(vars(automation))
        exec(compile((Path(__file__).parent/"automation_patch_methods.py").read_text(encoding="utf-8"),"automation_patch_methods.py","exec"),namespace)
        self.worker._download_consumer=MethodType(namespace["_Patch"]._download_consumer,self.worker)
        self.pending=asyncio.Queue()
        self.lock=asyncio.Lock()

    async def asyncTearDown(self):
        self.worker.stop_event.set()
        self.worker.pause_event.set()
        self.worker.checkpoint_event.set()
        self.db_patch.stop()
        self.temp.cleanup()

    async def enqueue(self,count):
        for index in range(count):
            item={"url":f"https://www.tiktok.com/@fixture/video/{1234567890123456700+index}",
                  "title":f"food fixture {index}","likes":600000,"comments":20,"favorites":30,"duration":12}
            item["record_id"]=database.add_candidate({"source_url":item["url"],"title":item["title"],
                "likes":item["likes"],"duration_seconds":12})
            await self.pending.put(item)
            self.worker._batch_accepted+=1

    async def test_serial_wait_does_not_finish_when_consumer_only_dequeues(self):
        await self.pending.put({"record_id": 1})
        waiting=asyncio.create_task(self.worker._wait_for_serial_download(self.pending))
        await asyncio.sleep(0)
        await self.pending.get()
        await asyncio.sleep(0.02)
        self.assertFalse(waiting.done(), "Dequeued is not downloaded")
        self.pending.task_done()
        await asyncio.wait_for(waiting, 1)

    async def test_serial_wait_can_stop_while_transfer_pending(self):
        await self.pending.put({"record_id": 1})
        waiting=asyncio.create_task(self.worker._wait_for_serial_download(self.pending))
        await asyncio.sleep(0.01)
        self.worker.stop_event.set()
        await asyncio.wait_for(waiting, 1)

    async def test_serial_wait_releases_after_failed_task_terminal(self):
        await self.pending.put({"record_id": 1})
        waiting=asyncio.create_task(self.worker._wait_for_serial_download(self.pending))
        await self.pending.get()
        self.pending.task_done()
        await asyncio.wait_for(waiting, 1)

    async def test_five_parallel_downloads_checkpoint_and_clear_keep_files(self):
        active=0;peak=0
        class Download:
            suggested_filename="video.mp4"
            async def save_as(self,path):
                nonlocal active,peak
                active+=1;peak=max(peak,active)
                await asyncio.sleep(0.15)
                Path(path).write_bytes(b"synthetic-media-for-pipeline-test")
                active-=1
        async def prepare(page,item):return Download()
        class Page:
            def is_closed(self):return False
        await self.enqueue(5)
        with patch.object(self.worker,"_prepare_savesora_download",side_effect=prepare), \
             patch.object(self.worker,"_duration",return_value=12):
            tasks=[asyncio.create_task(self.worker._download_consumer(Page(),self.root,self.pending,self.lock)) for _ in range(2)]
            try:
                checkpoint=None
                deadline=asyncio.get_running_loop().time()+8
                while asyncio.get_running_loop().time()<deadline and checkpoint is None:
                    while not self.worker.events.empty():
                        event=self.worker.events.get_nowait()
                        if event["kind"]=="checkpoint":checkpoint=event
                    await asyncio.sleep(.02)
                self.assertIsNotNone(checkpoint)
                self.assertEqual(checkpoint["batch_completed"],5)
                rows=database.query_videos()
                self.assertEqual(len(rows),5)
                self.assertTrue(all(row["status"]=="已完成" for row in rows))
                self.assertEqual(peak,2)
                files=[Path(row["file_path"]) for row in rows]
                self.assertTrue(all(file.exists() and "美食_food fixture" in file.name and "00分12秒" in file.name for file in files))
                self.worker.answer_checkpoint(False)
                await asyncio.wait_for(self.pending.join(),3)
                await asyncio.wait_for(asyncio.gather(*tasks),3)
                self.assertEqual(database.clear_videos(),5)
                self.assertEqual(database.query_videos(),[])
                self.assertTrue(all(file.exists() for file in files))
            finally:
                for task in tasks:
                    if not task.done():task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)

    async def test_failed_preparation_releases_batch_slot(self):
        class Page:
            def is_closed(self):return False
        await self.enqueue(1)
        with patch.object(self.worker,"_prepare_savesora_download",side_effect=RuntimeError("fixture service failure")):
            task=asyncio.create_task(self.worker._download_consumer(Page(),self.root,self.pending,self.lock))
            try:
                await asyncio.wait_for(self.pending.join(),3)
                self.assertEqual(self.worker._batch_accepted,0)
                self.assertEqual(database.query_videos()[0]["status"],"失败")
                self.assertEqual(list(self.root.glob("*.mp4")),[])
            finally:
                self.worker.stop_event.set()
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)

    async def test_checkpoint_continue_resets_batch_only(self):
        self.worker.completed=5
        self.worker._batch_accepted=5
        self.worker._batch_succeeded=5
        self.worker.checkpoint_decision=True
        self.worker.checkpoint_event.set()
        # _checkpoint intentionally clears a previous answer before asking.
        task=asyncio.create_task(self.worker._checkpoint())
        await asyncio.sleep(.02)
        self.worker.answer_checkpoint(True)
        self.assertTrue(await asyncio.wait_for(task,2))
        self.assertEqual(self.worker.completed,5)
        self.assertEqual(self.worker._batch_accepted,0)
        self.assertEqual(self.worker._batch_succeeded,0)
