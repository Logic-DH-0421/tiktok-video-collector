"""Explicit ownership of native browser downloads; no duplicate HTTP fallback."""
import asyncio
from diagnostics import log


class NativeDownload:
    def __init__(self, download, context=None):
        self.download=download
        self.context=context
        self.suggested_filename=download.suggested_filename
        self.expected_size=None

    async def probe_size(self):
        if not self.context or not self.download.url.startswith(('https://','http://')):
            return None
        response=None
        try:
            response=await self.context.request.head(self.download.url,timeout=10000)
            size=int(response.headers.get('content-length','0'))
            if response.ok and size>0:self.expected_size=size
        except Exception as exc:
            # Size is optional metadata, never proof of a successful transfer.
            log("DEBUG","download_handles","download.size_probe_unavailable","Download size not observable",
                error_type=type(exc).__name__)
        finally:
            if response:await response.dispose()
        return self.expected_size

    async def save_as(self,path):
        await self.download.save_as(path)

    async def cancel(self):
        await asyncio.wait_for(self.download.cancel(),10)


class BytesDownload:
    suggested_filename='video.mp4'
    def __init__(self,data):
        self.data=data
        self.expected_size=len(data)
    async def save_as(self,path):
        from pathlib import Path
        Path(path).write_bytes(self.data)
    async def cancel(self):
        self.data=b''
