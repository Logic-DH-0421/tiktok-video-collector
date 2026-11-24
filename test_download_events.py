import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from download_events import receive_download


class Emitter:
    def __init__(self):self.listeners={}
    def on(self,name,handler):self.listeners.setdefault(name,[]).append(handler)
    def remove_listener(self,name,handler):self.listeners[name].remove(handler)
    def emit(self,name,value):
        for handler in list(self.listeners.get(name,[])):handler(value)


class DownloadEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context=Emitter()
        self.page=Emitter();self.page.context=self.context;self.page.url="https://savesora.com/zh-cn/tiktok"
        self.page.opener=AsyncMock(return_value=None)
        self.file=SimpleNamespace(suggested_filename="video.mp4")
        self.candidate=SimpleNamespace(click=AsyncMock())

    def response(self,page,body):
        return SimpleNamespace(ok=True,url="https://media.example/video.mp4",
                               headers={"content-type":"video/mp4"},frame=SimpleNamespace(page=page),body=body)

    def assert_clean(self):
        self.assertTrue(all(not handlers for handlers in self.page.listeners.values()))
        self.assertTrue(all(not handlers for handlers in self.context.listeners.values()))

    async def test_regular_download_event(self):
        async def click(**_):self.page.emit("download",self.file)
        self.candidate.click.side_effect=click
        result=await receive_download(self.page,self.candidate,.1)
        self.assertIs(result["download"],self.file)
        self.assert_clean()

    async def test_popup_download_after_unreadable_body(self):
        popup=Emitter();popup.opener=AsyncMock(return_value=self.page)
        async def click(**_):
            self.context.emit("page",popup)
            self.context.emit("response",self.response(self.page,AsyncMock(side_effect=RuntimeError("No data found"))))
            asyncio.get_running_loop().call_later(.01,popup.emit,"download",self.file)
        self.candidate.click.side_effect=click
        result=await receive_download(self.page,self.candidate,.1)
        self.assertIs(result["download"],self.file)
        self.assertTrue(all(not handlers for handlers in popup.listeners.values()))
        self.assert_clean()

    async def test_unrelated_tiktok_media_is_not_saved(self):
        other=Emitter();other.opener=AsyncMock(return_value=None)
        body=AsyncMock(return_value=b"unrelated-video")
        async def click(**_):
            self.context.emit("response",self.response(other,body))
            asyncio.get_running_loop().call_later(.01,self.page.emit,"download",self.file)
        self.candidate.click.side_effect=click
        result=await receive_download(self.page,self.candidate,.1)
        self.assertIs(result["download"],self.file)
        body.assert_not_awaited()
        self.assert_clean()

    async def test_response_body_can_be_used(self):
        async def click(**_):self.context.emit("response",self.response(self.page,AsyncMock(return_value=b"fixture-media")))
        self.candidate.click.side_effect=click
        self.assertEqual((await receive_download(self.page,self.candidate,.1))["data"],b"fixture-media")
        self.assert_clean()

    async def test_unreadable_response_refetched_in_browser_context(self):
        fetched=SimpleNamespace(ok=True,status=200,headers={"content-type":"video/mp4"},body=AsyncMock(return_value=b"fixture-media"))
        self.context.request=SimpleNamespace(get=AsyncMock(return_value=fetched))
        async def click(**_):self.context.emit("response",self.response(self.page,AsyncMock(side_effect=RuntimeError("No data found"))))
        self.candidate.click.side_effect=click
        self.assertEqual((await receive_download(self.page,self.candidate,.03))["data"],b"fixture-media")
        self.context.request.get.assert_awaited_once()
        self.assert_clean()

    async def test_no_event_times_out_and_removes_listeners(self):
        with self.assertRaisesRegex(RuntimeError,"没有相关下载"):
            await receive_download(self.page,self.candidate,.02)
        self.assert_clean()
