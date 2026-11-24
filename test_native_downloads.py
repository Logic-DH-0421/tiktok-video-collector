"""Real Edge/local HTTP fault tests, no TikTok/SaveSora or screen input."""
import asyncio
import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from playwright.async_api import async_playwright
import automation
import database
from config_store import AppConfig,edge_executable
from download_handles import NativeDownload


class RealNativeDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.db=patch.object(database,'DB_PATH',self.root/'test.db');self.db.start();database.initialize()
        self.connections=set()
        async def serve(reader,writer):
            task=asyncio.current_task();self.connections.add(task)
            try:
                line=await reader.readline()
                await reader.readuntil(b'\r\n\r\n')
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n'
                    b'Content-Disposition: attachment; filename="fixture.mp4"\r\n'
                    b'Content-Length: 33554432\r\nConnection: close\r\n\r\n')
                await writer.drain()
                if not line.startswith(b'HEAD'):
                    for _ in range(1024):
                        writer.write(b'x'*32768);await writer.drain();await asyncio.sleep(.02)
            except (ConnectionError,asyncio.IncompleteReadError):pass
            finally:
                self.connections.discard(task);writer.close()
                try:await writer.wait_closed()
                except ConnectionError:pass
        self.server=await asyncio.start_server(serve,'127.0.0.1',0)
        port=self.server.sockets[0].getsockname()[1]
        self.pw=await async_playwright().start()
        self.browser=await self.pw.chromium.launch(executable_path=edge_executable(),headless=True)
        self.context=await self.browser.new_context(accept_downloads=True)
        self.page=await self.context.new_page()
        await self.page.set_content(f'<a href="http://127.0.0.1:{port}/file">download</a>')
        async with self.page.expect_download() as received:await self.page.locator('a').click()
        self.native=await received.value
        self.download=NativeDownload(self.native,self.context)
        self.item=dict(url='https://www.tiktok.com/@fixture/video/1234567890123456789')
        self.item['record_id']=database.add_candidate(dict(source_url=self.item['url']))
    async def asyncTearDown(self):
        await self.browser.close();await self.pw.stop()
        self.server.close();await self.server.wait_closed()
        for task in list(self.connections):task.cancel()
        await asyncio.gather(*self.connections,return_exceptions=True)
        self.db.stop();self.temp.cleanup()
    async def test_real_native_oversize_is_cancelled(self):
        worker=automation.AutomationWorker(AppConfig(max_file_size_mb=1),queue.Queue())
        self.assertFalse(await worker._save_prepared_download(self.download,self.item,self.root,
            asyncio.Lock(),count_toward_batch=False))
        self.assertEqual(await self.native.failure(),'canceled')
        self.assertEqual(list(self.root.glob('*.mp4')),[])
    async def test_real_native_timeout_is_cancelled(self):
        worker=automation.AutomationWorker(AppConfig(download_timeout_seconds=.1),queue.Queue())
        self.assertFalse(await worker._save_prepared_download(self.download,self.item,self.root,
            asyncio.Lock(),count_toward_batch=False))
        self.assertEqual(await self.native.failure(),'canceled')
        self.assertFalse(worker.stop_event.is_set())
        self.assertEqual(list(self.root.glob('*.mp4')),[])
