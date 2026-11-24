"""Isolated settings matrix and explicit reproductions of unfixed defects.

FixedIssueTests assert desired behavior after the lifecycle repair.
No real browser, user configuration, or user database is modified.
"""
import asyncio
import itertools
import math
import queue
import tempfile
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import automation
import app
import database
from config_store import AppConfig
from feed_metrics import normalize_api_item


class FilterMatrixTests(unittest.TestCase):
    def test_settings_validation_matrix(self):
        def variable(value):return SimpleNamespace(get=lambda:str(value))
        with tempfile.TemporaryDirectory() as folder:
            base=dict(likes_var=500000,comments_var=0,favorites_var=0,duration_var=0,
                max_duration_var=0,checkpoint_var=5,concurrent_downloads_var=1,metric_timeout_var=20)
            cases=[({},True),({'metric_timeout_var':1},True),({'metric_timeout_var':60},True),
                ({'likes_var':-1},False),({'comments_var':-1},False),({'favorites_var':-1},False),
                ({'duration_var':30,'max_duration_var':15},False),({'checkpoint_var':0},False),
                ({'concurrent_downloads_var':0},False),({'concurrent_downloads_var':5},False),
                ({'metric_timeout_var':0},False),({'metric_timeout_var':'bad'},False),
                ({'duration_var':15,'max_duration_var':15},True)]
            for overrides,expected in cases:
                fake=SimpleNamespace(**{key:variable(value) for key,value in (base|overrides).items()},
                    path_var=variable(folder),config_data=AppConfig(),
                    rule_summary_var=SimpleNamespace(set=lambda value:None))
                with self.subTest(overrides=overrides),patch.object(AppConfig,'save'), \
                     patch.object(app.messagebox,'showerror'):
                    self.assertEqual(app.MainWindow._parse_config(fake) is not None,expected)

    def test_counter_threshold_matrix(self):
        # 27 configurations x 125 combinations, including zero/unknown/boundary.
        for gates in itertools.product((0, 1, 500000), repeat=3):
            worker=automation.AutomationWorker(AppConfig(**dict(zip(
                ('min_likes','min_comments','min_favorites'),gates))),queue.Queue())
            for values in itertools.product((None,0,1,499999,500000),repeat=3):
                item=dict(zip(('likes','comments','favorites'),values),duration=12)
                expected=all(gate==0 or value is not None and value>=gate
                             for gate,value in zip(gates,values))
                with self.subTest(gates=gates,values=values):
                    self.assertEqual(worker._qualifies(item),expected)

    def test_duration_bounds_matrix(self):
        for lower,upper in ((0,0),(0,15),(10,0),(10,30),(15,15)):
            worker=automation.AutomationWorker(AppConfig(min_likes=0,
                min_duration_seconds=lower,max_duration_seconds=upper),queue.Queue())
            for duration in (None,0,9,10,14.99,15,15.01,30,31):
                known=duration is not None and duration>0
                expected=(not lower and not upper) or known and duration>=lower and (not upper or duration<=upper)
                with self.subTest(lower=lower,upper=upper,duration=duration):
                    self.assertEqual(worker._qualifies(dict(duration=duration)),bool(expected))


class BatchMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_and_consumer_capacity(self):
        namespace=dict(vars(automation))
        exec(compile((Path(__file__).parent/'automation_patch_methods.py').read_text(encoding='utf-8'),
                     'automation_patch_methods.py','exec'),namespace)
        for target,capacity in itertools.product((1,5,20),(1,2,4)):
            with self.subTest(target=target,capacity=capacity), tempfile.TemporaryDirectory() as folder:
                root=Path(folder)
                with patch.object(database,'DB_PATH',root/'test.db'):
                    database.initialize()
                    worker=automation.AutomationWorker(AppConfig(download_dir=folder,
                        checkpoint_every=target,max_concurrent_downloads=capacity),queue.Queue())
                    worker._download_consumer=MethodType(namespace['_Patch']._download_consumer,worker)
                    pending=asyncio.Queue();lock=asyncio.Lock();active=0;peak=0
                    class Download:
                        suggested_filename='video.mp4'
                        async def save_as(self,path):
                            nonlocal active,peak
                            active+=1;peak=max(peak,active)
                            await asyncio.sleep(.01)
                            Path(path).write_bytes(b'SYNTHETIC not actual MP4')
                            active-=1
                    class Page:
                        def is_closed(self):return False
                    for index in range(target):
                        url=f'https://www.tiktok.com/@fixture/video/{1234567890123456700+index}'
                        record_id=database.add_candidate(dict(source_url=url,title='fixture',likes=600000))
                        await pending.put(dict(url=url,record_id=record_id,title='fixture',likes=600000))
                        worker._batch_accepted+=1
                    tasks=[]
                    with patch.object(worker,'_prepare_savesora_download',new=AsyncMock(side_effect=lambda *a: Download())), \
                         patch.object(worker,'_duration',return_value=12):
                        try:
                            tasks=[asyncio.create_task(worker._download_consumer(Page(),root,pending,lock))
                                   for _ in range(capacity)]
                            checkpoint=None
                            async with asyncio.timeout(8):
                                while checkpoint is None:
                                    while not worker.events.empty():
                                        event=worker.events.get_nowait()
                                        if event['kind']=='checkpoint':checkpoint=event
                                    await asyncio.sleep(.01)
                            self.assertEqual(checkpoint['batch_completed'],target)
                            self.assertEqual(worker.completed,target)
                            self.assertEqual(len(database.query_videos()),target)
                            self.assertTrue(all(row['status']=='已完成' for row in database.query_videos()))
                            self.assertEqual(peak,min(target,capacity))
                            worker.answer_checkpoint(False)
                            await asyncio.wait_for(pending.join(),2)
                        finally:
                            worker.stop_event.set();worker.checkpoint_event.set()
                            for task in tasks:task.cancel()
                            await asyncio.gather(*tasks,return_exceptions=True)


class FixedIssueTests(unittest.IsolatedAsyncioTestCase):
    async def test_nonfinite_timeout_is_rejected_by_ui_validator(self):
        def variable(value):return SimpleNamespace(get=lambda:str(value))
        with tempfile.TemporaryDirectory() as folder:
            for value in ('nan','inf'):
                fake=SimpleNamespace(**{key:variable(v) for key,v in dict(likes_var=500000,
                    comments_var=0,favorites_var=0,duration_var=0,max_duration_var=0,
                    checkpoint_var=5,concurrent_downloads_var=1,metric_timeout_var=value).items()},
                    path_var=variable(folder),config_data=AppConfig(),
                    rule_summary_var=SimpleNamespace(set=lambda value:None))
                with patch.object(AppConfig,'save'),patch.object(app.messagebox,'showerror'):
                    cfg=app.MainWindow._parse_config(fake)
                self.assertIsNone(cfg)

    async def test_timeout_cancels_underlying_browser_download(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(database,'DB_PATH',Path(folder)/'test.db'):
            database.initialize()
            url='https://www.tiktok.com/@fixture/video/1234567890123456789'
            rid=database.add_candidate(dict(source_url=url))
            worker=automation.AutomationWorker(AppConfig(download_timeout_seconds=1),queue.Queue())
            download=SimpleNamespace(suggested_filename='video.mp4',
                save_as=AsyncMock(side_effect=lambda path: None),cancel=AsyncMock())
            async def blocked(path):await asyncio.Event().wait()
            download.save_as.side_effect=blocked
            ticks=iter((0,0,0,301))
            clock=SimpleNamespace(monotonic=lambda:next(ticks,301))
            with patch.object(automation,'time',clock):
                result=await worker._save_prepared_download(download,dict(record_id=rid,url=url),
                    Path(folder),asyncio.Lock(),count_toward_batch=False)
            self.assertFalse(result)
            download.cancel.assert_awaited_once()
            self.assertFalse(database.source_exists(url))
            self.assertEqual(database.query_videos()[0]['status'],'失败')

    async def test_api_invalid_modern_uses_valid_legacy(self):
        raw=dict(id='1234567890123456789',author=dict(uniqueId='fixture'),
            video=dict(duration=12),stats=dict(diggCount=600000),statsV2=dict(diggCount='bad'))
        self.assertEqual(normalize_api_item(raw,{'likes'},False)['likes'],600000)

    async def test_api_bad_optional_duration_keeps_good_metrics(self):
        raw=dict(id='1234567890123456789',author=dict(uniqueId='fixture'),
            video=dict(duration='bad'),stats=dict(diggCount=600000))
        self.assertFalse(normalize_api_item(raw,{'likes'},False)['duration_known'])
        self.assertIsNone(normalize_api_item(raw,{'likes'},True))


if __name__=='__main__':unittest.main(verbosity=2)
