"""Receive a SaveSora click result without consuming unrelated TikTok media."""
import asyncio
import contextlib
import time


async def receive_download(page, candidate, timeout=12, max_bytes=0):
    loop=asyncio.get_running_loop()
    download=loop.create_future()
    media=loop.create_future()
    watched={}
    jobs=set()
    transferred=False

    def deliver(result):
        nonlocal transferred
        transferred='download' in result
        return result

    def schedule(coroutine):
        task=asyncio.create_task(coroutine)
        jobs.add(task);task.add_done_callback(jobs.discard)

    async def related(source):
        if source is page:return True
        try:return await source.opener() is page
        except Exception:return False

    async def accept_download(source,value):
        if await related(source) and not download.done():download.set_result(value)

    def watch(source):
        handler=lambda value:schedule(accept_download(source,value))
        source.on("download",handler)
        watched[source]=handler

    async def inspect_response(response):
        try:
            content_type=response.headers.get("content-type","").lower()
            if response.ok and (content_type.startswith("video/") or "application/octet-stream" in content_type):
                if not media.done() and await related(response.frame.page):media.set_result(response)
        except Exception:
            # Worker/service responses without a frame cannot be identity-bound.
            return

    def on_response(response):schedule(inspect_response(response))
    watch(page)
    page.context.on("page",watch)
    page.context.on("response",on_response)
    deadline=time.monotonic()+timeout
    try:
        await candidate.click(timeout=10000,no_wait_after=True)
        done,_=await asyncio.wait({download,media},timeout=max(.1,deadline-time.monotonic()),
                                  return_when=asyncio.FIRST_COMPLETED)
        if download in done:return deliver({"download":download.result()})
        if media not in done:raise RuntimeError("点击后没有相关下载事件或媒体响应")
        response=media.result()
        if max_bytes and int(response.headers.get('content-length','0') or 0)>max_bytes:
            raise RuntimeError("媒体大小超过设置上限")
        # A download response may intentionally have no CDP-readable body. Keep
        # listening for a download event, including events from popup pages.
        data=None
        try:
            data=await response.body()
        except Exception:
            pass
        if data:
            if max_bytes and len(data)>max_bytes:raise RuntimeError("媒体大小超过设置上限")
            return deliver({"data":data})
        remaining=deadline-time.monotonic()
        if remaining>0:
            with contextlib.suppress(asyncio.TimeoutError):
                return deliver({"download":await asyncio.wait_for(asyncio.shield(download),remaining)})
        if download.done() and not download.cancelled():return deliver({"download":download.result()})
        # Re-fetch only the proven SaveSora-related media URL through its browser
        # context, not arbitrary media responses from the scanning TikTok tab.
        fetched=await page.context.request.get(response.url,headers={"Referer":page.url},timeout=30000)
        content_type=fetched.headers.get("content-type","").lower()
        if not fetched.ok or "text/html" in content_type:
            raise RuntimeError(f"媒体文件请求失败 HTTP {fetched.status}")
        data=await fetched.body()
        if not data:raise RuntimeError("媒体文件为空")
        if max_bytes and len(data)>max_bytes:raise RuntimeError("媒体大小超过设置上限")
        return deliver({"data":data})
    finally:
        page.context.remove_listener("page",watch)
        page.context.remove_listener("response",on_response)
        for source,handler in watched.items():
            with contextlib.suppress(Exception):source.remove_listener("download",handler)
        for job in list(jobs):job.cancel()
        await asyncio.gather(*jobs,return_exceptions=True)
        if not transferred and download.done() and not download.cancelled():
            value=download.result()
            if hasattr(value,'cancel'):await asyncio.wait_for(value.cancel(),10)
        for future in (download,media):
            if not future.done():future.cancel()
