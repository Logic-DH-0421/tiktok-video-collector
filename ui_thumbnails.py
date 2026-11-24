"""Read Windows video thumbnails off the UI thread, without modifying media files."""
from __future__ import annotations

import ctypes as c
import queue
import sys
import threading
import subprocess
import tempfile
import tkinter as tk
import uuid
from pathlib import Path
import diagnostics


def isolated_thumbnail(path: str) -> bytes | None:
    """Untrusted shell codecs must never execute in the Tk process."""
    with tempfile.TemporaryDirectory(prefix="collector-thumb-") as directory:
        output = Path(directory) / "thumbnail.ppm"
        command = ([sys.executable] if getattr(sys, "frozen", False)
                   else [sys.executable, str(Path(__file__).resolve())])
        try:
            result = subprocess.run(command + ["--thumbnail-worker", path, str(output)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=8,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                diagnostics.log("WARN", "thumbnails", "thumbnail.worker_failed",
                                "Thumbnail unavailable; category icon retained", exit_code=result.returncode)
                return None
            data = output.read_bytes() if output.exists() else None
            if data and len(data) <= 32 * 32 * 3 + 64 and data.startswith(b"P6\n32 32\n255\n"):
                return data
        except subprocess.TimeoutExpired:
            diagnostics.log("WARN", "thumbnails", "thumbnail.worker_timeout",
                            "Thumbnail extraction timed out; category icon retained", timeout_seconds=8)
        except OSError as exc:
            diagnostics.log("WARN", "thumbnails", "thumbnail.worker_unavailable",
                            "Thumbnail worker unavailable; category icon retained", error=exc)
    return None


def thumbnail_worker(arguments) -> int:
    data = shell_thumbnail(arguments[0])
    if data:
        Path(arguments[1]).write_bytes(data)
    return 0


def shell_thumbnail(path: str, size: int = 32) -> bytes | None:
    if sys.platform != "win32" or not Path(path).is_file():
        return None
    from ctypes import wintypes as w
    class Size(c.Structure): _fields_ = [("cx",c.c_long),("cy",c.c_long)]
    class Guid(c.Structure): _fields_ = [("data",c.c_ubyte*16)]
    class Header(c.Structure):
        _fields_ = [("size",w.DWORD),("width",c.c_long),("height",c.c_long),
                    ("planes",w.WORD),("bits",w.WORD),("compression",w.DWORD),
                    ("image_size",w.DWORD),("x",c.c_long),("y",c.c_long),
                    ("used",w.DWORD),("important",w.DWORD)]
    ole, shell, gdi, user = c.windll.ole32,c.windll.shell32,c.windll.gdi32,c.windll.user32
    initialized = ole.CoInitializeEx(None,2) >= 0
    factory,hbitmap,dc = c.c_void_p(),c.c_void_p(),None
    release = None
    try:
        guid=Guid((c.c_ubyte*16).from_buffer_copy(uuid.UUID("bcc18b79-ba16-442f-80c4-8a59c30c463b").bytes_le))
        shell.SHCreateItemFromParsingName.argtypes=[w.LPCWSTR,c.c_void_p,c.POINTER(Guid),c.POINTER(c.c_void_p)]
        if shell.SHCreateItemFromParsingName(str(Path(path).resolve()),None,c.byref(guid),c.byref(factory)) < 0:
            return None
        table=c.cast(factory,c.POINTER(c.POINTER(c.c_void_p))).contents
        release=c.WINFUNCTYPE(w.ULONG,c.c_void_p)(table[2])
        get_image=c.WINFUNCTYPE(c.c_long,c.c_void_p,Size,c.c_int,c.POINTER(c.c_void_p))(table[3])
        if get_image(factory,Size(size,size),0x0028,c.byref(hbitmap)) < 0:
            return None
        user.GetDC.restype=w.HDC
        user.GetDC.argtypes=[w.HWND]
        user.ReleaseDC.argtypes=[w.HWND,w.HDC]
        gdi.DeleteObject.argtypes=[w.HGDIOBJ]
        dc=user.GetDC(None)
        header=Header(c.sizeof(Header),size,-size,1,32,0,0,0,0,0,0)
        pixels=c.create_string_buffer(size*size*4)
        gdi.GetDIBits.argtypes=[w.HDC,w.HBITMAP,w.UINT,w.UINT,c.c_void_p,c.c_void_p,w.UINT]
        if not gdi.GetDIBits(dc,hbitmap,0,size,pixels,c.byref(header),0):
            return None
        raw=pixels.raw
        rgb=bytearray(size*size*3)
        for index in range(size*size):
            rgb[index*3:index*3+3]=raw[index*4:index*4+3][::-1]
        return f"P6\n{size} {size}\n255\n".encode()+rgb
    except (OSError,ValueError,AttributeError):
        return None
    finally:
        if dc: user.ReleaseDC(None,dc)
        if hbitmap.value: gdi.DeleteObject(hbitmap)
        if release and factory.value: release(factory)
        if initialized: ole.CoUninitialize()


class ThumbnailCache:
    def __init__(self,root):
        self.root=root
        self.pending=set()
        self.images={}
        self.jobs=queue.Queue(maxsize=128)
        self.results=queue.Queue()
        self.closed=False
        threading.Thread(target=self._worker,daemon=True,name="video-thumbnails").start()
        root.bind("<Destroy>",self._destroy,add="+")
        root.after(150,self._poll)

    def _destroy(self,event):
        if event.widget is self.root:
            self.closed=True
            try:self.jobs.put_nowait(None)
            except queue.Full:pass

    def get(self,path):
        if not path:return None
        key=str(path)
        if key in self.images:return self.images[key]
        if key not in self.pending and Path(key).is_file():
            try:self.jobs.put_nowait(key)
            except queue.Full:return None
            self.pending.add(key)
        return None

    def _worker(self):
        while not self.closed:
            path=self.jobs.get()
            if path is None:return
            self.results.put((path,isolated_thumbnail(path)))

    def _poll(self):
        if self.closed:return
        changed=False
        while True:
            try:path,data=self.results.get_nowait()
            except queue.Empty:break
            try:self.images[path]=decode_thumbnail(self.root,data) if data else None
            except tk.TclError:self.images[path]=None
            changed=True
        if changed and hasattr(self.root,"table_canvas"):
            from ui_table import redraw
            redraw(self.root)
        self.root.after(150,self._poll)


def thumbnail(widget,path):
    root=widget._root()
    if not hasattr(root,"_video_thumbnail_cache"):
        root._video_thumbnail_cache=ThumbnailCache(root)
    return root._video_thumbnail_cache.get(path)


def decode_thumbnail(root,data):
    # Tk's PPM decoder requires the original binary bytes, not base64.
    return tk.PhotoImage(master=root,data=data,format="PPM")


if __name__ == "__main__" and "--thumbnail-worker" in sys.argv:
    raise SystemExit(thumbnail_worker(sys.argv[-2:]))
